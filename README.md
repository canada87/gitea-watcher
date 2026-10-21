# gitea-watcher

Piccolo servizio che tiene d'occhio i mirror di Gitea rispetto a GitHub e
espone lo stato via HTTP, così Home Assistant può mostrarlo e avvisare.

Versione 0.1: **sola lettura**. Non crea, modifica né cancella niente né su
GitHub né su Gitea.

## Cosa controlla

Per ogni sorgente (un account personale o un'organizzazione GitHub):

- **Repo mancanti**: presenti su GitHub (non archiviate) ma non in Gitea.
- **Mirror fermi**: ultimo aggiornamento più vecchio di `STALE_AFTER_HOURS`
  (default 72 ore, con mirror a 24 ore sono tre cicli saltati).
  È un'euristica basata sul campo `mirror_updated` di Gitea.
- **Repo in più in Gitea**: presenti in Gitea ma non più su GitHub (cancellate
  o non più visibili). È solo una nota, non un problema.
- **Scadenza del token GitHub**: letta dall'header
  `github-authentication-token-expiration`, con avviso sotto `TOKEN_WARN_DAYS`.
- **Errori di accesso**: token scaduto o non valido, organizzazione non
  trovata, Gitea non raggiungibile.

Il controllo gira all'avvio e poi ogni `CHECK_INTERVAL_MIN` minuti
(default 60). L'ultimo stato viene salvato in `/data/status.json`, così un
riavvio del container non lo azzera.

## API

| Metodo | Percorso   | Autenticazione        | Risposta |
|--------|------------|-----------------------|----------|
| GET    | `/health`  | nessuna               | `{"status": "ok"}` |
| GET    | `/status`  | `Bearer <API_TOKEN>`  | stato dell'ultimo controllo (503 durante il primo) |
| POST   | `/refresh` | `Bearer <API_TOKEN>`  | 202, forza un nuovo controllo |

Esempio:

```bash
curl -H "Authorization: Bearer $API_TOKEN" http://IP_VM:8765/status
```

I campi principali di `/status`: `healthy` (booleano), `totals`
(`github_repos`, `gitea_repos`, `missing`, `stale`, `source_errors`,
`min_token_days`), `problems` (frasi pronte da mostrare), `notes` e il dettaglio
per sorgente in `sources`. Le liste lunghe sono troncate a 50 elementi.

## Configurazione

Tutto da variabili d'ambiente. Parti da `stack.env.example`. Il file reale
`stack.env` contiene segreti ed è escluso da `.gitignore`.

| Variabile | Obbligatoria | Descrizione |
|-----------|--------------|-------------|
| `GITEA_URL` | sì | URL di Gitea raggiungibile **dal container** (IP e porta della VM, non `localhost`) |
| `GITEA_TOKEN` | sì (o per sorgente) | Token Gitea di sola lettura: permessi `read:repository` e `read:organization` |
| `API_TOKEN` | sì | Segreto che Home Assistant manda come `Bearer` |
| `SOURCE_n_NAME` | sì | Nome della sorgente (univoco), n da 1 a 50 |
| `SOURCE_n_GH_TOKEN` | sì | Token GitHub fine-grained, solo `Contents: read` |
| `SOURCE_n_GH_ORG` | no | Organizzazione GitHub. Vuoto = account personale del token |
| `SOURCE_n_GITEA_OWNER` | no | Utente o organizzazione in Gitea (default: il nome) |
| `SOURCE_n_GITEA_TOKEN` | no | Token Gitea specifico per la sorgente, se quello globale non vede le sue repo private |
| `CHECK_INTERVAL_MIN` | no | Default 60 (minimo 1) |
| `STALE_AFTER_HOURS` | no | Default 72 |
| `TOKEN_WARN_DAYS` | no | Default 30 |
| `VERIFY_TLS` | no | `false` solo se `GITEA_URL` è https con certificato non valido |
| `LOG_LEVEL` | no | Default `INFO` |

Se l'utente Gitea del token non vede le repo private di un altro utente, in
`/status` quelle repo risulteranno "mancanti": è il segnale per usare
`SOURCE_n_GITEA_TOKEN` con il token del proprietario.

## Installazione

### Da Portainer (consigliato)

1. Pubblica questa cartella su GitHub (repo privato va bene).
2. Portainer → Stacks → Add stack → **Repository**: URL della repo, riferimento
   `refs/heads/main`, file `compose.yaml`. Per un repo privato attiva
   l'autenticazione con un token GitHub di sola lettura su quella repo.
3. In **Environment variables** incolla il contenuto di `stack.env.example`
   compilato (modalità "Advanced"). Portainer lo rende disponibile al
   container tramite `env_file: stack.env`.
4. Deploy. Portainer costruisce l'immagine con il `Dockerfile`.

### Da riga di comando

```bash
git clone <repo> gitea-watcher && cd gitea-watcher
cp stack.env.example stack.env && nano stack.env && chmod 600 stack.env
docker compose up -d --build
docker compose logs -f
```

L'API è sulla porta **8765** della VM. Tienila raggiungibile solo da LAN e
Tailscale: contiene i nomi delle repo, anche private.

## Home Assistant

1. In `secrets.yaml`: `gitea_watcher_auth: "Bearer <API_TOKEN>"`.
2. Aggiungi `homeassistant/gitea_watcher.yaml` (sostituendo `IP_VM`) come
   package o dentro `configuration.yaml`, poi ricarica la configurazione.
   Crea questi sensori: repo mancanti, mirror fermi, repo in Gitea, giorni alla
   scadenza del token, ultimo controllo, e un binary sensor "problemi" con
   l'elenco in `problems`.
3. Crea l'automazione di avviso da `homeassistant/gitea_watcher_automation.yaml`
   (sostituendo `NOME` con il tuo `notify.mobile_app_<nome>`). Notifica quando
   la lista dei problemi cambia e quando il watcher non risponde per 30 minuti.

## Sicurezza

- I token GitHub e Gitea stanno solo nell'ambiente del container e non vengono
  mai scritti nei log né nello stato.
- L'API usa un `Bearer` confrontato a tempo costante; `/health` è l'unico
  endpoint pubblico e non restituisce dati.
- Il container gira come utente non privilegiato e con limite di 128 MB.
- Usa token GitHub **fine-grained di sola lettura** e un token Gitea di sola
  lettura: anche in caso di fuga non permettono di scrivere.

## Sviluppo

```bash
python -m unittest discover -s tests -v
```

I test avviano finti server GitHub e Gitea locali: non servono rete né
credenziali. Il programma usa solo la libreria standard (Python 3.11 o
successivo).

## Prossimi passi

- **0.2**: importare automaticamente le repo nuove come mirror (richiede un
  token Gitea con permesso di scrittura, per questo è separato dalla 0.1).
