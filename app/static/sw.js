// Retire the password-era shell and any cached board data on the next worker update.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("dope-shell-")) await caches.delete(key);
    }
    await self.registration.unregister();
  })());
});
