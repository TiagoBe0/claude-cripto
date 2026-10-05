"""Detector de barridas de liquidez y test de si después el precio revierte.

Uso:
    python estrategia/barridas.py        # usa data/backtest/BTCUSDT_perp_1h.csv y BTCUSDT_metrics_5m.csv

Barrida (definida antes de mirar resultados, velas de 1 h del perpetuo):
- Niveles: máximos/mínimos pivote de ±12 h de los últimos 30 días que ninguna vela posterior superó
  (ahí se acumulan stops y liquidaciones). Un pivote recién se usa cuando se confirma (12 h después).
- Barrida de máximos en la vela t: high(t) supera uno o más niveles y close(t) vuelve a cerrar debajo
  del nivel superado más alto (mecha que toma la liquidez y no se sostiene). Mínimos: simétrico.
- Una sola barrida por lado cada 12 h (las siguientes son parte del mismo evento).

Variantes (filtros fijados de antemano):
- todas         solo precio
- oi            además el open interest cae >= 0,5 % en esa hora (se cerraron posiciones: stops/liquidaciones)
- trades        además la cantidad de trades de la vela es >= 2 veces la mediana de las 24 h previas
- oi + trades   ambos

Resultado: se opera la reversión (corto tras barrer máximos, largo tras barrer mínimos) entrando en la
apertura de t+1 y saliendo al cierre de t+H, H = 4, 12 y 24 h. Se compara contra la línea de base
(misma operación en todas las velas) y se descuenta el costo de ida y vuelta (0,20 %).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from estrategia.backtest import DATA  # noqa: E402

PIVOT_N = 12
LOOKBACK_H = 30 * 24
COOLDOWN_H = 12
HORIZONS = [4, 12, 24]
ROUND_TRIP = 0.0020
OI_DROP = -0.005
TRADES_MULT = 2.0


def load(data_dir: Path = DATA) -> pd.DataFrame:
    k = pd.read_csv(data_dir / "BTCUSDT_perp_1h.csv")
    k.index = pd.to_datetime(k.timestamp, unit="ms")
    df = k[["open", "high", "low", "close", "trades"]].copy()
    mp = data_dir / "BTCUSDT_metrics_5m.csv"
    if mp.exists():
        m = pd.read_csv(mp, usecols=["timestamp", "open_interest"])
        m.index = pd.to_datetime(m.timestamp, unit="ms")
        # último OI dentro de cada hora ~ OI al cierre de la vela; cambio contra la hora anterior
        oi = m.open_interest.resample("1h").last()
        df["oi_chg"] = (oi / oi.shift() - 1).reindex(df.index)
    else:
        df["oi_chg"] = np.nan
    df["trades_ratio"] = df.trades / df.trades.shift().rolling(24).median()
    return df


def detect(df: pd.DataFrame, n=PIVOT_N, lookback=LOOKBACK_H, cooldown=COOLDOWN_H) -> pd.DataFrame:
    """Una fila por barrida: tiempo de la vela, lado ("high"/"low"), nivel barrido, cuántos niveles tomó
    y cuánto se pasó (%)."""
    h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    roll_max = df.high.rolling(2 * n + 1, center=True).max().to_numpy()
    roll_min = df.low.rolling(2 * n + 1, center=True).min().to_numpy()
    highs, lows = [], []  # niveles activos: (índice del pivote, precio)
    events, last = [], {"high": -10**9, "low": -10**9}
    for t in range(len(df)):
        for side, levels in (("high", highs), ("low", lows)):
            taken = [px for _, px in levels if (h[t] > px if side == "high" else l[t] < px)]
            if taken:
                lvl = max(taken) if side == "high" else min(taken)
                rejected = c[t] < lvl if side == "high" else c[t] > lvl
                if rejected and t - last[side] >= cooldown:
                    beyond = (h[t] / lvl - 1) if side == "high" else (1 - l[t] / lvl)
                    events.append((t, side, lvl, len(taken), beyond * 100))
                    last[side] = t
                # los niveles superados dejan de existir, haya cerrado adentro o no
                levels[:] = [(p, px) for p, px in levels if px not in taken]
        # pivote confirmado al cierre de t: el de t - n
        p = t - n
        if p >= n:
            if h[p] == roll_max[p]:
                highs.append((p, h[p]))
            if l[p] == roll_min[p]:
                lows.append((p, l[p]))
        highs[:] = [(q, px) for q, px in highs if q >= t - lookback]
        lows[:] = [(q, px) for q, px in lows if q >= t - lookback]
    ev = pd.DataFrame(events, columns=["i", "side", "level", "n_levels", "beyond_pct"])
    ev.index = df.index[ev.i]
    return ev


def forward(df: pd.DataFrame, hz: int) -> np.ndarray:
    """Retorno de comprar en la apertura de t+1 y vender al cierre de t+hz, para cada vela t."""
    o, c = df.open.to_numpy(), df.close.to_numpy()
    out = np.full(len(df), np.nan)
    out[:-hz] = c[hz:] / o[1:len(o) - hz + 1] - 1
    return out


def table(df: pd.DataFrame, ev: pd.DataFrame, start, end) -> list[str]:
    in_period = (df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end) if end else True)
    variants = {
        "todas": np.ones(len(ev), bool),
        "oi": (ev.oi_chg <= OI_DROP).to_numpy(),
        "trades": (ev.trades_ratio >= TRADES_MULT).to_numpy(),
        "oi + trades": ((ev.oi_chg <= OI_DROP) & (ev.trades_ratio >= TRADES_MULT)).to_numpy(),
    }
    ev_in = np.asarray((ev.index >= pd.Timestamp(start)) & ((ev.index < pd.Timestamp(end)) if end else True))
    lines = []
    for hz in HORIZONS:
        fwd = forward(df, hz)
        for side, sign, label in (("high", -1, "máx -> corto"), ("low", 1, "mín -> largo")):
            base = np.nanmean(sign * fwd[in_period])
            for vname, mask in variants.items():
                sel = ev[(ev.side == side).to_numpy() & mask & ev_in]
                r = sign * fwd[sel.i.to_numpy()]
                r = r[~np.isnan(r)]
                if len(r) < 5:
                    lines.append(f"  {hz:2d} h  {label}  {vname:12s} n={len(r):4d}  (pocos eventos)")
                    continue
                t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))
                lines.append(f"  {hz:2d} h  {label}  {vname:12s} n={len(r):4d}  aciertos {np.mean(r > 0):4.0%}  "
                             f"media {r.mean():+7.2%}  base {base:+6.2%}  exceso {r.mean() - base:+6.2%}  "
                             f"neta {r.mean() - ROUND_TRIP:+7.2%}  t {t:+5.2f}")
        lines.append("")
    return lines


if __name__ == "__main__":
    df = load()
    ev = detect(df)
    ev = ev.join(df[["oi_chg", "trades_ratio"]])
    print(f"{len(df)} velas 1h {df.index[0]:%Y-%m-%d} a {df.index[-1]:%Y-%m-%d}; {len(ev)} barridas "
          f"({(ev.side == 'high').sum()} de máximos, {(ev.side == 'low').sum()} de mínimos); "
          f"OI desde {df.oi_chg.first_valid_index():%Y-%m-%d}")
    for name, (a, b) in {"in-sample 2019-10 a 2023": ("2019-10-01", "2024-01-01"),
                         "out-of-sample 2024-hoy": ("2024-01-01", None)}.items():
        print(f"\n{name}  (las variantes con oi solo tienen datos desde 2021-12)")
        print("\n".join(table(df, ev, a, b)))
