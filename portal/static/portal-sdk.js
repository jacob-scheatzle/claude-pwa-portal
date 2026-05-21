// Portal SDK v0.1
// Child apps include this via <script src="/portal-sdk.js"></script>.
// It exposes window.portal with auth-aware shortcuts to portal services.
//
// Auth: the SDK uses the existing session cookie (same-origin), so users must
// already be signed into the portal. App scope is auto-detected from the URL.

(function () {
	"use strict";

	var pathMatch = window.location.pathname.match(/^\/apps\/([^\/]+)/);
	var appSlug = pathMatch ? pathMatch[1] : null;

	function requireAppSlug() {
		if (!appSlug) throw new Error("Portal SDK: this script must be loaded from /apps/<slug>/...; current path is " + window.location.pathname);
	}

	function encKey(k) {
		return String(k).split("/").map(encodeURIComponent).join("/");
	}

	function buildHeaders(extra) {
		var h = { "X-Portal-App": appSlug || "" };
		if (extra) for (var k in extra) h[k] = extra[k];
		return h;
	}

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
		opts = opts || {};
		opts.credentials = "same-origin";
		var method = (opts.method || "GET").toUpperCase();
		if (CSRF_METHODS.indexOf(method) !== -1) {
			var csrf = await getCsrf();
			if (csrf) {
				opts.headers = buildHeaders(opts.headers);
				opts.headers["X-CSRF-Token"] = csrf;
			} else {
				opts.headers = buildHeaders(opts.headers);
			}
		} else {
			opts.headers = buildHeaders(opts.headers);
		}
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
