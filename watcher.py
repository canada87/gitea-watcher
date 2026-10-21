#!/usr/bin/env python3
"""gitea-watcher

Controlla periodicamente che i mirror di Gitea siano allineati con GitHub e
espone lo stato via HTTP (JSON) per Home Assistant.

Solo libreria standard. Versione 0.1: SOLA LETTURA su Gitea e GitHub.

Controlli per ogni "sorgente" (account personale o organizzazione GitHub):
  - repo presenti su GitHub (non archiviate) ma assenti in Gitea
  - mirror fermi: ultimo aggiornamento piu' vecchio di STALE_AFTER_HOURS
  - repo presenti in Gitea ma non piu' su GitHub (solo informativo)
  - scadenza del token GitHub (header github-authentication-token-expiration)
  - errori di accesso (token scaduto o non valido, owner non trovato, ...)

Endpoint:
  GET  /health    senza autenticazione, risponde sempre {"status": "ok"}
  GET  /status    Authorization: Bearer <API_TOKEN>, stato dell'ultimo controllo
  POST /refresh   Authorization: Bearer <API_TOKEN>, forza un nuovo controllo
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse

VERSION = "0.1.0"
MAX_SOURCES = 50
LIST_CAP = 50  # elementi massimi nelle liste dello stato (missing, stale, ...)

log = logging.getLogger("gitea-watcher")


# --------------------------------------------------------------------------
# Configurazione
# --------------------------------------------------------------------------
@dataclass
class Source:
    name: str
    gh_org: str  # vuoto = account personale del token
    gitea_owner: str
    gh_token: str
    gitea_token: str


@dataclass
class Config:
    gitea_url: str
    gitea_token: str
    github_api: str
    api_token: str
    port: int
    interval_s: int
    stale_after: timedelta
    warn_days: int
    verify_tls: bool
    data_dir: str
    sources: list


class ConfigError(Exception):
    pass


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    errors = []

    def need(key):
        val = (env.get(key) or "").strip()
        if not val:
            errors.append(f"variabile mancante: {key}")
        return val

    gitea_url = need("GITEA_URL").rstrip("/")
    api_token = need("API_TOKEN")
    gitea_token = (env.get("GITEA_TOKEN") or "").strip()

    def num(key, default, cast=int):
        raw = (env.get(key) or "").strip()
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            errors.append(f"valore non numerico per {key}: {raw!r}")
            return default

    sources = []
    for n in range(1, MAX_SOURCES + 1):
        name = (env.get(f"SOURCE_{n}_NAME") or "").strip()
        if not name:
            continue
        gh_token = (env.get(f"SOURCE_{n}_GH_TOKEN") or "").strip()
        if not gh_token:
            errors.append(f"variabile mancante: SOURCE_{n}_GH_TOKEN")
        owner = (env.get(f"SOURCE_{n}_GITEA_OWNER") or name).strip()
        src_gitea_token = (env.get(f"SOURCE_{n}_GITEA_TOKEN") or "").strip() or gitea_token
        if not src_gitea_token:
            errors.append(f"serve GITEA_TOKEN oppure SOURCE_{n}_GITEA_TOKEN")
        sources.append(Source(
            name=name,
            gh_org=(env.get(f"SOURCE_{n}_GH_ORG") or "").strip(),
            gitea_owner=owner,
            gh_token=gh_token,
            gitea_token=src_gitea_token,
        ))
    if not sources:
        errors.append("nessuna sorgente configurata (SOURCE_1_NAME, SOURCE_1_GH_TOKEN, ...)")
    names = [s.name for s in sources]
    if len(set(names)) != len(names):
        errors.append("i nomi delle sorgenti (SOURCE_n_NAME) devono essere univoci")

    cfg = Config(
        gitea_url=gitea_url,
        gitea_token=gitea_token,
        github_api=(env.get("GITHUB_API_URL") or "https://api.github.com").strip().rstrip("/"),
        api_token=api_token,
        port=num("PORT", 8080),
        interval_s=max(60, num("CHECK_INTERVAL_MIN", 60) * 60),
        stale_after=timedelta(hours=num("STALE_AFTER_HOURS", 72, float)),
        warn_days=num("TOKEN_WARN_DAYS", 30),
        verify_tls=(env.get("VERIFY_TLS", "true").strip().lower() not in ("0", "false", "no")),
        data_dir=(env.get("DATA_DIR") or "/data").strip(),
        sources=sources,
    )
    if errors:
        raise ConfigError("; ".join(errors))
    return cfg


# --------------------------------------------------------------------------
# Accesso HTTP
# --------------------------------------------------------------------------
class ApiError(Exception):
    """Errore di una chiamata API. Il messaggio non contiene mai token."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _ssl_context(verify: bool):
    if verify:
        return None
    return ssl._create_unverified_context()  # noqa: SLF001 - scelta esplicita dell'utente


