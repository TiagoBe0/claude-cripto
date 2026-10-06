"""Avisos al cierre de vela: patrones de velas (4h y 1d), cruces de medias (1d), orden de la estrategia C y
cambios de la cobertura sugerida (ronda 8).

Uso (lo llama binance_extract.py al final de cada corrida si "alerts" está en la config):
    python -m alertas.velas

Las señales salen de estrategia/eventos.py, las mismas que mide el estudio de eventos, y cada correo
trae lo que dio esa señal en el backtest. Ninguna de estas señales mostró ventaja en los dos períodos:
el correo lo dice, para que el aviso se lea como "mirá el gráfico" y no como "comprá".
"""

import json
import logging
from pathlib import Path

import pandas as pd

from alertas import mailer
from alertas.estado import Estado
from estrategia import eventos

DATA = mailer.ROOT / "data"
PANEL = "https://malbecmotion.com/sbs/btc/"
CROSSES = {"Cruce dorado SMA50 > SMA200", "Cruce de la muerte SMA50 < SMA200",
           "Cierre cruza arriba de SMA200", "Cierre cruza abajo de SMA200"}
# Qué señales se avisan en cada timeframe: los cruces solo en diario (en 4h son ruido frecuente)
WATCH = {"4h": lambda name: name not in CROSSES, "1d": lambda name: True}
STUDY_DAYS = 3

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


def backtest_line(c: pd.DataFrame, tf: str, name: str) -> str:
    res = eventos.study(c, tf)
    r = res[(res["señal"] == name) & (res["días"] == STUDY_DAYS)].set_index("período")
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
                backtest_line(c, tf, name),
                "",
                "Esta señal NO mostró ventaja en los dos períodos del backtest (estrategia/eventos.py):",
                "tomala como aviso para mirar el gráfico, no como orden de compra o venta.",
                "",
                f"Panel: {PANEL}#{tf}",
            ])
            est.notify(key, kind, subject, text)


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


def check_cobertura(est: Estado) -> None:
    """Avisa cuando la cobertura sugerida se activa o se apaga (estrategia/cobertura.py)."""
    path = DATA / "estrategia" / "cobertura.json"
    if not path.exists():
        return
    c = json.loads(path.read_text())
    key = f"cobertura:{c['since_utc']}:{c['hedge']}"
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
        for fn in (check_candles, check_strategy, check_cobertura):
            try:
                fn(est)
            except Exception:  # noqa: BLE001 - una parte que falla no frena la otra
                log.exception("alertas: falló %s", fn.__name__)
    finally:
        est.save()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
