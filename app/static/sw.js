/* Alfred — service worker.
 *
 * Exists for two reasons: iOS only allows Web Push to a PWA that was installed
 * to the home screen, and installation requires a service worker. Caching is a
 * secondary benefit and is kept deliberately shallow.
 *
 * The shell is cached; API responses never are. Alfred's answers depend on
 * live files, a live calendar, and a live mailbox, so a stale cached reply
 * would be worse than an honest failure.
 */
"use strict";

const SHELL_CACHE = "alfred-shell-v2";
const SHELL = ["/", "/assets/app.js", "/assets/hud.css", "/assets/hud.js", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  /* Cache each asset separately rather than with addAll.
   *
   * addAll is atomic: one failed request rejects the whole thing, the worker
   * never finishes installing, and every `navigator.serviceWorker.ready` in
   * every tab then hangs forever waiting for an activation that will never
   * come. Caching the shell is a convenience; it must never be able to take
   * the page down with it. */
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) =>
        Promise.all(
          SHELL.map((url) =>
            cache.add(url).catch((error) => {
              console.warn("Could not pre-cache", url, error);
            })
          )
        )
      )
      .catch((error) => console.warn("Shell cache unavailable:", error))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never serve a cached answer, status, or audio clip.
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok && SHELL.includes(url.pathname)) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/")))
  );
});

/* Phase 5 delivers the daily briefing through here. */
self.addEventListener("push", (event) => {
  let payload = { title: "Alfred", body: "You have a briefing waiting, sir." };
  try {
    if (event.data) payload = Object.assign(payload, event.data.json());
  } catch { /* keep the default */ }

  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/assets/icons/alfred-192.png",
      badge: "/assets/icons/alfred-192.png",
      tag: payload.tag || "alfred-briefing",
      data: { url: payload.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
