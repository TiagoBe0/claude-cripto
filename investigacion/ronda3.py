"""Ronda 3: BTC siempre largo con tamaño gestionado por volatilidad (reglas en HIPOTESIS.md, ronda 3).

Uso:
    python -m investigacion.ronda3              # período de investigación (sin holdout)
    python -m investigacion.ronda3 --holdout    # UNA sola corrida en el holdout, con todo cerrado

Simula hora por hora sobre el perpetuo: el capital se mueve con qty × Δprecio, el funding se cobra cada 8 h sobre
el nocional abierto, cada rebalanceo paga 0,10 % del nocional operado y se verifica liquidación con el mínimo de
cada vela.

Comparación: comprar y mantener el perpetuo **a 1x**, rebalanceando con la misma banda (paga el mismo funding y los
mismos costos). La primera versión lo simulaba sin rebalancear nunca: el funding se comía el margen, la exposición
subía sola (1,42x de promedio en 2020-2022) y terminaba liquidado en mayo de 2021. Eso no es "a 1x" (rival de paja),
así que se corrigió antes de juzgar. Se informa también BTC spot (sin funding ni costos).
"""

import argparse
import json

import numpy as np
import pandas as pd

from investigacion.estudio import DATA, HOLDOUT
from investigacion.ronda2 import load_hourly

COST_SIDE = 0.001
MAINT = 0.005  # margen de mantenimiento aproximado de Binance para este tamaño
RULES = dict(window=30, target=0.50, max_lev=1.5, band=0.10)
HALVES = {"A": ("2020-01-01", "2023-01-01"), "B": ("2023-01-01", str(HOLDOUT.date()))}


def target_exposure(close_h: pd.Series, window: int, target: float, max_lev: float) -> pd.Series:
    """Exposición objetivo decidida al cierre de cada día D (rige desde las 00:00 de D+1)."""
    daily = close_h.resample("1D").last()
    vol = np.log(daily).diff().rolling(window).std() * np.sqrt(365)
    return (target / vol).clip(upper=max_lev)


def simulate(h: pd.DataFrame, f: pd.Series, expo: pd.Series | None, start, end, band: float) -> dict:
    """expo = None: comprar y mantener a 1x (entra en la primera vela y no rebalancea)."""
    h = h[(h.index >= start) & (h.index < end)]
    o, lo, c = h.open.to_numpy(), h.low.to_numpy(), h.close.to_numpy()
    t = h.index
    fund = f.reindex(t).to_numpy()
    if expo is not None:
        dec = expo.copy()
        dec.index = dec.index + pd.Timedelta(days=1)
        dec = dec.reindex(t).to_numpy()
    eq, qty, traded, liquidated = 1.0, 0.0, 0.0, False
    curve, expos = np.empty(len(t)), np.full(len(t), np.nan)
    for i in range(len(t)):
        px = c[i - 1] if i else o[i]
        if qty and not np.isnan(fund[i]):
            eq -= qty * px * fund[i]
        want = None
        if expo is None:
            want = 1.0 if qty == 0 else None
        elif not np.isnan(dec[i]):
            cur = qty * px / eq if eq > 0 else 0.0
            if qty == 0 or abs(dec[i] - cur) > band:
                want = dec[i]
        if want is not None and eq > 0:
            dq = want * eq / px - qty
            eq -= COST_SIDE * abs(dq) * px
            traded += abs(dq) * px
            qty += dq
        # Liquidación: capital al mínimo de la vela contra el margen de mantenimiento
        if qty > 0 and eq + qty * (lo[i] - px) <= MAINT * qty * lo[i]:
            eq, qty, liquidated = 0.0, 0.0, True
        eq += qty * (c[i] - px)
        curve[i] = eq
        expos[i] = qty * c[i] / eq if eq > 0 else 0.0
        if liquidated:
            curve[i:] = 0.0
            break
    return {"curve": pd.Series(curve, t), "expo": pd.Series(expos, t), "traded": traded, "liquidated": liquidated}


def spot_metrics(close: pd.Series) -> dict:
    daily = close.resample("1D").last().dropna()
    r = daily.pct_change().dropna()
    return {"ret_total": float(daily.iloc[-1] / daily.iloc[0] - 1),
            "sharpe": float(r.mean() / r.std() * np.sqrt(365)), "max_dd": float((daily / daily.cummax() - 1).min())}


