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

def _hourly(d: Path) -> pd.DataFrame:
    """Velas de 1 h del perpetuo con el open interest al cierre de cada hora (último dato de 5 min)."""
    oi = _oi_hourly(_read(d / f"{SYMBOL}_metrics_5m.csv").open_interest)
    k = _read(d / f"{SYMBOL}_perp_1h.csv")[["high", "low", "close"]]
    return k.join(oi.rename("oi"), how="inner").dropna()


def _oi_hourly(oi: pd.Series) -> pd.Series:
    """Último open interest válido de cada hora. Binance a veces publica 0 (julio de 2024 tiene decenas de horas
    así): tomado al pie de la letra, el mapa se vacía entero y a la hora siguiente se abre todo el OI en un solo precio."""
    return oi.where(oi > 0).resample("1h").last()


def simulate_levels(df: pd.DataFrame, lookback_days: int = 30):
    """Recorre las horas de `df` (high, low, close, oi) y devuelve, después de cada una,
    (hora, cierre, precios, BTC, es_largo, tramo de apalancamiento) de los niveles vivos.

    Cada aumento de open interest en una hora abre la misma cantidad de largos y de cortos al precio
    medio de esa hora. Se reparten según LEVERAGE_MIX y se calcula su precio de liquidación. Un nivel
    desaparece cuando el precio lo toca (se liquidó) o cuando tiene más de lookback_days, y todos se
    achican en proporción cuando el open interest baja (posiciones cerradas)."""
    lev = np.array(list(LEVERAGE_MIX))
    w = np.array(list(LEVERAGE_MIX.values()))
    tiers = np.arange(len(lev))
    px, btc, is_long = np.empty(0), np.empty(0), np.empty(0, bool)
    tier, born = np.empty(0, int), np.empty(0, "datetime64[ns]")
    prev_oi = None
    for t, high, low, close, cur_oi in df[["high", "low", "close", "oi"]].itertuples():
        now = np.datetime64(t.tz_localize(None) if t.tzinfo else t)
        if len(px):
            keep = ~np.where(is_long, px >= low, px <= high)
            keep &= born > now - np.timedelta64(lookback_days, "D")
            px, btc, is_long, tier, born = px[keep], btc[keep], is_long[keep], tier[keep], born[keep]
        if prev_oi is not None:
            delta = cur_oi - prev_oi
            if delta < 0 and len(btc):
                btc = btc * cur_oi / prev_oi
            elif delta > 0:
                entry = (high + low) / 2
                px = np.concatenate([px, entry * (1 - 1 / lev + MAINT_MARGIN), entry * (1 + 1 / lev - MAINT_MARGIN)])
                btc = np.concatenate([btc, delta * w, delta * w])
                is_long = np.concatenate([is_long, np.ones(len(lev), bool), np.zeros(len(lev), bool)])
                tier = np.concatenate([tier, tiers, tiers])
                born = np.concatenate([born, np.repeat(now, 2 * len(lev))])
        prev_oi = cur_oi
        yield t, close, px, btc, is_long, tier


def estimated_bins(d: Path, lookback_days: int = 30, bin_pct: float = 0.5,
                   max_dist_pct: float = 15) -> tuple[float, pd.DataFrame]:
    """El mapa de liquidaciones de ahora (ver simulate_levels).

    Devuelve el último precio y los tramos de bin_pct a menos de max_dist_pct del precio:
    columnas `price` (centro del tramo), `usd` y `side` ("long" abajo, "short" arriba)."""
    df = _hourly(d)
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=lookback_days)]
    for _, price, lvl_px, lvl_btc, lvl_long, _ in simulate_levels(df, lookback_days):
        pass
    width = price * bin_pct / 100
    m = np.abs(lvl_px / price - 1) * 100 <= max_dist_pct
    bins = (pd.DataFrame({"bin": np.floor(lvl_px[m] / width), "long": lvl_long[m], "usd": lvl_btc[m] * lvl_px[m]})
            .groupby(["bin", "long"]).usd.sum().reset_index())
    bins["price"] = (bins.bin + 0.5) * width
    bins["side"] = np.where(bins.long, "long", "short")
    return float(price), bins[["price", "usd", "side"]]


def heatmap(d: Path, days: int = 30, lookback_days: int = 30, bin_pct: float = 0.1, margin_pct: float = 10) -> dict:
    """Mapa de calor del mapa estimado: USD de liquidaciones por hora y tramo de precio, por apalancamiento.

    Grilla de precios fija (tramos de bin_pct del último precio) entre el mínimo y el máximo de la ventana
    ± margin_pct. Para que viaje liviano, cada celda va en un byte con escala logarítmica:
    0 = nada, q >= 1 → USD = 10 ** (lo + (q - 1) * (hi - lo) / 254). Matriz [tramo][hora][fila de precio],
    filas de abajo hacia arriba, en base64."""
    import base64
    df = _hourly(d)
    df = df[df.index >= df.index[-1] - pd.Timedelta(days=days + lookback_days)]
    win = df[df.index > df.index[-1] - pd.Timedelta(days=days)]
    width = float(df.close.iloc[-1]) * bin_pct / 100
    lo_px = np.floor(win.low.min() * (1 - margin_pct / 100) / width) * width
    rows = int(np.ceil(win.high.max() * (1 + margin_pct / 100) / width - lo_px / width))
    n_tiers = len(LEVERAGE_MIX)
    grid = np.zeros((n_tiers, len(win), rows), np.float32)
    col = 0
    for t, _, px, btc, _, tier in simulate_levels(df, lookback_days):
        if t < win.index[0]:
            continue
        r = np.floor((px - lo_px) / width).astype(int)
        m = (r >= 0) & (r < rows)
        np.add.at(grid[:, col], (tier[m], r[m]), btc[m] * px[m])
        col += 1
    top = float(grid.max()) or 1.0
    hi, lo = np.log10(top), np.log10(top) - 4  # 4 órdenes de magnitud; lo menor va al primer escalón
    with np.errstate(divide="ignore"):
        q = np.where(grid > 0, np.clip(np.round(1 + 254 * (np.log10(grid) - lo) / (hi - lo)), 1, 255), 0)
    return {"t0": int(win.index[0].timestamp()), "step_s": 3600, "hours": len(win),
            "price0": round(float(lo_px), 2), "bin": round(width, 2), "rows": rows,
            "tiers": [f"{k}x" for k in LEVERAGE_MIX], "log10_lo": round(lo, 4), "log10_hi": round(hi, 4),
            "data": base64.b64encode(q.astype(np.uint8).tobytes()).decode()}


