"""Ronda 9: imán de liquidez, ¿el precio va a buscar el pool cercano? (HIPOTESIS.md, ronda 9).

Uso:
    python -m investigacion.ronda9              # período de investigación (sin holdout)
    python -m investigacion.ronda9 --holdout    # UNA sola corrida en el holdout, solo con lo que pasó

Dos fuentes de pools: L (liquidaciones estimadas con el open interest, reporte/liquidez.estimated_bins reconstruido hora
a hora) y S (máximos y mínimos sin barrer, reporte/liquidez.swing_groups con las velas hasta la decisión).
Imán = 100 × (atracción arriba − atracción abajo) / suma, atracción = tamaño del pool más cercano / distancia.

Prueba 1: qué pool toca primero el precio en H días, contra lo que da la geometría (caminos simulados con las velas de
1 h de los 30 días anteriores, sin tendencia). Prueba 2: las señales como operación de investigacion/estudio.py.

Escribe data/research/ronda9.csv, ronda9.json y ronda9_iman.csv (o ronda9_holdout.json).
"""

import argparse
import json

import numpy as np
import pandas as pd

from investigacion import estudio
from investigacion.estudio import DATA, HALVES, HOLDOUT, HORIZONS, SYMBOL, pct
from reporte import liquidez

BIN_PCT, MAX_DIST, LOOKBACK_D, POOL_Q = 0.5, 15.0, 30, 0.90
NULL_DAYS, NULL_PATHS = 30, 1000
SIG_HI, SIG_LO = 90, 10
MAXH = max(HORIZONS) * 24
START = {"L": "2020-01-01", "S": "2020-01-01"}


def _read(name: str, cols: list[str], until) -> pd.DataFrame:
    return estudio._read(name, cols, until)


# --- pools en cada decisión (cierre del día D = 00:00 UTC de D+1) ---------------------------------

def liq_pools(h: pd.DataFrame, until) -> pd.DataFrame:
    """Mapa estimado de liquidaciones (mismas fórmulas que liquidez.estimated_bins) hora a hora; al cierre de cada
    día, el tramo pool (USD en el 10 % más alto) más cercano de cada lado."""
    oi = _read(f"{SYMBOL}_metrics_5m.csv", ["open_interest"], until).open_interest.resample("1h").last()
    df = h[["high", "low", "close"]].join(oi.rename("oi"), how="inner").dropna()
    start = df.index[0] + pd.Timedelta(days=LOOKBACK_D)
    rows = []
    for t, close, px, btc, is_long, _ in liquidez.simulate_levels(df, LOOKBACK_D):
        if t.hour != 23 or t < start:  # la vela de las 23 h cierra el día
            continue
        width = close * BIN_PCT / 100
        m = np.abs(px / close - 1) * 100 <= MAX_DIST
        b = (pd.DataFrame({"bin": np.floor(px[m] / width), "long": is_long[m], "usd": btc[m] * px[m]})
             .groupby(["bin", "long"]).usd.sum().reset_index())
        b["price"] = (b.bin + 0.5) * width
        pools = b[b.usd >= b.usd.quantile(POOL_Q)] if len(b) else b
        up = pools[~pools.long & (pools.price > close)].sort_values("price")
        dn = pools[pools.long & (pools.price < close)].sort_values("price", ascending=False)
        rows.append({"day": t.normalize(), "price": close,
                     "up_px": up.price.iloc[0] if len(up) else np.nan, "up_size": up.usd.iloc[0] if len(up) else np.nan,
                     "dn_px": dn.price.iloc[0] if len(dn) else np.nan, "dn_size": dn.usd.iloc[0] if len(dn) else np.nan})
    return pd.DataFrame(rows).set_index("day")


