"""Estado en vivo de la estrategia (donchian_trend) sobre las velas del extractor.

Uso:
    python estrategia/live_signal.py         # usa data/BTCUSDT_4h.csv
    python estrategia/live_signal.py otra/carpeta

Re-simula toda la historia guardada con el mismo código que backtest.py y escribe
en <data_dir>/estrategia/:

- state.json        estado después de la última vela cerrada: posición, stop vigente,
                    orden para la apertura siguiente y precio que dispara la entrada
- trades.csv        trades teóricos de la simulación (se reescribe en cada corrida)
- signals_log.csv   una fila por vela cerrada evaluada en vivo (solo se agrega); sirve
                    para el paper trading y para detectar si una señal ya emitida
                    cambia al recalcular (no debería)

Al recalcular desde cero en cada corrida, el estado no depende de corridas
anteriores: si el cron se saltea velas, la próxima corrida queda igual.
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import Appender, last_timestamp  # noqa: E402

from estrategia.backtest import COST, PARAMS, signals, simulate  # noqa: E402

LOG_COLUMNS = ["timestamp", "datetime_utc", "close", "ema", "donchian_high", "atr",
               "in_position", "action_next_open", "stop", "entry_trigger"]


def _round(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def evaluate(df: pd.DataFrame) -> tuple[dict, list[dict]]:
    p = PARAMS
    if len(df) < p["ema_len"]:
        raise ValueError(f"hacen falta {p['ema_len']} velas para la EMA, hay {len(df)}")
    entry, exit_, stop = signals(df, **p)
    sim = simulate(df, entry, exit_, stop)

    last = df.iloc[-1]
    trend = df.close.ewm(span=p["ema_len"], adjust=False, min_periods=p["ema_len"]).mean().iloc[-1]
    atr = (last.close - stop.iloc[-1]) / p["atr_mult"]  # signals() da stop = close - mult * ATR
    don_prev = df.high.iloc[-p["don_len"] - 1:-1].max()  # el canal con el que se evaluó esta vela
    don_next = df.high.iloc[-p["don_len"]:].max()  # el canal contra el que se evalúa la próxima

    in_pos = sim["in_position"]
    trades = sim["trades"]
    open_trade = trades[-1] if trades and "exit_time" not in trades[-1] else None
    # Cierre mínimo de la próxima vela para que haya señal de compra (si no hay posición).
    # Es aproximado: la EMA también se mueve con ese cierre, pero muy poco con 1200 velas.
    trigger = None if in_pos else max(don_next, trend)

    state = {
        "strategy": "donchian_trend",
        "params": p,
        "cost_per_side": COST,
        "last_candle_open_utc": df.index[-1].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "close": _round(last.close),
        "ema": _round(trend),
        "donchian_high": _round(don_prev),
        "atr": _round(atr),
        "in_position": in_pos,
        "action_next_open": sim["pending"],  # "buy", "sell" o None
        "stop": _round(sim["trail"]),  # stop vigente durante la próxima vela
        "stop_distance_pct": _round((sim["trail"] / last.close - 1) * 100) if sim["trail"] else None,
        "entry_trigger": _round(trigger),
        "entry_trigger_distance_pct": _round((trigger / last.close - 1) * 100) if trigger else None,
        "open_trade": None if open_trade is None else {
            "entry_time_utc": open_trade["entry_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "entry_price": _round(open_trade["entry_price"]),
            "unrealized_pct": _round((last.close * (1 - COST) / (open_trade["entry_price"] * (1 + COST)) - 1) * 100),
        },
    }
    return state, trades


def update(data_dir: Path, symbol: str = "BTCUSDT", timeframe: str = "4h") -> dict:
    df = pd.read_csv(data_dir / f"{symbol}_{timeframe}.csv")
    df.index = pd.to_datetime(df.timestamp, unit="ms")
    state, trades = evaluate(df)
    state["symbol"], state["timeframe"] = symbol, timeframe

    out = data_dir / "estrategia"
    out.mkdir(exist_ok=True)
    tmp = out / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(out / "state.json")

    t = pd.DataFrame(trades)
    if len(t):
        t["ret_pct"] = (t.pop("ret") * 100).round(2) if "ret" in t else None
        t = t.round({"entry_price": 2, "exit_price": 2})
    t.to_csv(out / "trades.csv.tmp", index=False, date_format="%Y-%m-%dT%H:%M:%SZ")
    (out / "trades.csv.tmp").replace(out / "trades.csv")

    log_path = out / "signals_log.csv"
    ts = int(df.timestamp.iloc[-1])
    if (last_timestamp(log_path) or 0) < ts:
        with Appender(log_path, LOG_COLUMNS) as app:
            app.write([[ts, state["close"], state["ema"], state["donchian_high"], state["atr"],
                        int(state["in_position"]), state["action_next_open"] or "",
                        state["stop"] or "", state["entry_trigger"] or ""]])
    return state


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    print(json.dumps(update(d), indent=2, ensure_ascii=False))