def history_frame(d: Path) -> pd.DataFrame:
    """Velas de 1 h del perpetuo (open, high, low, close) con el open interest, desde lo más viejo que haya:
    la copia de data/research (el OI de Binance arranca en septiembre de 2020) seguida de data/; donde se pisan
    manda data/, que es la que se actualiza."""
    parts = []
    for base in (d / "research", d):
        kp, mp = base / f"{SYMBOL}_perp_1h.csv", base / f"{SYMBOL}_metrics_5m.csv"
        if not (kp.exists() and mp.exists()):
            continue
        k = pd.read_csv(kp, usecols=["timestamp", "open", "high", "low", "close"])
        k.index = pd.to_datetime(k.pop("timestamp"), unit="ms", utc=True)
        m = pd.read_csv(mp, usecols=["timestamp", "open_interest"])
        oi = _oi_hourly(m.set_index(pd.to_datetime(m.timestamp, unit="ms", utc=True)).open_interest)
        parts.append(k.join(oi.rename("oi"), how="inner").dropna())
    df = pd.concat(parts)
    return df[~df.index.duplicated(keep="last")].sort_index()


def heatmap_history(d: Path, lookback_days: int = 30, bin_pct: float = 0.5, margin_pct: float = 10) -> dict:
    """El mismo mapa estimado que heatmap(), pero de toda la historia y por día: cada columna es el promedio de
    las 24 fotos horarias del día (la última, de las horas que van). Como el precio va de ~10 mil a más de 100 mil,
    los tramos son logarítmicos: la fila r va de price0 · ratio^r a price0 · ratio^(r+1), con ratio = 1 + bin_pct.
    El primer mes no se muestra: es lo que tarda en llenarse el mapa (los niveles viven lookback_days).
    Matriz [tramo][día][fila] en un byte por celda, con la misma escala que heatmap()."""
    import base64
    df = history_frame(d)
    t0 = df.index[0].floor("D") + pd.Timedelta(days=lookback_days)
    win = df[df.index >= t0]
    ratio = 1 + bin_pct / 100
    price0 = round(float(win.low.min()) * (1 - margin_pct / 100), 2)
    rows = int(np.ceil(np.log(float(win.high.max()) * (1 + margin_pct / 100) / price0) / np.log(ratio)))
    cols = (win.index[-1].floor("D") - t0).days + 1
    grid = np.zeros((len(LEVERAGE_MIX), cols, rows), np.float64)
    hours = np.zeros(cols)
    for t, _, px, btc, _, tier in simulate_levels(df, lookback_days):
        if t < t0:
            continue
        c = (t - t0).days
        r = np.floor(np.log(px / price0) / np.log(ratio)).astype(int)
        m = (r >= 0) & (r < rows)
        np.add.at(grid[:, c], (tier[m], r[m]), btc[m] * px[m])
        hours[c] += 1
    grid /= np.maximum(hours, 1)[None, :, None]
    top = float(grid.max()) or 1.0
    hi, lo = np.log10(top), np.log10(top) - 4
    with np.errstate(divide="ignore"):
        q = np.where(grid > 0, np.clip(np.round(1 + 254 * (np.log10(grid) - lo) / (hi - lo)), 1, 255), 0)
    days = win.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    days = days.reindex(pd.date_range(t0, periods=cols, freq="1D"))
    return {"t0": int(t0.timestamp()), "step_s": 86400, "cols": cols, "price0": price0, "ratio": ratio,
            "rows": rows, "tiers": [f"{k}x" for k in LEVERAGE_MIX], "log10_lo": round(lo, 4), "log10_hi": round(hi, 4),
            "data": base64.b64encode(q.astype(np.uint8).tobytes()).decode(), "days": days}


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


def swing_groups(frames: dict, merge_pct: float = 0.5) -> dict:
    """{"above": [...], "below": [...]}: pivotes sin barrer de cada timeframe (frames = {tf: (velas, n)}),
    del más cercano al precio al más lejano. Pivotes cercanos (< merge_pct) se agrupan."""
    out = {}
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
        out[side] = groups
    return out


def swing_pools(d: Path, merge_pct: float = 0.5, top: int = 3) -> dict:
    """Stops de cortos arriba de máximos no barridos, stops de largos abajo de mínimos no barridos.
    Pivotes cercanos (< merge_pct) se agrupan: varios máximos iguales concentran más stops."""
    frames = {"4h": (_read(d / f"{SYMBOL}_4h.csv").iloc[-90 * 6:], 6),   # pivote de ±1 día, 90 días
              "1d": (_read(d / f"{SYMBOL}_1d.csv").iloc[-365:], 5)}      # pivote de ±5 días, 1 año
    price = frames["4h"][0].close.iloc[-1]
    out = {"price": _r(price)}
    for side, groups in swing_groups(frames, merge_pct).items():
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