def http_json(url, headers, verify_tls=True, timeout=30):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context(verify_tls)) as resp:
            body = resp.read()
            return (json.loads(body) if body else None), resp.headers
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.loads(e.read() or b"{}").get("message") or "")[:120]
        except Exception:  # corpo non JSON
            pass
        raise ApiError(e.code, f"HTTP {e.code}" + (f" ({detail})" if detail else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        reason = getattr(e, "reason", e)
        raise ApiError(0, f"connessione fallita: {reason}") from None


_EXP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def parse_token_expiry(value):
    """Interpreta l'header github-authentication-token-expiration (UTC)."""
    if not value:
        return None
    m = _EXP_RE.search(value)
    if not m:
        return None
    return datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}").replace(tzinfo=timezone.utc)


def parse_time(value):
    """ISO 8601 -> datetime con fuso. None se vuoto o data 'zero' di Gitea."""
    if not value or value.startswith("0001-"):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def github_repos(cfg: Config, src: Source):
    """Tutte le repo visibili al token. Ritorna (repo, scadenza_token)."""
    if src.gh_org:
        base = f"{cfg.github_api}/orgs/{quote(src.gh_org)}/repos?type=all"
    else:
        base = f"{cfg.github_api}/user/repos?affiliation=owner"
    headers = {
        "Authorization": f"Bearer {src.gh_token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": f"gitea-watcher/{VERSION}",
    }
    repos, expires, page = [], None, 1
    while True:
        data, hdrs = http_json(f"{base}&per_page=100&page={page}", headers, True)
        if expires is None:
            expires = parse_token_expiry(hdrs.get("github-authentication-token-expiration"))
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1
    return repos, expires


def gitea_repos(cfg: Config, src: Source):
    """Tutte le repo del proprietario in Gitea (utente o organizzazione)."""
    kind = "orgs" if src.gh_org else "users"
    base = f"{cfg.gitea_url}/api/v1/{kind}/{quote(src.gitea_owner)}/repos"
    headers = {"Authorization": f"token {src.gitea_token}", "Accept": "application/json"}
    repos, page, limit = [], 1, 50
    while True:
        data, hdrs = http_json(f"{base}?limit={limit}&page={page}", headers, cfg.verify_tls)
        if not data:
            break
        repos.extend(data)
        total = hdrs.get("X-Total-Count")
        if total and total.isdigit() and len(repos) >= int(total):
            break
        if not total and len(data) < limit:
            break
        page += 1
    return repos


# --------------------------------------------------------------------------
# Controlli
# --------------------------------------------------------------------------
def check_source(cfg: Config, src: Source, now: datetime) -> dict:
    res = {
        "github_org": src.gh_org or None,
        "gitea_owner": src.gitea_owner,
        "github_total": None,
        "gitea_total": None,
        "missing": [],
        "extra": [],
        "stale": [],
        "token_expires_utc": None,
        "token_days_left": None,
        "errors": [],
    }
    gh, gt = None, None

    try:
        gh, expires = github_repos(cfg, src)
        res["github_total"] = sum(1 for r in gh if not r.get("archived"))
        if expires:
            res["token_expires_utc"] = expires.isoformat(timespec="seconds")
            res["token_days_left"] = int((expires - now).total_seconds() // 86400)
    except ApiError as e:
        res["errors"].append(f"GitHub: {e}")

    try:
        gt = gitea_repos(cfg, src)
        res["gitea_total"] = len(gt)
        for r in gt:
            if not r.get("mirror"):
                continue
            updated = parse_time(r.get("mirror_updated"))
            age = None if updated is None else now - updated
            if age is None or age > cfg.stale_after:
                res["stale"].append({
                    "name": r["name"],
                    "hours": None if age is None else int(age.total_seconds() // 3600),
                })
        res["stale"].sort(key=lambda x: (x["hours"] is not None, -(x["hours"] or 0), x["name"]))
    except ApiError as e:
        res["errors"].append(f"Gitea: {e}")

    if gh is not None and gt is not None:
        gitea_lower = {r["name"].lower() for r in gt}
        gh_all_lower = {r["name"].lower() for r in gh}
        res["missing"] = sorted(r["name"] for r in gh
                                if not r.get("archived") and r["name"].lower() not in gitea_lower)
        res["extra"] = sorted(r["name"] for r in gt if r["name"].lower() not in gh_all_lower)
    return res


def build_status(cfg: Config, results: dict, started: datetime, duration: float) -> dict:
    problems, notes = [], []
    tot = {"github_repos": 0, "gitea_repos": 0, "missing": 0, "stale": 0,
           "source_errors": 0, "min_token_days": None}
    sources_out = {}
    for name, r in results.items():
        tot["github_repos"] += r["github_total"] or 0
        tot["gitea_repos"] += r["gitea_total"] or 0
        tot["missing"] += len(r["missing"])
        tot["stale"] += len(r["stale"])
        tot["source_errors"] += len(r["errors"])
        days = r["token_days_left"]
        if days is not None and (tot["min_token_days"] is None or days < tot["min_token_days"]):
            tot["min_token_days"] = days

        for err in r["errors"]:
            problems.append(f"{name}: {err}")
        if r["missing"]:
            sample = ", ".join(r["missing"][:5]) + ("..." if len(r["missing"]) > 5 else "")
            problems.append(f"{name}: {len(r['missing'])} repo su GitHub non presenti in Gitea ({sample})")
        if r["stale"]:
            sample = ", ".join(x["name"] for x in r["stale"][:5]) + ("..." if len(r["stale"]) > 5 else "")
            problems.append(f"{name}: {len(r['stale'])} mirror fermi da oltre "
                            f"{int(cfg.stale_after.total_seconds() // 3600)}h ({sample})")
        if days is not None and days < cfg.warn_days:
            when = "e' scaduto" if days < 0 else f"scade tra {days} giorni"
            problems.append(f"{name}: il token GitHub {when}")
        if r["extra"]:
            notes.append(f"{name}: {len(r['extra'])} repo in Gitea non piu' presenti su GitHub")

        sources_out[name] = dict(r, missing=r["missing"][:LIST_CAP], extra=r["extra"][:LIST_CAP],
                                 stale=r["stale"][:LIST_CAP])

    return {
        "version": VERSION,
        "last_run_utc": started.isoformat(timespec="seconds"),
        "duration_s": round(duration, 1),
        "healthy": not problems,
        "totals": tot,
        "problems": problems,
        "notes": notes,
        "sources": sources_out,
    }


# --------------------------------------------------------------------------
# Stato e scheduler
# --------------------------------------------------------------------------
class State:
    def __init__(self, data_dir):
        self._lock = threading.Lock()
        self._status = None
        self._path = os.path.join(data_dir, "status.json")
        self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as f:
                self._status = json.load(f)
            log.info("stato precedente caricato da %s", self._path)
        except (OSError, ValueError):
            pass

    def get(self):
        with self._lock:
            return self._status

    def set(self, status):
        with self._lock:
            self._status = status
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f)
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("impossibile salvare lo stato su disco: %s", e)


def run_check(cfg: Config, state: State):
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    results = {}
    for src in cfg.sources:
        try:
            results[src.name] = check_source(cfg, src, started)
        except Exception as e:  # un bug in una sorgente non ferma le altre
            log.exception("errore imprevisto nella sorgente %s", src.name)
            results[src.name] = {
                "github_org": src.gh_org or None, "gitea_owner": src.gitea_owner,
                "github_total": None, "gitea_total": None, "missing": [], "extra": [],
                "stale": [], "token_expires_utc": None, "token_days_left": None,
                "errors": [f"errore interno: {type(e).__name__}"],
            }
    status = build_status(cfg, results, started, time.monotonic() - t0)
    state.set(status)
    log.info("controllo completato in %.1fs: %s", status["duration_s"],
             "tutto ok" if status["healthy"] else f"{len(status['problems'])} problemi")
    for p in status["problems"]:
        log.warning("problema: %s", p)
    return status


def scheduler(cfg, state, stop: threading.Event, trigger: threading.Event):
    while not stop.is_set():
        try:
            run_check(cfg, state)
        except Exception:
            log.exception("controllo fallito")
        trigger.wait(cfg.interval_s)
        trigger.clear()


# --------------------------------------------------------------------------
# API HTTP
# --------------------------------------------------------------------------
class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler, cfg, state, trigger):
        super().__init__(addr, handler)
        self.cfg, self.state, self.trigger = cfg, state, trigger


class Handler(BaseHTTPRequestHandler):
    server_version = "gitea-watcher"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.debug("http %s", fmt % args)

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.server.cfg.api_token.encode())

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, {"status": "ok"})
        if path == "/status":
            if not self._authorized():
                return self._send(401, {"error": "non autorizzato"})
            status = self.server.state.get()
            if status is None:
                return self._send(503, {"status": "primo controllo in corso"})
            return self._send(200, status)
        return self._send(404, {"error": "non trovato"})

    def do_POST(self):
        if urlparse(self.path).path == "/refresh":
            if not self._authorized():
                return self._send(401, {"error": "non autorizzato"})
            self.server.trigger.set()
            return self._send(202, {"status": "controllo richiesto"})
        return self._send(404, {"error": "non trovato"})


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        cfg = load_config()
    except ConfigError as e:
        log.error("configurazione non valida: %s", e)
        return 2

    log.info("gitea-watcher %s: %d sorgenti, controllo ogni %d min, API sulla porta %d",
             VERSION, len(cfg.sources), cfg.interval_s // 60, cfg.port)
    state = State(cfg.data_dir)
    stop, trigger = threading.Event(), threading.Event()
    server = Server(("0.0.0.0", cfg.port), Handler, cfg, state, trigger)
    threading.Thread(target=scheduler, args=(cfg, state, stop, trigger), daemon=True).start()

    def shutdown(*_):
        stop.set()
        trigger.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
