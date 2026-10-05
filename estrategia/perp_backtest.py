"""Investigación: estrategias en el perpetuo BTCUSDT (long/short, con apalancamiento).

Uso:
    python estrategia/perp_backtest.py       # baja o actualiza perp 4h y funding desde 2019 en data/backtest/

Convenciones (las mismas que backtest.py):
- Señal al cierre de la vela t (velas spot, que tienen historia desde 2017 para calentar la EMA)
  -> orden a mercado en la apertura de t+1 sobre el perpetuo.
- Exposición = valor nocional / capital, con signo (+ largo, - corto). Se fija al entrar y no se
  rebalancea: el apalancamiento efectivo se mueve con el precio.
- Stop trailing por ATR fijado al cierre de t, activo en t+1; se ejecuta a min/max(open, stop).
- Funding cada 8 h (00, 08, 16 UTC, coincide con aperturas de vela de 4 h): el largo paga
  exposición × tasa si la tasa es positiva, el corto la cobra.
- Liquidación: si en la vela el capital al peor precio cae bajo el margen de mantenimiento,
  se pierde todo. Con apalancamiento <= 3x y stop de 4 ATR no debería pasar, pero se verifica.
- Costo por lado sobre el nocional operado = taker perp 0,05 % + slippage 0,05 %.
"""

import sys
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import futures_data  # noqa: E402
from estrategia.backtest import DATA, download, rma  # noqa: E402

COST = 0.0010  # por lado, sobre el nocional
MAINT_MARGIN = 0.004
PERP_START = "2019-09-08"  # primera vela del perpetuo BTCUSDT
BARS_PER_YEAR = 6 * 365


def download_perp() -> tuple[pd.DataFrame, pd.Series]:
    """Velas 4h del perpetuo y funding, incrementales en data/backtest/."""
    ex = ccxt.binance({"enableRateLimit": True})
    start = ex.parse8601(f"{PERP_START}T00:00:00Z")
    kp, fp = DATA / "BTCUSDT_perp_4h.csv", DATA / "BTCUSDT_funding.csv"
    try:
        futures_data.update_klines(ex, "fapiPublicGetKlines", "BTCUSDT", "4h", kp, start,
                                   futures_data.PERP_COLUMNS, futures_data.perp_fields)
        futures_data.update_funding(ex, "BTCUSDT", fp, start)
    except ccxt.NetworkError as e:
        print(f"perp: no se pudo actualizar ({e}), uso los CSV guardados")
    return load_perp(kp, fp)


def load_perp(klines_csv: Path, funding_csv: Path) -> tuple[pd.DataFrame, pd.Series]:
    """Velas del perpetuo (OHLC) y funding por apertura de vela de 4 h, índices UTC sin zona."""
    k = pd.read_csv(klines_csv)
    k.index = pd.to_datetime(k.timestamp, unit="ms")
    f = pd.read_csv(funding_csv)
    # fundingTime viene a veces con algunos ms de más: se lleva a la apertura de la vela
    f.index = pd.to_datetime(f.timestamp, unit="ms").dt.floor("4h")
    return k[["open", "high", "low", "close"]], f.funding_rate.groupby(level=0).sum()


def indicators(spot: pd.DataFrame, ema_len=1200, don_len=120, atr_len=14, vol_days=30) -> pd.DataFrame:
    c, h, l = spot.close, spot.high, spot.low
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return pd.DataFrame({
        "close": c,
        "trend": c.ewm(span=ema_len, adjust=False, min_periods=ema_len).mean(),
        "don_high": h.rolling(don_len).max().shift(),
        "don_low": l.rolling(don_len).min().shift(),
        "atr": rma(tr, atr_len),
        # volatilidad realizada anualizada de 30 días
        "rv": np.log(c / pc).rolling(vol_days * 6).std() * np.sqrt(BARS_PER_YEAR),
    })