def swing_pools(h: pd.DataFrame, until) -> pd.DataFrame:
    """Máximos y mínimos sin barrer (liquidez.swing_groups) con las velas cerradas al momento de cada decisión."""
    h4 = _read(f"{SYMBOL}_perp_4h.csv", ["high", "low", "close"], until)
    d1 = h[["high", "low", "close"]].resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    rows = []
    for day in d1.index[365:]:
        cut = day + pd.Timedelta(days=1)
        f4, f1 = h4[h4.index < cut].iloc[-90 * 6:], d1[d1.index <= day].iloc[-365:]
        price = f1.close.iloc[-1]
        g = liquidez.swing_groups({"4h": (f4, 6), "1d": (f1, 5)})
        up = [x for x in g["above"] if (x["ref"] / price - 1) * 100 <= MAX_DIST]
        dn = [x for x in g["below"] if (1 - x["ref"] / price) * 100 <= MAX_DIST]
        rows.append({"day": day, "price": price,
                     "up_px": up[0]["ref"] if up else np.nan, "up_size": up[0]["n"] if up else np.nan,
                     "dn_px": dn[0]["ref"] if dn else np.nan, "dn_size": dn[0]["n"] if dn else np.nan})
    return pd.DataFrame(rows).set_index("day")


def add_iman(p: pd.DataFrame) -> pd.DataFrame:
    p["d_up"] = (p.up_px / p.price - 1) * 100
    p["d_dn"] = (1 - p.dn_px / p.price) * 100
    a_up, a_dn = p.up_size / p.d_up, p.dn_size / p.d_dn
    p["iman"] = 100 * (a_up - a_dn) / (a_up + a_dn)
    p["iman_pct"] = pct(p.iman)
    return p


# --- primer toque: real y esperado por geometría --------------------------------------------------

def _first(lvl, hu, ld, up, dn):
    """Primer paso (1..T) en que se toca arriba / abajo; T + 1 si nunca. Las primeras dimensiones son de caminos."""
    T = hu.shape[-1]
    hit_up, hit_dn = lvl + hu >= up, lvl + ld <= dn
    fu = np.where(hit_up.any(-1), hit_up.argmax(-1) + 1, T + 1)
    fd = np.where(hit_dn.any(-1), hit_dn.argmax(-1) + 1, T + 1)
    return fu, fd


def _outcome(fu, fd, steps):
    """1 si arriba primero, 0 abajo, ½ si en la misma vela, nan si ninguno en `steps` velas."""
    hit = np.minimum(fu, fd) <= steps
    out = np.where(fu < fd, 1.0, np.where(fd < fu, 0.0, 0.5))
    return np.where(hit, out, np.nan)


def touches(p: pd.DataFrame, h: pd.DataFrame, seed: int = 9) -> pd.DataFrame:
    """Para cada día con pools de los dos lados: resultado real y esperado por geometría en cada H."""
    rng = np.random.default_rng(seed)
    lc = np.log(h.close)
    r = (lc - lc.shift()).to_numpy()
    hu = (np.log(h.high) - lc.shift()).to_numpy()
    ld = (np.log(h.low) - lc.shift()).to_numpy()
    idx = h.index
    out = []
    for day, row in p.dropna(subset=["iman"]).iterrows():
        t0 = idx.searchsorted(day + pd.Timedelta(days=1))  # primera vela después de la decisión
        if t0 < NULL_DAYS * 24 + 1:
            continue
        up, dn = np.log(row.up_px / row.price), np.log(row.dn_px / row.price)
        res = {"day": day}
        # real: el nivel de partida de cada vela es el cierre anterior (relativo al precio de la decisión)
        fut = slice(t0, min(t0 + MAXH, len(idx)))
        if fut.stop <= fut.start:
            continue
        lvl = np.concatenate([[0.0], np.cumsum(r[fut])[:-1]])
        fu, fd = _first(lvl, hu[fut], ld[fut], up, dn)
        # geometría: velas de los 30 días anteriores, sin la tendencia
        past = slice(t0 - NULL_DAYS * 24, t0)
        rp, hp, lp = r[past], hu[past], ld[past]
        ok = ~(np.isnan(rp) | np.isnan(hp) | np.isnan(lp))
        rp, hp, lp = rp[ok], hp[ok], lp[ok]
        mu = rp.mean()
        pick = rng.integers(0, len(rp), (NULL_PATHS, MAXH))
        rs, hs, ls = rp[pick] - mu, hp[pick] - mu, lp[pick] - mu
        lv = np.concatenate([np.zeros((NULL_PATHS, 1)), np.cumsum(rs, 1)[:, :-1]], 1)
        su, sd = _first(lv, hs, ls, up, dn)
        for hz in HORIZONS:
            steps = hz * 24
            if t0 + steps > len(idx):  # la ventana no entra en los datos (en investigación, corta en el holdout)
                res[f"obs_{hz}"] = res[f"exp_{hz}"] = np.nan
                continue
            res[f"obs_{hz}"] = float(_outcome(np.array(fu), np.array(fd), steps))
            sim = _outcome(su, sd, steps)
            res[f"exp_{hz}"] = np.nanmean(sim) if np.isfinite(sim).any() else np.nan
        out.append(res)
    t = pd.DataFrame(out).set_index("day")
    return p.join(t, how="inner")


