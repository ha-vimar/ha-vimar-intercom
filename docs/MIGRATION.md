# Coming from noiseheroes-lab/ha-vimar-intercom

🇮🇹 *[Italiano](MIGRATION.it.md)* · [← README](../README.md)

The original [noiseheroes-lab/ha-vimar-intercom](https://github.com/noiseheroes-lab/ha-vimar-intercom) is archived and points to this project. This page is for moving an existing install over.

## Same domain, so one at a time

Both integrations use the domain `vimar_intercom` and install into the same folder,
`custom_components/vimar_intercom`. They can't run side by side: installing this one over the old
folder replaces it. The old config entry doesn't carry over either: it is saved as version 2, this
integration only knows version 1 and has no migration for it, so the old entry stops at
*Migration error*. The way across is: remove the old one, install this one, add it again.

## Step by step

1. **Make a list** of where you use the old integration: automations, scripts, dashboards. Entity
   ids, services and events are different here (see [What changes](#what-changes)).
2. **Delete the old entry while the old code is still installed.** Settings → Devices & services →
   Vimar Intercom → ⋮ → *Delete*. Doing it in this order lets the old integration remove its own
   stored data (the plant phonebook and the call log under `.storage/`). If it also runs on another
   Home Assistant (a test one, say), stop it there too: one registration per account, and two
   clients knock each other off.
3. **Remove the old code.** In HACS open the old *Vimar Intercom* (the one from `noiseheroes-lab`),
   ⋮ → *Remove*, then in HACS → ⋮ → *Custom repositories* delete
   `https://github.com/noiseheroes-lab/ha-vimar-intercom` if it's listed there. Installed by hand?
   Delete `config/custom_components/vimar_intercom`.
4. **Restart Home Assistant.**
5. **Install this integration:** steps 1 and 2 of the [README](../README.md#installation) (HACS, then
   restart).
6. **Add it:** Settings → Devices & services → *Add integration* → *Vimar Intercom*. Use the same
   pairing QR code as before (same slot on the indoor unit, or the one the VIEW app shows): the old
   integration read a photo of it, this one wants its **text**, so read it with any QR scanner app
   and paste what it shows. That text holds the SIP password: use the phone's own camera or scanner,
   not an online decoder or an app that keeps a history.
7. **Get the phonebook:** *Configure* → *Download the phonebook from the intercom*, *Download the
   phonebook from the Vimar cloud* or *Import actuators from rubrica.db*. The old integration
   fetched it on its own; here it is one step in the options, and it sets the door, the actuators
   and where commands go.
8. **Refresh the frontend:** reload the browser tab or the app so the new card is picked up, then
   fix the automations and dashboards from your list.

## What changes

- **Entities:** Home Assistant sees them as new entities on a new device, so names, areas and
  icons you set on the old ones don't carry over. Entity ids may differ too (the camera, for one, was
  `camera.vimar_intercom_intercom` and is now `camera.intercom`): check yours against
  [Entities](ENTITIES.md). History only carries over where an id happens to come out the same.
- **Services:** the old `play_video_message`, `mark_video_message_read`, `delete_video_message`,
  `delete_all_video_messages` and `clear_missed_calls` have no equivalent here yet. This
  integration has `call`, `answer`, `decline`, `hangup`, `open_door` and a few more, listed in
  [Entities, services and automations](ENTITIES.md#services-servicesyaml).
- **Events:** the old `vimar_intercom_ring` bus event is gone. The doorbell is an `event` entity:
  trigger on its state, or on the ringing binary sensor. The old event said which panel rang; here
  that is the ringing sensor's `chiamante` attribute and the *Intercom Ultimo Chiamante* (last
  caller) sensor. `vimar_intercom_missed_call` exists but with a different payload, and the old
  `vimar_intercom_video_message` is now `vimar_intercom_videomessage`, also with a different
  payload. Details in [Events](ENTITIES.md#events).
- **Not here (yet):** the video-message list and playback, the missed-calls counter (the last-missed-call
  sensor has a `totale` attribute, counted since startup), the next/previous camera buttons and the reconnect button.
- **Dashboard card:** the type is still `custom:vimar-intercom-card` and it still loads by itself,
  with nothing to add under Resources. The options are different, though (`title`, `device_id`,
  `show_actuators` and `hidden_entities` are ignored), so the simplest fix is to delete the old card
  and add **Vimar Intercom** from the card picker. See [The intercom card](CARD.md).

## Already swapped the code?

If you installed this integration before deleting the old entry, the old one sits at *Migration
error*, and adding the new one stops at *Vimar Intercom is already set up*. Delete the old entry
from Settings → Devices & services and carry on from step 6. Two small files of the old
integration then stay in `config/.storage/`: `vimar_intercom.<old entry id>.plant` and
`vimar_intercom.<old entry id>.call_log`. Nothing reads them any more; delete them with Home
Assistant stopped if you like a tidy folder.
