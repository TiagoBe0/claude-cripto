"""Estudio de eventos de las hipótesis de investigacion/HIPOTESIS.md (reglas fijadas antes de mirar).

Uso:
    python -m investigacion.estudio          # imprime la tabla y escribe data/research/estudio.csv

Nunca usa datos desde HOLDOUT: ni para señales ni para el precio de salida de un trade.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "research"
SYMBOL = "BTCUSDT"
HOLDOUT = pd.Timestamp("2025-10-05")
HALVES = {"A 2020-2022": ("2020-01-01", "2023-01-01"), "B 2023-2025": ("2023-01-01", "2025-10-05")}
HORIZONS = (1, 3, 7)
COST = 0.002  # ida y vuelta
PCT_WINDOW, PCT_MIN = 365, 180


def _read(name: str, cols: list[str]) -> pd.DataFrame:
    df = pd.read_csv(DATA / name, usecols=["timestamp", *cols])
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms")
    return df[df.index < HOLDOUT]


def pct(s: pd.Series) -> pd.Series:
    """Percentil (0-100) del valor de hoy entre los últimos 365 días, hoy incluido."""
    return s.rolling(PCT_WINDOW, min_periods=PCT_MIN).rank(pct=True) * 100


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / down)


def daily_frame() -> pd.DataFrame:
    """Una fila por día D con lo conocido al cierre de D (00:00 UTC de D+1)."""
    h = _read(f"{SYMBOL}_perp_1h.csv", ["close", "volume", "taker_buy_volume"])
    full = h.close.resample("1D").count() == 24
    d = pd.DataFrame({"close": h.close.resample("1D").last()})
    d["taker_share_24h"] = h.taker_buy_volume.resample("1D").sum() / h.volume.resample("1D").sum()

    f = _read(f"{SYMBOL}_funding.csv", ["funding_rate"]).funding_rate
    f.index = f.index.floor("h")  # a veces vienen con +1 ms
    # Funding conocido al cierre de D: cobros con hora <= D+1 00:00
    known = f.copy()
    known.index = known.index - pd.Timedelta(microseconds=1)  # el cobro de las 00:00 de D+1 cae en D
    d["funding_3d"] = known.rolling("3D").mean().resample("1D").last()

    p = _read(f"{SYMBOL}_premium_1h.csv", ["premium_close"]).premium_close
    m24 = p.resample("1D").mean()
    d["premium_z"] = (m24 - m24.rolling(30, min_periods=20).mean()) / m24.rolling(30, min_periods=20).std()

    mp = DATA / f"{SYMBOL}_metrics_5m.csv"
    if mp.exists():
        m = _read(mp.name, ["open_interest", "global_account_ls_ratio", "top_trader_position_ls_ratio"])
        md = m.resample("1D").last()
        d["oi"] = md.open_interest
        d["retail_ls"] = md.global_account_ls_ratio
        d["top_ls"] = md.top_trader_position_ls_ratio

    # Funding pagado por un largo entre el cierre de D y el de D+H: cobros en (D+1 00:00, D+1+H 00:00]
    fd = f.copy()
    fd.index = fd.index - pd.Timedelta(microseconds=1)
    fday = fd.resample("1D").sum().reindex(d.index, fill_value=0.0)
    for hz in HORIZONS:
        d[f"ret_{hz}"] = d.close.shift(-hz) / d.close - 1
        d[f"fund_{hz}"] = fday[::-1].rolling(hz).sum()[::-1].shift(-1)
        # el trade cierra en D + H: si eso cae en el holdout o más allá de los datos, no cuenta
        # (el cierre del día D + H es a las 00:00 de D + H + 1, que tiene que ser <= HOLDOUT)
        exit_day = d.index + pd.Timedelta(days=hz)
        d.loc[exit_day > HOLDOUT - pd.Timedelta(days=1), [f"ret_{hz}", f"fund_{hz}"]] = np.nan
    # Recién ahora se sacan los días incompletos: los retornos ya se midieron sobre el calendario entero
    return d[full.reindex(d.index, fill_value=False)]


def signals(d: pd.DataFrame) -> dict[tuple[str, int], pd.Series]:
    """(hipótesis, lado) -> serie booleana. Lado +1 largo, -1 corto. Reglas de HIPOTESIS.md."""
    s = {}
    fp = pct(d.funding_3d)
    s[("H1 funding extremo", -1)] = fp >= 90
    s[("H1 funding extremo", +1)] = fp <= 10
    s[("H2 premium extremo", -1)] = d.premium_z >= 2
    s[("H2 premium extremo", +1)] = d.premium_z <= -2
    ch3 = d.close / d.close.shift(3) - 1
    if "oi" in d:
        oip = pct(d.oi / d.oi.shift(3) - 1)
        s[("H3 squeeze OI", +1)] = (oip >= 80) & (ch3 < 0)
        s[("H3 squeeze OI", -1)] = (oip >= 80) & (ch3 > 0) & (fp >= 80)
        rp = pct(d.retail_ls)
        top3 = d.top_ls - d.top_ls.shift(3)
        s[("H4 retail vs top", -1)] = (rp >= 90) & (top3 < 0)
        s[("H4 retail vs top", +1)] = (rp <= 10) & (top3 > 0)
    tp = pct(d.taker_share_24h)
    ch1 = d.close / d.close.shift(1) - 1
    s[("H5 absorción", -1)] = (tp >= 90) & (ch1 <= 0)
    s[("H5 absorción", +1)] = (tp <= 10) & (ch1 >= 0)
    s[("H6 fuerza RSI", +1)] = rsi(d.close) > 65
    return {k: v.fillna(False).astype(bool) for k, v in s.items()}


def _spaced(idx: pd.DatetimeIndex, h: int) -> pd.DatetimeIndex:
    keep, last = [], None
    for t in idx:
        if last is None or (t - last).days >= h:
            keep.append(t)
            last = t
    return pd.DatetimeIndex(keep)


def evaluate(d: pd.DataFrame, sig: pd.Series, side: int, hz: int, a: str, b: str) -> dict:
    w = (d.index >= a) & (d.index < b)
    net = side * d[f"ret_{hz}"] - COST - side * d[f"fund_{hz}"]
    base = net[w].dropna()
    ev = net[w & sig.to_numpy()].dropna()
    out = {"n": len(ev), "base": base.mean() * 100 if len(base) else np.nan}
    if len(ev) < 3:
        return {**out, "n_ind": len(ev), "mean": np.nan, "excess": np.nan, "t": np.nan, "pos": np.nan}
    ind = net[_spaced(ev.index, hz)]
    excess = ev.mean() - base.mean()
    se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 2 else np.nan
    return {**out, "n_ind": len(ind), "mean": ev.mean() * 100, "excess": excess * 100,
            "t": excess / se if se and se > 0 else np.nan, "pos": (ev > 0).mean() * 100}


def run() -> pd.DataFrame:
    d = daily_frame()
    rows = []
    for (name, side), sig in signals(d).items():
        for hz in HORIZONS:
            r = {"hipotesis": name, "lado": "largo" if side > 0 else "corto", "H": hz}
            tot = evaluate(d, sig, side, hz, "2020-01-01", str(HOLDOUT.date()))
            halves = {k: evaluate(d, sig, side, hz, a, b) for k, (a, b) in HALVES.items()}
            r.update({"n": tot["n"], "n_ind": tot["n_ind"], "media_%": tot["mean"], "base_%": tot["base"],
                      "exceso_%": tot["excess"], "t": tot["t"], "acierto_%": tot["pos"]})
            for k, v in halves.items():
                r[f"exceso {k[:1]}"] = v["excess"]
                r[f"n {k[:1]}"] = v["n"]
            ea, eb = r["exceso A"], r["exceso B"]
            r["pasa"] = bool(pd.notna(ea) and pd.notna(eb) and ea > 0 and eb > 0
                             and pd.notna(tot["t"]) and tot["t"] >= 2 and tot["n_ind"] >= 20)
            rows.append(r)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    t = run()
    t.to_csv(DATA / "estudio.csv", index=False)
    print(t.round(2).to_string(index=False))
    print(f"\nPasan {int(t.pasa.sum())} de {len(t)} pruebas. Con {len(t)} pruebas se esperan ~{len(t) / 20:.1f} por azar.")
    sys.exit(0)
