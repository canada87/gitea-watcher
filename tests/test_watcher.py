"""Test di gitea-watcher contro finti server GitHub e Gitea (solo libreria standard).

Esecuzione:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import watcher  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
GH_TOKEN = "fake-github-token"
GT_TOKEN = "fake-gitea-token"
IMPORT_TOKEN = "fake-import-token"


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def gh_repo(name, archived=False, size=10):
    return {"name": name, "archived": archived, "fork": False, "size": size,
            "clone_url": f"https://github.com/org1/{name}.git"}


def gt_repo(name, hours_ago=2, mirror=True, never=False):
    updated = "0001-01-01T00:00:00Z" if never else iso(NOW - timedelta(hours=hours_ago))
    return {"name": name, "mirror": mirror, "mirror_updated": updated}


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        srv.posts.append({"path": urlparse(self.path).path, "auth": self.headers.get("Authorization"),
                          "body": body})
        if self.headers.get("Authorization") != (srv.post_auth or srv.expected_auth):
            return self._send(401, {"message": "token is required"})
        code, reply = srv.post_replies.get(body.get("repo_name"), (201, {}))
        self._send(code, reply)

    def do_GET(self):
        srv = self.server
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if self.headers.get("Authorization") != srv.expected_auth:
            return self._send(401, {"message": "Bad credentials"})
        data = srv.routes.get(url.path)
        if data is None:
            return self._send(404, {"message": "Not Found"})
        per = min(int((query.get("per_page") or query.get("limit") or ["30"])[0]), srv.cap)
        page = int((query.get("page") or ["1"])[0])
        headers = dict(srv.extra_headers)
        if srv.send_total:
            headers["X-Total-Count"] = str(len(data))
        srv.hits.append(url.path)
        self._send(200, data[(page - 1) * per: page * per], headers)


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, expected_auth, routes, cap=100, extra_headers=None, send_total=False,
                 post_auth=None, post_replies=None):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.post_auth, self.post_replies, self.posts = post_auth, post_replies or {}, []
        self.expected_auth, self.routes, self.cap = expected_auth, routes, cap
        self.extra_headers, self.send_total, self.hits = extra_headers or {}, send_total, []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()


def make_cfg(github_url, gitea_url, **kw):
    src = watcher.Source("org1", "org1", "org1", GH_TOKEN, GT_TOKEN)
    base = dict(gitea_url=gitea_url, gitea_token=GT_TOKEN, github_api=github_url, api_token="secret",
                port=0, interval_s=3600, stale_after=timedelta(hours=72), warn_days=30,
                verify_tls=True, data_dir=tempfile.gettempdir(), sources=[src])
    base.update(kw)
    return watcher.Config(**base), src


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def fake(self, *args, **kw):
        s = Fake(*args, **kw)
        self.servers.append(s)
        return s

    def run_check(self, gh_routes, gt_routes, gh_kw=None, gt_kw=None, cfg_kw=None):
        gh = self.fake(f"Bearer {GH_TOKEN}", gh_routes, **(gh_kw or {}))
        gt = self.fake(f"token {GT_TOKEN}", gt_routes, cap=50, send_total=True, **(gt_kw or {}))
        cfg, src = make_cfg(gh.url, gt.url, **(cfg_kw or {}))
        res = watcher.check_source(cfg, src, NOW)
        return cfg, src, res, gh, gt

    def test_missing_extra_archived_and_case(self):
        _, _, res, _, _ = self.run_check(
            {"/orgs/org1/repos": [gh_repo("a"), gh_repo("B"), gh_repo("old", archived=True), gh_repo("d")]},
            {"/api/v1/orgs/org1/repos": [gt_repo("a"), gt_repo("b"), gt_repo("old"), gt_repo("gone")]},
        )
        self.assertEqual(res["missing"], ["d"])          # 'old' archiviata e 'B'/'b' stesso nome
        self.assertEqual(res["extra"], ["gone"])
        self.assertEqual(res["github_total"], 3)         # archiviata esclusa
        self.assertEqual(res["gitea_total"], 4)
        self.assertEqual(res["errors"], [])

    def test_stale_and_never_synced(self):
        _, _, res, _, _ = self.run_check(
            {"/orgs/org1/repos": [gh_repo("fresh"), gh_repo("old"), gh_repo("never"), gh_repo("plain")]},
            {"/api/v1/orgs/org1/repos": [gt_repo("fresh", 5), gt_repo("old", 24 * 5),
                                          gt_repo("never", never=True), gt_repo("plain", 9999, mirror=False)]},
        )
        stale = {x["name"]: x["hours"] for x in res["stale"]}
        self.assertEqual(stale, {"old": 120, "never": None})  # non-mirror e freschi ignorati
        self.assertEqual(res["stale"][0]["name"], "never")    # mai sincronizzati per primi

    def test_pagination_both_sides(self):
        n = 250
        _, _, res, gh, gt = self.run_check(
            {"/orgs/org1/repos": [gh_repo(f"r{i}") for i in range(n)]},
            {"/api/v1/orgs/org1/repos": [gt_repo(f"r{i}") for i in range(n)]},
        )
        self.assertEqual((res["github_total"], res["gitea_total"]), (n, n))
        self.assertEqual((res["missing"], res["stale"], res["extra"]), ([], [], []))
        self.assertEqual(len(gh.hits), 3)   # 100 per pagina
        self.assertEqual(len(gt.hits), 5)   # 50 per pagina

    def test_personal_account_uses_user_endpoints(self):
        gh = self.fake(f"Bearer {GH_TOKEN}", {"/user/repos": [gh_repo("x")]})
        gt = self.fake(f"token {GT_TOKEN}", {"/api/v1/users/me/repos": [gt_repo("x")]}, cap=50)
        cfg, _ = make_cfg(gh.url, gt.url)
        src = watcher.Source("me", "", "me", GH_TOKEN, GT_TOKEN)
        res = watcher.check_source(cfg, src, NOW)
        self.assertEqual((res["github_total"], res["gitea_total"], res["errors"]), (1, 1, []))

    def test_token_expiry_and_warning(self):
        cfg, src, res, _, _ = self.run_check(
            {"/orgs/org1/repos": [gh_repo("a")]},
            {"/api/v1/orgs/org1/repos": [gt_repo("a")]},
            gh_kw={"extra_headers": {"github-authentication-token-expiration": "2026-11-05 10:30:00 UTC"}},
        )
        self.assertEqual(res["token_days_left"], 14)
        status = watcher.build_status(cfg, {"org1": res}, NOW, 1.0)
        self.assertEqual(status["totals"]["min_token_days"], 14)
        self.assertFalse(status["healthy"])
        self.assertTrue(any("scade tra 14 giorni" in p for p in status["problems"]))

    def test_healthy_when_everything_ok(self):
        cfg, _, res, _, _ = self.run_check(
            {"/orgs/org1/repos": [gh_repo("a")]},
            {"/api/v1/orgs/org1/repos": [gt_repo("a")]},
        )
        status = watcher.build_status(cfg, {"org1": res}, NOW, 0.5)
        self.assertTrue(status["healthy"])
        self.assertEqual(status["problems"], [])
        self.assertIsNone(status["totals"]["min_token_days"])  # token senza scadenza

    def test_github_401_reports_error_and_never_leaks_tokens(self):
        gh = self.fake("Bearer un-altro-token", {"/orgs/org1/repos": []})
        gt = self.fake(f"token {GT_TOKEN}", {"/api/v1/orgs/org1/repos": [gt_repo("a")]}, cap=50)
        cfg, src = make_cfg(gh.url, gt.url)
        res = watcher.check_source(cfg, src, NOW)
        self.assertEqual(res["missing"], [])  # senza GitHub non si puo' dire cosa manca
        self.assertEqual(res["gitea_total"], 1)
        self.assertIn("GitHub: HTTP 401", res["errors"][0])
        status = watcher.build_status(cfg, {"org1": res}, NOW, 0.1)
        self.assertFalse(status["healthy"])
        blob = json.dumps(status)
        self.assertNotIn(GH_TOKEN, blob)
        self.assertNotIn(GT_TOKEN, blob)

    def test_gitea_owner_not_found(self):
        gh = self.fake(f"Bearer {GH_TOKEN}", {"/orgs/org1/repos": [gh_repo("a")]})
        gt = self.fake(f"token {GT_TOKEN}", {}, cap=50)
        cfg, src = make_cfg(gh.url, gt.url)
        res = watcher.check_source(cfg, src, NOW)
        self.assertIn("Gitea: HTTP 404", res["errors"][0])
        self.assertEqual(res["missing"], [])

    def test_connection_refused(self):
        cfg, src = make_cfg("http://127.0.0.1:1", "http://127.0.0.1:1")
        res = watcher.check_source(cfg, src, NOW)
        self.assertEqual(len(res["errors"]), 2)
        self.assertTrue(all("connessione fallita" in e for e in res["errors"]))

    def test_run_check_survives_unexpected_exception(self):
        cfg, _ = make_cfg("http://127.0.0.1:1", "http://127.0.0.1:1")
        original = watcher.check_source
        watcher.check_source = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with tempfile.TemporaryDirectory() as d:
                status = watcher.run_check(cfg, watcher.State(d))
        finally:
            watcher.check_source = original
        self.assertFalse(status["healthy"])
        self.assertIn("errore interno: RuntimeError", status["problems"][0])


class AutoImportTests(unittest.TestCase):
    """Import automatico dei repo mancanti (AUTO_IMPORT=true)."""

    def setUp(self):
        self.servers = []
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for s in self.servers:
            s.stop()
        self.tmp.cleanup()

    def setup_run(self, gh_repos, gt_repos, post_replies=None, **cfg_kw):
        gh = Fake(f"Bearer {GH_TOKEN}", {"/orgs/org1/repos": gh_repos})
        gt = Fake(f"token {GT_TOKEN}", {"/api/v1/orgs/org1/repos": gt_repos}, cap=50, send_total=True,
                  post_auth=f"token {IMPORT_TOKEN}", post_replies=post_replies)
        self.servers += [gh, gt]
        kw = dict(auto_import=True, import_token=IMPORT_TOKEN, data_dir=self.tmp.name)
        kw.update(cfg_kw)
        cfg, _ = make_cfg(gh.url, gt.url, **kw)
        return cfg, gt

    def run_once(self, cfg, state=None):
        return watcher.run_check(cfg, state or watcher.State(self.tmp.name))

    def test_disabled_by_default_does_not_write(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("b")], [gt_repo("a")], auto_import=False)
        status = self.run_once(cfg)
        self.assertEqual(gt.posts, [])
        self.assertEqual(status["sources"]["org1"]["missing"], ["b"])
        self.assertFalse(status["auto_import"])
        self.assertFalse(status["healthy"])

    def test_imports_missing_as_private_code_only_mirrors(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("c"), gh_repo("b")], [gt_repo("a")])
        status = self.run_once(cfg)
        self.assertEqual([p["body"]["repo_name"] for p in gt.posts], ["b", "c"])
        post = gt.posts[0]
        self.assertEqual(post["path"], "/api/v1/repos/migrate")
        self.assertEqual(post["auth"], f"token {IMPORT_TOKEN}")
        body = post["body"]
        self.assertEqual(body["clone_addr"], "https://github.com/org1/b.git")
        self.assertEqual((body["service"], body["auth_token"], body["repo_owner"]),
                         ("github", GH_TOKEN, "org1"))
        self.assertTrue(body["mirror"] and body["private"])
        self.assertEqual(body["mirror_interval"], "24h0m0s")
        for key in ("lfs", "wiki", "issues", "pull_requests", "labels", "milestones", "releases"):
            self.assertIs(body[key], False, key)
        src = status["sources"]["org1"]
        self.assertEqual((src["imported"], src["missing"], src["gitea_total"]), (["b", "c"], [], 3))
        self.assertEqual(status["totals"]["imported"], 2)
        self.assertTrue(status["healthy"])
        self.assertIn("importate 2 repo", status["notes"][0])
        self.assertEqual([i["repo"] for i in status["recent_imports"]], ["b", "c"])
        blob = json.dumps(status)
        for secret in (GH_TOKEN, GT_TOKEN, IMPORT_TOKEN):
            self.assertNotIn(secret, blob)

    def test_limit_per_run_leaves_the_rest_for_next_run(self):
        cfg, gt = self.setup_run([gh_repo(x) for x in "abcd"], [], import_max=2)
        status = self.run_once(cfg)
        self.assertEqual([p["body"]["repo_name"] for p in gt.posts], ["a", "b"])
        self.assertEqual(status["sources"]["org1"]["missing"], ["c", "d"])
        self.assertFalse(status["healthy"])   # restano da importare: lo si vede

    def test_too_large_is_not_imported_and_is_reported(self):
        cfg, gt = self.setup_run([gh_repo("small"), gh_repo("huge", size=600 * 1024)], [], import_max_mb=500)
        status = self.run_once(cfg)
        self.assertEqual([p["body"]["repo_name"] for p in gt.posts], ["small"])
        self.assertEqual(status["sources"]["org1"]["too_large"], [{"name": "huge", "mb": 600}])
        self.assertTrue(any("troppo grandi" in p and "huge (600 MB)" in p for p in status["problems"]))

    def test_size_limit_zero_means_no_limit(self):
        cfg, gt = self.setup_run([gh_repo("huge", size=600 * 1024)], [], import_max_mb=0)
        self.run_once(cfg)
        self.assertEqual(len(gt.posts), 1)

    def test_excluded_repos_are_neither_missing_nor_imported(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("secret"), gh_repo("tmp")], [],
                                 exclude=frozenset({"secret", "org1/tmp"}))
        status = self.run_once(cfg)
        self.assertEqual([p["body"]["repo_name"] for p in gt.posts], ["a"])
        self.assertTrue(status["healthy"])

    def test_exclusion_applies_even_when_auto_import_is_off(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("secret")], [gt_repo("a")],
                                 auto_import=False, exclude=frozenset({"secret"}))
        self.assertTrue(self.run_once(cfg)["healthy"])

    def test_archived_repos_are_not_imported(self):
        cfg, gt = self.setup_run([gh_repo("old", archived=True)], [])
        self.run_once(cfg)
        self.assertEqual(gt.posts, [])

    def test_already_exists_409_is_not_an_error(self):
        cfg, gt = self.setup_run([gh_repo("a")], [], post_replies={"a": (409, {"message": "exists"})})
        status = self.run_once(cfg)
        self.assertTrue(status["healthy"])
        self.assertEqual(status["sources"]["org1"]["imported"], [])

    def test_failure_is_reported_and_other_repos_continue(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("b")], [],
                                 post_replies={"a": (422, {"message": f"boom {GH_TOKEN}"})})
        status = self.run_once(cfg)
        src = status["sources"]["org1"]
        self.assertEqual(src["imported"], ["b"])
        self.assertEqual(src["import_failed"][0]["name"], "a")
        self.assertEqual(src["missing"], ["a"])
        self.assertTrue(any("import automatico fallito per a" in p for p in status["problems"]))
        self.assertNotIn(GH_TOKEN, json.dumps(status))

    def test_invalid_import_token_stops_everything(self):
        cfg, gt = self.setup_run([gh_repo("a"), gh_repo("b")], [], import_token="token-sbagliato")
        status = self.run_once(cfg)
        self.assertEqual(len(gt.posts), 1)     # al primo 401 non insiste
        self.assertEqual(status["sources"]["org1"]["imported"], [])
        self.assertFalse(status["healthy"])
        self.assertNotIn("token-sbagliato", json.dumps(status))

    def test_recent_imports_survive_between_runs(self):
        cfg, gt = self.setup_run([gh_repo("a")], [])
        state = watcher.State(self.tmp.name)
        self.run_once(cfg, state)
        gt.routes["/api/v1/orgs/org1/repos"] = [gt_repo("a")]
        status = self.run_once(cfg, state)
        self.assertEqual([i["repo"] for i in status["recent_imports"]], ["a"])
        self.assertEqual(status["totals"]["imported"], 0)

    def test_config_options(self):
        base = ConfigTests.BASE
        cfg = watcher.load_config(dict(base))
        self.assertEqual((cfg.auto_import, cfg.import_max, cfg.import_max_mb, cfg.import_interval),
                         (False, 5, 500, "24h0m0s"))
        on = dict(base, AUTO_IMPORT="true", GITEA_IMPORT_TOKEN="w", AUTO_IMPORT_MAX_PER_RUN="3",
                  AUTO_IMPORT_MAX_MB="0", AUTO_IMPORT_MIRROR_INTERVAL="12h0m0s",
                  EXCLUDE_REPOS=" Foo, me/Bar ,")
        cfg = watcher.load_config(on)
        self.assertEqual((cfg.auto_import, cfg.import_token, cfg.import_max, cfg.import_max_mb),
                         (True, "w", 3, 0))
        self.assertEqual(cfg.exclude, frozenset({"foo", "me/bar"}))

    def test_config_errors(self):
        base = ConfigTests.BASE
        with self.assertRaises(watcher.ConfigError) as ctx:
            watcher.load_config(dict(base, AUTO_IMPORT="true"))
        self.assertIn("GITEA_IMPORT_TOKEN", str(ctx.exception))
        for key, value in (("AUTO_IMPORT_MAX_PER_RUN", "0"), ("AUTO_IMPORT_MAX_MB", "-1"),
                           ("AUTO_IMPORT_MIRROR_INTERVAL", "ogni giorno")):
            with self.assertRaises(watcher.ConfigError, msg=key):
                watcher.load_config(dict(base, **{key: value}))


class ParsingTests(unittest.TestCase):
    def test_token_expiry_formats(self):
        self.assertEqual(watcher.parse_token_expiry("2026-11-05 10:30:00 UTC"),
                         datetime(2026, 11, 5, 10, 30, tzinfo=timezone.utc))
        self.assertIsNone(watcher.parse_token_expiry(None))
        self.assertIsNone(watcher.parse_token_expiry("boh"))

    def test_parse_time(self):
        self.assertIsNone(watcher.parse_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(watcher.parse_time(""))
        self.assertEqual(watcher.parse_time("2026-10-21T14:00:00+02:00"), NOW)


class ConfigTests(unittest.TestCase):
    BASE = {"GITEA_URL": "http://x:3010/", "API_TOKEN": "t", "GITEA_TOKEN": "g",
            "SOURCE_1_NAME": "me", "SOURCE_1_GH_TOKEN": "a",
            "SOURCE_3_NAME": "acme", "SOURCE_3_GH_ORG": "acme", "SOURCE_3_GH_TOKEN": "b",
            "SOURCE_3_GITEA_OWNER": "Acme", "SOURCE_3_GITEA_TOKEN": "own"}

    def test_valid_config(self):
        cfg = watcher.load_config(dict(self.BASE))
        self.assertEqual(cfg.gitea_url, "http://x:3010")
        self.assertEqual([s.name for s in cfg.sources], ["me", "acme"])  # tollera buchi nella numerazione
        self.assertEqual(cfg.sources[0].gitea_owner, "me")               # default = nome
        self.assertEqual(cfg.sources[0].gitea_token, "g")                # token globale
        self.assertEqual(cfg.sources[1].gitea_token, "own")              # override per sorgente
        self.assertEqual((cfg.interval_s, cfg.warn_days), (3600, 30))

    def test_missing_values_are_all_reported(self):
        with self.assertRaises(watcher.ConfigError) as ctx:
            watcher.load_config({"SOURCE_1_NAME": "me"})
        msg = str(ctx.exception)
        for part in ("GITEA_URL", "API_TOKEN", "SOURCE_1_GH_TOKEN", "GITEA_TOKEN"):
            self.assertIn(part, msg)

    def test_no_sources_and_duplicates(self):
        with self.assertRaises(watcher.ConfigError):
            watcher.load_config({"GITEA_URL": "http://x", "API_TOKEN": "t", "GITEA_TOKEN": "g"})
        dup = dict(self.BASE, SOURCE_2_NAME="me", SOURCE_2_GH_TOKEN="c")
        with self.assertRaises(watcher.ConfigError):
            watcher.load_config(dup)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg, _ = make_cfg("http://127.0.0.1:1", "http://127.0.0.1:1", data_dir=self.tmp.name)
        self.state = watcher.State(self.tmp.name)
        self.trigger = threading.Event()
        self.server = watcher.Server(("127.0.0.1", 0), watcher.Handler, cfg, self.state, self.trigger)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, method="GET", token=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=b"" if method == "POST" else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_is_public(self):
        self.assertEqual(self.call("/health"), (200, {"status": "ok"}))

    def test_status_requires_token(self):
        self.assertEqual(self.call("/status")[0], 401)
        self.assertEqual(self.call("/status", token="sbagliato")[0], 401)

    def test_status_before_and_after_first_run(self):
        self.assertEqual(self.call("/status", token="secret")[0], 503)
        self.state.set({"healthy": True, "totals": {"missing": 0}})
        code, body = self.call("/status", token="secret")
        self.assertEqual((code, body["healthy"]), (200, True))

    def test_refresh_requires_token_and_sets_trigger(self):
        self.assertEqual(self.call("/refresh", "POST")[0], 401)
        self.assertFalse(self.trigger.is_set())
        self.assertEqual(self.call("/refresh", "POST", token="secret")[0], 202)
        self.assertTrue(self.trigger.is_set())

    def test_unknown_path(self):
        self.assertEqual(self.call("/nope")[0], 404)

    def test_state_is_persisted_and_reloaded(self):
        self.state.set({"healthy": False, "problems": ["x"]})
        self.assertEqual(watcher.State(self.tmp.name).get(), {"healthy": False, "problems": ["x"]})


if __name__ == "__main__":
    unittest.main()