def simulate(perp: pd.DataFrame, funding: pd.Series, ind: pd.DataFrame, *, shorts: bool,
             target_vol: float | None, max_lev: float, atr_mult=4.0, cost=COST,
             funding_cap: float | None = None, start=None, end=None) -> dict:
    """target_vol None = exposición fija max_lev; si no, |exposición| = min(max_lev, target_vol / rv).

    funding_cap: no abrir largos si el funding medio de 7 días anualizado supera este valor
    (ni cortos si está por debajo de -funding_cap): posicionamiento muy cargado de un lado.
    """
    df = perp.join(ind.drop(columns="close"), how="inner")
    df["sig_close"] = ind.close.reindex(df.index)
    fr = funding.reindex(df.index).fillna(0.0)
    f7 = funding.rolling(21).mean().reindex(df.index).ffill() * 3 * 365
    if start:
        df, fr, f7 = (x[x.index >= pd.Timestamp(start)] for x in (df, fr, f7))
    if end:
        df, fr, f7 = (x[x.index < pd.Timestamp(end)] for x in (df, fr, f7))

    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    sc, trend, dh, dl, atr, rv = (df[k].to_numpy() for k in ("sig_close", "trend", "don_high", "don_low", "atr", "rv"))
    fr, f7 = fr.to_numpy(), f7.to_numpy()

    eq, q, trail, pending = 1.0, 0.0, np.nan, None  # q = BTC con signo
    curve, trades, liquidations = np.empty(len(df)), [], 0
    turnover = funding_paid = 0.0

    def fill(i, price, new_q, reason):
        nonlocal eq, q, turnover
        dq = new_q - q
        eq -= abs(dq) * price * cost
        turnover += abs(dq) * price
        if q != 0 and trades:
            t = trades[-1]
            t.update(exit_time=df.index[i], exit_price=price, exit_reason=reason,
                     ret_pct=round(np.sign(q) * (price / t["entry_price"] - 1) * t["lev"] * 100, 2))
        if new_q != 0:
            trades.append(dict(entry_time=df.index[i], entry_price=price, side="long" if new_q > 0 else "short",
                               lev=round(abs(new_q) * price / eq, 2)))
        q = new_q

    for i in range(len(df)):
        # 1. funding a las 00/08/16 UTC: lo paga la posición que venía de la vela anterior
        if q != 0 and fr[i] != 0:
            pay = q * o[i] * fr[i]
            eq -= pay
            funding_paid += pay
        # 2. orden pendiente en la apertura (se ejecuta después del funding)
        if pending is not None:
            x, reason = pending
            fill(i, o[i], x * eq / o[i] if x else 0.0, reason)
            pending, trail = None, np.nan
        # 3. liquidación y stop dentro de la vela
        if q != 0:
            worst = l[i] if q > 0 else h[i]
            if eq + q * (worst - o[i]) <= MAINT_MARGIN * abs(q) * worst:
                liquidations += 1
                eq, q, trail = 0.0, 0.0, np.nan
                trades[-1].update(exit_time=df.index[i], exit_price=worst, exit_reason="liquidación", ret_pct=-100.0)
            elif not np.isnan(trail) and (l[i] <= trail if q > 0 else h[i] >= trail):
                px = min(o[i], trail) if q > 0 else max(o[i], trail)
                eq += q * (px - o[i])  # el resto de la vela ya no afecta al capital
                fill(i, px, 0.0, "stop ATR")
                trail = np.nan
                curve[i] = eq
                # sin posición: puede evaluar entrada al cierre
                pending = _entry(i, sc, trend, dh, dl, rv, f7, shorts, target_vol, max_lev, funding_cap)
                continue
        # 4. resultado de la vela
        eq += q * (c[i] - o[i])
        curve[i] = eq
        if eq <= 0:
            curve[i:] = 0.0
            break
        # 5. señales al cierre
        if q != 0:
            stop = sc[i] - atr_mult * atr[i] if q > 0 else sc[i] + atr_mult * atr[i]
            # el stop se calcula sobre la vela spot y se aplica en el perp: se corre por la base actual
            stop += c[i] - sc[i]
            trail = stop if np.isnan(trail) else (max(trail, stop) if q > 0 else min(trail, stop))
            if (q > 0 and sc[i] < trend[i]) or (q < 0 and sc[i] > trend[i]):
                pending = (0.0, "cruce EMA")
        else:
            pending = _entry(i, sc, trend, dh, dl, rv, f7, shorts, target_vol, max_lev, funding_cap)

    curve = pd.Series(curve, df.index)
    return dict(curve=curve, trades=trades, liquidations=liquidations, funding_paid=funding_paid,
                turnover=turnover, q=q, trail=None if np.isnan(trail) else float(trail), pending=pending,
                index=df.index)


