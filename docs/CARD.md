# The intercom card

🇮🇹 *[Italiano](CARD.it.md)* · [← README](../README.md)

## Intercom card (two-way audio)

![The intercom card: at rest with the ring history, while the doorbell rings, and in a call](images/intercom-card.png)

*The card at rest with the ring history, during a ring (video preview before answering) and in a call. The camera picture is a demo scene.*

**Layouts** (all with demo pictures): `overlay`, `sotto` and `popup` while the doorbell rings, the ring history, and the visual editor.

| `overlay` | `sotto` |
|---|---|
| ![overlay layout](images/card-overlay.png) | ![sotto layout](images/card-below.png) |

| `popup`: compact card above, live popup open | History drawer | `compact_style: tile` |
|---|---|---|
| ![popup layout](images/card-popup.png) | ![ring history](images/card-history.png) | ![compact tile](images/card-compact-tile.png) |

The integration ships a dashboard card and loads it itself, so there is nothing to add under
Resources. Pick **Citofono Vimar** in the card picker (camera, name, layout and history have a
visual editor; the rest stays in YAML) or add it by hand:

```yaml
type: custom:vimar-intercom-card
# optional, these are the defaults:
name: Citofono
camera: camera.vimar_intercom_intercom
status: sensor.vimar_intercom_intercom_stato
lock: lock.vimar_intercom_serratura
last_ring: sensor.vimar_intercom_intercom_ultimo_squillo
anchor: citofono   # "" = off
history: 8         # 0 = off
layout: overlay    # or "sotto" or "popup"
compact_style: pillola   # "popup" layout only: the compact card is a "pillola" (pill) or a "tile"
idle_picture: last_ring  # at rest the scene and the compact card's photo show the last ring;
                         # "standby" = the doorbell icon instead (photos stay in the history)
```

Opening the card never calls the panel. The live video starts only while the doorbell rings or
during a call. Without video the card is one row: the photo of the last ring, name, state, time
of the last ring and the three buttons. With video the card shows a 4:3 pane; `layout` decides
where the buttons go while it is live:

- `overlay` (default): the card *is* the video. State top left, history top right, buttons on
  a dark strip at the bottom of the picture.
- `sotto`: the video sits above the row, the buttons stay in the row under it; nothing covers
  the picture.

After a hang-up the last picture stays 1.5 s with the buttons off, so a second tap does not land
on whatever moves in below when the card shrinks. **Vedi esterno** calls the video panel; the
view lasts as long as the panel allows (it depends on the plant: about 30 s on a 2FV2 with the silence frames the integration sends, 120 s on a 2F 40507), then the video ends
and the card closes. To look again, press **Vedi esterno** again, as on the in-home monitor.
The buttons change with the state:

| State | Buttons |
|---|---|
| idle | Vedi esterno, Parla, Apri |
| ringing | —, Rispondi (answers and opens the microphone), Apri |
| calling | Annulla, Microfono (only to turn it off), Apri (the pill counts the seconds; after 20 s it says the panel is not answering) |
| in call | Riaggancia, Microfono (on/off), Apri |

Each button keeps its place: call on the left, voice in the middle, door on the right, so a tap
never lands on a button that just changed. A failed call, answer or door opening shows its error
in place of the state line for a few seconds. **Apri** needs two taps within 3 s. There is no
reject button during the ring: the ring stops on its own, and a stray tap would send the visitor
away.

Audio goes over `/api/vimar_intercom/audio_ws`, the same channel the iOS app uses: 8 kHz PCM
both ways, with the browser's echo cancellation. The browser only allows the microphone over
**HTTPS** (or on localhost); over plain HTTP the Parla button is off (its tooltip says why) and
the rest still works. Labels are in Italian.

**Video.** Over HTTPS, on browsers with WebCodecs (Chrome, Edge, Firefox, Safari and iOS from
16.4), the card decodes the panel's H.264 from the same WebSocket and paints it on a canvas:
the first frame shows about 0.1 s after the panel sends it, and HA's stream is not opened.
Anywhere else (plain HTTP, older Safari) it falls back to HA's own camera stream, which takes
2-4 s to start.

**Deep link.** When the page URL ends with `#citofono` (option `anchor`) the card scrolls itself
into view, e.g. `/lovelace/camera#citofono` as the tap action of a ring notification.

**Last rings.** With a snapshot folder (`snapshot_dir`) set, the photo of the last ring on the
row is the history button (during a call the button is on the video): the latest rings
(option `history`, default 8) with photo, time and outcome: *Risposto* (answered from HA), *Rifiutato* (declined from HA),
*Aperto* (the door opened from HA during the ring),
*Messaggio di assenza* (away message), *Risposto altrove* (answered on another device: the indoor
monitor or the Vimar app, when the plant says so), *Nessuna risposta* (not answered, as far as HA knows;
on a 40507, for example, a ring answered on the Tab itself ends like an unanswered one and shows here, while one
answered in the Vimar app shows as *Risposto altrove*). Tap a photo to see it large; a ring with a clip shows a play
icon on its thumbnail and the tap plays the video instead. The photo appears about a second
after the ring and is replaced by a better one after `snapshot_delay`; the clip when the ring
(or the call) ends. The integration keeps the list in `squillo.json` next to the files (last 200
rings). Without the folder there is no history. For notifications, the "Intercom Ultimo Squillo"
sensor carries `foto` / `clip` (paths on disk) and `foto_url` / `clip_url` (relative URLs the
companion app fetches with its own login) as soon as each file exists.

