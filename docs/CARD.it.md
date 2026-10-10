# La card del citofono

🇬🇧 *[English](CARD.md)* · [← README](../README.it.md)

## Card del citofono (audio bidirezionale)

![La card del citofono: a riposo con la cronologia degli squilli, durante lo squillo e in chiamata](images/intercom-card.png)

*La card a riposo con la cronologia degli squilli, durante uno squillo (anteprima video prima di rispondere) e in chiamata. L'immagine della telecamera è una scena dimostrativa.*

**Layout** (immagini dimostrative): `overlay`, `sotto` e `popup` durante lo squillo, la cronologia degli squilli e l'editor visuale.

| `overlay` | `sotto` |
|---|---|
| ![layout overlay](images/card-overlay.png) | ![layout sotto](images/card-below.png) |

| `popup`: card compatta sopra, popup in diretta aperto | Cronologia | `compact_style: tile` |
|---|---|---|
| ![layout popup](images/card-popup.png) | ![cronologia squilli](images/card-history.png) | ![tile compatto](images/card-compact-tile.png) |

L'integrazione include una card per le dashboard e la carica da sola: non va aggiunta tra le
Risorse. Si sceglie **Citofono Vimar** dall'elenco delle card (telecamera, nome, layout e
cronologia hanno l'editor visuale; il resto resta in YAML) oppure si scrive a mano:

```yaml
type: custom:vimar-intercom-card
# facoltativi, questi sono i valori predefiniti:
name: Citofono
camera: camera.vimar_intercom_intercom
status: sensor.vimar_intercom_intercom_stato
lock: lock.vimar_intercom_serratura
last_ring: sensor.vimar_intercom_intercom_ultimo_squillo
anchor: citofono   # "" = disattivato
history: 8         # 0 = disattivato
layout: overlay    # oppure "sotto" o "popup"
compact_style: pillola   # solo layout "popup": la card compatta è una "pillola" o un "tile"
idle_picture: last_ring  # da fermo la scena e la foto della card compatta mostrano l'ultimo squillo;
                         # "standby" = l'icona del citofono (le foto restano in cronologia)
```

Aprire la card non chiama mai la targa. Il video dal vivo parte solo durante lo squillo o una
chiamata. Senza video la card è una riga sola: la foto dell'ultimo squillo, nome, stato, ora
dell'ultimo squillo e i tre pulsanti. Col video compare il riquadro 4:3; `layout` decide dove
stanno i pulsanti durante la diretta:

- `overlay` (predefinito): la card *è* il video. Stato in alto a sinistra, cronologia in alto a
  destra, pulsanti su una fascia scura in fondo all'immagine.
- `sotto`: il video sta sopra la riga, i pulsanti restano nella riga sotto; niente copre
  l'immagine.

Dopo il riaggancio l'ultima immagine resta 1,5 s con i pulsanti spenti, così un secondo tocco
non finisce su quello che risale quando la card si restringe. **Vedi esterno** chiama la targa
video; la visione dura quanto la concede la targa (dipende dall'impianto: circa 30 s su un 2FV2 con i pacchetti di silenzio che manda l'integrazione, 120 s su un 2F 40507), poi il video
finisce e la card si richiude. Per guardare di nuovo si ripreme **Vedi esterno**, come sul
monitor di casa. I pulsanti cambiano con lo stato:

| Stato | Pulsanti |
|---|---|
| a riposo | Vedi esterno, Parla, Apri |
| squillo | —, Rispondi (risponde e apre il microfono), Apri |
| in collegamento | Annulla, Microfono (solo per spegnerlo), Apri (l'etichetta conta i secondi; dopo 20 s avvisa che la targa non risponde) |
| in chiamata | Riaggancia, Microfono (acceso/spento), Apri |

Ogni pulsante resta al suo posto: chiamata a sinistra, voce al centro, porta a destra, così un
tocco non finisce su un pulsante appena cambiato. Se chiamata, risposta o apertura falliscono,
l'errore prende il posto della riga di stato per qualche secondo. **Apri** vuole due tocchi
entro 3 s. Durante lo squillo non c'è un pulsante per rifiutare: lo squillo finisce da solo, e
un tocco sbagliato manderebbe via chi ha suonato.

L'audio passa da `/api/vimar_intercom/audio_ws`, lo stesso canale dell'app iOS: PCM a 8 kHz nei
due sensi, con la cancellazione dell'eco del browser. Il browser concede il microfono solo in
**HTTPS** (o su localhost); in HTTP semplice il pulsante Parla è spento (il suggerimento dice
perché) e il resto funziona comunque.

**Video.** In HTTPS, sui browser con WebCodecs (Chrome, Edge, Firefox, Safari e iOS dalla
16.4), la card decodifica l'H.264 della targa dallo stesso WebSocket e lo disegna su un canvas:
il primo fotogramma compare circa 0,1 s dopo che la targa lo manda, senza aprire lo stream di
HA. Altrove (HTTP semplice, Safari vecchi) ripiega sullo stream della telecamera di HA, che
parte in 2-4 s.

**Link diretto.** Se l'URL della pagina finisce con `#citofono` (opzione `anchor`) la card si
porta in vista da sola, es. `/lovelace/camera#citofono` come azione al tocco di una notifica di
squillo.

**Ultimi squilli.** Con la cartella foto (`snapshot_dir`) impostata, la foto dell'ultimo squillo
sulla riga è il tasto della cronologia (in chiamata il tasto sta sul video): gli ultimi squilli
(opzione `history`, predefinito 8) con foto, ora ed esito: *Risposto* (risposto da HA), *Rifiutato* (rifiutato da HA),
*Aperto* (porta aperta da HA durante lo squillo),
*Messaggio di assenza*, *Risposto altrove* (risposto da un altro dispositivo: il monitor interno o
l'app Vimar, quando l'impianto lo comunica), *Nessuna risposta* (nessuna risposta, per quanto ne sa HA;
su un 40507, per esempio, uno squillo risposto sul Tab stesso finisce come uno senza risposta e compare qui, mentre
uno risposto dall'app Vimar compare come *Risposto altrove*). Un tocco sulla foto la apre in grande; uno squillo col clip ha il tasto
play sulla miniatura e il tocco fa partire il video al posto della foto. La foto compare circa
un secondo dopo lo squillo e dopo `snapshot_delay` la sostituisce quella migliore; il clip a
squillo (o chiamata) finiti. L'integrazione tiene l'elenco in `squillo.json` accanto ai file
(ultimi 200 squilli). Senza cartella non c'è cronologia. Per le notifiche, il sensore
«Intercom Ultimo Squillo» porta `foto` / `clip` (percorsi su disco) e `foto_url` / `clip_url`
(URL relativi, che l'app companion scarica col suo login) appena ogni file esiste.

