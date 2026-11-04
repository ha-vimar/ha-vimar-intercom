# Resoconto notte 2026-11-04 (ramo notte/20261104)

Nessun comando verso dispositivi reali, nessun push.

## Fatto

Giro 1 (dipendenze non ancora installate, test non eseguibili allora):

- `c728fcb` test(tools): copre tools/parse_rubrica.py
- `2fdaa07` docs(rubrica): niente piu' rimandi a PROTOCOL.md, roadmap aggiornata

Giro 2 (verifica con le dipendenze installate):

- Suite predefinita: 2344 passati, 27 saltati.
- Suite come in CI (`-m "not live and not media and not browser and not docker" --cov`):
  2447 passati, 28 saltati, copertura 97,15% (soglia 95%). I test nuovi del giro 1
  (`tests/test_parse_rubrica_tool.py`) passano.
- Test lenti (`-m slow`): 103 passati, 3 saltati.
- `ruff check .`: pulito (ruff 0.16.10).
- Nessuna correzione al codice necessaria: niente commit di codice in questo giro.

Giro 3 (revisione dei commit di stanotte e degli ultimi 7 giorni: `1ccb4b2`, `c728fcb`,
`2fdaa07`, `59c09bf`). Le affermazioni dei commit di documentazione tornano col codice
(icone in `button.py`, download cloud dalla 1.0.12, README/README.it allineati). Trovati
due bug nel codice che quei commit documentano e testano:

- `9094567` fix(rubrica): percorsi con '#', '%' o '?' aperti correttamente. L'URI SQLite
  in sola lettura non faceva l'escape del percorso: un `rubrica #2.db` caricato dalle
  opzioni dava risultati vuoti senza errori, un `%` "file non trovato". Corretto in
  `rubrica_import.py` e in `tools/parse_rubrica.py`, con test.
- `2c56ea6` fix(tools): `parse_rubrica.py` su un file non SQLite usciva con un traceback;
  ora con un messaggio, come per il file mancante.
- Suite come in CI dopo la prima correzione: 2450 passati, 29 saltati, 1 fallito
  (vedi sotto, test dipendente dai tempi), copertura 97,15%. `ruff check .` pulito.

## Non fatto e perche'

- `tests/test_tmp_uri_probe.py`: file di prova creato nel giro 3 per riprodurre il bug
  dei percorsi. Cancellare/spostare file non mi era permesso, quindi l'ho svuotato (solo
  un commento, nessun test) e NON l'ho committato: si puo' cancellare.
- `tests/test_homekit_media.py::test_after_a_stall_silence_resumes_after_fill_after` e'
  fallito una volta nella suite completa (giro 3) e passa ripetuto da solo: misura tempi
  reali (`time.monotonic`, soglie di 30 ms) e sotto carico puo' sforare. Non toccato,
  causa non certa.

- `mypy`: non installato nella `.venv` e non usato dal progetto (nessun passo in CI,
  non e' in `requirements-dev.txt`); non l'ho aggiunto per non cambiare le dipendenze.
- Test `-m media` (15) e `-m browser` (95): saltati, mancano ffmpeg e i browser di
  Playwright. Installarli sull'host e' vietato da AGENTS.md; il runner Docker
  `.claude/skills/card-browser-tests/run.sh` resta la strada per eseguirli.
- `tests/test_config_flow_registration.py::test_a_closed_port_is_a_socket_error` e'
  fallito una volta (alla prima esecuzione con `-x`: timeout di 2 s invece di
  "Socket error") e poi e' passato in tutte le esecuzioni successive, da solo e
  nella suite. Probabile dipendenza dai tempi con cui Windows consegna il rifiuto
  ICMP sulla porta UDP chiusa. Causa non certa, quindi non toccato.

## Da decidere

- Roadmap di `docs/RUBRICA.md` §6: supporto `TIME` ed entita' derivate dal
  `PHONEBOOK` (+ `TVCC_LIST`). Sono funzioni nuove, scelte di design non ancora
  prese nei documenti.
- Commenti nel codice che rimandano ancora a `PROTOCOL.md`, che non c'e' (`const.py`,
  `event.py`, `hub.py`, `plant_messages.py`, `rubrica_import.py`, `ARCHITECTURE.md`):
  sistemarli o recuperare il documento.
- Se rendere robusto il test della porta chiusa sopra (es. accettare anche il
  timeout su Windows) o lasciarlo com'e' finche' non fallisce in CI.
