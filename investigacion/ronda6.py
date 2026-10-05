"""Ronda 6 de investigacion/HIPOTESIS.md: medias móviles proyectadas (reglas fijadas antes de correrla).

Uso:
    python -m investigacion.ronda6          # imprime las tablas y escribe data/research/ronda6.csv

Medias sobre cierres diarios spot (velas de 4 h de Binance desde 2017, data/BTCUSDT_4h.csv), proyectadas con
el precio quieto en el cierre del día. Mismo motor que la ronda 1 (estudio.py): perpetuo a 1x, costo y funding
reales, mitades A y B, nunca usa datos desde el holdout.
"""

import sys

import numpy as np
import pandas as pd

from investigacion.estudio import DATA, HALVES, HOLDOUT, ROOT, evaluate, pct, daily_frame

HORIZONS = (7, 14, 30)
CROSS_BARS = 20


def spot_daily(until: pd.Timestamp = HOLDOUT) -> pd.Series:
    """Cierre diario spot: el de la última vela de 4 h del día, como la vela diaria de Binance.

    Algunos días de mantenimiento de Binance (2018-2020) tienen menos de 6 velas de 4 h; se usan igual
    (exigir las 6 dejaría la SMA 200 vacía 200 días después de cada hueco). Decidido antes de mirar retornos.
    """
    h = pd.read_csv(ROOT / "data" / "BTCUSDT_4h.csv", usecols=["timestamp", "close"])
    h.index = pd.to_datetime(h.pop("timestamp"), unit="ms")
    h = h[h.index < until].close
    return h.resample("1D").last().asfreq("1D")


def oldest_mean(close: pd.Series, n: int, k: int) -> pd.Series:
    """Promedio de los k cierres más viejos de la ventana de n que termina hoy (los que salen en k días)."""
    return close.shift(n - k).rolling(k).mean()


def projected_change(close: pd.Series, n: int, k: int) -> pd.Series:
    """Cambio de la SMA n en k días con el precio quieto, en fracción de la SMA de hoy (exacto)."""
    sma = close.rolling(n).mean()
    return k * (close - oldest_mean(close, n, k)) / n / sma


def announced_cross(close: pd.Series, k: int = CROSS_BARS) -> tuple[pd.Series, pd.Series]:
    """Primer día en que, con el precio quieto, la SMA 50 cruza la 200 dentro de k días (dorado, muerte)."""
    s50, s200 = close.rolling(50).mean(), close.rolling(200).mean()
    below = s50 < s200
    golden = pd.Series(False, index=close.index)
    death = pd.Series(False, index=close.index)
    out50 = out200 = 0.0
    for j in range(1, k + 1):
        out50 = out50 + close.shift(50 - j)  # cierres que salen de cada ventana hasta el día j
        out200 = out200 + close.shift(200 - j)
        diff = (s50 + (j * close - out50) / 50) - (s200 + (j * close - out200) / 200)
        golden |= below & (diff > 0)
        death |= ~below & (diff < 0)
    valid = s200.notna() & close.shift(199).notna()
    golden, death = golden & valid, death & valid
    return golden & ~golden.shift(fill_value=False), death & ~death.shift(fill_value=False)


