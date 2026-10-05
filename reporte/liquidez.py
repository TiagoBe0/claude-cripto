"""Dónde está la liquidez alrededor del precio, con fuentes gratuitas.

- estimated_liquidations   mapa ESTIMADO de liquidaciones a partir del open interest de Binance
- swing_pools              máximos y mínimos todavía no barridos (donde se acumulan stops)
- order_book_walls         órdenes límite grandes: Binance spot (hasta ±1%, lo que cubran 5000 niveles) y Coinbase (libro completo, ±5%)
- realized_liquidations    liquidaciones reales capturadas por liquidations_ws.py

Uso directo:  python reporte/liquidez.py [data_dir]   (imprime el JSON)
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

SYMBOL = "BTCUSDT"

# Supuestos del mapa estimado. Nadie publica el apalancamiento real de cada posición:
# esta mezcla es un supuesto nuestro (los heatmaps comerciales no publican la suya).
LEVERAGE_MIX = {10: 0.30, 25: 0.30, 50: 0.25, 100: 0.15}
MAINT_MARGIN = 0.004  # margen de mantenimiento de BTCUSDT en el primer tramo de Binance


def _r(x, nd=0):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.timestamp, unit="ms", utc=True)
    return df


def _clusters(prices: np.ndarray, usd: np.ndarray, price: float, side: str, top: int) -> list[dict]:
    """Los `top` grupos de mayor USD; `prices` ya viene agrupado en bins."""
    order = np.argsort(usd)[::-1][:top]
    return [{"price": _r(prices[i]), "usd_m": _r(usd[i] / 1e6, 1),
             "distance_pct": _r((prices[i] / price - 1) * 100, 2), "side": side} for i in order if usd[i] > 0]


# --- 1. mapa estimado de liquidaciones -------------------------------------------

def estimated_bins(d: Path, lookback_days: int = 30, bin_pct: float = 0.5,
                   max_dist_pct: float = 15) -> tuple[float, pd.DataFrame]:
    """Cada aumento de open interest en una hora abre la misma cantidad de largos y de cortos
    al precio medio de esa hora. Se reparten según LEVERAGE_MIX y se calcula su precio de
    liquidación. Un nivel desaparece cuando el precio lo toca (se liquidó) y todos se achican
    en proporción cuando el open interest baja (posiciones cerradas).

    Devuelve el último precio y los tramos de bin_pct a menos de max_dist_pct del precio:
    columnas `price` (centro del tramo), `usd` y `side` ("long" abajo, "short" arriba)."""
    oi = _read(d / f"{SYMBOL}_metrics_5m.csv").open_interest.resample("1h").last()
    k = _read(d / f"{SYMBOL}_perp_1h.csv")[["high", "low", "close"]]
    df = k.join(oi.rename("oi"), how="inner").dropna()
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=lookback_days)]

    lev = np.array(list(LEVERAGE_MIX))
    w = np.array(list(LEVERAGE_MIX.values()))
    lvl_px, lvl_btc, lvl_long = np.empty(0), np.empty(0), np.empty(0, bool)
    prev_oi = None
    for high, low, close, cur_oi in df[["high", "low", "close", "oi"]].itertuples(index=False):
        if len(lvl_px):
            hit = np.where(lvl_long, lvl_px >= low, lvl_px <= high)
            lvl_px, lvl_btc, lvl_long = lvl_px[~hit], lvl_btc[~hit], lvl_long[~hit]
        if prev_oi is not None:
            delta = cur_oi - prev_oi
            if delta < 0 and len(lvl_btc):
                lvl_btc = lvl_btc * cur_oi / prev_oi
            elif delta > 0:
                entry = (high + low) / 2
                lvl_px = np.concatenate([lvl_px, entry * (1 - 1 / lev + MAINT_MARGIN), entry * (1 + 1 / lev - MAINT_MARGIN)])
                lvl_btc = np.concatenate([lvl_btc, delta * w, delta * w])
                lvl_long = np.concatenate([lvl_long, np.ones(len(lev), bool), np.zeros(len(lev), bool)])
        prev_oi = cur_oi

    price = float(df.close.iloc[-1])
    width = price * bin_pct / 100
    m = np.abs(lvl_px / price - 1) * 100 <= max_dist_pct
    bins = (pd.DataFrame({"bin": np.floor(lvl_px[m] / width), "long": lvl_long[m], "usd": lvl_btc[m] * lvl_px[m]})
            .groupby(["bin", "long"]).usd.sum().reset_index())
    bins["price"] = (bins.bin + 0.5) * width
    bins["side"] = np.where(bins.long, "long", "short")
    return price, bins[["price", "usd", "side"]]


def estimated_liquidations(d: Path, lookback_days: int = 30, bin_pct: float = 0.5, max_dist_pct: float = 15) -> dict:
    """Resumen del mapa estimado (ver estimated_bins): totales cerca del precio y los 3 tramos mayores."""
    price, bins = estimated_bins(d, lookback_days, bin_pct, max_dist_pct)
    out = {"model": f"OI de Binance, últimos {lookback_days} días, apalancamiento supuesto "
                    + ", ".join(f"{l}x {p:.0%}" for l, p in LEVERAGE_MIX.items()),
           "price": _r(price), "bin_pct": bin_pct}
    for key, side in (("longs_below", "long"), ("shorts_above", "short")):
        b = bins[bins.side == side]
        dist = np.abs(b.price / price - 1) * 100
        out[key] = {
            "usd_m_within_2pct": _r(b.usd[dist <= 2].sum() / 1e6, 1),
            "usd_m_within_5pct": _r(b.usd[dist <= 5].sum() / 1e6, 1),
            "top_clusters": _clusters(b.price.to_numpy(), b.usd.to_numpy(), price, side, 3),
        }
    return out


# --- 2. máximos y mínimos sin barrer ---------------------------------------------

def _unswept(df: pd.DataFrame, n: int, highs: bool) -> pd.Series:
    """Pivotes (extremo de 2n+1 velas) que ninguna vela posterior superó."""
    s = df.high if highs else df.low
    win = s.rolling(2 * n + 1, center=True)
    piv = s[(s == (win.max() if highs else win.min()))].dropna()
    # extremo de todo lo que vino después de cada pivote
    after = s[::-1].cummax()[::-1].shift(-1) if highs else s[::-1].cummin()[::-1].shift(-1)
    keep = piv < after[piv.index] if not highs else piv > after[piv.index]
    return piv[keep]


def swing_pools(d: Path, merge_pct: float = 0.5, top: int = 3) -> dict:
    """Stops de cortos arriba de máximos no barridos, stops de largos abajo de mínimos no barridos.
    Pivotes cercanos (< merge_pct) se agrupan: varios máximos iguales concentran más stops."""
    frames = {"4h": (_read(d / f"{SYMBOL}_4h.csv").iloc[-90 * 6:], 6),   # pivote de ±1 día, 90 días
              "1d": (_read(d / f"{SYMBOL}_1d.csv").iloc[-365:], 5)}      # pivote de ±5 días, 1 año
    price = frames["4h"][0].close.iloc[-1]
    out = {"price": _r(price)}
    for side, highs in (("above", True), ("below", False)):
        pts = pd.concat([_unswept(df, n, highs).to_frame("px").assign(tf=tf) for tf, (df, n) in frames.items()])
        pts = pts.sort_values("px", ascending=highs)  # del más cercano al más lejano
        groups = []
        for ts, row in pts.iterrows():
            g = groups[-1] if groups else None
            if g and abs(row.px / g["ref"] - 1) * 100 <= merge_pct:
                g["n"] += 1
                g["tf"].add(row.tf)
                g["last"] = max(g["last"], ts)
                g["ref"] = max(g["ref"], row.px) if highs else min(g["ref"], row.px)
            else:
                groups.append({"ref": row.px, "n": 1, "tf": {row.tf}, "last": ts})
        out[side] = [{"price": _r(g["ref"]), "distance_pct": _r((g["ref"] / price - 1) * 100, 2),
                      "touches": g["n"], "timeframes": sorted(g["tf"]),
                      "last_pivot": g["last"].strftime("%Y-%m-%d")} for g in groups[:top]]
    return out


# --- 3. muros del libro de órdenes ----------------------------------------------

def _walls(bids, asks, band_pct: float, bucket_pct: float, skip_pct: float, top: int = 3) -> dict:
    """Tramos del libro con más USD entre skip_pct y band_pct del precio medio. Lo más pegado
    al precio se excluye de los muros porque siempre es lo más grueso (market makers)."""
    bids = np.array(bids, float)[:, :2]
    asks = np.array(asks, float)[:, :2]
    mid = (bids[0, 0] + asks[0, 0]) / 2
    width = mid * bucket_pct / 100
    # Si el libro devuelto no llega a ±band_pct (Binance da 5000 niveles), se achica la banda
    # a lo cubierto por ambos lados: si no, el lado más corto parece tener menos liquidez.
    reach = min(1 - bids[-1, 0] / mid, asks[-1, 0] / mid - 1) * 100
    band_pct = min(band_pct, reach)
    out = {"mid": _r(mid), "band_pct": _r(band_pct, 2), "bucket_pct": bucket_pct}
    for side, book in (("bids", bids), ("asks", asks)):
        book = book[np.abs(book[:, 0] / mid - 1) * 100 <= band_pct]
        b = pd.Series(book[:, 0] * book[:, 1]).groupby(np.floor(book[:, 0] / width)).sum()
        centers, usd = (b.index.to_numpy() + 0.5) * width, b.to_numpy()
        far = np.flatnonzero(np.abs(centers / mid - 1) * 100 >= skip_pct)
        # x_median: cuántas veces más grande que un tramo típico del libro (un "muro" real es >> 1)
        out[side] = {"total_usd_m": _r(usd.sum() / 1e6, 1),
                     "walls": [{"price": _r(centers[i]), "usd_m": _r(usd[i] / 1e6, 1),
                                "distance_pct": _r((centers[i] / mid - 1) * 100, 2),
                                "x_median": _r(usd[i] / np.median(usd[far]), 1)}
                               for i in far[np.argsort(usd[far])[::-1][:top]]]}
    return out


def order_book_walls() -> dict:
    out = {}
    try:
        b = requests.get("https://api.binance.com/api/v3/depth",
                         params={"symbol": SYMBOL, "limit": 5000}, timeout=30).json()
        out["binance_spot"] = _walls(b["bids"], b["asks"], band_pct=1, bucket_pct=0.1, skip_pct=0.2)
    except (requests.RequestException, KeyError, ValueError) as e:
        out["binance_spot"] = {"error": str(e)}
    try:
        c = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/book", params={"level": 2},
                         headers={"User-Agent": "claude-cripto"}, timeout=30).json()
        out["coinbase"] = _walls(c["bids"], c["asks"], band_pct=5, bucket_pct=0.25, skip_pct=0.5)
    except (requests.RequestException, KeyError, ValueError) as e:
        out["coinbase"] = {"error": str(e)}
    return out


# --- 4. liquidaciones reales ----------------------------------------------------

def realized_liquidations(d: Path) -> dict | None:
    p = d / f"{SYMBOL}_liquidations.csv"
    if not p.exists() or p.stat().st_size == 0:
        return None
    df = _read(p)
    now = pd.Timestamp.now(tz="UTC")
    out = {"collected_since_utc": df.index[0].strftime("%Y-%m-%dT%H:%M:%SZ") if len(df) else None,
           "note": "Binance publica como máximo una liquidación por segundo: en cascadas subestima. Bybit manda todas."}
    for label, hours in (("24h", 24), ("7d", 168)):
        w = df[df.index >= now - pd.Timedelta(hours=hours)]
        by_hour = w.usd.resample("1h").sum()
        big = w.iloc[w.usd.values.argmax()] if len(w) else None
        out[label] = {
            "long_liquidated_usd_m": _r(w.usd[w.side == "long"].sum() / 1e6, 2),
            "short_liquidated_usd_m": _r(w.usd[w.side == "short"].sum() / 1e6, 2),
            "events": len(w),
            "by_exchange_usd_m": {k: _r(v / 1e6, 2) for k, v in w.groupby("exchange").usd.sum().items()},
            "largest": None if big is None else {
                "usd_m": _r(big.usd / 1e6, 2), "side": big.side, "exchange": big.exchange,
                "price": _r(big.price), "time_utc": big.datetime_utc},
            "max_hour": None if by_hour.empty else {"usd_m": _r(by_hour.max() / 1e6, 2),
                                                    "hour_utc": by_hour.idxmax().strftime("%Y-%m-%dT%H:00Z")},
        }
    return out


def build(d: Path) -> dict:
    out = {}
    for name, fn in [("estimated_liquidations", lambda: estimated_liquidations(d)),
                     ("swing_pools", lambda: swing_pools(d)),
                     ("order_book_walls", order_book_walls),
                     ("realized_liquidations", lambda: realized_liquidations(d))]:
        try:
            out[name] = fn()
        except (OSError, KeyError, IndexError, ValueError) as e:
            out[name] = {"error": f"{type(e).__name__}: {e}"}
    return out


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    print(json.dumps(build(d), indent=1, ensure_ascii=False))
