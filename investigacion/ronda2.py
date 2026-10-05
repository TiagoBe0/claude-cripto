"""Ronda 2: estrategia de posicionamiento contrario combinado (reglas en HIPOTESIS.md, ronda 2).

Uso:
    python -m investigacion.ronda2              # período de investigación (sin holdout)
    python -m investigacion.ronda2 --holdout    # UNA sola corrida en el holdout, con todo cerrado

Simula hora por hora sobre el perpetuo: decisiones al cierre diario (00:00 UTC), stop de catástrofe con
los máximos y mínimos de cada vela de 1 h, funding cobrado cada 8 h a la posición abierta y 0,10 % por
lado sobre el nocional.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from investigacion import estudio
from investigacion.estudio import DATA, SYMBOL, pct

COST_SIDE = 0.001
RULES = dict(p=90, hold=3, thr=1, stop=0.10)
HALF_B = "2023-01-01"
HOLDOUT = estudio.HOLDOUT


def score(d: pd.DataFrame, p: float = 90) -> pd.Series:
    """Puntaje de −3 a +3: + multitud corta (ir largo), − multitud larga (ir corto)."""
    fp, rp = pct(d.funding_3d), pct(d.retail_ls)
    top3 = d.top_ls - d.top_ls.shift(3)
    lo, hi = 100 - p, p
    parts = [
        (fp <= lo).astype(int) - (fp >= hi).astype(int),
        ((rp <= lo) & (top3 > 0)).astype(int) - ((rp >= hi) & (top3 < 0)).astype(int),
        (d.premium_z <= -2).astype(int) - (d.premium_z >= 2).astype(int),
    ]
    s = sum(parts)
    # Solo días con las tres señales calculables (percentiles con historia suficiente)
    valid = fp.notna() & rp.notna() & top3.notna() & d.premium_z.notna()
    return s.where(valid)


def load_hourly(until: pd.Timestamp | None) -> tuple[pd.DataFrame, pd.Series]:
    h = pd.read_csv(DATA / f"{SYMBOL}_perp_1h.csv", usecols=["timestamp", "open", "high", "low", "close"])
    h.index = pd.to_datetime(h.pop("timestamp"), unit="ms")
    f = pd.read_csv(DATA / f"{SYMBOL}_funding.csv", usecols=["timestamp", "funding_rate"])
    f.index = pd.DatetimeIndex(pd.to_datetime(f.pop("timestamp"), unit="ms")).floor("h")
    f = f.funding_rate.groupby(level=0).last()
    if until is not None:
        h, f = h[h.index < until], f[f.index < until]
    return h, f


def simulate(h: pd.DataFrame, f: pd.Series, sc: pd.Series, start, end, hold: int, thr: int, stop: float) -> dict:
    """Devuelve curva horaria de capital (base 1) y trades."""
    h = h[(h.index >= start) & (h.index < end)]
    o, hi, lo, c = (h[k].to_numpy() for k in ("open", "high", "low", "close"))
    t = h.index
    fund = f.reindex(t).to_numpy()
    # Decisión a las 00:00 de D+1 con el puntaje del día D
    dec = sc.copy()
    dec.index = dec.index + pd.Timedelta(days=1)
    dec = dec.reindex(t)
    # Todas las 00:00 son decisión: un día sin puntaje cuenta como "sin señal" (el plazo sigue corriendo)
    dec = dec.where(~((t.hour == 0) & dec.isna()), 0).to_numpy()

    eq, side, qty, entry, left, stop_px = 1.0, 0, 0.0, 0.0, 0, np.nan
    curve, trades = np.empty(len(t)), []

    def close_pos(i, price, reason):
        nonlocal eq, side, qty
        pnl = side * qty * (price - entry) - COST_SIDE * qty * price
        eq += pnl
        trades[-1].update(exit_time=t[i], exit_price=price, reason=reason, ret=eq / trades[-1]["eq0"] - 1)
        side, qty = 0, 0.0

    def open_pos(i, s, price):
        nonlocal eq, side, qty, entry, left, stop_px
        side, entry, left = s, price, hold
        qty = eq / price
        eq -= COST_SIDE * qty * price
        stop_px = price * (1 - stop) if s > 0 else price * (1 + stop)
        trades.append({"entry_time": t[i], "side": s, "entry_price": price, "eq0": eq + COST_SIDE * qty * price})

    for i in range(len(t)):
        px_open = c[i - 1] if i else o[i]  # cierre anterior = precio de la decisión de las 00:00
        # 1. Funding a la posición abierta en ese instante (antes de cualquier cambio de las 00:00)
        if side and not np.isnan(fund[i]):
            eq -= side * qty * px_open * fund[i]
        # 2. Decisión diaria
        if not np.isnan(dec[i]):
            want = 1 if dec[i] >= thr else -1 if dec[i] <= -thr else 0
            if side:
                left -= 1
                if want == side:
                    left = hold
                elif want == -side:
                    close_pos(i, px_open, "señal contraria")
                elif left <= 0:
                    close_pos(i, px_open, "plazo")
            if not side and want:
                open_pos(i, want, px_open)
        # 3. Stop de catástrofe dentro de la vela
        if side > 0 and lo[i] <= stop_px:
            close_pos(i, min(o[i], stop_px), "stop")
        elif side < 0 and hi[i] >= stop_px:
            close_pos(i, max(o[i], stop_px), "stop")
        curve[i] = eq + (side * qty * (c[i] - entry) if side else 0.0)
    if side:  # posición abierta al final: se valúa al último cierre
        close_pos(len(t) - 1, c[-1], "fin del período")
    return {"curve": pd.Series(curve, t), "trades": trades}


def metrics(curve: pd.Series, trades: list, close: pd.Series) -> dict:
    daily = curve.resample("1D").last().dropna()
    r = daily.pct_change().dropna()
    yrs = (daily.index[-1] - daily.index[0]).days / 365.25
    rets = np.array([x["ret"] for x in trades if "ret" in x])
    held = sum(((x["exit_time"] - x["entry_time"]).total_seconds() for x in trades if "exit_time" in x))
    span = (curve.index[-1] - curve.index[0]).total_seconds()
    bh = close[(close.index >= curve.index[0]) & (close.index <= curve.index[-1])]
    bh_d = bh.resample("1D").last().dropna()
    bh_r = bh_d.pct_change().dropna()
    both = pd.concat([r, bh_r], axis=1, join="inner")
    mix = both.mean(axis=1)  # 50 % estrategia + 50 % comprar y mantener, rebalanceo diario
    sh = lambda x: float(x.mean() / x.std() * np.sqrt(365)) if x.std() > 0 else float("nan")
    return {"ret_total": float(daily.iloc[-1] / daily.iloc[0] - 1), "cagr": float((daily.iloc[-1] / daily.iloc[0]) ** (1 / yrs) - 1),
            "sharpe": sh(r), "max_dd": float((daily / daily.cummax() - 1).min()), "trades": int(len(rets)),
            "win_rate": float((rets > 0).mean()) if len(rets) else float("nan"),
            "exposure": held / span if span else float("nan"),
            "bh_ret_total": float(bh_d.iloc[-1] / bh_d.iloc[0] - 1), "bh_sharpe": sh(bh_r),
            "bh_max_dd": float((bh_d / bh_d.cummax() - 1).min()),
            "corr_btc": float(both.corr().iloc[0, 1]), "mix_sharpe": sh(mix)}


def fmt(m: dict) -> str:
    return (f"ret {m['ret_total']:+7.1%}  CAGR {m['cagr']:+6.1%}  Sharpe {m['sharpe']:5.2f}  MaxDD {m['max_dd']:6.1%}  "
            f"trades {m['trades']:3d}  aciertos {m['win_rate']:5.1%}  en mercado {m['exposure']:5.1%}  "
            f"| BTC ret {m['bh_ret_total']:+7.1%} Sharpe {m['bh_sharpe']:5.2f} MaxDD {m['bh_max_dd']:6.1%}  "
            f"| corr {m['corr_btc']:+.2f}  50/50 Sharpe {m['mix_sharpe']:5.2f}")


def run(holdout: bool) -> dict:
    d = estudio.daily_frame(until=None if holdout else HOLDOUT)
    h, f = load_hourly(None if holdout else HOLDOUT)
    close = h.close
    out = {"rules": RULES}
    sc = score(d, RULES["p"])
    first = sc.first_valid_index() + pd.Timedelta(days=1)
    if holdout:
        periods = {"holdout": (HOLDOUT, h.index[-1] + pd.Timedelta(hours=1))}
    else:
        periods = {"A": (first, pd.Timestamp(HALF_B)), "B": (pd.Timestamp(HALF_B), HOLDOUT), "total": (first, HOLDOUT)}
    out["periods"] = {}
    for name, (a, b) in periods.items():
        sim = simulate(h, f, sc, a, b, RULES["hold"], RULES["thr"], RULES["stop"])
        m = metrics(sim["curve"], sim["trades"], close)
        out["periods"][name] = {"from": str(a.date()), "to": str(b.date()), **m}
        print(f"{name:8s} {a.date()} → {b.date()}  {fmt(m)}")
        if name in ("total", "holdout"):
            sides = pd.Series([x["side"] for x in sim["trades"]])
            reasons = pd.Series([x.get("reason") for x in sim["trades"]]).value_counts().to_dict()
            print(f"         largos {int((sides > 0).sum())} · cortos {int((sides < 0).sum())} · salidas {reasons}")
    if not holdout:
        print("\nRobustez (Sharpe en la mitad B; la grilla no elige, muestra estabilidad):")
        grid = {}
        for p in (80, 90, 95):
            s = score(d, p)
            row = []
            for hold in (1, 3, 7):
                m = metrics(*simulate(h, f, s, pd.Timestamp(HALF_B), HOLDOUT, hold, RULES["thr"], RULES["stop"]).values(), close)
                row.append(m["sharpe"])
                grid[f"p{p}_h{hold}"] = m["sharpe"]
            print(f"  percentil {p}: " + "  ".join(f"plazo {hh} d {x:5.2f}" for hh, x in zip((1, 3, 7), row)))
        out["grid_B"] = grid
        A, B, T = (out["periods"][k] for k in ("A", "B", "total"))
        checks = {
            "1 retorno > 0 y Sharpe >= 0,5 en A y B": all(x["ret_total"] > 0 and x["sharpe"] >= 0.5 for x in (A, B)),
            "2 trades >= 30 y caída < 35 %": T["trades"] >= 30 and T["max_dd"] > -0.35,
            "3 robustez: >= 6 de 9 con Sharpe > 0 en B": sum(v > 0 for v in grid.values()) >= 6,
        }
    else:
        H = out["periods"]["holdout"]
        checks = {"holdout: retorno > 0, Sharpe > 0, caída < 35 %": H["ret_total"] > 0 and H["sharpe"] > 0 and H["max_dd"] > -0.35}
    out["checks"] = checks
    print()
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'} {k}")
    print(f"\n{'PASA' if all(checks.values()) else 'NO PASA'}")
    (DATA / ("ronda2_holdout.json" if holdout else "ronda2.json")).write_text(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="una sola corrida en el holdout")
    run(ap.parse_args().holdout)