def real_cross(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    above = close.rolling(50).mean() > close.rolling(200).mean()
    ok = close.rolling(200).mean().notna() & close.rolling(200).mean().shift().notna()
    return ok & above & ~above.shift(fill_value=True), ok & ~above & above.shift(fill_value=False)


def signals(close: pd.Series) -> dict[tuple[str, int], pd.Series]:
    m1 = pct(projected_change(close, 200, 20))
    m2 = pct(projected_change(close, 50, 10))
    golden, death = announced_cross(close)
    return {
        ("M1 SMA 200 va a subir", +1): m1 >= 80,
        ("M1 SMA 200 va a subir", -1): m1 <= 20,
        ("M2 SMA 50 va a subir", +1): m2 >= 80,
        ("M2 SMA 50 va a subir", -1): m2 <= 20,
        ("M3 cruce anunciado", +1): golden,
        ("M3 cruce anunciado", -1): death,
    }


def informative(close: pd.Series) -> dict[tuple[str, int], pd.Series]:
    """Comparaciones que se informan sin decidir: momentum simple y cruces reales."""
    r190 = pct(close / close.shift(190) - 1)
    r45 = pct(close / close.shift(45) - 1)
    golden, death = real_cross(close)
    return {
        ("ref M1: retorno 190 d", +1): r190 >= 80,
        ("ref M1: retorno 190 d", -1): r190 <= 20,
        ("ref M2: retorno 45 d", +1): r45 >= 80,
        ("ref M2: retorno 45 d", -1): r45 <= 20,
        ("ref M3: cruce real", +1): golden,
        ("ref M3: cruce real", -1): death,
    }


def table(d: pd.DataFrame, sigs: dict[tuple[str, int], pd.Series]) -> pd.DataFrame:
    rows = []
    for (name, side), sig in sigs.items():
        sig = sig.reindex(d.index).fillna(False).astype(bool)
        for hz in HORIZONS:
            r = {"hipotesis": name, "lado": "largo" if side > 0 else "corto", "H": hz}
            tot = evaluate(d, sig, side, hz, "2020-01-01", str(HOLDOUT.date()))
            halves = {k: evaluate(d, sig, side, hz, a, b) for k, (a, b) in HALVES.items()}
            r.update({"n": tot["n"], "n_ind": tot["n_ind"], "media_%": tot["mean"], "base_%": tot["base"],
                      "exceso_%": tot["excess"], "t": tot["t"], "acierto_%": tot["pos"]})
            for k, v in halves.items():
                r[f"exceso {k[:1]}"] = v["excess"]
                r[f"n_ind {k[:1]}"] = v["n_ind"]
            ea, eb = r["exceso A"], r["exceso B"]
            r["pasa"] = bool(pd.notna(ea) and pd.notna(eb) and ea > 0 and eb > 0
                             and pd.notna(tot["t"]) and tot["t"] >= 2 and tot["n_ind"] >= 20)
            rows.append(r)
    return pd.DataFrame(rows)


def cross_followthrough(close: pd.Series) -> pd.DataFrame:
    """De los cruces anunciados en el período de investigación, cuántos se dieron de verdad en 20 días."""
    golden, death = announced_cross(close)
    rg, rd = real_cross(close)
    rows = []
    for name, ann, real in (("dorado", golden, rg), ("muerte", death, rd)):
        days = ann[(ann.index >= "2020-01-01") & (ann.index < HOLDOUT) & ann].index
        hits = [real[(real.index > t) & (real.index <= t + pd.Timedelta(days=CROSS_BARS))].any() for t in days]
        lead = [((real[(real.index > t) & real].index.min() - t).days) for t, h in zip(days, hits) if h]
        rows.append({"cruce": name, "anunciados": len(days), "se dieron en 20 d": int(sum(hits)),
                     "anticipación mediana (días)": float(np.median(lead)) if lead else np.nan})
    return pd.DataFrame(rows)


def run() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    d = daily_frame(horizons=HORIZONS)
    close = spot_daily()
    return table(d, signals(close)), table(d, informative(close)), cross_followthrough(close)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    t, ref, fol = run()
    t.to_csv(DATA / "ronda6.csv", index=False)
    print(t.round(2).to_string(index=False))
    print(f"\nPasan {int(t.pasa.sum())} de {len(t)} pruebas. Con {len(t)} pruebas se esperan ~{len(t) / 20:.1f} por azar.")
    print("\nSe informa, sin decidir: momentum simple y cruces reales con las mismas reglas")
    print(ref.drop(columns="pasa").round(2).to_string(index=False))
    print("\nCruces anunciados (precio quieto, 20 días) que terminaron en cruce real")
    print(fol.to_string(index=False))
    sys.exit(0)
