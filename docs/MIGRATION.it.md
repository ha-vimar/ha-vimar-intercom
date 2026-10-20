# Arrivi da noiseheroes-lab/ha-vimar-intercom

🇬🇧 *[English](MIGRATION.md)* · [← README](../README.it.md)

L'integrazione originale [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom) è archiviata e rimanda a questo progetto. Questa pagina serve a spostare un'installazione esistente.

## Stesso dominio, quindi una alla volta

Le due integrazioni usano lo stesso dominio, `vimar_intercom`, e si installano nella stessa cartella,
`custom_components/vimar_intercom`. Non possono convivere: installare questa sopra la vecchia
cartella la sostituisce. Anche la voce di configurazione non passa: la vecchia è salvata alla
versione 2, questa integrazione conosce solo la versione 1 e non ha una migrazione, quindi la
vecchia voce si ferma su *Errore di migrazione*. La strada è: togli la vecchia, installa questa,
aggiungila di nuovo.

## Passo per passo

1. **Fai una lista** di dove usi la vecchia integrazione: automazioni, script, dashboard. Gli entity
   id, i servizi e gli eventi qui sono diversi (vedi [Cosa cambia](#cosa-cambia)).
2. **Elimina la vecchia voce mentre il vecchio codice è ancora installato.** Impostazioni →
   Dispositivi e servizi → Vimar Intercom → ⋮ → *Elimina*. In questo ordine la vecchia integrazione
   cancella da sola i propri dati salvati (la rubrica dell'impianto e il registro chiamate in
   `.storage/`). Se gira anche su un altro Home Assistant (uno di prova, per dire), fermala anche
   lì: una sola registrazione per account, e due client si buttano fuori a vicenda.
3. **Togli il vecchio codice.** In HACS apri il vecchio *Vimar Intercom* (quello di `noiseheroes-lab`),
   ⋮ → *Rimuovi*, poi in HACS → ⋮ → *Repository personalizzati* elimina
   `https://github.com/noiseheroes-lab/ha-vimar-intercom` se c'è. Installata a mano? Cancella
   `config/custom_components/vimar_intercom`.
4. **Riavvia Home Assistant.**
5. **Installa questa integrazione:** i passi 1 e 2 del [README](../README.it.md#installazione) (HACS,
   poi riavvio).
6. **Aggiungila:** Impostazioni → Dispositivi e servizi → *Aggiungi integrazione* → *Vimar Intercom*.
   Usa lo stesso QR di abbinamento di prima (lo stesso slot del posto interno, o quello che mostra
   l'app VIEW): la vecchia integrazione ne leggeva una foto, questa vuole il **testo**, quindi
   leggilo con una qualsiasi app per QR e incolla quello che mostra. Quel testo contiene la password
   SIP: usa la fotocamera o lo scanner del telefono, non un decoder online o un'app che tiene la
   cronologia.
7. **Scarica la rubrica:** *Configura* → *Scarica la rubrica dal citofono*, *Scarica la rubrica dal
   cloud Vimar* o *Importa attuatori da rubrica.db*. La vecchia integrazione la prendeva da sola; qui
   è un passo nelle opzioni, e imposta la porta, gli attuatori e dove vanno i comandi.
8. **Aggiorna il frontend:** ricarica la scheda del browser o l'app perché prenda la nuova card, poi
   sistema automazioni e dashboard della tua lista.

## Cosa cambia

- **Entità:** per Home Assistant sono entità nuove su un dispositivo nuovo, quindi nomi, aree e
  icone che avevi impostato sulle vecchie non passano. Anche gli entity id possono cambiare (la
  telecamera, per dirne una, era `camera.vimar_intercom_intercom` e ora è `camera.intercom`):
  controlla i tuoi in [Entità](ENTITIES.it.md). La cronologia passa solo dove un id viene uguale per caso.
- **Servizi:** i vecchi `play_video_message`, `mark_video_message_read`, `delete_video_message`,
  `delete_all_video_messages` e `clear_missed_calls` qui non hanno ancora un equivalente. Questa
  integrazione ha `call`, `answer`, `decline`, `hangup`, `open_door` e qualche altro, elencati in
  [Entità, servizi e automazioni](ENTITIES.it.md#servizi-servicesyaml).
- **Eventi:** il vecchio evento sul bus `vimar_intercom_ring` non c'è più. Il campanello è
  un'entità `event`: usa come trigger il suo stato, o il sensore binario dello squillo. Il vecchio
  evento diceva quale targa suonava; qui c'è l'attributo `chiamante` del sensore dello squillo e il
  sensore *Ultimo chiamante* (`sensor.vimar_intercom_intercom_ultimo_chiamante`). `vimar_intercom_missed_call` esiste ma con un payload
  diverso, e il vecchio
  `vimar_intercom_video_message` ora è `vimar_intercom_videomessage`, anche lui con un payload
  diverso. Dettagli in [Eventi](ENTITIES.it.md#eventi).
- **Non ci sono (ancora):** l'elenco e la riproduzione dei videomessaggi, il contatore delle chiamate
  perse (il sensore dell'ultima chiamata persa ha un attributo `totale`, contato dall'avvio), i pulsanti telecamera successiva/precedente
  e il pulsante di riconnessione.
- **Card della dashboard:** il tipo resta `custom:vimar-intercom-card` e si carica ancora da sola,
  senza niente da aggiungere in Risorse. Le opzioni però sono diverse (`title`, `device_id`,
  `show_actuators` e `hidden_entities` vengono ignorate), quindi la cosa più semplice è eliminare la
  vecchia card e aggiungere **Citofono Vimar** dal selettore delle card. Vedi
  [La card del citofono](CARD.it.md).

## Hai già cambiato il codice?

Se hai installato questa integrazione prima di eliminare la vecchia voce, quella vecchia resta su
*Errore di migrazione*, e aggiungere la nuova si ferma su *C'è già un'installazione di Vimar
Intercom*. Elimina la vecchia voce da Impostazioni → Dispositivi e servizi e riparti dal passo 6. In
`config/.storage/` restano allora due file piccoli della vecchia integrazione:
`vimar_intercom.<id vecchia voce>.plant` e `vimar_intercom.<id vecchia voce>.call_log`. Nessuno li
legge più; cancellali a Home Assistant fermo se ti piace la cartella in ordine.
