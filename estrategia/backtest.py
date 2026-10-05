"""Backtest de la estrategia de donchian_trend.pine (misma lógica, mismos costos).

Uso:
    python estrategia/backtest.py            # baja o actualiza BTC/ETH 4h desde 2017 en data/backtest/

Long-only spot, 4h. Señal al cierre de la vela t -> orden a mercado en la
apertura de t+1 (como TradingView con process_orders_on_close=false).
El stop trailing se fija al cierre de t y queda activo en t+1: si low <= stop
se ejecuta a min(open, stop), así un gap en contra se paga entero.
Costo por lado = comisión 0,10% + slippage 0,05% + medio spread 0,01%.
"""

from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parent.parent / "data" / "backtest"
COST = 0.0016  # por lado
PARAMS = dict(ema_len=1200, don_len=120, atr_len=14, atr_mult=4.0)


def download(symbol: str, tf: str = "4h") -> pd.DataFrame:
    """Baja (o completa desde la última vela guardada) el CSV de data/backtest/."""
    path = DATA / f"{symbol.replace('/', '')}_{tf}.csv"
    DATA.mkdir(parents=True, exist_ok=True)
    ex = ccxt.binance({"enableRateLimit": True})
    old = pd.read_csv(path) if path.exists() else None
    since = ex.parse8601("2017-08-17T00:00:00Z") if old is None else int(old.timestamp.iloc[-1]) + 1
    rows = []
    try:
        while batch := ex.fetch_ohlcv(symbol, tf, since=since, limit=1000):
            rows += batch
            if len(batch) < 1000:
                break
            since = batch[-1][0] + 1
    except ccxt.NetworkError as e:
        if old is None:
            raise
        print(f"{symbol}: no se pudo actualizar ({e}), uso el CSV guardado")
    new = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    new = new[new.timestamp + ex.parse_timeframe(tf) * 1000 <= ex.milliseconds()]  # solo velas cerradas
    if len(new):
        pd.concat([old, new] if old is not None else [new]).to_csv(path, index=False)
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.timestamp, unit="ms")
    return df


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def signals(df, ema_len, don_len, atr_len, atr_mult):
    trend = df.close.ewm(span=ema_len, adjust=False, min_periods=ema_len).mean()
    don_high = df.high.rolling(don_len).max().shift()
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    entry = (df.close > don_high) & (df.close > trend)
    exit_ = df.close < trend
    stop = df.close - atr_mult * rma(tr, atr_len)
    return entry, exit_, stop


def simulate(df, entry, exit_, stop, cost=COST, start=None, end=None) -> dict:
    """Recorre las velas y devuelve la curva de capital, los trades y el estado final.

    Estado final: `pending` es la orden a ejecutar en la apertura de la vela
    siguiente a la última ("buy", "sell" o None) y `trail` el stop vigente
    durante esa vela. `trails` es el stop fijado al cierre de cada vela (vigente
    en la siguiente), NaN sin posición: lo dibuja el dashboard.
    """
    o, l, c = df.open.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    en, ex, st = entry.to_numpy(), exit_.to_numpy(), stop.to_numpy()
    m = np.ones(len(df), bool)
    if start:
        m &= df.index >= pd.Timestamp(start)
    if end:
        m &= df.index < pd.Timestamp(end)
    cash, qty, trail, pending = 1.0, 0.0, np.nan, None
    curve, trails, trades = np.full(len(df), np.nan), np.full(len(df), np.nan), []

    def close_trade(i, price, reason):
        t = trades[-1]
        t.update(exit_time=df.index[i], exit_price=price, exit_reason=reason,
                 ret=price * (1 - cost) / (t["entry_price"] * (1 + cost)) - 1)
        return qty * price * (1 - cost)

    for i in np.flatnonzero(m):
        if pending == "buy":
            qty, cash, trail = cash / (o[i] * (1 + cost)), 0.0, np.nan
            trades.append(dict(entry_time=df.index[i], entry_price=o[i]))
        elif pending == "sell":
            cash, qty = close_trade(i, o[i], "bajo EMA"), 0.0
        pending = None
        if qty > 0 and not np.isnan(trail) and l[i] <= trail:
            cash, qty = close_trade(i, min(o[i], trail), "stop ATR"), 0.0
        if qty > 0:
            if not np.isnan(st[i]):
                trail = st[i] if np.isnan(trail) else max(trail, st[i])
            if ex[i]:
                pending = "sell"
        else:
            trail = np.nan
            if en[i]:
                pending = "buy"
        curve[i] = cash + qty * c[i]
        if qty > 0:
            trails[i] = trail
    return dict(curve=pd.Series(curve, df.index).dropna(), trades=trades, trails=pd.Series(trails, df.index),
                in_position=qty > 0, trail=None if np.isnan(trail) else float(trail), pending=pending)


def period_stats(df, entry, exit_, stop, cost=COST, start=None, end=None) -> dict:
    sim = simulate(df, entry, exit_, stop, cost, start, end)
    curve = sim["curve"]
    # una posición abierta se valúa como si se vendiera al último cierre del período
    last = df.close[curve.index[-1]]
    rets = [t.get("ret", last * (1 - cost) / (t["entry_price"] * (1 + cost)) - 1) for t in sim["trades"]]
    return stats(curve, np.array(rets), df.close[curve.index])


def run(df, entry, exit_, stop, cost=COST, start=None, end=None):
    return report(period_stats(df, entry, exit_, stop, cost, start, end))


def stats(curve, tr, close) -> dict:
    """Métricas de una curva de capital (base 1) y sus retornos por trade, contra comprar y mantener."""
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    daily = curve.resample("1D").last().pct_change().dropna()
    gains, losses = tr[tr > 0].sum(), -tr[tr <= 0].sum()
    return dict(cagr=curve.iloc[-1] ** (1 / yrs) - 1, max_dd=(curve / curve.cummax() - 1).min(),
                sharpe=daily.mean() / daily.std() * np.sqrt(365), trades=len(tr),
                win_rate=float(np.mean(tr > 0)) if len(tr) else float("nan"),
                profit_factor=gains / losses if losses else float("nan"),
                bh_cagr=(close.iloc[-1] / close.iloc[0]) ** (1 / yrs) - 1,
                bh_max_dd=(close / close.cummax() - 1).min())


def report(m: dict) -> str:
    return (f"CAGR {m['cagr']:6.1%}  MaxDD {m['max_dd']:6.1%}  Sharpe {m['sharpe']:4.2f}  "
            f"trades {m['trades']:3d}  aciertos {m['win_rate']:5.1%}  PF {m['profit_factor']:4.2f}  "
            f"| buy&hold CAGR {m['bh_cagr']:6.1%}  MaxDD {m['bh_max_dd']:6.1%}")


if __name__ == "__main__":
    periods = {"in-sample 2018-2022": ("2018-01-01", "2023-01-01"),
               "out-of-sample 2023-hoy": ("2023-01-01", None),
               "total 2018-hoy": ("2018-01-01", None)}
    for symbol in ("BTC/USDT", "ETH/USDT"):
        df = download(symbol)
        sig = signals(df, **PARAMS)
        print(f"\n{symbol} 4h  {PARAMS}")
        for name, (a, b) in periods.items():
            for cost in (COST, 2 * COST):
                print(f"  {name:23s} costo/lado {cost:.2%}  {run(df, *sig, cost=cost, start=a, end=b)}")
