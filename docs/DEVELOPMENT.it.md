# Sviluppo Home Assistant con Docker Compose

Dalla radice del repository:

```bash
./docker/dev.sh
```

Servono Docker con il plugin Compose, Bash per il launcher e accesso a internet
per scaricare le immagini al primo avvio. Non servono Python sul computer host
o HACS. Il comando avvia `vimar-intercom-dev`, completa l'onboarding HA, verifica accesso e modulo iniziale
Vimar e stampa URL e credenziali. Apri
**http://localhost:18123/vimar-dev/development** e accedi con
**`developer` / `developer`**. Lo stack compare anche in Docker Desktop.

## Modifiche e aggiornamenti

L'integrazione e il frontend sono montati in sola lettura dal checkout corrente.
Dopo modifiche Python o un cambio di branch, esegui nuovamente `./docker/dev.sh`:
i container vengono ricreati e caricano il codice aggiornato, conservando dati e
accesso. Il codice Python non viene ricaricato automaticamente.

Dalla radice del repository puoi usare direttamente il file Docker Compose:

```bash
docker compose -f docker/docker-compose.yaml up -d --force-recreate
docker compose -f docker/docker-compose.yaml logs bootstrap           # stato, URL e credenziali
docker compose -f docker/docker-compose.yaml logs -f homeassistant    # log applicativi
docker compose -f docker/docker-compose.yaml down                    # arresta, conservando i dati
```

Lo script riunisce avvio, attesa del bootstrap e stampa delle credenziali;
restituisce un errore se l'avvio o il bootstrap fallisce. Per un'altra porta:

```bash
HA_DEV_PORT=18124 ./docker/dev.sh
```

Mantieni la stessa variabile per i comandi Compose successivi. Per più checkout
contemporanei, assegna anche un `COMPOSE_PROJECT_NAME` distinto a ciascuno.
Ogni checkout conserva i propri dati.

## File e dati persistenti

`docker/docker-compose.yaml` definisce immagini, servizi, controlli di avvio e reti. `docker/`
contiene launcher, configurazione HA minima, dashboard, proxy Nginx e script di
inizializzazione. `seed.py` crea solo i file mancanti; `bootstrap.py` crea
l'utente tramite l'API di onboarding HA, verifica accesso, modulo e dashboard,
quindi revoca la propria sessione temporanea.

`.dev-ha/` è esclusa da Git e separata dalla produzione. Contiene configurazione
e credenziali (`credentials.json`). L'accesso fisso è destinato solo all'istanza
locale; le credenziali compaiono anche nei log del bootstrap. Non condividere
directory o log. Le modifiche ai template non sovrascrivono configurazioni già
esistenti.

Su Linux Docker Engine, HA gira come root: i file dentro `.dev-ha/` possono
appartenere a root e non essere leggibili dall'utente host. Il launcher stampa
comunque le credenziali. Per cancellare i dati di sviluppo e ripartire da zero:

```bash
./docker/dev.sh reset
```

Il reset arresta/rimuove container e reti di questo progetto Compose, poi svuota
solo `.dev-ha/` con un container temporaneo senza rete e rimuove la directory
vuota. **Cancella accesso, configurazione e cronologia dell'istanza di sviluppo.**
Salva prima i dati che vuoi conservare. Usa gli stessi `COMPOSE_PROJECT_NAME` e
`HA_DEV_PORT` dell'avvio. Non servono `sudo` sull'host e non vengono coinvolti
produzione o altri progetti Compose. Una `.dev-ha` che sia un link simbolico viene
rifiutata.

Poi `./docker/dev.sh` crea una nuova istanza. Per conservare i dati, usa invece
`docker compose -f docker/docker-compose.yaml down` oppure arresta i container.
La suite pytest include, sui runner Linux temporanei di GitHub Actions, una
verifica con Docker Engine: onboarding, persistenza al riavvio, reset dei dati
di root e nuovo onboarding dopo il reset. Nei test locali questa verifica viene
saltata e Docker non viene avviato automaticamente.

## Isolamento e limiti

Solo Nginx pubblica una porta, su `127.0.0.1`. HA utilizza una rete Docker interna
senza accesso in uscita a LAN o internet. Il proxy consente l'accesso dal browser,
non l'uscita da HA. La configurazione minima omette `default_config` e discovery.

Al primo avvio il bootstrap apre e annulla il modulo iniziale Vimar senza QR,
credenziali SIP o registrazione. Non importa configurazioni o automazioni dalla
produzione. Un'integrazione già configurata viene conservata; HA ne esegue le
normali operazioni iniziali nei limiti della rete isolata.

L'ambiente serve per sviluppo di moduli, opzioni e frontend; non simula un
citofono e non verifica chiamate, squilli o media reali. Questi test richiedono
una rete configurata separatamente e il consenso del proprietario per le azioni
specifiche. Non sono inclusi inoltri LAN, rete host o modifiche SIP sperimentali.

Le immagini sono fissate per ripetibilità. Quando aggiorni HA, verifica onboarding
da zero e un secondo avvio con dati conservati: `seed.py` usa le costanti di
archiviazione onboarding di quella versione.
