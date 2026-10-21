# gitea-watcher

Piccolo servizio che tiene d'occhio i mirror di Gitea rispetto a GitHub e
espone lo stato via HTTP, così Home Assistant può mostrarlo e avvisare.

Per default è in **sola lettura**: non crea, modifica né cancella niente né su
GitHub né su Gitea. Dalla versione 0.2 può anche importare da solo le repo
nuove come mirror (vedi "Import automatico"), ma solo se lo attivi tu.

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
(`github_repos`, `gitea_repos`, `missing`, `stale`, `imported`, `source_errors`,
`min_token_days`), `problems` (frasi pronte da mostrare), `notes`,
`recent_imports` (ultimi 20 import automatici, con data) e il dettaglio per
sorgente in `sources`. Le liste lunghe sono troncate a 50 elementi.

## Configurazione

Tutto da variabili d'ambiente. Parti da `stack.env.example`. Il file reale
`stack.env` contiene segreti ed è escluso da `.gitignore`.

| Variabile | Obbligatoria | Descrizione |
|-----------|--------------|-------------|
| `GITEA_URL` | sì | URL di Gitea raggiungibile **dal container** (IP e porta della VM, non `localhost`) |
| `GITEA_TOKEN` | sì (o per sorgente) | Token Gitea di sola lettura: permessi `read:repository`, `read:organization` e `read:user` |
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
| `EXCLUDE_REPOS` | no | Repo da ignorare, separate da virgola: `nome` (vale per tutte le sorgenti) oppure `sorgente/nome`. Non risultano mai mancanti e non vengono importate |
| `AUTO_IMPORT` | no | `true` per importare automaticamente le repo mancanti. Default spento |
| `GITEA_IMPORT_TOKEN` | con `AUTO_IMPORT` | Token Gitea con permesso di scrittura sui repository, usato solo per creare i mirror |
| `AUTO_IMPORT_MAX_PER_RUN` | no | Repo importate al massimo per controllo. Default 5 |
| `AUTO_IMPORT_MAX_MB` | no | Le repo più grandi non vengono importate da sole (le segnala). Default 500, `0` = nessun limite |
| `AUTO_IMPORT_MIRROR_INTERVAL` | no | Intervallo di aggiornamento dei nuovi mirror. Default `24h0m0s` |

Se l'utente Gitea del token non vede le repo private di un altro utente, in
`/status` quelle repo risulteranno "mancanti": è il segnale per usare
`SOURCE_n_GITEA_TOKEN` con il token del proprietario.

## Import automatico

Spento di default. Con `AUTO_IMPORT=true`, a ogni controllo il watcher crea in
Gitea un mirror per ogni repo che c'è su GitHub e non in Gitea:

- mirror **privato**, solo codice (niente issue, pull request, wiki, release né LFS),
  con l'intervallo `AUTO_IMPORT_MIRROR_INTERVAL`;
- proprietario in Gitea = `SOURCE_n_GITEA_OWNER`, credenziali = `SOURCE_n_GH_TOKEN`;
- repo archiviate escluse, fork inclusi, come nell'import iniziale;
- al massimo `AUTO_IMPORT_MAX_PER_RUN` repo per controllo: se ne compaiono molte
  insieme il resto arriva ai controlli successivi (e intanto compaiono tra i
  problemi);
- le repo oltre `AUTO_IMPORT_MAX_MB` non vengono importate: compaiono tra i
  problemi con la dimensione, per decidere a mano;
- dopo 5 tentativi falliti nello stesso controllo si ferma; con un token di
  import non valido si ferma al primo errore;
- non cancella e non modifica mai nulla di esistente: se il repo esiste già
  (risposta 409) lo considera a posto.

Cosa vedi: le repo importate compaiono in `notes` e in `recent_imports` di
`/status`; gli errori di import sono in `problems`.

Il token di import (`GITEA_IMPORT_TOKEN`) è separato da `GITEA_TOKEN`, che
resta di sola lettura. Va creato in Gitea (Impostazioni -> Applicazioni) con
**repository: Lettura e Scrittura** e, per creare repo dentro le organizzazioni,
dall'utente che ne è proprietario (o amministratore). Per creare un mirror
Gitea salva il token GitHub nella configurazione del mirror, come già avviene
per quelli importati a mano.

## Installazione

L'immagine Docker viene costruita e pubblicata da GitHub Actions su GitHub
Packages: `ghcr.io/canada87/gitea-watcher`. Nessuna build sulla VM e nessun
accesso di Portainer alla repo.

Prima volta: dopo il primo push su `main`, apri la pagina del pacchetto
(profilo GitHub -> Packages -> `gitea-watcher` -> Package settings) e imposta la
visibilità su **Public**, altrimenti la VM dovrebbe fare `docker login ghcr.io`.
L'immagine non contiene segreti (la configurazione arriva dall'ambiente).

### Da Portainer (consigliato)

1. Stacks -> Add stack -> **Web editor**, nome `gitea-watcher`.
2. Incolla il contenuto di `compose.yaml`. Meglio sostituire `:latest` con un
   tag fisso (vedi "Rilasci"), come per qualsiasi servizio.
3. In **Environment variables** (modalità "Advanced") incolla il contenuto di
   `stack.env.example` compilato. Portainer lo rende disponibile al container
   tramite `env_file: stack.env`.
4. Deploy. Per aggiornare: cambia il tag nel compose e ridistribuisci con
   "Re-pull image".

### Da riga di comando

```bash
mkdir gitea-watcher && cd gitea-watcher
# copia qui compose.yaml e crea stack.env a partire da stack.env.example
nano stack.env && chmod 600 stack.env
docker compose up -d
docker compose logs -f
```

Per provare modifiche locali senza passare da GitHub:
`docker build -t ghcr.io/canada87/gitea-watcher:dev .` e metti `:dev` nel compose.

L'API è sulla porta **8765** della VM. Tienila raggiungibile solo da LAN e
Tailscale: contiene i nomi delle repo, anche private.

## Rilasci

Il workflow `.github/workflows/docker.yml` esegue i test e prova l'immagine
(si avvia, risponde su `/health`, `/status` rifiuta le richieste senza token,
non gira come root). Se è tutto a posto pubblica:

| Evento | Tag pubblicati |
|--------|----------------|
| push su `main` | `latest`, `sha-xxxxxxx` |
| tag `v0.1.0` | `0.1.0`, `0.1`, `latest` (oltre a `sha-...`) |
| pull request | niente, solo test |

Per fare un rilascio: `git tag v0.1.0 && git push --tags`. Dependabot propone
ogni mese gli aggiornamenti delle action e dell'immagine base.

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
- L'unico token con scrittura è `GITEA_IMPORT_TOKEN`, richiesto solo se accendi
  `AUTO_IMPORT`; permette di creare repo in Gitea, non di modificare o cancellare
  quelle esistenti tramite il watcher. Il codice non contiene alcuna chiamata di
  modifica o cancellazione.

## Sviluppo

```bash
python -m unittest discover -s tests -v
```

I test avviano finti server GitHub e Gitea locali: non servono rete né
credenziali. Il programma usa solo la libreria standard (Python 3.11 o
successivo).

## Prossimi passi

Nessuno in programma. Idee: segnalare i mirror il cui token GitHub è scaduto
come causa del blocco, e un sensore Home Assistant per `recent_imports`.