def _entry(i, sc, trend, dh, dl, rv, f7, shorts, target_vol, max_lev, funding_cap):
    if np.isnan(trend[i]) or np.isnan(rv[i]):
        return None
    size = max_lev if target_vol is None else min(max_lev, target_vol / rv[i])
    if sc[i] > dh[i] and sc[i] > trend[i] and not (funding_cap is not None and f7[i] > funding_cap):
        return (size, "ruptura")
    if shorts and sc[i] < dl[i] and sc[i] < trend[i] and not (funding_cap is not None and f7[i] < -funding_cap):
        return (-size, "ruptura")
    return None


def stats(curve: pd.Series) -> dict:
    yrs = (curve.index[-1] - curve.index[0]).days / 365.25
    daily = curve.resample("1D").last().pct_change().dropna()
    dd = (curve / curve.cummax() - 1).min()
    cagr = curve.iloc[-1] ** (1 / yrs) - 1 if curve.iloc[-1] > 0 else -1.0
    return dict(cagr=cagr, dd=dd, sharpe=daily.mean() / daily.std() * np.sqrt(365),
                vol=daily.std() * np.sqrt(365), calmar=cagr / -dd if dd < 0 else np.nan)


def fmt(s: dict, extra: str = "") -> str:
    return (f"CAGR {s['cagr']:7.1%}  MaxDD {s['dd']:6.1%}  Sharpe {s['sharpe']:5.2f}  "
            f"Calmar {s['calmar']:5.2f}  vol {s['vol']:5.1%}{extra}")


def simulate_target(perp: pd.DataFrame, funding: pd.Series, target: pd.Series, *, band=0.25,
                    cost=COST, start=None, end=None) -> dict:
    """Exposición objetivo continua (calculada al cierre de t, ejecutada en la apertura de t+1).

    Solo se rebalancea si la exposición actual se aleja más de `band` del objetivo (o el objetivo
    es 0), para no pagar costos por cambios chicos.
    """
    df = perp.join(target.rename("x"), how="inner")
    fr = funding.reindex(df.index).fillna(0.0)
    if start:
        df, fr = df[df.index >= pd.Timestamp(start)], fr[fr.index >= pd.Timestamp(start)]
    if end:
        df, fr = df[df.index < pd.Timestamp(end)], fr[fr.index < pd.Timestamp(end)]
    o, h, l, c, x = (df[k].to_numpy() for k in ("open", "high", "low", "close", "x"))
    fr = fr.to_numpy()
    eq, q, want = 1.0, 0.0, None
    curve, liquidations, funding_paid, turnover, n_rebal = np.empty(len(df)), 0, 0.0, 0.0, 0
    for i in range(len(df)):
        if q != 0 and fr[i] != 0:  # funding antes de la orden de la apertura, como en simulate()
            funding_paid += q * o[i] * fr[i]
            eq -= q * o[i] * fr[i]
        if want is not None:
            new_q = want * eq / o[i]
            eq -= abs(new_q - q) * o[i] * cost
            turnover += abs(new_q - q) * o[i]
            q, want, n_rebal = new_q, None, n_rebal + 1
        if q != 0:
            worst = l[i] if q > 0 else h[i]
            if eq + q * (worst - o[i]) <= MAINT_MARGIN * abs(q) * worst:
                liquidations += 1
                curve[i:] = 0.0
                break
        eq += q * (c[i] - o[i])
        curve[i] = eq
        cur = q * c[i] / eq
        tgt = 0.0 if np.isnan(x[i]) else x[i]
        if (tgt == 0 and q != 0) or (tgt != 0 and abs(cur - tgt) > band):
            want = tgt
    return dict(curve=pd.Series(curve, df.index), liquidations=liquidations, funding_paid=funding_paid,
                turnover=turnover, rebalances=n_rebal, q=q, pending=want, index=df.index)


