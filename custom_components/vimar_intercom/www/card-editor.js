// The visual config editor, moved out of vimar-intercom-card.js as is.

import { setLang, t } from "./card-i18n.js";

const DEFAULTS = {
  camera: "camera.vimar_intercom_intercom",
  status: "sensor.vimar_intercom_intercom_stato",
  lock: "lock.vimar_intercom_serratura",
  last_ring: "sensor.vimar_intercom_intercom_ultimo_squillo",
  anchor: "citofono",
  history: 8,
  layout: "overlay",  // o "sotto" o "popup"
  compact_style: "pillola",  // card compatta del layout popup: o "tile"
  idle_picture: "last_ring",  // o "standby": da fermo niente foto dell'ultimo squillo
  confirm_open: true,
  listen_on_ring: false,
};

// Editor visuale: un ha-form di HA con cinque campi. Le altre chiavi (status, lock, last_ring,
// anchor) restano in YAML e passano intatte. Un campo svuotato o lasciato al default non finisce in YAML.
const EDITOR_TAG = "vimar-intercom-card-editor";
// Option labels in the user's language: ed_<field>_<value> (compact_style: ed_compact_<value>).
const opts = (key, values) => ({ select: { mode: "dropdown", options: values.map((value) => ({ value, label: t(`ed_${key}_${value}`) })) } });
const schema = () => [
  { name: "camera", selector: { entity: { domain: "camera" } } },
  { name: "name", selector: { text: {} } },
  { name: "layout", selector: opts("layout", ["overlay", "sotto", "popup"]) },
  { name: "compact_style", selector: opts("compact", ["pillola", "tile"]) },
  { name: "idle_picture", selector: opts("idle", ["last_ring", "standby"]) },
  { name: "shortcuts", selector: { entity: { multiple: true, domain: ["lock", "button"] } } },
  { name: "history", selector: { number: { min: 0, max: 50, mode: "box" } } },
  { name: "confirm_open", selector: { boolean: {} } },
  { name: "listen_on_ring", selector: { boolean: {} } },
];

class VimarIntercomCardEditor extends HTMLElement {
  setConfig(config) {
    this._cfg = config;
    this._sync();
  }

  set hass(hass) {
    this._hass = hass;
    setLang(hass);
    this._sync();
  }

  _sync() {
    if (!this._hass || !this._cfg) return;  // il selettore entità vuole hass
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = this._schema = schema();
      this._form.computeLabel = (s) => t(`ed_${s.name}`);
      this._form.addEventListener("value-changed", (e) => {
        e.stopPropagation();
        const config = { ...this._cfg };
        for (const { name } of this._schema) {
          const v = e.detail.value[name];
          if (v === undefined || v === "" || v === DEFAULTS[name] || v?.length === 0) delete config[name]; else config[name] = v;
        }
        this._cfg = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.append(this._form);
    }
    this._form.hass = this._hass;
    this._form.data = { ...DEFAULTS, ...this._cfg };
  }
}

export { DEFAULTS, EDITOR_TAG, VimarIntercomCardEditor };
