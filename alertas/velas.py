"""Avisos al cierre de vela: patrones de velas (4h y 1d), cruces de medias (1d), orden de la estrategia C,
cambios de la cobertura sugerida (ronda 8) y el resumen diario (alertas/resumen.py).

Uso (lo llama binance_extract.py al final de cada corrida si "alerts" está en la config):
    python -m alertas.velas

Las señales salen de estrategia/eventos.py, las mismas que mide el estudio de eventos, y cada correo
trae lo que dio esa señal en el backtest. Ninguna de estas señales mostró ventaja en los dos períodos:
el correo lo dice, para que el aviso se lea como "mirá el gráfico" y no como "comprá".

Los patrones con |t| fuera de muestra < T_MIN no salen sueltos: van al resumen del día (eran la mayor parte de la
bandeja y su backtest da exceso ~0). Los cruces de medias diarios salen siempre: son pocos y marcan niveles.
"""

import json
import logging
from pathlib import Path

import pandas as pd

from alertas import mailer, resumen
from alertas.estado import Estado
from estrategia import eventos

DATA = mailer.ROOT / "data"
PANEL = "https://malbecmotion.com/sbs/btc/"
CROSSES = {"Cruce dorado SMA50 > SMA200", "Cruce de la muerte SMA50 < SMA200",
           "Cierre cruza arriba de SMA200", "Cierre cruza abajo de SMA200",
           "Cierre cruza arriba de SMA50", "Cierre cruza abajo de SMA50"}
DAILY_ONLY = CROSSES | {"Mecha bajo SMA50 y cierre arriba"}
# Qué señales se avisan en cada timeframe: las de medias solo en diario (en 4h son ruido frecuente)
WATCH = {"4h": lambda name: name not in DAILY_ONLY, "1d": lambda name: True}
STUDY_DAYS = 3
T_MIN = 2.0
OOS = "OOS 2023-hoy"

log = logging.getLogger("alertas.velas")


def ar(x: float, d: int = 2) -> str:
    """Número con formato argentino: 86.522,00."""
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def closed_frames() -> dict[str, pd.DataFrame]:
    frames = eventos.load(DATA / "BTCUSDT_4h.csv")
    # El último día agrupado puede estar a medias: solo cuenta si ya cerró su vela de las 20:00 UTC
    last_4h = frames["4h"].index[-1]
    d1 = frames["1d"]
    if last_4h < d1.index[-1] + pd.Timedelta(hours=20):
        frames["1d"] = d1.iloc[:-1]
    return frames


def backtest(c: pd.DataFrame, tf: str, name: str) -> pd.DataFrame:
    res = eventos.study(c, tf)
    return res[(res["señal"] == name) & (res["días"] == STUDY_DAYS)].set_index("período")


def backtest_line(r: pd.DataFrame) -> str:
    sign = lambda x: ("+" if x >= 0 else "−") + ar(abs(x))  # noqa: E731
    parts = [f"{p}: {int(r.loc[p, 'n'])} casos, acierto {ar(r.loc[p, 'acierto %'], 0)} %, "
             f"exceso sobre una vela cualquiera {sign(r.loc[p, 'exceso %'])} % (t {ar(r.loc[p, 't'], 1)})"
             for p in r.index]
    return f"Backtest a {STUDY_DAYS} días desde la apertura siguiente:\n  " + "\n  ".join(parts)


