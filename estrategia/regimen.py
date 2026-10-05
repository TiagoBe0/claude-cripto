"""Lectura de régimen: en qué estado está BTC hoy y qué pasó después en días parecidos.

Uso:
    python estrategia/regimen.py              # imprime la lectura (usa data/)

build_dashboard.py la escribe en data/dashboard/regime.json.

Todo se mide sobre velas diarias al cierre (00:00 UTC), armadas con las de 4h spot desde 2017:

- Factores con historia desde 2017: tendencia (cierre vs SMA 200 d), momentum (retorno 30 d),
  RSI 14 d, volatilidad realizada 30 d (percentil frente al último año, así no mira el futuro)
  y distancia al máximo histórico.
- Factores con historia corta (desde 2024, lo que baja el extractor): Fear & Greed, funding
  anualizado y cambio de open interest en 7 días. Pocos casos: se muestran, pero valen menos.
- Para cada factor, los días con el MISMO estado que hoy y el retorno de BTC 7 y 30 días después,
  comprando al cierre. Se compara contra la base (todos los días): BTC subió mucho desde 2017,
  así que casi cualquier estado "gana"; lo que importa es la diferencia con la base.
- t: diferencia con la base / error estándar, con días separados al menos el horizonte (si no,
  una misma racha cuenta 30 veces y la t sale inflada).
- Se sostiene: la diferencia tiene el mismo signo en 2018-2022 y en 2023-hoy, y |t| >= 2.
- Días parecidos: los que comparten el estado de hoy en todos los factores de historia larga; si
  son menos de MIN_SIMILAR, se van soltando factores desde el último de la lista.

Es descriptivo, no una señal: un estado que "se sostiene" en el pasado puede dejar de hacerlo.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

SYMBOL = "BTCUSDT"
HORIZONS = (7, 30)
MIN_SIMILAR = 40
HALVES = (("2018-2022", "2018-01-01", "2023-01-01"), ("2023-hoy", "2023-01-01", None))


def daily_candles(data_dir: Path) -> pd.DataFrame:
    df = pd.read_csv(data_dir / f"{SYMBOL}_4h.csv")
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms")
    d = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    # Solo días completos (las 6 velas de 4h): el último puede estar en curso
    full = df.close.resample("1D").count() == 6
    return d[full].dropna()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / down)


def _bucket(value: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(value, [-np.inf, *edges, np.inf], labels=labels, right=False).astype(object).where(value.notna())


def features(data_dir: Path, d: pd.DataFrame) -> list[dict]:
    """Cada factor: clave, nombre, estado por día (texto) y valor numérico de hoy."""
    c = d.close
    out = []
    sma = c.rolling(200).mean()
    out.append({"key": "trend", "label": "Tendencia (SMA 200 d)", "long": True,
                "state": np.where(sma.isna(), None, np.where(c > sma, "sobre la media", "debajo de la media")),
                "value": (c / sma - 1) * 100, "unit": "% vs media"})
    mom = (c / c.shift(30) - 1) * 100
    out.append({"key": "momentum", "label": "Momentum 30 d", "long": True,
                "state": _bucket(mom, [-10, 10], ["cae > 10 %", "entre −10 y +10 %", "sube > 10 %"]),
                "value": mom, "unit": "% en 30 d"})
    rsi = _rsi(c)
    out.append({"key": "rsi", "label": "RSI 14 d", "long": True,
                "state": _bucket(rsi, [35, 65], ["bajo (< 35)", "medio", "alto (> 65)"]), "value": rsi, "unit": ""})
    rv = np.log(c).diff().rolling(30).std() * np.sqrt(365) * 100
    pct = rv.rolling(365, min_periods=180).rank(pct=True) * 100
    out.append({"key": "vol", "label": "Volatilidad 30 d (vs último año)", "long": True,
                "state": _bucket(pct, [33.3, 66.7], ["baja", "media", "alta"]), "value": rv, "unit": "% anual"})
    dd = (c / c.cummax() - 1) * 100
    out.append({"key": "ath", "label": "Distancia al máximo histórico", "long": True,
                "state": _bucket(dd, [-30, -10], ["más de 30 % abajo", "entre 10 y 30 % abajo", "a menos de 10 %"]),
                "value": dd, "unit": "%"})

    # Historia corta: lo que baja el extractor desde start_date
    fng_path = data_dir / "fear_greed.csv"
    if fng_path.exists():
        f = pd.read_csv(fng_path, usecols=["timestamp", "value"])
        f = f.set_index(pd.to_datetime(f.timestamp, unit="ms")).value.reindex(d.index)
        out.append({"key": "fng", "label": "Fear & Greed", "long": False,
                    "state": _bucket(f, [25, 45, 56, 76], ["miedo extremo", "miedo", "neutral", "codicia",
                                                           "codicia extrema"]), "value": f, "unit": ""})
    fund_path = data_dir / f"{SYMBOL}_funding.csv"
    if fund_path.exists():
        fr = pd.read_csv(fund_path, usecols=["timestamp", "funding_rate"])
        fr = fr.set_index(pd.to_datetime(fr.timestamp, unit="ms")).funding_rate
        # Funding de los últimos 7 días, anualizado (3 cobros por día)
        ann = fr.rolling("7D").mean() * 3 * 365 * 100
        ann = ann.resample("1D").last().reindex(d.index)
        out.append({"key": "funding", "label": "Funding 7 d (anualizado)", "long": False,
                    "state": _bucket(ann, [0, 15], ["negativo", "entre 0 y 15 %", "alto (> 15 %)"]),
                    "value": ann, "unit": "% anual"})
    met_path = data_dir / f"{SYMBOL}_metrics_5m.csv"
    if met_path.exists():
        m = pd.read_csv(met_path, usecols=["timestamp", "open_interest_usd"])
        oi = m.set_index(pd.to_datetime(m.timestamp, unit="ms")).open_interest_usd.resample("1D").last()
        ch = ((oi / oi.shift(7) - 1) * 100).reindex(d.index)
        out.append({"key": "oi", "label": "Open interest 7 d", "long": False,
                    "state": _bucket(ch, [-5, 5], ["baja > 5 %", "estable", "sube > 5 %"]), "value": ch, "unit": "%"})
    for f in out:
        f["state"] = pd.Series(f["state"], index=d.index, dtype=object)
    return out


def _spaced(idx: pd.DatetimeIndex, h: int) -> pd.DatetimeIndex:
    """Días separados al menos h días entre sí (el primero de cada racha)."""
    keep, last = [], None
    for t in idx:
        if last is None or (t - last).days >= h:
            keep.append(t)
            last = t
    return pd.DatetimeIndex(keep)


def _stats(mask: pd.Series, fwd: dict[int, pd.Series]) -> dict:
    out = {"n": int(mask.sum())}
    for h, r in fwd.items():
        sel, base = r[mask].dropna(), r.dropna()
        if len(sel) < 5:
            out[h] = None
            continue
        ind = r[_spaced(sel.index, h)].dropna()
        excess = sel.mean() - base.mean()
        se = ind.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 2 else np.nan
        halves = {}
        for name, a, b in HALVES:
            w = (r.index >= a) & ((r.index < b) if b else True)
            s_h, b_h = r[mask & w].dropna(), r[w].dropna()
            halves[name] = round(float(s_h.mean() - b_h.mean()) * 100, 2) if len(s_h) >= 5 else None
        t = excess / se if se and se > 0 else np.nan
        signs = [np.sign(v) for v in halves.values() if v is not None]
        out[h] = {"mean": round(float(sel.mean()) * 100, 2), "median": round(float(sel.median()) * 100, 2),
                  "pos": round(float((sel > 0).mean()) * 100, 1), "n_ind": int(len(ind)),
                  "excess": round(float(excess) * 100, 2), "t": None if np.isnan(t) else round(float(t), 2),
                  "halves": halves,
                  "holds": bool(len(signs) == 2 and signs[0] == signs[1] != 0 and not np.isnan(t) and abs(t) >= 2)}
    return out


def regime(data_dir: Path) -> dict:
    d = daily_candles(data_dir)
    c = d.close
    fwd = {h: c.shift(-h) / c - 1 for h in HORIZONS}
    base = {}
    for h, r in fwd.items():
        r = r.dropna()
        base[h] = {"mean": round(float(r.mean()) * 100, 2), "median": round(float(r.median()) * 100, 2),
                   "pos": round(float((r > 0).mean()) * 100, 1), "n": int(len(r))}
    feats = features(data_dir, d)
    today = d.index[-1]
    rows = []
    for f in feats:
        state = f["state"].iloc[-1]
        valid = f["state"].notna()
        row = {"key": f["key"], "label": f["label"], "long_history": f["long"], "state": state,
               "value": None if pd.isna(f["value"].iloc[-1]) else round(float(f["value"].iloc[-1]), 2),
               "unit": f["unit"], "since": valid.idxmax().strftime("%Y-%m-%d") if valid.any() else None}
        if state is not None:
            row.update(_stats(f["state"] == state, fwd))
        rows.append(row)

    # Días parecidos: mismo estado en todos los factores largos con estado hoy
    longs = [f for f in feats if f["long"] and f["state"].iloc[-1] is not None]
    similar = None
    while longs:
        mask = pd.Series(True, index=d.index)
        for f in longs:
            mask &= f["state"] == f["state"].iloc[-1]
        mask.iloc[-1] = False  # hoy no cuenta como caso del pasado
        if mask.sum() >= MIN_SIMILAR or len(longs) == 1:
            similar = {"factors": [f["key"] for f in longs], "labels": [f["label"] for f in longs],
                       "dropped": [f["key"] for f in feats if f["long"] and f not in longs], **_stats(mask, fwd)}
            similar["last_dates"] = [t.strftime("%Y-%m-%d") for t in _spaced(mask[mask].index, 30)[-6:]]
            break
        longs = longs[:-1]
    return {"asof": today.strftime("%Y-%m-%d"), "close": round(float(c.iloc[-1]), 2), "since": d.index[0].strftime("%Y-%m-%d"),
            "horizons": list(HORIZONS), "base": base, "factors": rows, "similar": similar}


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "data"
    print(json.dumps(regime(data), indent=2, ensure_ascii=False, default=str))