# --- evaluación -----------------------------------------------------------------------------------

def _spaced(s: pd.Series, hz: int) -> pd.Series:
    return s[estudio._spaced(s.index, hz)]


def excess_stats(x: pd.Series, hz: int) -> dict:
    """x: exceso por evento (a favor del lado señalado). Media, t con eventos separados H días."""
    x = x.dropna()
    ind = _spaced(x, hz)
    se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 2 else np.nan
    return {"n": len(x), "n_ind": len(ind), "exceso": x.mean() * 100 if len(x) else np.nan,
            "t": x.mean() / se if se and se > 0 else np.nan}


def side_excess(t: pd.DataFrame, side: int, hz: int) -> pd.Series:
    """Arriba primero: obs − esperado; abajo primero: lo opuesto. En puntos de probabilidad (fracción)."""
    return side * (t[f"obs_{hz}"] - t[f"exp_{hz}"])


def signal(t: pd.DataFrame, side: int) -> pd.Series:
    return t.iman_pct >= SIG_HI if side > 0 else t.iman_pct <= SIG_LO


def prueba1(tabs: dict) -> pd.DataFrame:
    rows = []
    for src, t in tabs.items():
        for side in (+1, -1):
            for hz in HORIZONS:
                x = side_excess(t, side, hz)[signal(t, side)]
                tot = excess_stats(x[x.index >= START[src]], hz)
                r = {"fuente": src, "lado": "arriba" if side > 0 else "abajo", "H": hz, **tot}
                for k, (a, b) in HALVES.items():
                    s = excess_stats(x[(x.index >= a) & (x.index < b)], hz)
                    r[f"exceso {k[:1]}"], r[f"n_ind {k[:1]}"] = s["exceso"], s["n_ind"]
                ea, eb = r["exceso A"], r["exceso B"]
                r["pasa"] = bool(pd.notna(ea) and pd.notna(eb) and ea > 0 and eb > 0
                                 and pd.notna(r["t"]) and r["t"] >= 2 and r["n_ind"] >= 20)
                rows.append(r)
    return pd.DataFrame(rows)


def prueba2(tabs: dict) -> pd.DataFrame:
    d = estudio.daily_frame()
    rows = []
    for src, t in tabs.items():
        for side in (+1, -1):
            sig = signal(t, side).reindex(d.index, fill_value=False).astype(bool)
            for hz in HORIZONS:
                tot = estudio.evaluate(d, sig, side, hz, START[src], str(HOLDOUT.date()))
                r = {"fuente": src, "lado": "largo" if side > 0 else "corto", "H": hz, "n": tot["n"],
                     "n_ind": tot["n_ind"], "media_%": tot["mean"], "base_%": tot["base"],
                     "exceso_%": tot["excess"], "t": tot["t"]}
                for k, (a, b) in HALVES.items():
                    r[f"exceso {k[:1]}"] = estudio.evaluate(d, sig, side, hz, a, b)["excess"]
                ea, eb = r["exceso A"], r["exceso B"]
                r["pasa"] = bool(pd.notna(ea) and pd.notna(eb) and ea > 0 and eb > 0
                                 and pd.notna(r["t"]) and r["t"] >= 2 and r["n_ind"] >= 20)
                rows.append(r)
    return pd.DataFrame(rows)


