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
