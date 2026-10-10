// The card's CSS, moved out of vimar-intercom-card.js as is.

const CA = ':host([layout="popup"][compact]) ha-card:not(.pop)';
const CP = ':host([layout="popup"][compact="pillola"]) ha-card:not(.pop)';
const CT = ':host([layout="popup"][compact="tile"]) ha-card:not(.pop)';
const STYLE = `
  /* Misure e colori ritoccabili senza toccare il resto: chip sul video, tondi delle scorciatoie e della barra, pannello popup. */
  :host { display: block; scroll-margin-top: calc(var(--header-height, 56px) + 8px);
          /* Colori: quelli del tema di HA (variabili standard), i valori dopo la virgola sono il ripiego. Tutto il resto usa solo --vi-*. */
          --vi-primary: var(--primary-color, #0b5cad);
          --vi-ok: var(--success-color, #30b35f);
          --vi-bad: var(--error-color, #e5483d);
          --vi-warn: var(--warning-color, #ffb547);
          --vi-info: var(--info-color, #6cb2ff);
          --vi-line: var(--divider-color, rgba(127,127,127,.3));
          --vi-ink: var(--primary-text-color, #1b1b1f);
          --vi-icon: var(--state-icon-color, var(--secondary-text-color, #6f6a60));
          --vi-card: var(--ha-card-background, var(--card-background-color, #fff));
          --vi-radius: var(--ha-card-border-radius, 12px);
          /* Tinte derivate: pieno scurito (testo bianco leggibile), chiaro per il vetro scuro, squillo con testo scuro. */
          --vi-primary-solid: color-mix(in srgb, var(--vi-primary) 72%, #000);
          --vi-ok-solid: color-mix(in srgb, var(--vi-ok) 72%, #000);
          --vi-bad-solid: color-mix(in srgb, var(--vi-bad) 78%, #000);
          --vi-primary-lite: color-mix(in srgb, var(--vi-primary) 45%, #fff);
          --vi-ok-lite: color-mix(in srgb, var(--vi-ok) 45%, #fff);
          --vi-bad-lite: color-mix(in srgb, var(--vi-bad) 50%, #fff);
          --vi-warn-lite: color-mix(in srgb, var(--vi-warn) 55%, #fff);
          --vi-warn-bg: color-mix(in srgb, var(--vi-warn) 78%, #fff);
          --vi-on-warn: color-mix(in srgb, var(--vi-warn) 18%, #000);
          --vi-chip: rgba(0,0,0,.45); --vi-sc-size: 40px; --vi-bar-size: 64px; --vi-sc-pop-size: 48px;
          --vi-pop-bg: #111; --vi-pop-radius: 32px; --vi-backdrop: rgba(15,15,20,.5);
          --vi-glass: rgba(20,22,26,.5); --vi-glass-bar: rgba(20,22,26,.55); --vi-glass-btn: rgba(255,255,255,.18); }
  /* Senza backdrop-filter il vetro non sfoca: più opaco, così il testo resta leggibile. */
  @supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
    :host { --vi-glass: rgba(20,22,26,.72); --vi-glass-bar: rgba(20,22,26,.72); }
  }
  ha-card { position: relative; overflow: hidden; --st: var(--vi-primary); --ink: var(--primary-text-color, #1b1b1f);
            --dim: var(--secondary-text-color, #6f6a60); --fill: color-mix(in srgb, var(--ink) 7%, transparent); }
  [data-state="ringing"] { --st: var(--vi-warn); }
  [data-state="calling"] { --st: var(--vi-info); }
  [data-state="in_call"] { --st: var(--vi-ok); }
  [data-state="ringing"] { --dot: var(--vi-warn); }
  [data-state="calling"] { --dot: var(--vi-info); }
  [data-state="in_call"] { --dot: var(--vi-ok); }
  [data-state="offline"] { --st: var(--disabled-text-color, var(--dim)); }
  button { all: unset; box-sizing: border-box; position: relative; cursor: pointer; -webkit-tap-highlight-color: transparent; }
  button:disabled { cursor: default; }
  button:focus-visible { outline: 2px solid var(--vi-primary); outline-offset: 2px; }
  [hidden] { display: none !important; }
  @keyframes blink { 50% { opacity: .25; } }
  @keyframes pulse { 50% { transform: scale(1.5); opacity: .5; } }
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes halo { to { box-shadow: 0 0 0 14px transparent; } }

  /* Nome · ● stato · ultimo squillo su una riga; un avviso prende il posto di stato+ultimo per 4 s. */
  .name { font-size: 14px; font-weight: 600; line-height: 18px; color: var(--ink); white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; }
  .sub { display: flex; align-items: center; gap: 6px; min-width: 0; font-size: 12px; line-height: 16px;
         color: var(--dim); white-space: nowrap; }
  .sub > * { overflow: hidden; text-overflow: ellipsis; }
  .pill { display: inline-flex; align-items: center; gap: 5px; flex: none; color: var(--st); font-weight: 600; }
  .pill::before { content: ""; flex: none; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  [data-state="ringing"] .pill { padding: 0 8px 0 6px; border-radius: 999px; color: var(--text-primary-color); background: var(--st); }
  [data-state="ringing"] .pill::before { animation: pulse 1s ease-in-out infinite; }
  [data-state="calling"] .pill::before, [data-state="in_call"] .pill::before { animation: blink 2s infinite; }
  .last:not(:empty)::before { content: "· "; }
  .last:empty, .err:empty { display: none; }
  .err { color: var(--vi-warn); font-weight: 500; }
  .sub:has(.err:not(:empty)) > :not(.err) { display: none; }

  /* Foto dell'ultimo squillo = tasto cronologia (senza foto: campanello, non cliccabile). */
  #photo { flex: none; width: 56px; height: 44px; border-radius: 8px; overflow: hidden; display: grid; place-items: center;
           color: var(--st); background: color-mix(in srgb, var(--st) 14%, transparent); transition: box-shadow .15s; }
  #photo img { display: none; width: 100%; height: 100%; object-fit: cover; }
  #photo img[src] { display: block; }
  #photo img[src] + ha-icon { display: none; }
  #photo ha-icon { --mdc-icon-size: 26px; }
  #photo:focus-visible { outline-offset: 0; }
  /* Bollino "cronologia" sull'angolo della foto: dice che è un tasto anche da fermo. */
  #photo .hb { position: absolute; right: 3px; bottom: 3px; width: 18px; height: 18px; border-radius: 50%; display: grid;
               place-items: center; --mdc-icon-size: 13px; color: #fff; background: var(--vi-chip); }
  #photo:disabled .hb { opacity: .5; }
  [data-drawer="true"] #photo { box-shadow: inset 0 0 0 2px var(--vi-primary); }
  [data-state="ringing"] #photo { box-shadow: inset 0 0 0 2px var(--st); }

  /* Scena: video dal vivo o foto dell'ultimo squillo. Niente animazione di altezza né
     overflow nascosto animato: Safari/iOS non dipinge il <video> lì dentro. */
  #video > * { position: absolute; inset: 0; --ha-card-border-radius: 0; --ha-card-box-shadow: none; --ha-card-border-width: 0; }
  #video > canvas { width: 100%; height: 100%; object-fit: cover; }
  #video, .still, .ph { position: absolute; inset: 0; display: none; }
  .live #video { display: block; }
  .still { width: 100%; height: 100%; object-fit: cover; }
  ha-card:not(.live) .still[src] { display: block; }
  ha-card:not(.live) .still:not([src]) + .ph { display: grid; place-items: center; color: rgba(255,255,255,.28); }
  .ph ha-icon { --mdc-icon-size: 48px; }
  ha-card.live.wait .ph { display: grid; place-content: center; justify-items: center; gap: 8px; color: rgba(255,255,255,.6); }
  ha-card.live.wait .ph::after { content: "In attesa del video…"; font-size: 15px; }
  .badge { position: absolute; top: 10px; left: 10px; z-index: 2; display: none; align-items: center; gap: 6px;
           padding: 4px 10px 4px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 16px;
           color: #fff; background: rgba(0,0,0,.55); }
  .live .badge { display: inline-flex; }
  .badge::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--st); }
  [data-state="ringing"] .badge::before { animation: pulse 1s ease-in-out infinite; }
  [data-state="calling"] .badge::before { animation: blink 1.2s infinite; }
  .hold .row button { pointer-events: none; opacity: .5; }

  /* Cronologia: righe 52 px (thumb 56×42, ora / esito), separatore quando cambia giorno. */
  .hist { flex: 1; overflow-y: auto; overscroll-behavior: contain; -webkit-overflow-scrolling: touch; padding: 4px 6px; }
  .day { padding: 8px 8px 2px; font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--dim); }
  .ring { display: grid; grid-template-columns: 56px 1fr; gap: 10px; align-items: center; width: 100%; height: 52px;
          padding: 0 6px; border-radius: 10px; text-align: left; transition: background .15s; }
  .ring:not(:disabled):hover, .ring:not(:disabled):active { background: var(--fill); }
  .ring:focus-visible { outline-offset: -2px; }
  .th { position: relative; display: grid; place-items: center; width: 56px; height: 42px; border-radius: 6px; overflow: hidden;
        background: var(--fill); color: var(--dim); }
  .th img { width: 100%; height: 100%; object-fit: cover; }
  .th ha-icon { --mdc-icon-size: 20px; }
  /* Squillo con clip: tasto play sulla miniatura (anche senza foto). */
  .th .play { position: absolute; inset: 0; display: grid; place-items: center; color: #fff; background: rgba(0,0,0,.3); }
  .th .play ha-icon { --mdc-icon-size: 24px; }
  .ring:disabled .th { opacity: .6; }
  .txt { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
  .at { font-size: 13px; font-weight: 600; line-height: 1.2; }
  .out { display: flex; align-items: center; gap: 5px; font-size: 12px; line-height: 1.2; color: var(--dim);
         white-space: nowrap; overflow: hidden; }
  .out::before { content: ""; flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--oc, var(--dim)); }
  [data-outcome="answered"], [data-outcome="answered_elsewhere"],
  [data-outcome="opened"] { --oc: var(--vi-ok); }
  [data-outcome="declined"] { --oc: var(--vi-bad); }
  [data-outcome="away"] { --oc: var(--vi-info); }
  [data-outcome="missed"] { --oc: var(--vi-warn); }
  .empty { display: none; flex: 1; flex-direction: column; align-items: center; justify-content: center; gap: 6px;
           padding: 14px; text-align: center; font-size: 13px; color: var(--dim); }
  .empty ha-icon { --mdc-icon-size: 32px; opacity: .6; }
  .hist:empty + .empty { display: flex; }

  .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  button.busy ha-icon { display: none; }
  button.busy .ic::after { content: ""; width: 12px; height: 12px; border-radius: 50%; border: 2px solid currentColor;
                           border-top-color: transparent; animation: spin 1s linear infinite; }
  dialog.photo { padding: 0; border: 0; background: none; max-width: 95vw; color: #fff; text-align: center; }
  dialog.photo::backdrop { background: rgba(0,0,0,.8); }
  dialog.photo img, dialog.photo video { display: block; max-width: 95vw; max-height: 80vh; border-radius: 14px; }
  dialog.photo video { width: min(95vw, 640px); background: #000; }
  .cap { margin: 12px 0 0; font-size: 14px; font-weight: 500; }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }

  .drawer { position: absolute; top: 0; right: 0; bottom: 0; z-index: 1; width: min(64%, 320px); display: flex; flex-direction: column;
            color: var(--ink); background: color-mix(in srgb, var(--card-background-color, #fff) 94%, transparent);
            box-shadow: -6px 0 24px rgba(0,0,0,.25); transform: translateX(100%); visibility: hidden;
            transition: transform .28s cubic-bezier(.2,.8,.2,1), visibility .28s; }
  [data-drawer="true"] .drawer { transform: none; visibility: visible; }

  #log { position: absolute; top: 8px; right: 8px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: var(--vi-chip); }
  #log ha-icon { --mdc-icon-size: 22px; }
  .live #log { display: grid; }
  [data-drawer="true"] #log { background: var(--vi-primary); }

  /* Muto locale (audio in arrivo dalla targa): tondo sul video, accanto alla cronologia;
     visibile per tutta la diretta (in JS, hidden segue lo stato "live"), non solo mentre suona. */
  #mute { position: absolute; top: 8px; right: 56px; z-index: 3; width: 40px; height: 40px; border-radius: 50%;
          display: grid; place-items: center; color: #fff; background: var(--vi-chip); }
  #mute ha-icon { --mdc-icon-size: 22px; }

  /* Adatta/Riempi: tondo accanto a cronologia e muto, solo con il video in vista. */
  #fit { position: absolute; top: 8px; right: 104px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: var(--vi-chip); }
  #fit ha-icon { --mdc-icon-size: 22px; }
  .live #fit { display: grid; }
  ha-card[data-fit="contain"] #video > canvas, ha-card[data-fit="contain"] .still { object-fit: contain; }
  /* Video and box of the same shape: fill and fit draw the same picture (#42). */
  ha-card[data-fit-same] #fit { display: none !important; }

  /* Impostazioni: ingranaggio accanto alla cronologia (sul video e nella card compatta). */
  #cfg { position: absolute; top: 8px; right: 152px; z-index: 3; width: 40px; height: 40px; border-radius: 50%; display: none;
         place-items: center; color: #fff; background: var(--vi-chip); }
  #cfgc { display: none; }
  #cfg ha-icon { --mdc-icon-size: 22px; }
  .live #cfg { display: grid; }
  .row { display: flex; gap: 6px; min-width: 0; }
  /* Se la riga è stretta (anteprima dell'editor, ~330 px) si accorcia solo la pill più lunga
     ("Vedi es…"): "Parla" e "Apri" restano leggibili per intero. */
  #view, #hangup { order: 1; flex: 0 1 auto; } #talk { order: 2; } #open { order: 3; }
  .row button { display: inline-flex; flex: none; align-items: center; gap: 4px; height: 36px; padding: 0 9px; border-radius: 18px; min-width: 0;
                font-size: 12px; font-weight: 600; color: var(--vi-primary);
                background: color-mix(in srgb, var(--vi-primary) 12%, transparent); transition: transform .1s, background .2s; }
  .row button::before { content: ""; position: absolute; inset: -4px 0; }  /* bersaglio 44 px */
  .ic { display: grid; place-items: center; flex: none; width: 18px; height: 18px; }
  .ic ha-icon { --mdc-icon-size: 18px; }
  .row button:active { transform: scale(.96); }
  button.fill { background: var(--vi-primary); color: var(--text-primary-color); }
  button.ok { background: var(--vi-ok); color: var(--text-primary-color); }
  button.warn { background: var(--vi-warn); color: var(--text-primary-color); }
  button.bad { background: var(--vi-bad); color: var(--text-primary-color); }
  #hangup { background: var(--vi-bad); color: var(--text-primary-color); }
  .row button:disabled { opacity: .45; }
  button.answer { box-shadow: 0 0 0 0 color-mix(in srgb, var(--vi-ok) 55%, transparent); animation: halo 1.4s ease-out infinite; }

  .head { display: grid; grid-template-columns: 56px minmax(0, 1fr); grid-template-rows: 18px 36px; column-gap: 10px; row-gap: 4px;
          align-items: center; padding: 7px 10px 7px 12px; }
  #photo { grid-row: 1 / 3; }
  .ttl { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .name { flex: 0 1 auto; }
  .ttl .sub::before { content: "·"; flex: none; }

  ha-card { display: flex; flex-direction: column; }
  .media { display: none; position: relative; aspect-ratio: 4 / 3; min-height: 0; background: #0b0e12; color: #fff; }
  .live .media, [data-drawer="true"] .media { display: block; }
  .live #photo { display: none; }  /* in diretta la cronologia sta sul video */

  /* ---- layout="overlay" (vetro): in diretta il video riempie la card; pill di stato e cronologia in alto,
     barra di vetro flottante in basso con 4 posti uguali [audio][rispondi/parla][apri][rifiuta/riaggancia]. */
  :host([layout="overlay"]) ha-card.live { --ha-card-border-radius: 28px; }
  :host([layout="overlay"]) ha-card:not(.live) .ph { right: min(64%, 320px); }
  :host([layout="overlay"]) .live .head { position: absolute; inset: 0; z-index: 3; display: block; padding: 0; pointer-events: none; }
  :host([layout="overlay"]) .live :is(.ttl, #view) { display: none; }
  :host([layout="overlay"]) .live .badge { top: 12px; left: 12px; height: 36px; padding: 0 14px; gap: 8px; font-size: 14px;
               background: var(--vi-glass); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  :host([layout="overlay"]) .live .badge::before { width: 8px; height: 8px; }
  :host([layout="overlay"]) .live .badge::before, .pop .badge::before { animation: none; background: var(--dot, var(--st)); }
  :host([layout="overlay"]) .live :is(#log, #fit, #cfg) { top: 12px; right: 12px; width: 44px; height: 44px; background: var(--vi-glass);
               -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  :host([layout="overlay"]) .live #fit { right: 64px; }
  :host([layout="overlay"]) .live #cfg { right: 116px; }
  :host([layout="overlay"]) .live[data-drawer="true"] #log { background: var(--vi-primary); }
  :host([layout="overlay"]) .live .row { position: absolute; left: 12px; right: 12px; bottom: 12px; height: 84px; padding: 0; gap: 0;
               display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); align-items: center; pointer-events: none;
               border-radius: 22px; border: 1px solid var(--vi-glass-btn); background: var(--vi-glass-bar);
               -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px); }
  :host([layout="overlay"]) .live .row button { pointer-events: auto; display: flex; flex-direction: column; align-items: center;
               justify-content: center; gap: 6px; order: 0; min-height: 64px; height: auto; padding: 0; border-radius: 0; background: none;
               color: #fff; font-size: 12px; font-weight: 600; }
  :host([layout="overlay"]) .live .row button::before { inset: 0; }
  :host([layout="overlay"]) .live .row button:active { transform: none; opacity: .7; }
  :host([layout="overlay"]) .live #talk { grid-column: 2; }
  :host([layout="overlay"]) .live #open { grid-column: 3; }
  :host([layout="overlay"]) .live #hangup { order: 4; grid-column: 4; background: none; color: var(--vi-bad-lite); }
  :host([layout="overlay"]) .live button.ok, :host([layout="overlay"]) .live button.answer { background: none; color: var(--vi-ok-lite); }
  :host([layout="overlay"]) .live button.fill { background: none; color: var(--vi-primary-lite); }
  :host([layout="overlay"]) .live button.warn { background: none; color: var(--vi-warn-lite); }
  :host([layout="overlay"]) .live button.bad { background: none; color: var(--vi-bad-lite); }
  :host([layout="overlay"]) .live button.answer { animation: none; box-shadow: none; }
  :host([layout="overlay"]) .live .ic { width: 24px; height: 24px; background: none; }
  :host([layout="overlay"]) .live .ic ha-icon { --mdc-icon-size: 24px; }
  :host([layout="overlay"]) .live button.busy .ic::after { width: 20px; height: 20px; border-width: 3px; }
  /* Audio (#mute) sta nel primo posto della barra: stessa cella, etichetta da CSS. */
  :host([layout="overlay"]) .live #mute { top: auto; right: auto; bottom: 12px; left: 12px; z-index: 4; width: calc((100% - 24px) / 4);
               height: 84px; border-radius: 0; background: none; display: flex; flex-direction: column; align-items: center;
               justify-content: center; gap: 6px; font-size: 12px; font-weight: 600; }
  :host([layout="overlay"]) .live #mute::after { content: "Audio"; line-height: 16px; }
  :host([layout="overlay"]) .live #mute ha-icon { --mdc-icon-size: 24px; }
  /* Scorciatoie "Apri" extra: chip di vetro sopra la barra (la prima è "Apri", già nella barra). */
  :host([layout="overlay"]) .live .sc { display: flex; position: absolute; left: 12px; right: 12px; bottom: 108px; justify-content: center;
               gap: 8px; pointer-events: none; }
  :host([layout="overlay"]) .live .sc:not(:has(button:nth-child(2))), :host([layout="overlay"]) .live .sc button:first-child { display: none; }
  :host([layout="overlay"]) .live .drawer { top: 64px; right: 12px; bottom: 108px; border-radius: 16px; }
  :host([layout="overlay"]) ha-card.live:has(.sc button:nth-child(2)) .drawer { bottom: 156px; }

  /* ---- layout="sotto": in diretta il video sta sopra la riga, i tasti restano nella riga (a piena larghezza).
     Da fermo la cronologia è una lista sotto la riga, non un palco 4:3. */
  :host([layout="sotto"]) .live .ttl, :host([layout="sotto"]) .live .row { grid-column: 1 / 3; }
  :host([layout="sotto"]) .live .head { padding-top: 8px; }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] .media { order: 1; aspect-ratio: auto; background: none; color: var(--ink); }
  :host([layout="sotto"]) ha-card:not(.live)[data-drawer="true"] :is(.still, .ph) { display: none; }
  :host([layout="sotto"]) ha-card:not(.live) .drawer { position: static; width: auto; transform: none; visibility: visible; box-shadow: none;
               background: none; transition: none; border-top: 1px solid var(--vi-line); }
  :host([layout="sotto"]) ha-card:not(.live) .hist { max-height: 176px; }

  /* ---- layout="popup": compatta in dashboard (mai .live: niente video). Nel <dialog> la card intera, in un
     pannello tondo centrato (vetro sul video: margini 12 px, max 720): il video riempie il pannello, in alto
     pill di stato + cronologia + X, in basso il pannello di vetro con i 4 tondi [audio][parla|rispondi][apri][riaggancia]
     e sotto i chip delle scorciatoie. Su desktop il pannello è 4:3. */
  :host([layout="popup"]) ha-card:not(.pop) { cursor: pointer; }
  #x { display: none; position: absolute; top: 8px; right: 8px; z-index: 3; place-items: center; color: #fff; background: var(--vi-chip); }
  /* Scorciatoie: tondi sulla card compatta; nel popup solo le altre (la prima è "Apri"), in fila sotto la barra. */
  .sc, #hist, .bell { display: none; }  /* #hist e .bell: solo nella card compatta del layout popup */
  ha-card:not(.live):not(.pop):has(.sc button) .head { grid-template-columns: 56px minmax(0, 1fr) auto; }
  ha-card:not(.live):not(.pop) .sc { display: flex; gap: 4px; grid-column: 3; grid-row: 1 / 3; }
  ha-card:not(.live):not(.pop):has(.sc button) #open { display: none; }  /* da fermo "Apri" è la scorciatoia */
  .sc button { display: flex; flex-direction: column; align-items: center; gap: 2px; max-width: 60px; font-size: 11px; color: var(--ink); }
  .sc button::before { content: ""; position: absolute; inset: -2px -4px; }
  .sc .ic { width: var(--vi-sc-size); height: var(--vi-sc-size); border-radius: 50%; background: color-mix(in srgb, var(--vi-primary) 14%, transparent); color: var(--vi-primary); }
  .sc button.warn .ic { background: var(--vi-warn); color: var(--text-primary-color); }
  .sc button.ok .ic { background: var(--vi-ok); color: var(--text-primary-color); }
  .sc button:disabled { opacity: .45; }

  /* ---- layout="popup", card compatta in dashboard: due stili (compact_style). Stesso DOM: .head diventa la riga/griglia,
     .row e .sc "spariscono" (display: contents) e i loro tasti si dispongono con order. Il tocco fuori dai tasti apre il popup. */
  ${CA} { border-radius: var(--vi-radius); --fill: color-mix(in srgb, var(--ink) 12%, transparent);
          background: var(--vi-card);
          box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.18)); }
  ${CA} .media { display: none; }
  ${CA} :is(.row, .sc) { display: contents; }
  ${CA} .ttl { flex-direction: column; align-items: flex-start; gap: 0; min-width: 0; }
  ${CA} .ttl .sub::before { content: none; }
  ${CA} .pill { color: var(--dim); font-weight: 400; padding: 0; background: none; }
  ${CA} .pill::before, ${CA} #photo .hb { display: none; }
  ${CA} .name { max-width: 100%; }
  ${CA} :is(#view, #talk, #hangup, #open, #hist, .sc button) { flex: none; min-width: 0; max-width: none; animation: none; box-shadow: none; }
  ${CA} :is(#view, #talk, #hangup, #open, #hist, .sc button)::before { content: none; }
  ${CA} :is(#view, #talk, #hangup, #hist, .sc button) .ic { background: none; color: inherit; width: 22px; height: 22px; }
  ${CA} :is(#view, #talk, #hangup, #hist, .sc button) .ic ha-icon { --mdc-icon-size: 22px; }
  ${CA} :is(#open, #view, #talk, #hangup) { display: none; }
  ${CA}[data-state="ringing"] :is(#hist, #cfgc, .last) { display: none; }
  ${CA}[data-state="ringing"] #talk { display: flex; }
  ${CT}[data-state="ringing"] #hangup:not([hidden]) { display: flex; }

  /* Feedback al tocco (compatta): "Collegamento…" blu subito, tondi che girano / verde "Aperto" / rosso "Errore", :active visibile. */
  @keyframes ringpulse { 50% { box-shadow: 0 0 0 5px color-mix(in srgb, var(--vi-info) 45%, transparent); } }
  ${CA}[data-pending="true"] { --ha-card-background: color-mix(in srgb, var(--vi-info) 22%, var(--vi-card));
                background: color-mix(in srgb, var(--vi-info) 22%, var(--vi-card)); --ring: var(--vi-info); }
  ${CA}[data-pending="true"] #photo { animation: ringpulse 1.2s ease-in-out infinite; }
  ${CT}[data-pending="true"] .bell { color: color-mix(in srgb, var(--vi-info) 25%, #000); background: var(--vi-info); }
  ${CA} :is(#hist, #cfgc, .sc button) { transition: transform .1s, filter .1s; }
  ${CA} :is(#hist, #cfgc, .sc button, #view, #talk, #hangup):active { transform: scale(.94); filter: brightness(1.25) saturate(1.1); }
  ${CA}:active:not(:has(button:active)) { filter: brightness(.94); }
  ${CA} .sc button.busy { opacity: .85; }
  ${CA} .sc button.warn { animation: ringpulse 1s ease-in-out infinite; }
  ${CP} .sc button:first-child.bad { background: var(--vi-bad-solid); color: #fff; }
  ${CT} :is(#open, .sc button).bad { color: #fff; background: var(--vi-bad-solid); }

  /* Pillola: 64 px, raggio 32, foto tonda con anello di stato; a destra tondi 44 px. */
  ${CP} { --ha-card-border-radius: 32px; --ring: var(--vi-ok); }
  ${CP}[data-state="offline"] { --ring: var(--st); }
  ${CP} .head { display: flex; align-items: center; gap: 12px; height: 64px; padding: 0 8px; }
  ${CP} .bell { display: none; }
  ${CP} #photo { order: 1; width: 48px; height: 48px; border-radius: 50%; box-shadow: none; border: 2px solid var(--ring); box-sizing: border-box; }
  ${CP} #photo img { border-radius: 50%; }
  ${CP} .ttl { order: 2; flex: 1 1 0; }
  ${CP} .name { font-size: 15px; font-weight: 700; line-height: 20px; }
  ${CP} .sub { font-size: 12px; }
  ${CP} :is(#hist, #talk, .sc button:first-child) { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border-radius: 50%; }
  ${CP} :is(#hist, #cfgc) { order: 3; background: var(--fill); color: var(--vi-icon); }
  ${CP} #cfgc:not([hidden]), ${CP} #hist { flex: none; display: grid; place-items: center; width: 44px; height: 44px; padding: 0; border-radius: 50%; }
  ${CP} #cfgc ha-icon { --mdc-icon-size: 22px; }
  ${CP}[data-state="ringing"] #cfgc { display: none; }
  ${CT}[data-state="ringing"] #cfgc:not([hidden]) { display: none; }
  ${CP} .sc button:not(:first-child) { display: none; }
  ${CP} #talk { display: none; order: 4; background: var(--vi-ok-solid); color: #fff; }
  ${CP}[data-state="ringing"] #talk { display: grid; }
  ${CP} .sc button:first-child { order: 5; background: var(--vi-primary-solid); color: #fff; }
  ${CP} .sc button:first-child.warn { background: var(--vi-warn); color: var(--vi-on-warn); }
  ${CP} .sc button:first-child.ok { background: var(--vi-ok-solid); color: #fff; }
  ${CP} .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }  /* solo per i lettori di schermo */
  ${CP} :is(#hist, #talk, .sc button):disabled { opacity: .45; }
  ${CP}[data-state="ringing"] { --ha-card-background: var(--vi-warn-bg); background: var(--vi-warn-bg); --ink: var(--vi-on-warn); --dim: var(--vi-on-warn); --ring: #fff; }

  /* Tile: card 16, riga campanello + nome + miniatura, sotto griglia di tasti da 44. */
  ${CT} { --ha-card-border-radius: 16px; }
  ${CT} .head { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 8px; padding: 12px; }
  ${CT} .head::before { content: ""; order: 4; flex: 0 0 100%; height: 0; margin-top: -4px; }
  ${CT} .bell { order: 1; display: grid; place-items: center; flex: none; width: 40px; height: 40px; margin-right: 4px; border-radius: 50%;
                color: var(--vi-ok); background: color-mix(in srgb, var(--vi-ok) 18%, transparent); }
  ${CT} .bell ha-icon { --mdc-icon-size: 22px; }
  ${CT}[data-state="ringing"] .bell { color: var(--vi-on-warn); background: var(--vi-warn); }
  ${CT} .ttl { order: 2; flex: 1 1 0; }
  ${CT} .name { font-size: 15px; font-weight: 600; line-height: 20px; }
  ${CT}[data-state="ringing"] .name { font-weight: 700; }
  ${CT} .sub { font-size: 13px; line-height: 18px; }
  ${CT} #photo { order: 3; width: 48px; height: 36px; border-radius: 8px; }
  ${CT} :is(#view, #hist, #talk, #hangup, .sc button) { display: flex; flex-direction: row; align-items: center; justify-content: center; gap: 6px;
                height: 44px; flex: 1 1 calc(33.333% - 8px); padding: 0 8px; border-radius: 12px; font-size: 14px; font-weight: 600;
                color: var(--ink); background: var(--fill); }
  ${CT} #view { order: 5; display: flex; }
  ${CT} #view .lbl { font-size: 0; }  /* nel tile solo "Vedi": il nome accessibile resta "Vedi esterno" (aria-label) */
  ${CT} #view .lbl::after { content: "Vedi"; font-size: 14px; }
  ${CT}[data-state="ringing"] #view { display: none; }
  ${CT} :is(#open, .sc button) { order: 6; color: color-mix(in srgb, var(--vi-primary) 60%, var(--ink)); background: color-mix(in srgb, var(--vi-primary) 16%, transparent); }
  ${CT} :is(#open, .sc button).warn { color: var(--vi-on-warn); background: var(--vi-warn); }
  ${CT} :is(#open, .sc button).ok { color: #fff; background: var(--vi-ok-solid); }
  ${CT} #hist { order: 7; }
  ${CT} #cfgc:not([hidden]) { order: 2; display: grid; place-items: center; flex: none; width: 40px; height: 40px; border-radius: 50%; color: var(--vi-icon); background: var(--fill); }
  ${CT} #cfgc ha-icon { --mdc-icon-size: 22px; }
  ${CT} #talk { order: 5; color: #fff; background: var(--vi-ok-solid); font-weight: 700; }
  ${CT}:not([data-state="ringing"]) #talk { display: none; }
  ${CT} #hangup { order: 7; font-weight: 700; color: color-mix(in srgb, var(--vi-bad) 60%, var(--ink)); background: color-mix(in srgb, var(--vi-bad) 14%, transparent); }
  ${CT}[data-state="ringing"] { --ha-card-background: color-mix(in srgb, var(--vi-warn) 14%, var(--vi-card));
                background: color-mix(in srgb, var(--vi-warn) 14%, var(--vi-card)); }
  ${CT} :is(#view, #hist, #talk, #hangup, .sc button):disabled { opacity: .45; }

  /* Popup delle impostazioni: <dialog> nativo, nei colori del tema. */
  dialog.set { width: min(92vw, 420px); max-height: 90vh; margin: auto; padding: 0; border: 0; border-radius: 20px; overflow: auto; box-sizing: border-box;
               color: var(--vi-ink); background: var(--card-background-color, #fff); box-shadow: 0 20px 60px rgba(0,0,0,.35); }
  dialog.set::backdrop { background: rgba(15,15,20,.5); -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px); }
  .set-c { padding: 16px; }  /* il padding sta qui: cliccarlo non deve chiudere il dialog */
  .set-h { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .set-h h2 { flex: 1; margin: 0; font-size: 18px; font-weight: 600; }
  .set-x { display: grid; place-items: center; width: 44px; height: 44px; border-radius: 50%; color: var(--vi-ink);
           background: color-mix(in srgb, var(--vi-ink) 10%, transparent); }
  .set-r { display: flex; align-items: center; gap: 12px; min-height: 52px; padding: 6px 0; border-top: 1px solid var(--vi-line); }
  .set-r:first-of-type { border-top: 0; }
  .set-r.col { flex-direction: column; align-items: stretch; gap: 6px; }
  .set-l { flex: 1; min-width: 0; display: flex; flex-direction: column; font-size: 15px; font-weight: 500; }
  .set-l small { font-size: 12px; font-weight: 400; color: var(--secondary-text-color, #6f6a60); }
  .set-tg { flex: none; width: 52px; height: 32px; border-radius: 16px; background: color-mix(in srgb, var(--vi-ink) 25%, transparent); transition: background .15s; }
  .set-tg::before { content: ""; position: absolute; inset: -6px -4px; }
  .set-tg::after { content: ""; position: absolute; top: 4px; left: 4px; width: 24px; height: 24px; border-radius: 50%; background: #fff; transition: transform .15s; box-shadow: 0 1px 3px rgba(0,0,0,.3); }
  .set-tg[aria-checked="true"] { background: var(--vi-ok-solid); }
  .set-tg[aria-checked="true"]::after { transform: translateX(20px); }
  .set-tg:disabled { opacity: .45; }
  .set-in { box-sizing: border-box; width: 100%; min-height: 44px; padding: 8px 12px; font: inherit; font-size: 15px; border-radius: 10px;
            color: var(--vi-ink); background: color-mix(in srgb, var(--vi-ink) 8%, transparent); border: 1px solid var(--vi-line); }
  select.set-in { width: auto; max-width: 55%; }
  .set-r.col select.set-in { width: 100%; max-width: none; }
  .set-in:focus-visible { outline: 2px solid var(--vi-primary); outline-offset: 1px; }
  .set-up { display: flex; gap: 8px; }
  .set-r.col .set-up select.set-in { flex: 1; min-width: 0; }
  .set-ub { flex: none; min-height: 44px; padding: 0 16px; display: grid; place-items: center; border-radius: 10px; font-size: 15px; font-weight: 500;
            background: var(--vi-primary); color: var(--text-primary-color, #fff); }
  .set-ub:disabled { opacity: .45; }
  .set-e { min-height: 0; margin: 4px 0 0; font-size: 13px; color: var(--vi-bad); }
  .set-e:empty { display: none; }
  .pop .media { display: block; position: absolute; inset: 0; aspect-ratio: auto; background: #000; }
  .pop :is(#x, #log, #fit, #cfg) { display: grid; top: 14px; width: 44px; height: 44px; border-radius: 50%; background: var(--vi-glass);
                       -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop :is(#x, #log, #fit, #cfg, #mute) ha-icon { --mdc-icon-size: 22px; }
  .pop #x { right: 14px; } .pop #log { right: 66px; } .pop #fit { right: 118px; } .pop #cfg { right: 170px; }
  .pop[data-drawer="true"] #log { background: var(--vi-primary); }
  /* Pill di stato larga nella riga in alto (poi: ingranaggio, adatta/riempi, cronologia, X); se non ci sta si accorcia con i puntini, i tondi restano da 44. */
  .pop .badge { display: block; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; top: 14px; left: 14px; right: 224px; height: 44px; padding: 0 14px;
                font-size: 15px; font-weight: 700; line-height: 44px; background: var(--vi-glass); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop .badge::before { content: ""; display: inline-block; vertical-align: middle; margin: -2px 8px 0 0; width: 8px; height: 8px; border-radius: 50%; }
  .pop .ttl, .pop #photo, .pop #view { display: none; }
  /* Pannello di vetro in basso: riga dei tondi + chip delle scorciatoie (le altre: la prima è "Apri"). */
  .pop .head { position: absolute; left: 14px; right: 14px; bottom: 14px; z-index: 3; display: flex; flex-direction: column; align-items: stretch; gap: 12px;
               padding: 14px; border-radius: 26px; border: 1px solid var(--vi-glass-btn); background: var(--vi-glass-bar);
               -webkit-backdrop-filter: blur(18px); backdrop-filter: blur(18px); }
  .pop .row { display: grid; grid-template-columns: repeat(4, 1fr); justify-items: center; gap: 0; }
  .pop #talk { grid-column: 2; } .pop #open { grid-column: 3; } .pop #hangup { grid-column: 4; order: 4; }
  .pop .row button { order: 0; display: grid; place-items: center; width: 56px; height: 56px; padding: 0; border-radius: 50%; background: none; color: #fff; }
  .pop .row button::before { inset: -4px; }
  .pop .row button:active { transform: none; opacity: .7; }
  .pop .row .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }  /* solo per i lettori di schermo */
  .pop .ic { width: 56px; height: 56px; border-radius: 50%; background: var(--vi-glass-btn); color: #fff; }
  .pop .ic ha-icon { --mdc-icon-size: 24px; }
  .pop #talk .ic, .pop button.ok .ic { background: var(--vi-ok); }
  .pop #talk.fill .ic { background: var(--vi-primary); }
  .pop button.warn .ic { background: var(--vi-warn); }
  .pop button.bad .ic { background: var(--vi-bad); }
  .pop #hangup .ic { background: var(--vi-bad); }
  .pop button.answer { animation: none; box-shadow: none; }
  .pop button.answer .ic { box-shadow: 0 0 0 0 color-mix(in srgb, var(--vi-ok) 55%, transparent); animation: halo 1.4s ease-out infinite; }
  .pop button.busy .ic::after { width: 24px; height: 24px; border-width: 3px; }
  /* Audio (#mute) nel primo posto dei tondi: allineato alla griglia a 4 colonne del pannello. */
  .pop #mute { top: auto; right: auto; bottom: 29px; left: calc((100% - 58px) / 8 + 1px); z-index: 4; width: 56px; height: 56px; border-radius: 50%;
               background: var(--vi-glass-btn); }
  .pop:has(.sc button:nth-child(2)) #mute { bottom: 77px; }
  .pop .sc { display: flex; flex-wrap: nowrap; justify-content: center; gap: 8px; }
  .pop .sc:not(:has(button:nth-child(2))), .pop .sc button:first-child { display: none; }
  .pop .drawer { top: 70px; right: 14px; bottom: 130px; border-radius: 20px; }
  .pop:has(.sc button:nth-child(2)) .drawer { bottom: 178px; }
  /* Chip di vetro (popup e overlay). Tocco 44 px con ::before. */
  .pop .sc button, :host([layout="overlay"]) .live .sc button { flex-direction: row; gap: 6px; height: 36px; max-width: none; padding: 0 14px;
               border-radius: 18px; font-size: 14px; color: #fff; background: var(--vi-glass-btn); pointer-events: auto;
               -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); }
  .pop .sc button::before, :host([layout="overlay"]) .live .sc button::before { inset: -4px 0; }
  .pop .sc .ic, :host([layout="overlay"]) .live .sc .ic { width: 18px; height: 18px; background: none; color: #fff; }
  .pop .sc .ic ha-icon, :host([layout="overlay"]) .live .sc .ic ha-icon { --mdc-icon-size: 18px; }
  /* Il focus da tastiera resta visibile sul vetro. */
  .pop button:focus-visible, :host([layout="overlay"]) .live button:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  dialog.pop { width: calc(100% - 24px); max-width: 720px; height: min(620px, calc(100% - 24px)); max-height: calc(100% - 24px); margin: auto;
               padding: 0; border: 0; border-radius: var(--vi-pop-radius); overflow: hidden; color: #fff; background: var(--vi-pop-bg);
               box-shadow: 0 20px 60px rgba(0,0,0,.45); color-scheme: dark;
               /* Scuro fisso, qualunque tema: le variabili del tema chiaro non passano qui dentro. */
               --primary-text-color: #fff; --secondary-text-color: #c4c4c4; --card-background-color: var(--vi-pop-bg); --ha-card-background: var(--vi-pop-bg); }
  dialog.pop, .hist { overscroll-behavior: contain; }
  dialog.pop::backdrop { background: var(--vi-backdrop); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); }
  dialog.pop ha-card { height: 100%; background: none; --ha-card-border-radius: 0; --ha-card-box-shadow: none; --ha-card-border-width: 0; }
  /* Schermo largo (PC, tablet, telefono in orizzontale): il pannello è il video 4:3, a tutta larghezza (al massimo 900 px e 90% dell'altezza),
     con riga in alto e barra dei tasti di vetro SUL video: niente vuoto sotto. Il telefono in verticale resta il pannello alto. */
  @media (min-width: 700px), (min-aspect-ratio: 1 / 1) {
    dialog.pop { width: min(900px, calc(100% - 24px), calc((100vh - 24px) * 4 / 3)); max-width: none; height: fit-content; }  /* non auto: un <dialog> modale si allungherebbe a tutta altezza */
    dialog.pop ha-card { height: auto; }
    .pop .media { position: relative; inset: auto; aspect-ratio: 4 / 3; }
  }
`;

export { STYLE };
