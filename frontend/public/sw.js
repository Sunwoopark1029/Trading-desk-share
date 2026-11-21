// Trading Desk service worker (phase 5). Makes the dashboard installable on the phone.
// It caches NOTHING: every request goes to the home PC as usual, so no market or account data is stored.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
