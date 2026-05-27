"""Regression test for the v0.6.4 pwa-portal-caddy filter expansion."""
import re

HOST = (
    r"(?:::ffff:)?(?P<host>"
    r"(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}"
    r"|(?:\d{1,3}\.){3}\d{1,3}"
    r"|[\w\-.]+"
    r")"
)

patterns: list[str] = []
in_failregex = False
with open("contrib/fail2ban/filter.d/pwa-portal-caddy.conf") as f:
    for raw in f:
        if raw.startswith("failregex"):
            in_failregex = True
            patterns.append(raw.split("=", 1)[1].strip())
        elif in_failregex and raw.startswith((" ", "\t")):
            stripped = raw.strip()
            if stripped:
                patterns.append(stripped)
        elif in_failregex and raw.strip() == "":
            in_failregex = False
        else:
            in_failregex = False


def expand(p: str) -> str:
    # fail2ban filters use ``%%`` as an escape for a literal ``%`` because
    # ConfigParser interprets ``%(name)s``. Unescape before compiling for
    # this offline test.
    return p.replace("%%", "%").replace("<HOST>", HOST)


compiled = [re.compile(expand(p)) for p in patterns]


def caddy_line(path: str) -> str:
    return (
        '{"level":"info","ts":1779895953.123,'
        '"logger":"http.log.access","msg":"handled request",'
        '"request":{"remote_ip":"45.88.138.44","remote_port":"56375",'
        f'"client_ip":"45.88.138.44","proto":"HTTP/2.0","method":"GET",'
        '"host":"shelbyfish.example.com","uri":"' + path + '","headers":{}},"status":404}'
    )


# (path, should_ban) — comprehensive sample
samples = [
    # ----- scanner paths (must all BAN) -----
    ("/.env", True),
    ("/.env.production", True),
    ("/.git/config", True),
    ("/.aws/credentials", True),
    ("/.DS_Store", True),
    ("/.bash_history", True),
    ("/wp-login.php", True),
    ("/wp-admin/setup-config.php", True),
    ("/?rest_route=/wp/v2/users/", True),
    ("/wp-json/wp/v2/users", True),
    ("/xmlrpc.php", True),
    ("/wp-config.bak", True),
    ("/wp-config.php.swp", True),
    ("/wp-includes/wlwmanifest.xml", True),
    ("/actuator/env", True),
    ("/heapdump", True),
    ("/server-status", True),
    ("/phpmyadmin/", True),
    ("/info.php", True),
    ("/manager/html", True),
    ("/struts2/", True),
    ("/login.action", True),
    ("/telescope/requests", True),
    ("/telescope", True),
    ("/debug/pprof", True),
    ("/trace.axd", True),
    ("/%40vite/env", True),
    ("/@vite/env", True),
    ("/boaform/admin/formLogin", True),
    ("/Autodiscover/Autodiscover.xml", True),
    ("/owa/auth/logon.aspx", True),
    ("/___proxy_subdomain_whm/login", True),
    ("/api/backend/.env", True),
    ("/Jenkinsfile", True),
    ("/.gitlab-ci.yml", True),
    ("/v2/_catalog", True),
    ("/_cat/indices?v", True),
    ("/minio/health/live", True),
    ("/v1/kv/?recurse", True),
    ("/s/9383e2435323e2430323e25313/_/META-INF/", True),
    # ----- v0.6.4 NEW patterns -----
    ("/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php", True),  # CVE-2017-9841
    ("/vendor/laravel/framework/src/Illuminate/", True),
    ("/vendor/composer/installed.json", True),
    ("/composer.json", True),
    ("/composer.lock", True),
    ("/composer.phar", True),
    ("/_ignition/health-check", True),                       # CVE-2021-3129
    ("/_ignition/execute-solution", True),
    ("/storage/logs/laravel.log", True),
    ("/_profiler/", True),
    ("/_profiler/empty/search/results?limit=10", True),
    ("/_fragment?_path=", True),
    ("/HNAP1/", True),                                       # D-Link
    ("/cgi-bin/luci", True),                                 # OpenWRT
    ("/cgi-bin/php-cgi", True),
    ("/cgi-sys/php5", True),
    ("/sftp-config.json", True),
    ("/.ftpconfig", True),
    ("/.remote-sync.json", True),
    ("/.npmrc", True),
    ("/.docker/config.json", True),
    ("/.kube/config", True),
    ("/.ssh/id_rsa", True),
    ("/.ssh/authorized_keys", True),
    ("/.ssh/known_hosts", True),
    ("/id_rsa", True),
    ("/id_rsa.pub", True),
    ("/id_ed25519", True),
    ("/backup/site.tar.gz", True),
    ("/backups/", True),
    ("/db_backup/x.sql", True),
    ("/db-backup/dump.sql", True),
    ("/dump.sql", True),
    ("/dump.sql.gz", True),
    ("/database.tar.gz", True),
    ("/backup.zip", True),
    ("/backup.tgz", True),
    ("/wallet.dat", True),
    ("/.electrum/wallets/default_wallet", True),
    ("/jmx-console/HtmlAdaptor", True),
    ("/swagger-ui/", True),
    ("/swagger-ui.html", True),
    ("/v2/api-docs", True),
    ("/system/console/configMgr", True),                     # AEM
    ("/CHANGELOG.txt", True),
    ("/license.php", True),
    ("/cron.php", True),
    ("/install.php", True),
    ("/setup.cgi", True),
    ("/shell?cmd=id", True),
    ("/wls-wsat/CoordinatorPortType", True),                 # Oracle WebLogic
    ("/remote/login", True),                                 # Fortinet
    ("/remote/fgt_lang", True),
    ("/remote/hostcheck_validate", True),
    ("/druid/index.html", True),                             # Alibaba Druid
    ("/idx_config", True),
    # ----- legit traffic (must all pass) -----
    ("/", False),
    ("/login", False),
    ("/admin/audit", False),
    ("/admin/apps", False),
    ("/admin/settings", False),
    ("/apps/window-quote/", False),
    ("/app-icons/window-quote", False),
    ("/static/icons/icon-192.png", False),
    ("/manifest.webmanifest", False),
    ("/sw.js", False),
    ("/portal-sdk.js", False),
    ("/branding/logo-effd9c58.jpg", False),
    ("/branding/favicon-94437a9d.jpg", False),
    ("/health", False),
    ("/robots.txt", False),
    ("/favicon.ico", False),
    ("/s/abc123xyz789def", False),  # tokenized share link (not a CVE probe)
    ("/api/v1/csrf-token", False),
    ("/api/v1/user", False),
    ("/api/v1/apps", False),
    ("/api/v1/pdf/render", False),
    ("/api/v1/email/send", False),
    ("/api/v1/storage/list", False),
]

fails: list[str] = []
for path, should_ban in samples:
    matched = any(p.search(caddy_line(path)) for p in compiled)
    expected = "BAN" if should_ban else "pass"
    actual = "BAN" if matched else "pass"
    status = "OK" if expected == actual else "FAIL"
    print(f"{status:5s} expected={expected:4s} actual={actual:4s} | {path}")
    if expected != actual:
        fails.append(path)

print(f"\n{len(samples) - len(fails)}/{len(samples)} OK")
if fails:
    print(f"FAILURES: {fails}")
    raise SystemExit(1)
