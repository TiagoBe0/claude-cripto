"""Resumen diario: un solo correo con lo que no vale un aviso suelto.

- Patrones de velas sin ventaja en el backtest (|t| fuera de muestra < T_MIN): eran la mayor parte de la bandeja.
- Avisos de ballenas que el tope diario dejó sin correo.
- Cobertura: el estado de ahora y las extensiones del día (el correo ON dice "hasta", y si el modelo vuelve a
  esperar un movimiento grande se corre sin aviso; así pasó del 13/10 al 16/10 durante el CPI).
- Niveles diarios: cierre contra la SMA 50 y posición en el rango de 21 días.
- Eventos macro de los próximos 7 días (o el próximo) de "eventos_macro" en config.json: FOMC, CPI, empleo y
  PIB/PCE, con la hora en UTC. Las fechas salen de los calendarios de la Fed, BLS y BEA; los de 2027 son tentativos.

Quien quiere mandar algo al resumen llama a queue(); velas.py lo manda con send_if_due() en su primera corrida de
cada día UTC (00:06, después del cierre diario). La cola es un .jsonl con lock: ballenas.py escribe cada minuto.

Prueba sin mandar nada: python -m alertas.resumen (muestra el texto y no vacía la cola).
"""

import fcntl
import json
import time

import pandas as pd

from alertas import lectura_ballenas as lb, mailer
from alertas.estado import STATE_DIR

QUEUE = STATE_DIR / "resumen_pendiente.jsonl"
SECTIONS = {"cobertura": "Cobertura", "patron": "Patrones de velas sin ventaja en el backtest (no se avisan sueltos)",
            "ballena": "Avisos de ballenas que no salieron por el tope diario"}
DATA = mailer.ROOT / "data"


def queue(section: str, line: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"t": int(time.time()), "section": section, "line": line}, ensure_ascii=False) + "\n")


def _take(clear: bool) -> list[dict]:
    if not QUEUE.exists():
        return []
    with QUEUE.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        rows = [json.loads(x) for x in f if x.strip()]
        if clear:
            f.truncate(0)
    return rows


def _ar(x: float, d: int = 0) -> str:
    return lb.ar(x, d)


def niveles() -> list[str]:
    ind = lb._read(DATA / "BTCUSDT_1d_indicators.csv", 100_000)
    d1 = lb._read(DATA / "BTCUSDT_1d.csv", None)
    r = ind.iloc[-1]  # binance_extract solo guarda velas cerradas: es el día que acaba de cerrar
    rng = d1[d1.index > d1.index[-1] - pd.Timedelta(days=lb.RANGE_DAYS)]
    lo, hi = rng.low.min(), rng.high.max()
    dist = (r.close / r.sma_50 - 1) * 100
    out = [f"  Cierre diario {_ar(r.close)} · SMA 50 {_ar(r.sma_50)} ({'+' if dist >= 0 else '−'}{_ar(abs(dist), 1)} %)"
           f" · SMA 200 {_ar(r.sma_200)}",
           f"  Rango de {lb.RANGE_DAYS} días {_ar(lo)} – {_ar(hi)}: el cierre quedó al {_ar((r.close - lo) / (hi - lo) * 100)} %"]
    if d1.low.iloc[-1] < r.sma_50 < r.close:
        out.append("  La vela perforó la SMA 50 con la mecha y cerró arriba (en la historia eso no tuvo ventaja: t ≈ 0).")
    return out


def macro(cfg: dict) -> list[str]:
    now = pd.Timestamp.now(tz="UTC")
    events = sorted((pd.Timestamp(e["utc"]), e["nombre"]) for e in cfg.get("eventos_macro", []))
    nxt = [(t, n) for t, n in events if t > now]
    if not nxt:
        return ["  No hay eventos cargados: agregalos en \"eventos_macro\" de config.json."]
    # todo lo de la semana que viene; si no hay nada, el próximo
    week = [(t, n) for t, n in nxt if t - now < pd.Timedelta(days=7)] or nxt[:1]
    out = []
    for t, name in week:
        days = (t - now).total_seconds() / 86400
        when = "HOY" if days < 1 else "MAÑANA" if days < 2 else f"en {int(days)} días"
        out.append(f"  {name}: {t:%d/%m %H:%M} UTC ({when})")
    return out


def build(rows: list[dict], cobertura: list[str], cfg: dict) -> str:
    parts = []
    by = {}
    for r in rows:
        by.setdefault(r["section"], []).append(r)
    cob = cobertura + [f"  {time.strftime('%d/%m %H:%M', time.gmtime(r['t']))} UTC · {r['line']}"
                       for r in by.pop("cobertura", [])]
    if cob:
        parts.append(SECTIONS["cobertura"] + "\n" + "\n".join(cob))
    for key, title in SECTIONS.items():
        if key in by:
            parts.append(f"{title} ({len(by[key])})\n" + "\n".join(f"  {r['line']}" for r in by[key]))
    for title, fn in (("Niveles diarios", niveles), ("Eventos macro", lambda: macro(cfg))):
        try:
            parts.append(title + "\n" + "\n".join(fn()))
        except Exception as e:  # noqa: BLE001 - una parte que falta no frena el resumen
            parts.append(f"{title}\n  sin datos ({type(e).__name__})")
    return "\n\n".join(parts)


def send_if_due(est, cobertura: list[str]) -> None:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if est.get("resumen_fecha") == today:
        return
    est.set("resumen_fecha", today)
    cfg = json.loads((mailer.ROOT / "config.json").read_text())
    rows = _take(clear=True)
    text = build(rows, cobertura, cfg) + "\n\nNo es asesoramiento financiero. Panel: https://malbecmotion.com/sbs/btc/"
    n = sum(r["section"] in ("patron", "ballena") for r in rows)
    subject = f"BTC · Resumen del {pd.Timestamp(today) - pd.Timedelta(days=1):%d/%m}" + (f" · {n} avisos agrupados" if n else "")
    est.notify(f"resumen:{today}", "resumen", subject, text, "1d")


if __name__ == "__main__":
    cfg = json.loads((mailer.ROOT / "config.json").read_text())
    print(build(_take(clear=False), [], cfg))