def calibracion(tabs: dict) -> dict:
    """Se informa: todos los días con imán, por quintil, y el más cercano sin tamaño."""
    out = {}
    for src, t in tabs.items():
        o = {}
        for hz in HORIZONS:
            obs, exp = t[f"obs_{hz}"], t[f"exp_{hz}"]
            q = pd.qcut(t.iman, 5, labels=False, duplicates="drop")
            near_up = t.d_up < t.d_dn  # el más cercano es el de arriba
            sgn = np.where(near_up, 1, -1)
            o[f"H{hz}"] = {
                "sin_toque_%": round(float(obs.isna().mean() * 100), 1),
                "todos": excess_stats(obs - exp, hz),
                "por_quintil_iman": {int(k): {"obs_arriba_%": round(float(g[f"obs_{hz}"].mean() * 100), 1),
                                              "esperado_%": round(float(g[f"exp_{hz}"].mean() * 100), 1),
                                              "n": int(g[f"obs_{hz}"].notna().sum())}
                                     for k, g in t.groupby(q)},
                "mas_cercano_primero": {
                    "obs_%": round(float(np.nanmean(np.where(near_up, obs, 1 - obs)) * 100), 1),
                    "esperado_%": round(float(np.nanmean(np.where(near_up, exp, 1 - exp)) * 100), 1),
                    **excess_stats(pd.Series(sgn * (obs - exp), index=t.index), hz)},
            }
        out[src] = o
    return out


def build(until) -> dict:
    h = _read(f"{SYMBOL}_perp_1h.csv", ["open", "high", "low", "close"], until)
    pools = {"L": liq_pools(h, until), "S": swing_pools(h, until)}
    return {src: touches(add_iman(p), h) for src, p in pools.items()}


def _fmt(df: pd.DataFrame) -> str:
    return df.round(2).to_string(index=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout", action="store_true")
    holdout = ap.parse_args().holdout
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)

    if not holdout:
        tabs = build(HOLDOUT)
        p1, p2 = prueba1(tabs), prueba2(tabs)
        cal = calibracion(tabs)
        pd.concat([t.assign(fuente=s) for s, t in tabs.items()]).to_csv(DATA / "ronda9_iman.csv")
        pd.concat([p1.assign(prueba=1), p2.assign(prueba=2)]).to_csv(DATA / "ronda9.csv", index=False)
        (DATA / "ronda9.json").write_text(json.dumps(
            {"prueba1": p1.to_dict("records"), "prueba2": p2.to_dict("records"), "calibracion": cal},
            indent=1, ensure_ascii=False, default=float))
        for s, t in tabs.items():
            print(f"fuente {s}: {t.iman.notna().sum()} días con imán, desde {t.index[0].date()}")
        print("\nPrueba 1 · primer toque contra la geometría (exceso en puntos de probabilidad, a favor del lado)")
        print(_fmt(p1))
        print(f"Pasan {int(p1.pasa.sum())} de {len(p1)} (~{len(p1) / 20:.1f} por azar).")
        print("\nPrueba 2 · operable (exceso % contra la base del mismo lado, neto de costos)")
        print(_fmt(p2))
        print(f"Pasan {int(p2.pasa.sum())} de {len(p2)}.")
        print("\nCalibración (se informa)")
        print(json.dumps(cal, indent=1, ensure_ascii=False, default=float))
        return

    passed = pd.DataFrame(json.loads((DATA / "ronda9.json").read_text())["prueba1"])
    passed = passed[passed.pasa]
    if passed.empty:
        print("Ninguna combinación pasó la prueba 1: no hay holdout que correr.")
        return
    tabs = build(None)
    rows = []
    for r in passed.itertuples():
        t = tabs[r.fuente]
        side = 1 if r.lado == "arriba" else -1
        x = side_excess(t, side, r.H)[signal(t, side)]
        s = excess_stats(x[x.index >= HOLDOUT], r.H)
        rows.append({"fuente": r.fuente, "lado": r.lado, "H": r.H, **s,
                     "pasa": bool(pd.notna(s["t"]) and s["exceso"] > 0 and s["t"] >= 1.5)})
    out = pd.DataFrame(rows)
    (DATA / "ronda9_holdout.json").write_text(json.dumps(out.to_dict("records"), indent=1, default=float))
    print(_fmt(out))


if __name__ == "__main__":
    main()
