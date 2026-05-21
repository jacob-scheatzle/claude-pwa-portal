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

	function encKey(k) {
		return String(k).split("/").map(encodeURIComponent).join("/");
	}

	function buildHeaders(extra) {
		var h = { "X-Portal-App": appSlug || "" };
		if (extra) for (var k in extra) h[k] = extra[k];
		return h;
	}

	async function call(path, opts) {
		opts = opts || {};
		opts.credentials = "same-origin";
		opts.headers = buildHeaders(opts.headers);
		var res = await fetch("/api/v1" + path, opts);
		if (!res.ok) {
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
				var body, ct;
				if (value instanceof Blob) {
					body = value;
					ct = value.type || "application/octet-stream";
				} else if (typeof value === "string") {
					body = value;
					ct = "text/plain; charset=utf-8";
				} else {
					body = JSON.stringify(value);
					ct = "application/json";
				}
				var res = await call("/storage/" + encKey(key), {
					method: "PUT",
					headers: { "Content-Type": ct },
					body: body,
				});
				return res.json();
			},
			async get(key) {
				var res = await call("/storage/" + encKey(key));
				var ct = res.headers.get("Content-Type") || "";
				if (ct.indexOf("application/json") === 0) return res.json();
				if (ct.indexOf("text/") === 0) return res.text();
				return res.blob();
			},
			async list() {
				return (await call("/storage")).json();
			},
			async delete(key) {
				return (await call("/storage/" + encKey(key), { method: "DELETE" })).json();
			},
		},
	};

	window.portal = portal;
})();
