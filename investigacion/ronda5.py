"""Ronda 5 de investigacion/HIPOTESIS.md: liquidez y market makers (reglas fijadas antes de correrla).

Uso:
    python -m investigacion.ronda5          # imprime la tabla y escribe data/research/ronda5.csv

L1 mide volatilidad (variable propia, base por tercil de volatilidad pasada); L2 y L3 son reglas de
trading con el motor de la ronda 1 (estudio.py). Nunca usa datos desde el holdout.
"""

import sys

import numpy as np
import pandas as pd

from investigacion.estudio import (DATA, HALVES, HOLDOUT, SYMBOL, _read, _spaced, daily_frame, evaluate, pct)

HORIZONS = (1, 3, 7)
START = "2020-01-01"


def hourly() -> pd.DataFrame:
    h = _read(f"{SYMBOL}_perp_1h.csv", ["close", "quote_volume"])
    h["r"] = np.log(h.close).diff()
    # Amihud: cuánto movió el precio cada dólar operado en la hora (horas sin volumen: sin dato)
    h["illiq"] = h.r.abs() / h.quote_volume.where(h.quote_volume > 0)
    return h


def add_liquidity(d: pd.DataFrame, h: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    d["illiq"] = h.illiq.resample("1D").mean().reindex(d.index)
    d["illiq_pct"] = pct(d.illiq)
    # volatilidad horaria: pasada = 7 días que terminan al cierre de D; futura = los H días siguientes
    rv7 = h.r.rolling(7 * 24, min_periods=7 * 24 - 6).std().resample("1D").last()
    d["rv_past"] = rv7.reindex(d.index)
    d["rv_past_tercile"] = pd.cut(pct(d.rv_past), [-np.inf, 100 / 3, 200 / 3, np.inf], labels=False)
    for hz in HORIZONS:
        fut = h.r.rolling(hz * 24, min_periods=hz * 24 - 2).std().resample("1D").last().shift(-hz)
        d[f"vol_{hz}"] = np.log(fut.reindex(d.index) / d.rv_past)
    return d


def evaluate_vol(d: pd.DataFrame, sig: pd.Series, hz: int, a: str, b: str) -> dict:
    """Exceso de log(vol futura / vol pasada) contra los días del mismo tercil de volatilidad pasada."""
    w = (d.index >= a) & (d.index < b)
    y = d[f"vol_{hz}"].where(w)
    adj = y - y.groupby(d.rv_past_tercile).transform("mean")
    ev = adj[sig & y.notna() & d.rv_past_tercile.notna()].dropna()
    if len(ev) < 3:
        return {"n": len(ev), "n_ind": len(ev), "excess": np.nan, "t": np.nan}
    ind = adj[_spaced(ev.index, hz)]
    se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 2 else np.nan
    return {"n": len(ev), "n_ind": len(ind), "excess": ev.mean() * 100, "t": ev.mean() / se if se else np.nan}


def signals(d: pd.DataFrame) -> dict:
    ip = d.illiq_pct
    ch1 = d.close / d.close.shift(1) - 1
    ch3 = d.close / d.close.shift(3) - 1
    back = (ip.shift(1).rolling(3).max() >= 90) & (ip <= 50)
    s = {("L1 libro fino → volatilidad", +1): ip >= 80,
         ("L1 libro fino → volatilidad", -1): ip <= 20,
         ("L2 reversión sin liquidez", +1): (ip >= 90) & (ch1 < 0),
         ("L2 reversión sin liquidez", -1): (ip >= 90) & (ch1 > 0),
         ("L3 vuelven los bots", +1): back & (ch3 < 0),
         ("L3 vuelven los bots", -1): back & (ch3 > 0)}
    return {k: v.fillna(False).astype(bool) for k, v in s.items()}


def run() -> pd.DataFrame:
    d = add_liquidity(daily_frame(horizons=HORIZONS), hourly())
    rows = []
    for (name, side), sig in signals(d).items():
        vol = name.startswith("L1")
        for hz in HORIZONS:
            if vol:
                tot = evaluate_vol(d, sig, hz, START, str(HOLDOUT.date()))
                halves = {k: evaluate_vol(d, sig, hz, a, b) for k, (a, b) in HALVES.items()}
                # L1: ≥ p80 se espera más volatilidad (+), ≤ p20 menos (−)
                good = lambda x: pd.notna(x) and x * side > 0  # noqa: E731
                lado = "iliquidez ≥ p80 (+)" if side > 0 else "iliquidez ≤ p20 (−)"
                r = {"hipotesis": name, "lado": lado, "H": hz, "n": tot["n"], "n_ind": tot["n_ind"],
                     "exceso": tot["excess"], "t": tot["t"]}
                ok_t = pd.notna(tot["t"]) and tot["t"] * side >= 2
            else:
                tot = evaluate(d, sig, side, hz, START, str(HOLDOUT.date()))
                halves = {k: evaluate(d, sig, side, hz, a, b) for k, (a, b) in HALVES.items()}
                good = lambda x: pd.notna(x) and x > 0  # noqa: E731
                r = {"hipotesis": name, "lado": "largo" if side > 0 else "corto", "H": hz, "n": tot["n"],
                     "n_ind": tot["n_ind"], "exceso": tot["excess"], "t": tot["t"]}
                ok_t = pd.notna(tot["t"]) and tot["t"] >= 2
            for k, v in halves.items():
                r[f"exceso {k[:1]}"] = v["excess"]
                r[f"n_ind {k[:1]}"] = v["n_ind"]
            r["pasa"] = bool(good(r["exceso A"]) and good(r["exceso B"]) and ok_t and tot["n_ind"] >= 20)
            rows.append(r)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    t = run()
    t.to_csv(DATA / "ronda5.csv", index=False)
    print("exceso: L1 en puntos de log(vol futura / vol pasada) × 100 (≈ % más o menos volatilidad que días con la")
    print("misma volatilidad pasada); L2 y L3 en % de retorno neto de costos y funding sobre la base.\n")
    print(t.round(2).to_string(index=False))
    print(f"\nPasan {int(t.pasa.sum())} de {len(t)} pruebas. Con {len(t)} pruebas se esperan ~{len(t) / 20:.1f} por azar.")
    sys.exit(0)