def check_candles(est: Estado) -> None:
    for tf, c in closed_frames().items():
        last = c.index[-1]
        for name, (sig, direction) in eventos.signals(c).items():
            if not WATCH[tf](name) or not bool(sig.iloc[-1]):
                continue
            key = f"vela:{tf}:{int(last.timestamp())}:{name}"
            if est.seen(key):
                continue
            row = c.iloc[-1]
            bt = backtest(c, tf, name)
            t_oos = bt.loc[OOS, "t"] if OOS in bt.index else float("nan")
            # sin ventaja: al resumen del día y no a la bandeja (t NaN = sin casos, también)
            if name not in CROSSES and not abs(t_oos) >= T_MIN:
                est.data["sent"][key] = int(pd.Timestamp.now(tz="UTC").timestamp())
                resumen.queue("patron", f"{'▲' if direction > 0 else '▼'} {name} ({tf}) · vela del {last:%d/%m %H:%M} "
                                        f"UTC · cierre {ar(row.close, 0)} · t fuera de muestra {'−' if t_oos < 0 else ''}{ar(abs(t_oos), 1)}")
                continue
            useful = ((bt["exceso %"] > 0) & (bt["t"] > T_MIN)).all() and len(bt) == 2
            sma50, sma200 = c.close.rolling(50).mean().iloc[-1], c.close.rolling(200).mean().iloc[-1]
            kind = "cruce" if name in CROSSES else "patron"
            arrow = "▲" if direction > 0 else "▼"
            subject = f"BTC {arrow} {name} ({tf}) · cierre {ar(row.close, 0)}"
            text = "\n".join([
                f"{name} en la vela de {tf} que abrió el {last:%d/%m %H:%M} UTC.",
                f"Lectura {'alcista' if direction > 0 else 'bajista'}.",
                "",
                f"Apertura {ar(row.open)} · máximo {ar(row.high)} · mínimo {ar(row.low)} · cierre {ar(row.close)}",
                f"SMA 50 {ar(sma50)} · SMA 200 {ar(sma200)}",
                "",
                backtest_line(bt),
                "",
                *(["Esta señal tuvo ventaja en los dos períodos del backtest (estrategia/eventos.py). Igual es una",
                   "estadística, no una orden."] if useful else
                  ["Esta señal NO mostró ventaja en los dos períodos del backtest (estrategia/eventos.py):",
                   "tomala como aviso para mirar el gráfico, no como orden de compra o venta."]),
                "",
                f"Panel: {PANEL}#{tf}",
            ])
            est.notify(key, kind, subject, text, tf)


def check_strategy(est: Estado) -> None:
    """Orden de la cuenta C del paper trading (la única regla que aguantó el backtest)."""
    path = DATA / "estrategia" / "paper.json"
    if not path.exists():
        return
    p = json.loads(path.read_text())
    a = p.get("accounts", {}).get("C")
    if p.get("waiting") or not a or not a.get("order"):
        return
    state = json.loads((DATA / "estrategia" / "state.json").read_text())
    candle = state.get("last_candle_open_utc", "")
    order = a["order"]
    key = f"estrategia:C:{candle}:{json.dumps(order, sort_keys=True)}"
    target = order.get("target_exposure")
    action = "CERRAR la posición" if target == 0 else f"exposición {target}x"
    side = "abrir largo" if not a.get("side") and target else "ajustar" if target else "cerrar"
    subject = f"BTC · Estrategia C: {side} en la próxima apertura de 4h"
    text = "\n".join([
        f"La cuenta C del paper trading ({a['name']}) tiene una orden para la apertura de la próxima vela de 4h:",
        f"  {action}",
        "",
        f"Vela evaluada: {candle} UTC",
        f"Capital simulado: US$ {ar(a['equity_usd'], 0)} ({a['return_pct']:+.2f} %) · caída máxima {a['max_dd_pct']:.2f} %",
        f"Stop: {ar(a['stop'])}" if a.get("stop") else f"Entrada si cierra > {ar(a['entry_trigger'])}" if a.get("entry_trigger") else "",
        "",
        "C es la única regla que aguantó in-sample y out-of-sample (README, perp_backtest.py).",
        "Es paper trading: no se envía ninguna orden real.",
        "",
        f"Panel: {PANEL}#4h",
    ])
    est.notify(key, "estrategia", subject, text)


