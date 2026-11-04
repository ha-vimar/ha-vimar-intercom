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

## Non fatto e perche'

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
- Se rendere robusto il test della porta chiusa sopra (es. accettare anche il
  timeout su Windows) o lasciarlo com'e' finche' non fallisce in CI.
