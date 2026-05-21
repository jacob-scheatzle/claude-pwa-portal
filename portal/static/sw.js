// Portal service worker. Minimal for now: take control fast and pass through
// the network. Richer caching strategies (and child-app SW scopes) come later.

const CACHE = "portal-shell-v2";
const PRECACHE = [
	"/manifest.webmanifest",
	"/static/icons/icon-192.png",
	"/static/icons/icon-512.png",
	"/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
	event.waitUntil(
		caches.open(CACHE).then((c) => c.addAll(PRECACHE).catch(() => {}))
	);
	self.skipWaiting();
});

self.addEventListener("activate", (event) => {
	event.waitUntil(
		Promise.all([
			caches.keys().then((keys) =>
				Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
			),
			self.clients.claim(),
		])
	);
});

self.addEventListener("fetch", (event) => {
	if (event.request.method !== "GET") return;
	let url;
	try {
		url = new URL(event.request.url);
	} catch (_) {
		return;
	}
	if (!url.pathname.startsWith("/static/")) return;
	event.respondWith(
		fetch(event.request)
			.then((res) => {
				if (res && res.ok) {
					const copy = res.clone();
					caches.open(CACHE).then((c) => c.put(event.request, copy)).catch(() => {});
				}
				return res;
			})
			.catch(() => caches.match(event.request))
	);
});
