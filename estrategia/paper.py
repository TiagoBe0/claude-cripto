"""Paper trading en el perpetuo BTCUSDT de las reglas elegidas en perp_backtest.py.

Uso:
    python estrategia/paper.py               # usa data/ y la sección "paper" de config.json
    python estrategia/paper.py otra/carpeta

Cuentas (mismo código que el backtest, costo 0,10 %/lado y funding real):
- C  principal: ruptura de Donchian 120 sobre la EMA 1200, solo largo, tamaño por objetivo de
     volatilidad 50 % (máx 3x), stop trailing de 4 ATR. La más consistente dentro y fuera de muestra.
- G  candidata: momentum 30/90/180 días con objetivo de volatilidad 50 % (máx 2x). La mejor desde
     2023 pero la peor hasta 2022: se sigue para ver si se sostiene, no porque esté validada.

Como live_signal.py, re-simula desde `start` en cada corrida: el resultado no depende de
corridas anteriores. Las órdenes se ejecutan al precio de apertura real de la vela siguiente,
que recién se conoce cuando esa vela cierra. Escribe en <data_dir>/estrategia/:

- paper.json       estado de cada cuenta: capital, exposición, orden pendiente, stop, comparación
                   con comprar y mantener desde el mismo inicio
- paper_log.csv    una fila por vela cerrada evaluada (solo se agrega), para auditar que lo ya
                   simulado no cambie al recalcular
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import Appender, last_timestamp  # noqa: E402

from estrategia.perp_backtest import (COST, indicators, load_perp, simulate, simulate_target,  # noqa: E402
                                      target_tsmom)

SYMBOL = "BTCUSDT"
ACCOUNTS = {
    "C": {"name": "Donchian largo + vol 50 %", "role": "principal",
          "params": dict(shorts=False, target_vol=0.50, max_lev=3.0, atr_mult=4.0)},
    "G": {"name": "Momentum 30/90/180 d + vol 50 %", "role": "candidata",
          "params": dict(target_vol=0.50, max_lev=2.0, band=0.25)},
}
LOG_COLUMNS = ["timestamp", "datetime_utc"] + [f"{a}_{f}" for a in ACCOUNTS for f in ("equity", "exposure", "order")]


def _iso(ts) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def _summary(curve: pd.Series, q: float, close: float, capital: float, funding_paid: float) -> dict:
    eq = float(curve.iloc[-1])
    return {"equity_usd": _r(eq * capital), "return_pct": _r((eq - 1) * 100),
            "max_dd_pct": _r((curve / curve.cummax().clip(lower=1) - 1).min() * 100),
            "exposure": _r(q * close / eq, 3) if eq > 0 else 0.0,
            "side": "long" if q > 0 else "short" if q < 0 else None,
            "funding_paid_usd": _r(funding_paid * capital)}


def evaluate(spot: pd.DataFrame, perp: pd.DataFrame, funding: pd.Series, start: str, capital: float) -> dict:
    ind = indicators(spot)
    t0 = pd.Timestamp(start)
    t0 = t0.tz_convert(None) if t0.tzinfo else t0
    live = perp[perp.index >= t0]
    state = {"start_utc": _iso(t0), "capital_usd": capital, "cost_per_side": COST, "accounts": {}}
    if live.empty:
        state["waiting"] = True  # todavía no cerró la primera vela desde el inicio
        for key, a in ACCOUNTS.items():
            state["accounts"][key] = {"name": a["name"], "role": a["role"], "params": a["params"]}
        return state
    close = float(live.close.iloc[-1])
    state["waiting"] = False
    state["last_candle_open_utc"] = _iso(live.index[-1])
    state["close"] = _r(close)
    state["buy_hold_return_pct"] = _r((close / live.open.iloc[0] - 1) * 100)

    pc = ACCOUNTS["C"]["params"]
    r = simulate(perp, funding, ind, start=t0, **pc)
    c = {"name": ACCOUNTS["C"]["name"], "role": "principal", "params": pc,
         **_summary(r["curve"], r["q"], close, capital, r["funding_paid"]),
         "stop": _r(r["trail"]),
         "order": None if r["pending"] is None else {"target_exposure": _r(r["pending"][0], 3),
                                                     "reason": r["pending"][1]},
         "trades": [{**t, "entry_time": _iso(t["entry_time"]),
                     **({"exit_time": _iso(t["exit_time"])} if "exit_time" in t else {})}
                    for t in r["trades"][-5:]],
         "n_trades": len(r["trades"])}
    # Entrada si no hay posición: el mismo umbral que live_signal.py (cierre spot de 4 h)
    if r["q"] == 0:
        c["entry_trigger"] = _r(max(spot.high.iloc[-120:].max(), ind.trend.iloc[-1]))
    state["accounts"]["C"] = c

    pg = ACCOUNTS["G"]["params"]
    target = target_tsmom(spot, ind, pg["target_vol"], pg["max_lev"])
    r = simulate_target(perp, funding, target, band=pg["band"], start=t0)
    state["accounts"]["G"] = {
        "name": ACCOUNTS["G"]["name"], "role": "candidata", "params": pg,
        **_summary(r["curve"], r["q"], close, capital, r["funding_paid"]),
        "target_exposure": _r(target.reindex(live.index).iloc[-1], 3),
        "order": None if r["pending"] is None else {"target_exposure": _r(r["pending"], 3),
                                                    "reason": "rebalanceo"},
        "rebalances": r["rebalances"],
    }
    return state


def update(data_dir: Path, start: str, capital: float = 10_000) -> dict:
    spot = pd.read_csv(data_dir / f"{SYMBOL}_4h.csv")
    spot.index = pd.to_datetime(spot.timestamp, unit="ms")
    perp, funding = load_perp(data_dir / f"{SYMBOL}_perp_4h.csv", data_dir / f"{SYMBOL}_funding.csv")
    state = evaluate(spot, perp, funding, start, capital)

    out = data_dir / "estrategia"
    out.mkdir(exist_ok=True)
    tmp = out / "paper.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False))
    tmp.replace(out / "paper.json")

    if not state["waiting"]:
        ts = int(pd.Timestamp(state["last_candle_open_utc"]).timestamp() * 1000)
        log_path = out / "paper_log.csv"
        if (last_timestamp(log_path) or 0) < ts:
            row = [ts]
            for key in ACCOUNTS:
                a = state["accounts"][key]
                row += [a["equity_usd"], a["exposure"], "" if a["order"] is None else a["order"]["target_exposure"]]
            with Appender(log_path, LOG_COLUMNS) as app:
                app.write([row])
    return state


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "data"
    cfg = json.loads((root / "config.json").read_text())["paper"]
    print(json.dumps(update(d, cfg["start"], cfg.get("capital", 10_000)), indent=2, ensure_ascii=False))
