// Portal SDK v0.2
// Child apps include this via <script src="/portal-sdk.js"></script>.
// It exposes window.portal with auth-aware shortcuts to portal services.
//
// Two origin contexts the SDK handles:
//
//   1. App subdomain (default, post-Phase D): the page is loaded from
//      "<slug>.apps.<SITE_URL>/" inside an iframe wrapper. The portal
//      hands off a single-use launch token in the URL fragment
//      (#token=...). On boot the SDK strips it from the URL, POSTs it
//      to /api/v1/session/exchange on the same origin, and that mints
//      an HttpOnly app_session cookie scoped to this subdomain. Every
//      subsequent fetch is same-origin to the subdomain.
//
//   2. Portal origin (legacy fallback, CHILD_APPS_SAME_ORIGIN=true): the
//      page is served at "<SITE_URL>/apps/<slug>/...". The portal's own
//      UserSession cookie authenticates calls directly; no exchange.

(function () {
	"use strict";

	// "On a child-app subdomain?" — crude but stable: any host that has
	// ".apps." in it is one of ours. Production hosts look like
	// "myslug.apps.example.com"; dev hosts look like "myslug.apps.lvh.me:8000".
	// The portal origin itself is "example.com" / "lvh.me" — no ".apps." segment.
	var hostname = window.location.hostname;
	var onSubdomain = hostname.indexOf(".apps.") !== -1;

	// Slug derivation:
	//   - On a subdomain: read "<slug>.apps...": everything left of ".apps." is the slug.
	//   - On portal origin: read "/apps/<slug>/..." from the path (legacy).
	var appSlug = null;
	if (onSubdomain) {
		var idx = hostname.indexOf(".apps.");
		appSlug = idx > 0 ? hostname.slice(0, idx) : null;
	} else {
		var pathMatch = window.location.pathname.match(/^\/apps\/([^\/]+)/);
		appSlug = pathMatch ? pathMatch[1] : null;
	}

	function requireAppSlug() {
		if (!appSlug) throw new Error(
			"Portal SDK: app slug could not be determined from " +
			(onSubdomain ? "hostname " + hostname : "path " + window.location.pathname)
		);
	}

	function encKey(k) {
		return String(k).split("/").map(encodeURIComponent).join("/");
	}

	// Launch-token exchange. Resolved (or rejected with a clear error) by the
	// time the page's first SDK call runs. On portal origin the promise
	// resolves immediately — the portal's UserSession cookie is already set.
	//
	// Why hoisted to module scope: every call() awaits this before sending its
	// request, so calls made during the brief window between page load and
	// exchange completion queue up cleanly instead of racing the cookie.
	var _appSessionReady = (function bootstrap() {
		if (!onSubdomain) {
			// Legacy / portal-origin mode: no exchange needed. The portal's
			// own session cookie authenticates everything.
			return Promise.resolve();
		}

		var hash = window.location.hash || "";
		var m = hash.match(/(?:^#|&)token=([^&]+)/);
		if (!m) {
			// No token in the URL. Either the page reloaded (cookie should
			// already exist) or the iframe loaded without a token (the
			// server's no-cookie redirect will bounce us back to the portal
			// launcher to mint a fresh one). Either way the SDK has nothing
			// to do here — proceed and let API calls succeed or fail on the
			// strength of the existing cookie.
			return Promise.resolve();
		}

		var token = decodeURIComponent(m[1]);

		// Strip the token from the URL bar BEFORE we issue the network call
		// so a reload (or a user bookmarking) doesn't end up with the
		// single-use token in history. replaceState keeps the rest of the
		// URL (path + query) intact.
		try {
			history.replaceState(
				{},
				document.title,
				window.location.pathname + window.location.search
			);
		} catch (_) {
			// Some sandboxed environments disallow history manipulation;
			// failing to strip is not fatal (the token is one-shot anyway).
		}

		return fetch("/api/v1/session/exchange", {
			method: "POST",
			credentials: "same-origin",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ token: token }),
		}).then(function (res) {
			if (!res.ok) {
				return res.text().catch(function () { return ""; }).then(function (body) {
					throw new Error(
						"Portal SDK: launch-token exchange failed (" +
						res.status + "): " + (body || res.statusText)
					);
				});
			}
		});
	})();

	// Surface a bootstrap failure if no one ever awaits the SDK (so it isn't
	// swallowed as an unhandled rejection in the console). Apps that DO call
	// the SDK will see the same error rethrown by call().
	_appSessionReady.catch(function (err) {
		if (typeof console !== "undefined" && console.error) {
			console.error(err);
		}
	});

	// Lazy CSRF token cache. Populated on first state-changing call; cleared
	// and refetched once if the server rejects with 403 (e.g. session rotated
	// across login/logout while the SDK held a stale token).
	var _csrfToken = null;
	async function getCsrf() {
		if (_csrfToken) return _csrfToken;
		try {
			var res = await fetch("/api/v1/csrf-token", { credentials: "same-origin" });
			if (!res.ok) return null; // bearer-auth or unauthenticated — caller skips the header
			var data = await res.json();
			_csrfToken = data && data.csrf_token ? data.csrf_token : null;
			return _csrfToken;
		} catch (_) {
			return null;
		}
	}

	var CSRF_METHODS = ["POST", "PUT", "DELETE", "PATCH"];

	async function call(path, opts, _retried) {
		// Wait for the launch-token exchange (no-op on portal origin or page
		// reload). If exchange failed, every call rejects with the same
		// underlying error — apps see a clear message rather than a stream of
		// 401s from the API.
		await _appSessionReady;

		opts = opts || {};
		opts.credentials = "same-origin";
		var method = (opts.method || "GET").toUpperCase();
		var headers = {};
		if (opts.headers) {
			for (var k in opts.headers) headers[k] = opts.headers[k];
		}
		if (CSRF_METHODS.indexOf(method) !== -1) {
			var csrf = await getCsrf();
			if (csrf) headers["X-CSRF-Token"] = csrf;
		}
		opts.headers = headers;
		var res = await fetch("/api/v1" + path, opts);
		if (!res.ok) {
			// Single-shot retry on a 403 in case the cached CSRF went stale
			// (e.g. user logged out + back in). Capped at one retry to avoid loops.
			if (res.status === 403 && !_retried && CSRF_METHODS.indexOf(method) !== -1) {
				_csrfToken = null;
				return call(path, opts, true);
			}
			var detail = res.statusText;
			try {
				var body = await res.clone().json();
				if (body && body.detail) detail = body.detail;
			} catch (_) {
				try { detail = await res.text(); } catch (__) {}
			}
			var err = new Error("Portal API " + path + " " + res.status + ": " + detail);
			err.status = res.status;
			err.detail = detail;
			throw err;
		}
		return res;
	}

	var portal = {
		appSlug: appSlug,

		user: {
			async current() {
				return (await call("/user/me")).json();
			},
		},

		pdf: {
			async render(opts) {
				opts = opts || {};
				var res = await call("/pdf/render", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						html: opts.html || "",
						filename: opts.filename || "document.pdf",
					}),
				});
				return res.blob();
			},
			async download(opts) {
				opts = opts || {};
				var blob = await portal.pdf.render(opts);
				var a = document.createElement("a");
				a.href = URL.createObjectURL(blob);
				a.download = opts.filename || "document.pdf";
				document.body.appendChild(a);
				a.click();
				a.remove();
				setTimeout(function () { URL.revokeObjectURL(a.href); }, 0);
			},
		},

		email: {
			async send(opts) {
				opts = opts || {};
				var res = await call("/email/send", {
					method: "POST",
					headers: { "Content-Type": "application/json" },
					body: JSON.stringify({
						to: opts.to,
						subject: opts.subject || "",
						text: opts.text,
						html: opts.html,
					}),
				});
				return res.json();
			},
		},

		storage: {
			async put(key, value) {
				requireAppSlug();
				var body, ct;
				if (value instanceof Blob) {
					body = value;
					ct = value.type || "application/octet-stream";
				} else if (typeof value === "string") {
					body = value;
					ct = "text/plain; charset=utf-8";
				} else {
					try {
						body = JSON.stringify(value);
						ct = "application/json";
					} catch (e) {
						throw new Error("portal.storage.put: value is not JSON-serializable: " + e.message);
					}
				}
				var res = await call("/storage/" + encKey(key), {
					method: "PUT",
					headers: { "Content-Type": ct },
					body: body,
				});
				return res.json();
			},
			async get(key) {
				requireAppSlug();
				var res = await call("/storage/" + encKey(key));
				var ct = res.headers.get("Content-Type") || "";
				if (ct.indexOf("application/json") === 0) {
					try {
						return await res.json();
					} catch (e) {
						throw new Error("portal.storage.get(" + key + "): stored value is not valid JSON: " + e.message);
					}
				}
				if (ct.indexOf("text/") === 0) return res.text();
				return res.blob();
			},
			async list() {
				requireAppSlug();
				return (await call("/storage")).json();
			},
			async delete(key) {
				requireAppSlug();
				return (await call("/storage/" + encKey(key), { method: "DELETE" })).json();
			},
		},
	};

	window.portal = portal;
})();
