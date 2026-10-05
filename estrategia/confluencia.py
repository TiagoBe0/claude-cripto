"""Backtest de la "confluencia dinámica" (confluencia.pine): EMA 200 + cruce de MACD + RSI + stop/take por ATR.

Uso:
    python estrategia/confluencia.py

Reglas (long-only spot, mismas convenciones y costos que backtest.py):
- Señal al cierre de t: close > EMA 200, MACD(12,26,9) cruza sobre su señal en t, 50 < RSI 14 < 70
  (y ADX 14 > 25 en la variante con filtro).
- Compra a mercado en la apertura de t+1. Stop = close(t) − 2×ATR14(t) y take = close(t) + 4×ATR14(t):
  se fijan al cierre de la señal (como strategy.exit en confluencia.pine, que así queda activo desde
  la vela de entrada) y no se mueven hasta la salida. En cripto 24/7, close(t) ≈ open(t+1).
- Desde la vela de entrada inclusive: si low <= stop sale a min(open, stop); si high >= take sale a
  max(open, take). Si en la misma vela se tocan los dos, se asume el stop (no se sabe el orden).
- Sin posición nueva mientras hay una abierta.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from estrategia.backtest import COST, PARAMS, download, report, rma, run, signals, stats  # noqa: E402

CONF = dict(ema_len=200, rsi_lo=50, rsi_hi=70, atr_len=14, sl_mult=2.0, tp_mult=4.0, adx_min=None)


def adx(df, n=14):
    """ADX de Wilder (igual que ta.dmi de TradingView)."""
    up, down = df.high.diff(), -df.low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    atr = rma(tr, n)
    plus = 100 * rma(pd.Series(plus_dm, df.index), n) / atr
    minus = 100 * rma(pd.Series(minus_dm, df.index), n) / atr
    dx = 100 * (plus - minus).abs() / (plus + minus)
    return rma(dx, n)


def conf_signals(df, ema_len, rsi_lo, rsi_hi, atr_len, sl_mult, tp_mult, adx_min):
    c = df.close
    ema = lambda s, n: s.ewm(span=n, adjust=False, min_periods=n).mean()  # noqa: E731
    trend = ema(c, ema_len)
    macd = ema(c, 12) - ema(c, 26)
    sig = ema(macd, 9)
    cross = (macd > sig) & (macd.shift() <= sig.shift())  # ta.crossover
    delta = c.diff()
    rsi = 100 - 100 / (1 + rma(delta.clip(lower=0), 14) / rma(-delta.clip(upper=0), 14))
    pc = c.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    atr = rma(tr, atr_len)
    entry = (c > trend) & cross & (rsi > rsi_lo) & (rsi < rsi_hi)
    if adx_min is not None:
        entry &= adx(df) > adx_min
    return entry.fillna(False), atr


def simulate_bracket(df, entry, atr, sl_mult, tp_mult, cost=COST, start=None, end=None):
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    en, a = entry.to_numpy(), atr.to_numpy()
    m = np.ones(len(df), bool)
    if start:
        m &= df.index >= pd.Timestamp(start)
    if end:
        m &= df.index < pd.Timestamp(end)
    cash, qty, stop, take, pending = 1.0, 0.0, np.nan, np.nan, None
    curve, trades = np.full(len(df), np.nan), []
    for i in np.flatnonzero(m):
        if pending is not None:
            qty, cash = cash / (o[i] * (1 + cost)), 0.0
            stop, take = pending
            trades.append(dict(entry_time=df.index[i], entry_price=o[i]))
            pending = None
        if qty > 0:
            px = reason = None
            if l[i] <= stop:
                px, reason = min(o[i], stop), "stop"
            elif h[i] >= take:
                px, reason = max(o[i], take), "take"
            if px is not None:
                t = trades[-1]
                t.update(exit_time=df.index[i], exit_price=px, exit_reason=reason,
                         ret=px * (1 - cost) / (t["entry_price"] * (1 + cost)) - 1)
                cash, qty = qty * px * (1 - cost), 0.0
        if qty == 0 and en[i] and not np.isnan(a[i]):
            pending = (c[i] - sl_mult * a[i], c[i] + tp_mult * a[i])
        curve[i] = cash + qty * c[i]
    return pd.Series(curve, df.index).dropna(), trades


def run_conf(df, params, cost=COST, start=None, end=None):
    entry, atr = conf_signals(df, **params)
    curve, trades = simulate_bracket(df, entry, atr, params["sl_mult"], params["tp_mult"], cost, start, end)
    last = df.close[curve.index[-1]]
    rets = [t.get("ret", last * (1 - cost) / (t["entry_price"] * (1 + cost)) - 1) for t in trades]
    return report(stats(curve, np.array(rets), df.close[curve.index]))


def resample_1d(df):
    d = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return d.dropna()


if __name__ == "__main__":
    periods = {"in-sample 2018-2022": ("2018-01-01", "2023-01-01"),
               "out-of-sample 2023-hoy": ("2023-01-01", None)}
    variants = {"confluencia": CONF, "confluencia + ADX>25": {**CONF, "adx_min": 25}}
    for symbol in ("BTC/USDT", "ETH/USDT"):
        raw = download(symbol)
        for tf, df in (("4h", raw), ("1d", resample_1d(raw))):
            print(f"\n{symbol} {tf}")
            for name, (a, b) in periods.items():
                for vname, p in variants.items():
                    for cost in (COST, 2 * COST):
                        print(f"  {name:23s} {vname:21s} costo {cost:.2%}  {run_conf(df, p, cost, a, b)}")
                if tf == "4h":
                    print(f"  {name:23s} {'donchian (referencia)':21s} costo {COST:.2%}  "
                          f"{run(raw, *signals(raw, **PARAMS), cost=COST, start=a, end=b)}")
