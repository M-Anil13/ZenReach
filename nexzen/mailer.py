"""Optional SMTP mailer for invites / password resets.

If SMTP_USER + SMTP_PASS are configured (env or .env) it sends real email;
otherwise send_email() returns False and callers fall back to returning the
link, so every flow works with or without SMTP.
"""
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate


def _env_cfg():
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"):
        return {"user": os.environ["SMTP_USER"], "pass": os.environ["SMTP_PASS"],
                "server": os.environ.get("SMTP_SERVER", "smtp.hostinger.com"),
                "port": os.environ.get("SMTP_PORT", "465")}
    return None


def smtp_configured(cfg=None):
    cfg = cfg or _env_cfg()
    return bool(cfg and cfg.get("user") and cfg.get("pass"))


def send_email(to, subject, html, cfg=None):
    """cfg (per-org SMTP from settings) takes priority; falls back to env SMTP."""
    cfg = cfg or _env_cfg()
    if not smtp_configured(cfg):
        return False
    user = cfg["user"]
    pw = cfg["pass"]
    server = cfg.get("server", "smtp.hostinger.com")
    port = int(cfg.get("port", "465"))
    try:
        msg = MIMEText(html, "html")
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(server, port, context=ctx) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        return True
    except Exception:
        return False
