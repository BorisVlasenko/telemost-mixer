(() => {
  "use strict";

  const PREFIX = "tmixer_";

  function parseConfig() {
    const out = {
      name: "",
      camOff: true,
      delayMs: 0,
      channel: "",
    };
    try {
      const hash = location.hash.startsWith("#")
        ? location.hash.slice(1)
        : location.hash;
      const params = new URLSearchParams(hash);
      // Also accept query string (in case hash is stripped later).
      const q = new URLSearchParams(location.search);
      for (const src of [params, q]) {
        if (src.get(PREFIX + "name")) out.name = src.get(PREFIX + "name") || "";
        if (src.get(PREFIX + "cam") === "0") out.camOff = true;
        if (src.get(PREFIX + "cam") === "1") out.camOff = false;
        if (src.get(PREFIX + "delay")) {
          const n = parseInt(src.get(PREFIX + "delay") || "0", 10);
          if (!Number.isNaN(n)) out.delayMs = n;
        }
        if (src.get(PREFIX + "ch")) out.channel = src.get(PREFIX + "ch") || "";
      }
      // Persist early — React router may rewrite the URL.
      if (out.name || out.channel) {
        sessionStorage.setItem("tmixer_cfg", JSON.stringify(out));
      } else {
        const saved = sessionStorage.getItem("tmixer_cfg");
        if (saved) Object.assign(out, JSON.parse(saved));
      }
    } catch (_) {
      /* ignore */
    }
    return out;
  }

  const cfg = parseConfig();

  function applyLocalPrefs() {
    try {
      if (cfg.camOff) {
        localStorage.setItem("joinWithCameraOff", "true");
        localStorage.setItem("showMyVideo", "false");
      }
      // Mic stays available for PipeWire mixer; don't force joinWithMicrophoneOff.
    } catch (_) {
      /* ignore */
    }
  }

  applyLocalPrefs();

  function setReactInput(el, value) {
    const proto = el.tagName === "TEXTAREA"
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    // Lego TextInput sometimes listens to these:
    el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true }));
  }

  function findNameInput() {
    const nodes = document.querySelectorAll(
      'input[class*="displayNameInput"], input[class*="DisplayName"], textarea[class*="displayNameInput"]'
    );
    for (const el of nodes) {
      if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
        if (!el.readOnly && !el.disabled) return el;
      }
    }
    // Fallback: label text nearby
    const labels = document.querySelectorAll("div, span, label");
    for (const lab of labels) {
      const t = (lab.textContent || "").trim();
      if (t === "Ваше имя на встрече" || t === "Your name in the meeting") {
        const root = lab.parentElement;
        if (!root) continue;
        const input = root.querySelector("input, textarea");
        if (input instanceof HTMLInputElement || input instanceof HTMLTextAreaElement) {
          if (!input.readOnly && !input.disabled) return input;
        }
      }
    }
    return null;
  }

  function clickCameraOff() {
    // Prefer explicit "camera off" controls / aria.
    const candidates = [
      ...document.querySelectorAll("button, [role='button']"),
    ];
    for (const btn of candidates) {
      const aria = (btn.getAttribute("aria-label") || "").toLowerCase();
      const title = (btn.getAttribute("title") || "").toLowerCase();
      const text = (btn.textContent || "").trim().toLowerCase();
      const hay = `${aria} ${title} ${text}`;
      if (
        hay.includes("выключить камеру") ||
        hay.includes("turn off camera") ||
        hay.includes("camera off") ||
        (hay.includes("камера") && hay.includes("выкл"))
      ) {
        btn.click();
        return true;
      }
    }
    // Icon buttons: look for CameraOn state near preview — click first video toggle if pressed/on.
    for (const btn of candidates) {
      const pressed = btn.getAttribute("aria-pressed");
      const aria = (btn.getAttribute("aria-label") || "").toLowerCase();
      if (
        (aria.includes("камер") || aria.includes("camera") || aria.includes("video")) &&
        pressed === "true"
      ) {
        btn.click();
        return true;
      }
    }
    return false;
  }

  function retryMicIfBusy() {
    const bodyText = document.body ? document.body.innerText : "";
    const busy =
      /занят/i.test(bodyText) ||
      /in use/i.test(bodyText) ||
      /используется/i.test(bodyText) ||
      /busy/i.test(bodyText);
    if (!busy) return false;
    const candidates = [
      ...document.querySelectorAll("button, [role='button']"),
    ];
    for (const btn of candidates) {
      const aria = (btn.getAttribute("aria-label") || "").toLowerCase();
      const title = (btn.getAttribute("title") || "").toLowerCase();
      const hay = `${aria} ${title}`;
      if (
        hay.includes("микрофон") ||
        hay.includes("microphone") ||
        hay.includes("mic")
      ) {
        btn.click();
        setTimeout(() => btn.click(), 400); // off→on or retry
        return true;
      }
    }
    return false;
  }

  let nameFilled = false;
  let camDone = false;
  let micTried = false;

  function tick() {
    if (cfg.name && !nameFilled) {
      const input = findNameInput();
      if (input) {
        if ((input.value || "").trim() !== cfg.name) {
          setReactInput(input, cfg.name);
        }
        if ((input.value || "").trim() === cfg.name) nameFilled = true;
      }
    }
    if (cfg.camOff && !camDone) {
      if (clickCameraOff()) camDone = true;
    }
    if (!micTried) {
      if (retryMicIfBusy()) micTried = true;
    }
  }

  function startObservers() {
    const run = () => {
      tick();
      // Keep trying for a while — lobby UI mounts late.
      let n = 0;
      const id = setInterval(() => {
        tick();
        n += 1;
        if (n > 60 || (nameFilled && (camDone || !cfg.camOff) && micTried)) {
          clearInterval(id);
        }
      }, 500);
    };

    const kick = () => setTimeout(run, Math.max(0, cfg.delayMs));

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", kick, { once: true });
    } else {
      kick();
    }

    const obs = new MutationObserver(() => tick());
    const startObs = () => {
      if (document.documentElement) {
        obs.observe(document.documentElement, {
          childList: true,
          subtree: true,
        });
      }
    };
    if (document.documentElement) startObs();
    else document.addEventListener("DOMContentLoaded", startObs, { once: true });
  }

  startObservers();
})();
