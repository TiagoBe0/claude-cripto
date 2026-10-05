"""Envío de avisos por SMTP.

La config vive fuera del repo, en ~/.config/claude-cripto/alertas.env (KEY=valor por línea):

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=465                 # 465 = SSL directo, 587 = STARTTLS
    SMTP_USER=tu.cuenta@gmail.com
    SMTP_PASS=contraseña de aplicación (no la de la cuenta)
    ALERT_TO=destino@ejemplo.com  # varios separados por coma; por defecto SMTP_USER

Sin SMTP_HOST no se envía nada: el aviso queda solo en data/alertas/alertas.log, que es como conviene
probar. send() nunca lanza.
"""

import json
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from pathlib import Path

ENV_FILE = Path(os.environ.get("ALERTAS_ENV", Path.home() / ".config" / "claude-cripto" / "alertas.env"))
ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "data" / "alertas" / "alertas.log"

log = logging.getLogger("alertas")


def _config() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def _record(kind: str, subject: str, text: str, sent: bool, error: str | None = None) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps({"t": int(time.time()), "kind": kind, "subject": subject, "sent": sent,
                            "error": error, "text": text}, ensure_ascii=False) + "\n")


def send(kind: str, subject: str, text: str) -> bool:
    """Manda el aviso. Devuelve True si salió por correo."""
    cfg = _config()
    if not cfg.get("SMTP_HOST"):
        log.info("aviso sin SMTP (solo log): %s", subject)
        _record(kind, subject, text, False, "sin SMTP_HOST")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER", "")
    msg["To"] = cfg.get("ALERT_TO") or cfg.get("SMTP_USER", "")
    msg.set_content(text)
    try:
        port = int(cfg.get("SMTP_PORT", "465"))
        ctx = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(cfg["SMTP_HOST"], port, timeout=20)
            server.starttls(context=ctx)
        with server:
            if cfg.get("SMTP_USER"):
                server.login(cfg["SMTP_USER"], cfg.get("SMTP_PASS", ""))
            server.send_message(msg)
        _record(kind, subject, text, True)
        return True
    except Exception as e:  # noqa: BLE001 - un aviso que falla no corta nada
        log.warning("aviso no enviado (%s): %s", type(e).__name__, subject)
        _record(kind, subject, text, False, f"{type(e).__name__}: {e}")
        return False