def metrics(sim: dict) -> dict:
    daily = sim["curve"].resample("1D").last().dropna()
    r = daily.pct_change().dropna()
    yrs = (daily.index[-1] - daily.index[0]).days / 365.25
    return {"ret_total": float(daily.iloc[-1] / daily.iloc[0] - 1),
            "cagr": float((daily.iloc[-1] / daily.iloc[0]) ** (1 / yrs) - 1) if daily.iloc[-1] > 0 else -1.0,
            "sharpe": float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else float("nan"),
            "max_dd": float((daily / daily.cummax() - 1).min()), "avg_expo": float(sim["expo"].mean()),
            "turnover_per_year": float(sim["traded"] / yrs), "liquidated": sim["liquidated"]}


def fmt(m: dict) -> str:
    return (f"ret {m['ret_total']:+8.1%}  CAGR {m['cagr']:+6.1%}  Sharpe {m['sharpe']:5.2f}  MaxDD {m['max_dd']:6.1%}  "
            f"exposición media {m['avg_expo']:4.2f}x  rotación {m['turnover_per_year']:4.1f}x/año"
            + ("  LIQUIDADA" if m["liquidated"] else ""))


def run(holdout: bool) -> dict:
    h, f = load_hourly(None if holdout else HOLDOUT)
    expo = target_exposure(h.close, RULES["window"], RULES["target"], RULES["max_lev"])
    one = pd.Series(1.0, index=expo.index)  # comprar y mantener a 1x: misma simulación, exposición objetivo fija
    periods = {"holdout": (HOLDOUT, h.index[-1] + pd.Timedelta(hours=1))} if holdout else \
        {k: (pd.Timestamp(a), pd.Timestamp(b)) for k, (a, b) in HALVES.items()}
    out = {"rules": RULES, "periods": {}}
    for name, (a, b) in periods.items():
        vt, bh = metrics(simulate(h, f, expo, a, b, RULES["band"])), metrics(simulate(h, f, one, a, b, RULES["band"]))
        naive = metrics(simulate(h, f, None, a, b, 0))
        spot = spot_metrics(h.close[(h.index >= a) & (h.index < b)])
        out["periods"][name] = {"from": str(a.date()), "to": str(b.date()), "vol_target": vt, "buy_hold_perp": bh,
                                "buy_hold_perp_sin_rebalanceo": naive, "spot": spot}
        print(f"{name:8s} {a.date()} → {b.date()}")
        print(f"   volatilidad gestionada     {fmt(vt)}")
        print(f"   perp 1x (rebalanceado)     {fmt(bh)}")
        print(f"   perp sin rebalancear       {fmt(naive)}   (rival de paja, no cuenta)")
        print(f"   BTC spot                   ret {spot['ret_total']:+8.1%}  Sharpe {spot['sharpe']:5.2f}  MaxDD {spot['max_dd']:6.1%}")
    P = out["periods"]
    if holdout:
        H = P["holdout"]
        checks = {"holdout: Sharpe >= comprar y mantener y caída menor":
                  H["vol_target"]["sharpe"] >= H["buy_hold_perp"]["sharpe"] and H["vol_target"]["max_dd"] > H["buy_hold_perp"]["max_dd"]}
    else:
        print("\nRobustez: Sharpe por ventana (filas) y objetivo (columnas); entre paréntesis, comprar y mantener")
        wins = {}
        for name, (a, b) in periods.items():
            bh_sh = P[name]["buy_hold_perp"]["sharpe"]
            n = 0
            print(f"  mitad {name} (comprar y mantener {bh_sh:.2f}):")
            for w in (14, 30, 60):
                row = []
                for tg in (0.4, 0.5, 0.6):
                    m = metrics(simulate(h, f, target_exposure(h.close, w, tg, RULES["max_lev"]), a, b, RULES["band"]))
                    row.append(m["sharpe"])
                    n += m["sharpe"] > bh_sh
                print(f"    ventana {w:2d} d: " + "  ".join(f"obj {int(tg * 100)} % {x:5.2f}" for tg, x in zip((0.4, 0.5, 0.6), row)))
            wins[name] = n
        out["grid_wins"] = wins
        checks = {
            "1 en A y B: Sharpe mayor y caída menor que comprar y mantener": all(
                P[k]["vol_target"]["sharpe"] > P[k]["buy_hold_perp"]["sharpe"]
                and P[k]["vol_target"]["max_dd"] > P[k]["buy_hold_perp"]["max_dd"] for k in ("A", "B")),
            "2 robustez: >= 6 de 9 mejor que comprar y mantener en A y en B": all(v >= 6 for v in wins.values()),
        }
    out["checks"] = checks
    print()
    for k, v in checks.items():
        print(f"  {'✓' if v else '✗'} {k}")
    print(f"\n{'PASA' if all(checks.values()) else 'NO PASA'}")
    (DATA / ("ronda3_holdout.json" if holdout else "ronda3.json")).write_text(json.dumps(out, indent=2, default=str))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true", help="una sola corrida en el holdout")
    run(ap.parse_args().holdout)
