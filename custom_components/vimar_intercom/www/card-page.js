// The card and its page: background, page left, detached and reattached. A mixin like
// card-audio.js; the card calls super in connectedCallback and _teardown.

// App in the background (iPhone/iPad) or page left: no video, listening or microphone. On a desktop a hidden
// tab is not "gone": switching tabs must not stop the live view.
const PAGE_EVENTS = ["visibilitychange", "pagehide", "pageshow"];
const TOUCH = matchMedia("(pointer: coarse)").matches
  || (navigator.maxTouchPoints > 1 && /Macintosh/.test(navigator.userAgent));  // an iPad with a trackpad too
const pageGone = (e) => e?.type === "pagehide" || (TOUCH && document.visibilityState === "hidden");
const DETACH_MS = 1000;  // out of the page this long: teardown

const CardPage = (Base) => class extends Base {
  _hidden = pageGone();

  // App in the background or page left: microphone, listening and video close at once (iOS keeps an
  // AudioContext and the microphone alive in the background: the "activity" that stayed). The call does not:
  // the panel or the user ends it. Back (pageshow: the back-forward cache too) the video restarts by itself,
  // the microphone only on a tap.
  _onPageState = (e) => {
    const hidden = pageGone(e);
    if (hidden === this._hidden) return;
    this._hidden = hidden;
    if (hidden) {
      this._stopAudio();
      this._stopListen();
    }
    if (this._root && this._hass) this._render();  // _setVideo closes or reopens the player
  };

  connectedCallback() {
    for (const e of PAGE_EVENTS) window.addEventListener(e, this._onPageState);
    this._hidden = pageGone();
    clearTimeout(this._detachTimer);  // removed and put back at once (rotation, relayout): no teardown
    // A modal dialog taken out of the document leaves the top layer but stays `open`: back in the page
    // it would be an inline box with no backdrop and no Esc. Modal again, without a close event (the
    // popup's close hangs up a call the card answered).
    for (const d of this._root?.querySelectorAll("dialog[open]") ?? []) {
      if (!d.matches(":modal")) {
        d.removeAttribute("open");
        d.showModal();
      }
    }
  }

  // Home Assistant detaches and reattaches cards on rotation and relayout: back within DETACH_MS, the
  // live view goes on (closing the popup would also hang up a call the card answered). Otherwise a full
  // teardown.
  disconnectedCallback() {
    clearTimeout(this._detachTimer);
    this._detachTimer = setTimeout(() => this.isConnected || this._teardown(), DETACH_MS);
  }

  _teardown() {
    for (const e of PAGE_EVENTS) window.removeEventListener(e, this._onPageState);
  }
};

export { CardPage, DETACH_MS };