def cobertura_status() -> list[str]:
    """Estado de la cobertura para el resumen diario."""
    path = DATA / "estrategia" / "cobertura.json"
    if not path.exists():
        return []
    c = json.loads(path.read_text())
    since = f"{pd.Timestamp(c['since_utc']):%d/%m %H:%M} UTC"
    if not c["hedge"]:
        return [f"  OFF desde el {since}: 100 % BTC spot. Percentil de P(movimiento) ahora {c['pctl']:.0f}."]
    return [f"  ON desde el {since}: corto {c['hedge'] * 100:.0f} % en el perpetuo, hasta el "
            f"{pd.Timestamp(c['until_utc']):%d/%m %H:%M} UTC si el modelo no vuelve a pasar el percentil {c['rules']['pctl']}."]


def check_cobertura(est: Estado) -> None:
    """Avisa cuando la cobertura sugerida se activa o se apaga (estrategia/cobertura.py). Las extensiones de una
    cobertura ya activa no mandan correo: van al resumen del día."""
    path = DATA / "estrategia" / "cobertura.json"
    if not path.exists():
        return
    c = json.loads(path.read_text())
    key = f"cobertura:{c['since_utc']}:{c['hedge']}"
    until, prev = c.get("until_utc"), est.get("cobertura_until")
    est.set("cobertura_until", until)
    if c["hedge"] and est.seen(key) and prev and until and until > prev:
        resumen.queue("cobertura", f"se extendió hasta el {pd.Timestamp(until):%d/%m %H:%M} UTC "
                                   f"(antes {pd.Timestamp(prev):%d/%m %H:%M}): el modelo volvió a pasar el percentil "
                                   f"{c['rules']['pctl']}")
    since = pd.Timestamp(c["since_utc"])
    pct = lambda x: f"{x * 100:.0f} %"  # noqa: E731
    if c["hedge"]:
        subject = f"BTC · Cobertura ON: corto {pct(c['hedge'])} en el perpetuo hasta {pd.Timestamp(c['until_utc']):%d/%m %H:%M} UTC"
        first = [f"Desde el {since:%d/%m %H:%M} UTC la regla sugiere cubrir el {pct(c['hedge'])} del BTC spot con un corto en el perpetuo",
                 f"(exposición neta {pct(c['net_exposure'])}). Sigue hasta el {pd.Timestamp(c['until_utc']):%d/%m %H:%M} UTC si el modelo",
                 "no vuelve a esperar un movimiento grande; si lo espera, se extiende."]
    else:
        subject = "BTC · Cobertura OFF: sin corto, 100 % BTC spot"
        first = [f"Desde el {since:%d/%m %H:%M} UTC la regla sugiere cerrar el corto: 100 % en BTC spot."]
    p = c.get("paper", {})
    sg = lambda x: ("+" if x >= 0 else "−") + ar(abs(x)) + " %"  # noqa: E731
    paper = ("Paper trading todavía sin datos." if p.get("waiting") else
             f"Paper trading desde el {pd.Timestamp(p['start_utc']):%d/%m/%Y}: {sg(p['return_pct'])} contra {sg(p['buy_hold_return_pct'])} "
             f"de comprar y mantener · caída máx {sg(p['max_dd_pct'])} contra {sg(p['buy_hold_max_dd_pct'])}.")
    text = "\n".join([
        *first,
        "",
        f"P(movimiento 4 h) del modelo: {c['p_move'] * 100:.0f} %, percentil {c['pctl']:.0f} de los últimos 90 días "
        f"(umbral {c['rules']['pctl']}; {c['hours_over_threshold_24h']} de las últimas 24 h por encima).",
        paper,
        "",
        "Regla de la ronda 8: pasó la investigación (2025) y el holdout (oct 2025 a oct 2026). No es una señal de",
        "dirección: el modelo anticipa movimiento, no hacia dónde. Es paper trading: no se envía ninguna orden real.",
        "",
        f"Panel: {PANEL}",
    ])
    est.notify(key, "cobertura", subject, text)


def run() -> None:
    est = Estado("velas")
    try:
        for fn in (check_candles, check_strategy, check_cobertura,
                   lambda e: resumen.send_if_due(e, cobertura_status())):
            try:
                fn(est)
            except Exception:  # noqa: BLE001 - una parte que falla no frena la otra
                log.exception("alertas: falló %s", getattr(fn, "__name__", fn))
    finally:
        est.save()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
