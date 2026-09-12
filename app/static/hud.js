/* Presentation only. Status and voice activity come from the existing client. */
"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  const clock = byId("hudClock");
  function updateClock() {
    const now = new Date();
    byId("hudMonth").textContent = now.toLocaleDateString("en", { month: "short" }).toUpperCase();
    byId("hudDay").textContent = String(now.getDate()).padStart(2, "0");
    byId("hudWeekday").textContent = now.toLocaleDateString("en", { weekday: "long" });
    clock.textContent = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
    clock.dateTime = now.toISOString();
  }
  updateClock();
  setInterval(updateClock, 1000 * 30);

  const orb = byId("orb");
  const labels = {
    idle: "Voice standby", listening: "Listening", thinking: "Thinking",
    speaking: "Speaking", denied: "Mic unavailable",
  };
  const updateMode = () => {
    byId("hudMode").textContent = labels[orb.dataset.mode] || "Voice standby";
  };
  new MutationObserver(updateMode).observe(orb, { attributes: true, attributeFilter: ["data-mode"] });
  updateMode();

  // HUD readouts mirror the live client; they do not invent system telemetry.
  const connection = byId("connPill");
  function updateConnection() {
    const dot = byId("connDot");
    byId("hudLink").textContent = dot.classList.contains("dot--live") ? "ON"
      : dot.classList.contains("dot--down") ? "OFF" : "—";
    byId("hudConnection").textContent = byId("connText").textContent;
  }
  new MutationObserver(updateConnection).observe(connection, {
    attributes: true, childList: true, subtree: true, characterData: true,
  });
  updateConnection();

  const log = byId("log");
  const updateTurns = () => {
    byId("hudTurns").textContent = String(log.querySelectorAll(".turn").length).padStart(2, "0");
  };
  new MutationObserver(updateTurns).observe(log, { childList: true });
  updateTurns();

  // Reuse the client's textarea sizing after a breakpoint or orientation change.
  window.addEventListener("resize", () => {
    byId("input").dispatchEvent(new Event("input"));
  });

  function setRailOpen(open) {
    byId("rail").dataset.open = String(open);
    byId("railToggle").setAttribute("aria-expanded", String(open));
  }
  document.querySelectorAll("[data-hud-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = byId(button.dataset.hudTarget);
      const inRail = byId("rail").contains(target);
      setRailOpen(inRail && window.matchMedia("(max-width: 900px)").matches);
      if (target.id === "pairBtn") {
        target.click();
        return;
      }
      if (!target.matches("button, input, textarea, [tabindex]")) target.tabIndex = -1;
      target.scrollIntoView({ block: "nearest" });
      target.focus({ preventScroll: true });
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && byId("rail").dataset.open === "true") {
      setRailOpen(false);
      byId("railToggle").focus();
    }
  });

  // Give keyboard users the same press / release control as pointer users.
  // Existing pointer handlers retain ownership of recording and playback.
  let keyboardHold = false;
  orb.addEventListener("keydown", (event) => {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    if (event.repeat || keyboardHold) return;
    keyboardHold = true;
    orb.dispatchEvent(new PointerEvent("pointerdown", { pointerId: 0, bubbles: true }));
  });
  function releaseKeyboard() {
    if (!keyboardHold) return;
    keyboardHold = false;
    orb.dispatchEvent(new PointerEvent("pointerup", { pointerId: 0, bubbles: true }));
  }
  orb.addEventListener("keyup", (event) => {
    if (event.key !== " " && event.key !== "Enter") return;
    event.preventDefault();
    releaseKeyboard();
  });
  orb.addEventListener("blur", releaseKeyboard);
  window.addEventListener("blur", releaseKeyboard);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) releaseKeyboard();
  });
})();
