"""SMTP send helper, factored out so admin.py and api.py share one implementation.

Public surface:
    send_message(msg: EmailMessage, cfg: dict) -> None

``cfg`` is the dict returned by ``portal.settings_store.smtp_config`` — keys
host, port, username, password, from_addr, use_tls.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

# Cap connect/IO at 10s so an unreachable SMTP server can't hold a request open.
_SMTP_TIMEOUT_SECONDS = 10


def send_message(msg: EmailMessage, cfg: dict) -> None:
    """Send ``msg`` over SMTP using the resolved ``cfg`` dict.

    Port 465 uses implicit TLS via SMTP_SSL; everything else opens plain SMTP
    and optionally upgrades with STARTTLS when ``cfg['use_tls']`` is true.
    Login is skipped when no username is configured (relay or open submission).
    """
    host = cfg["host"]
    port = cfg["port"]
    # Verify the server's certificate and hostname. Without an explicit
    # context smtplib falls back to one that checks neither, so anyone on the
    # network path could pose as the mail server and collect the SMTP login.
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
        if cfg["use_tls"]:
            server.starttls(context=context)
    try:
        if cfg["username"]:
            server.login(cfg["username"], cfg["password"] or "")
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
