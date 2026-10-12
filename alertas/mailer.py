"""Envío de avisos por SMTP.

La config vive fuera del repo, en ~/.config/claude-cripto/alertas.env (KEY=valor por línea):

    SMTP_HOST=smtp.gmail.com
    SMTP_PORT=465                 # 465 = SSL directo, 587 = STARTTLS
    SMTP_USER=tu.cuenta@gmail.com
    SMTP_PASS=contraseña de aplicación (no la de la cuenta)
    ALERT_TO=destino@ejemplo.com  # varios separados por coma; por defecto SMTP_USER
    ALERT_ADJUNTOS=0              # opcional: sin el contexto de mercado ni los gráficos (alertas/adjuntos.py)

Sin SMTP_HOST no se envía nada: el aviso queda solo en data/alertas/alertas.log, que es como conviene
probar. send() nunca lanza. Prueba: python -m alertas.mailer

Cada correo sale en texto y en HTML: el aviso, el contexto de mercado (RSI, MACD, funding, open interest, libro,
liquidaciones, ballenas, niveles) y los gráficos de alertas/adjuntos.py dentro del cuerpo. Los gráficos solo se
dibujan cuando el correo sale de verdad; si fallan, el aviso sale igual en texto.
"""

import html
import json
import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import make_msgid
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


def _record(kind: str, subject: str, text: str, sent: bool, error: str | None = None, images: list | None = None) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {"t": int(time.time()), "kind": kind, "subject": subject, "sent": sent, "error": error, "text": text}
    if images is not None:
        row["images"] = images
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _pre(text: str) -> str:
    return ('<pre style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:14px;'
            f'white-space:pre-wrap;margin:0 0 12px">{html.escape(text)}</pre>')


def _compose(msg: EmailMessage, text: str, tf: str) -> list[str]:
    """Cuerpo en texto y HTML con el contexto de mercado y los gráficos. Devuelve los gráficos que entraron."""
    from alertas import adjuntos  # matplotlib solo cuando hace falta
    context, images = adjuntos.build(tf)
    plain = text + (f"\n\n— Contexto del mercado —\n\n{context}" if context else "")
    msg.set_content(plain + ("\n\nLos gráficos van en la versión HTML del correo." if images else ""))
    cids = [make_msgid(domain="claude-cripto") for _ in images]
    body = [_pre(text)]
    if context:
        body += ['<h3 style="font-family:Arial,sans-serif;margin:20px 0 6px">Contexto del mercado</h3>', _pre(context)]
    for (name, title, _), cid in zip(images, cids):
        body.append(f'<h3 style="font-family:Arial,sans-serif;margin:20px 0 6px">{html.escape(title)}</h3>'
                    f'<img src="cid:{cid[1:-1]}" alt="{html.escape(title)}" style="max-width:100%;height:auto">')
    msg.add_alternative(f'<html><body style="max-width:1100px">{"".join(body)}</body></html>', subtype="html")
    part = msg.get_payload()[1]
    for (name, _, png), cid in zip(images, cids):
        part.add_related(png, "image", "png", cid=cid, filename=f"{name}.png", disposition="inline")
    return [name for name, _, _ in images]


def send(kind: str, subject: str, text: str, tf: str = "4h") -> bool:
    """Manda el aviso. Devuelve True si salió por correo. tf: timeframe de las velas del gráfico adjunto."""
    cfg = _config()
    if not cfg.get("SMTP_HOST"):
        log.info("aviso sin SMTP (solo log): %s", subject)
        _record(kind, subject, text, False, "sin SMTP_HOST")
        return False
    msg = EmailMessage()
    images = None
    if cfg.get("ALERT_ADJUNTOS", "1") != "0":
        try:
            images = _compose(msg, text, tf)
        except Exception:  # noqa: BLE001 - sin gráficos el aviso sale igual, en texto
            log.exception("adjuntos: fallaron, el aviso sale solo en texto")
            msg = EmailMessage()
    if images is None:
        msg.set_content(text)
    msg["Subject"] = subject
    msg["From"] = cfg.get("SMTP_FROM") or cfg.get("SMTP_USER", "")
    msg["To"] = cfg.get("ALERT_TO") or cfg.get("SMTP_USER", "")
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
        _record(kind, subject, text, True, images=images)
        return True
    except Exception as e:  # noqa: BLE001 - un aviso que falla no corta nada
        log.warning("aviso no enviado (%s): %s", type(e).__name__, subject)
        _record(kind, subject, text, False, f"{type(e).__name__}: {e}", images=images)
        return False


if __name__ == "__main__":
    # python -m alertas.mailer → manda un correo de prueba con la config actual
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ok = send("prueba", "BTC · prueba de avisos", "Si llegó este correo, los avisos de claude-cripto funcionan.")
    print("enviado" if ok else f"no enviado: ver {LOG_FILE}")
    raise SystemExit(0 if ok else 1)