def target_trend_vol(ind: pd.DataFrame, target_vol=0.5, max_lev=2.0) -> pd.Series:
    """F: largo mientras el cierre está sobre la EMA 1200 (~200 días), tamaño = objetivo de volatilidad."""
    size = (target_vol / ind.rv).clip(upper=max_lev)
    return size.where(ind.close > ind.trend, 0.0).where(ind.trend.notna())


def target_tsmom(spot: pd.DataFrame, ind: pd.DataFrame, target_vol=0.5, max_lev=2.0) -> pd.Series:
    """G: momentum de serie temporal: promedio de sign(retorno) a 30, 90 y 180 días, en [0, 1]
    (solo largo), por el objetivo de volatilidad."""
    c = spot.close
    score = sum((c / c.shift(d * 6) > 1).astype(float) for d in (30, 90, 180)) / 3
    return (score * (target_vol / ind.rv).clip(upper=max_lev)).where(c.shift(180 * 6).notna())


VARIANTS = {
    "A long 1x":                    dict(shorts=False, target_vol=None, max_lev=1.0),
    "B long/short 1x":              dict(shorts=True, target_vol=None, max_lev=1.0),
    "C long, vol 50% (máx 3x)":     dict(shorts=False, target_vol=0.50, max_lev=3.0),
    "D long/short, vol 50% (3x)":   dict(shorts=True, target_vol=0.50, max_lev=3.0),
    "E = D + filtro funding 30%":   dict(shorts=True, target_vol=0.50, max_lev=3.0, funding_cap=0.30),
}
PERIODS = {"in-sample 2019-10 a 2022": ("2019-10-01", "2023-01-01"),
           "out-of-sample 2023-hoy": ("2023-01-01", None)}


if __name__ == "__main__":
    spot = download("BTC/USDT")
    perp, funding = download_perp()
    ind = indicators(spot)
    for pname, (a, b) in PERIODS.items():
        bh = perp.close[(perp.index >= a) & ((perp.index < b) if b else True)]
        print(f"\n{pname}")
        print(f"  {'buy & hold spot':30s} {fmt(stats(bh / bh.iloc[0]))}")
        for vname, p in VARIANTS.items():
            for cost in (COST, 2 * COST):
                r = simulate(perp, funding, ind, cost=cost, start=a, end=b, **p)
                tr = [t for t in r["trades"] if "ret_pct" in t]
                wins = np.mean([t["ret_pct"] > 0 for t in tr]) if tr else np.nan
                print(f"  {vname:30s} {fmt(stats(r['curve']))}  trades {len(r['trades']):3d}  "
                      f"aciertos {wins:4.0%}  funding {r['funding_paid']:+.3f}  liq {r['liquidations']}"
                      f"  costo {cost:.2%}")
        for vname, tgt in (("F EMA + vol 50% (máx 2x)", target_trend_vol(ind)),
                           ("G tsmom + vol 50% (máx 2x)", target_tsmom(spot, ind))):
            for cost in (COST, 2 * COST):
                r = simulate_target(perp, funding, tgt, cost=cost, start=a, end=b)
                print(f"  {vname:30s} {fmt(stats(r['curve']))}  rebal {r['rebalances']:3d}  "
                      f"funding {r['funding_paid']:+.3f}  liq {r['liquidations']}  costo {cost:.2%}")
