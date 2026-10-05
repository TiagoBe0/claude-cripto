"""Estudio de eventos: ¿qué pasa con BTC después de un cruce de medias o un patrón de velas?

Uso:
    python estrategia/eventos.py              # usa data/BTCUSDT_4h.csv (spot, desde 2017)

Todo fijado antes de mirar resultados:
- Velas spot de 4h y diarias (las diarias salen de agrupar las de 4h, así hay historia desde 2017).
- Señales evaluadas al CIERRE de la vela; se entra en la apertura de la siguiente.
- Retorno a futuro a 1, 3, 7 y 30 días. Las señales bajistas se miden como corto (retorno con signo
  invertido): un número positivo siempre significa "la señal acertó".
- Línea de base: la misma operación en todas las velas. BTC subió mucho desde 2017, así que cualquier
  señal de compra "gana" en promedio; lo que importa es el exceso sobre la base.
- Costo de ida y vuelta 0,32 % (0,16 % por lado, igual que backtest.py).
- In-sample 2018-2022 y out-of-sample 2023-hoy. Una señal sirve si el exceso tiene el mismo signo
  y es significativo en los dos períodos, no solo en uno.
- t: exceso medio / error estándar, con los eventos espaciados al menos el horizonte (si no, una
  misma racha cuenta varias veces y la t sale inflada).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
COST = 0.0032
HORIZONS_D = [1, 3, 7, 30]
PERIODS = {"IS 2018-22": ("2018-01-01", "2023-01-01"), "OOS 2023-hoy": ("2023-01-01", "2100-01-01")}


def load(path: Path) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(path, usecols=["timestamp", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    d1 = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                "volume": "sum"}).dropna()
    return {"4h": df, "1d": d1}


def signals(c: pd.DataFrame) -> dict[str, tuple[pd.Series, int]]:
    """Cada señal: (serie booleana, dirección +1 compra / -1 venta)."""
    o, h, l, cl = c.open, c.high, c.low, c.close
    body = (cl - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper = h - np.maximum(o, cl)
    lower = np.minimum(o, cl) - l
    po, pc = o.shift(), cl.shift()
    sma50, sma200 = cl.rolling(50).mean(), cl.rolling(200).mean()
    above = sma50 > sma200
    # Tendencia previa para los patrones de reversión: cierre debajo/encima del de 10 velas antes
    down_before, up_before = pc < cl.shift(11), pc > cl.shift(11)
    return {
        "Cruce dorado SMA50 > SMA200": (above & ~above.shift(fill_value=True), 1),
        "Cruce de la muerte SMA50 < SMA200": (~above & above.shift(fill_value=False), -1),
        "Cierre cruza arriba de SMA200": ((cl > sma200) & (pc <= sma200.shift()), 1),
        "Cierre cruza abajo de SMA200": ((cl < sma200) & (pc >= sma200.shift()), -1),
        "Envolvente alcista": ((pc < po) & (cl > o) & (o <= pc) & (cl >= po) & down_before, 1),
        "Envolvente bajista": ((pc > po) & (cl < o) & (o >= pc) & (cl <= po) & up_before, -1),
        "Martillo": ((lower >= 2 * body) & (upper <= 0.25 * rng) & (body >= 0.05 * rng) & down_before, 1),
        "Estrella fugaz": ((upper >= 2 * body) & (lower <= 0.25 * rng) & (body >= 0.05 * rng) & up_before, -1),
        "Tres soldados blancos": ((cl > o) & (pc > po) & (cl.shift(2) > o.shift(2))
                                  & (cl > pc) & (pc > cl.shift(2)) & (body > 0.5 * rng), 1),
        "Tres cuervos negros": ((cl < o) & (pc < po) & (cl.shift(2) < o.shift(2))
                                & (cl < pc) & (pc < cl.shift(2)) & (body > 0.5 * rng), -1),
    }


def study(c: pd.DataFrame, tf: str) -> pd.DataFrame:
    bars_per_day = {"4h": 6, "1d": 1}[tf]
    entry = c.open.shift(-1)  # apertura de la vela siguiente
    rows = []
    for name, (sig, direction) in signals(c).items():
        for hd in HORIZONS_D:
            n_bars = hd * bars_per_day
            exit_ = c.close.shift(-n_bars)
            fwd = np.log(exit_ / entry) * direction
            for period, (a, b) in PERIODS.items():
                m = (c.index >= a) & (c.index < b) & fwd.notna()
                base = fwd[m].mean()
                ev = fwd[m & sig.fillna(False).to_numpy()]
                # eventos espaciados al menos el horizonte, para la t
                kept, last = [], None
                for t in ev.index:
                    if last is None or (t - last) >= pd.Timedelta(days=hd):
                        kept.append(t)
                        last = t
                ind = ev.loc[kept]
                excess = ind - base
                se = excess.std(ddof=1) / np.sqrt(len(ind)) if len(ind) > 2 else np.nan
                rows.append({"tf": tf, "señal": name, "días": hd, "período": period, "n": len(ind),
                             "acierto %": round((ind > 0).mean() * 100, 1) if len(ind) else np.nan,
                             "media neta %": round((ind.mean() - COST) * 100, 2) if len(ind) else np.nan,
                             "exceso %": round(excess.mean() * 100, 2) if len(ind) else np.nan,
                             "t": round(excess.mean() / se, 2) if se and se == se else np.nan})
    return pd.DataFrame(rows)


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "BTCUSDT_4h.csv"
    frames = load(path)
    res = pd.concat([study(c, tf) for tf, c in frames.items()])
    wide = res.pivot_table(index=["tf", "señal", "días"], columns="período",
                           values=["n", "exceso %", "t"], sort=False)
    # Sirve si en los dos períodos el exceso es positivo y la t pasa de 2
    ok = (wide["exceso %"] > 0).all(axis=1) & (wide["t"] > 2).all(axis=1)
    wide["sirve"] = np.where(ok, "SÍ", "")
    pd.set_option("display.width", 220, "display.max_rows", 500)
    print(wide.round(2).to_string())
    print("\nSeñales que pasan los dos períodos:", ", ".join(f"{i[1]} ({i[0]}, {i[2]} d)"
          for i in wide.index[ok]) or "ninguna")


if __name__ == "__main__":
    main()
