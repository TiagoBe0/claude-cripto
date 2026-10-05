"""Ronda 4 de investigacion/HIPOTESIS.md: emisión de stablecoins (reglas fijadas antes de correrla).

Uso:
    python -m investigacion.ronda4          # imprime la tabla y escribe data/research/ronda4.csv

Mismo motor que la ronda 1 (estudio.py): perpetuo a 1x, costo y funding reales, mitades A y B,
nunca usa datos desde el holdout.
"""

import sys

import pandas as pd

from investigacion.estudio import DATA, HALVES, HOLDOUT, evaluate, pct, daily_frame

HORIZONS = (7, 14, 30)


def stable_supply(until: pd.Timestamp = HOLDOUT) -> pd.Series:
    """Oferta de USDT + USDC en unidades, indexada por la fecha de DefiLlama.

    stablecoins_1d.csv guarda cada fila con la fecha de DefiLlama menos un día (convención del pipeline:
    el dato de las 00:00 cierra el día anterior). Acá se vuelve a la fecha original y se usa recién al
    cierre de ese día: la decisión del día D solo ve datos fechados <= D (un día de demora a propósito).
    """
    s = pd.read_csv(DATA / "stablecoins_1d.csv", usecols=["timestamp", "usdt", "usdc"])
    s.index = pd.to_datetime(s.pop("timestamp"), unit="ms") + pd.Timedelta(days=1)
    s = s[s.index < until]
    supply = (s.usdt + s.usdc).asfreq("1D")  # calendario completo: los huecos quedan NaN, no se corren fechas
    return supply


def signals(d: pd.DataFrame, supply: pd.Series) -> dict[tuple[str, int], pd.Series]:
    sup = supply.reindex(d.index)
    net1 = pct((supply - supply.shift(1)).reindex(d.index))
    net30 = pct((supply - supply.shift(30)).reindex(d.index))
    net7 = pct((supply - supply.shift(7)).reindex(d.index))
    ch7 = d.close / d.close.shift(7) - 1
    s = {
        ("S1 día de emisión fuerte", +1): net1 >= 95,
        ("S1 día de emisión fuerte", -1): net1 <= 5,
        ("S2 régimen de liquidez", +1): net30 >= 80,
        ("S2 régimen de liquidez", -1): net30 <= 20,
        ("S3 pólvora seca", +1): (net7 >= 80) & (ch7 <= 0),
        ("S3 pólvora seca", -1): (net7 <= 20) & (ch7 >= 0),
    }
    return {k: (v & sup.notna()).fillna(False).astype(bool) for k, v in s.items()}


def run() -> pd.DataFrame:
    d = daily_frame(horizons=HORIZONS)
    supply = stable_supply()
    rows = []
    for (name, side), sig in signals(d, supply).items():
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


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    t = run()
    t.to_csv(DATA / "ronda4.csv", index=False)
    print(t.round(2).to_string(index=False))
    print(f"\nPasan {int(t.pasa.sum())} de {len(t)} pruebas. Con {len(t)} pruebas se esperan ~{len(t) / 20:.1f} por azar.")
    sys.exit(0)
