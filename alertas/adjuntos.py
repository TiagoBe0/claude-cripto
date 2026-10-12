"""Contexto de mercado para los correos: un resumen en texto y gráficos PNG que van dentro del correo.

- velas.png       velas del timeframe del aviso (4h o 1d) con SMA 50/200, volumen, RSI 14 y MACD
- pulso.png       últimas 24 h: precio, neto de ballenas, liquidaciones reales, libro ±0,5 % y open interest
- liquidez.png    mapa de liquidez: liquidaciones estimadas por precio, máx/mín sin barrer y muros del libro
- mapa_calor.png  mapa de calor de liquidaciones estimadas, últimos 7 días (data/dashboard/liq_heatmap.json)
- libro.png       mapa de calor del libro de órdenes, últimas 6 h, con las ballenas (book_heatmap.json)

Todo sale de lo que ya escriben los recolectores y build_dashboard.py: no pide nada a la red. Cada parte va
por separado: la que falla (archivo que falta, recolector caído) se saltea y el correo sale con el resto.

Prueba sin mandar nada (escribe los PNG y el texto en la carpeta):
    python -m alertas.adjuntos /tmp/adjuntos [4h|1d]
"""

import base64
import io
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from alertas import mailer

DATA = mailer.ROOT / "data"
DASH = DATA / "dashboard"
SYMBOL = "BTCUSDT"
MARKETS = ["binance_spot", "binance_perp", "coinbase"]
CANDLES = 120          # velas en velas.png
HEAT_HOURS = 7 * 24    # ventana de mapa_calor.png
BOOK_MIN = 6 * 60      # ventana de libro.png
# Colores del panel (dashboard.html y liquidez.html)
UP, DOWN, MUTED, GRID = "#2a78d6", "#e34948", "#85847f", "#efeeeb"
SMA50, SMA200, RSI, MACD = "#eb6834", "#6f4fd8", "#8e44ad", "#0097a7"
LIQ_LONG, LIQ_SHORT = "#ff8a3d", "#40c4ff"
HEAT_SPAN = 100            # libro.png: los colores cubren del máximo a máximo / HEAT_SPAN (como "×100" en liquidez.html)
MIN_DOT_LIQ = 250_000      # liquidaciones reales que se dibujan (US$ por hora y tramo)
MIN_DOT_WHALE = 500_000    # órdenes de ballenas que se dibujan en libro.png

log = logging.getLogger("alertas.adjuntos")


def ar(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def money(x: float) -> str:
    s = "−" if x < 0 else ""
    x = abs(x)
    if x == 0:
        return "0"
    return f"{s}US$ {ar(x / 1e9, 2)} mil M" if x >= 1e9 else f"{s}US$ {ar(x / 1e6, 1)} M" if x >= 1e6 \
        else f"{s}US$ {ar(x / 1e3, 0)} mil"


def pct(x: float, d: int = 1) -> str:
    return f"{'+' if x >= 0 else '−'}{ar(abs(x), d)} %"


def _csv(name: str, tail_bytes: int | None = None) -> pd.DataFrame:
    """CSV de data/ indexado por tiempo UTC. Con tail_bytes lee solo el final (los de 5 m pesan decenas de MB)."""
    path = DATA / name
    if tail_bytes is None or path.stat().st_size <= tail_bytes:
        df = pd.read_csv(path)
    else:
        with path.open("rb") as f:
            header = f.readline()
            f.seek(path.stat().st_size - tail_bytes)
            f.readline()  # la primera línea puede estar cortada
            df = pd.read_csv(io.BytesIO(header + f.read()))
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df.drop(columns="datetime_utc", errors="ignore")


def _heat(path: Path) -> tuple[dict, np.ndarray]:
    """Mapa de calor del panel -> (meta, US$ [capa][columna][fila]) deshaciendo la escala log de un byte."""
    hm = json.loads(path.read_text())
    q = np.frombuffer(base64.b64decode(hm["data"]), np.uint8).astype(np.float32)
    lo, hi = hm["log10_lo"], hm["log10_hi"]
    usd = np.where(q > 0, 10 ** (lo + (q - 1) * (hi - lo) / 254), 0)
    cols = hm.get("cols") or hm["hours"]
    return hm, usd.reshape(-1, cols, hm["rows"])


# --- gráficos -------------------------------------------------------------------------------------------

def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.color": GRID, "axes.edgecolor": "#c9c8c2",
                         "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 110,
                         "text.parse_math": False})  # sin esto "US$ ... US$" se lee como fórmula
    return plt


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=fig.get_facecolor())
    _plt().close(fig)
    return buf.getvalue()


def _thousands(ax, axis: str = "y") -> None:
    from matplotlib.ticker import FuncFormatter
    getattr(ax, f"{axis}axis").set_major_formatter(FuncFormatter(lambda v, _: ar(v, 0)))


def _musd(ax) -> None:
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{ar(v / 1e6, 0)} M"))


def _candles(ax, df: pd.DataFrame, width: float) -> None:
    up = df.close >= df.open
    for mask, color in ((up, UP), (~up, DOWN)):
        d = df[mask]
        ax.vlines(d.index, d.low, d.high, color=color, linewidth=0.7)
        ax.bar(d.index, (d.close - d.open).abs().clip(lower=df.close.iloc[-1] * 2e-5),
               bottom=np.minimum(d.open, d.close), width=width, color=color, linewidth=0)


def chart_velas(tf: str) -> bytes:
    plt = _plt()
    k = _csv(f"{SYMBOL}_{tf}.csv").tail(CANDLES)
    ind = _csv(f"{SYMBOL}_{tf}_indicators.csv", 2_000_000).drop(columns="close")
    df = k.join(ind, how="left")
    width = pd.Timedelta(tf).total_seconds() / 86400 * 0.7
    fig, axes = plt.subplots(4, 1, figsize=(10, 8.2), sharex=True, gridspec_kw={"height_ratios": [5, 1.2, 1.6, 1.6]})
    ax, axv, axr, axm = axes
    _candles(ax, df, width)
    ax.plot(df.index, df.sma_50, color=SMA50, lw=1.1, label="SMA 50")
    ax.plot(df.index, df.sma_200, color=SMA200, lw=1.1, label="SMA 200")
    last = df.close.iloc[-1]
    ax.axhline(last, color=MUTED, lw=0.6, ls=":")
    ax.annotate(ar(last, 0), (1, last), xycoords=("axes fraction", "data"), fontsize=8, color="#0b0b0b",
                ha="left", va="center", bbox={"boxstyle": "round,pad=0.2", "fc": "#fcfcfb", "ec": MUTED, "lw": 0.5})
    ax.legend(loc="upper left", frameon=False, ncol=2)
    ax.set_title(f"BTC/USDT spot · velas de {tf} · últimas {len(df)} · cierre {ar(last, 0)}", loc="left")
    _thousands(ax)
    up = df.close >= df.open
    axv.bar(df.index, df.volume, width=width, color=np.where(up, UP, DOWN), alpha=0.55)
    axv.set_ylabel("Volumen BTC")
    axr.plot(df.index, df.rsi_14, color=RSI, lw=1.1)
    for y in (30, 70):
        axr.axhline(y, color=MUTED, lw=0.6, ls="--")
    axr.fill_between(df.index, 70, df.rsi_14, where=df.rsi_14 > 70, color=DOWN, alpha=0.25)
    axr.fill_between(df.index, 30, df.rsi_14, where=df.rsi_14 < 30, color=UP, alpha=0.25)
    axr.set_ylim(10, 90)
    axr.set_ylabel(f"RSI 14 · {ar(df.rsi_14.iloc[-1], 1)}")
    axm.bar(df.index, df.macd_hist, width=width, color=np.where(df.macd_hist >= 0, UP, DOWN), alpha=0.6)
    axm.plot(df.index, df.macd, color=MACD, lw=1, label="MACD")
    axm.plot(df.index, df.macd_signal, color=SMA50, lw=1, label="señal")
    axm.axhline(0, color=MUTED, lw=0.6)
    axm.set_ylabel("MACD 12/26/9")
    axm.legend(loc="upper left", frameon=False, ncol=2, fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    return _png(fig)


def chart_pulso() -> bytes:
    plt = _plt()
    now = pd.Timestamp.now(tz="UTC")
    since = now - pd.Timedelta(hours=24)
    fig, axes = plt.subplots(5, 1, figsize=(10, 9), sharex=True, gridspec_kw={"height_ratios": [2.2, 1.4, 1.4, 1.4, 1.2]})
    ax_p, ax_w, ax_l, ax_b, ax_oi = axes
    book = _csv(f"{SYMBOL}_book_1m.csv", 3_000_000)
    book = book[book.index >= since]
    ax_p.plot(book.index, book.spot_mid, color="#0b0b0b", lw=1)
    ax_p.set_title(f"Pulso de las últimas 24 h · BTC {ar(book.spot_mid.iloc[-1], 0)}", loc="left")
    ax_p.set_ylabel("Precio spot")
    _thousands(ax_p)

    w = _csv(f"{SYMBOL}_whale_trades_1m.csv", 3_000_000)
    w = w[w.index >= since]
    net = sum(w[f"{m}_whale_buy_usd"] - w[f"{m}_whale_sell_usd"] for m in MARKETS).resample("1h").sum()
    ax_w.bar(net.index, net, width=1 / 24 * 0.8, align="edge", color=np.where(net >= 0, UP, DOWN), alpha=0.75)
    ax_w.axhline(0, color=MUTED, lw=0.6)
    ax_w.set_ylabel(f"Ballenas neto/h\n24 h: {money(net.sum())}")
    _musd(ax_w)

    lq = _csv(f"{SYMBOL}_liquidations.csv", 3_000_000)
    lq = lq[lq.index >= since]
    by = lq.groupby([pd.Grouper(freq="1h"), "side"]).usd.sum().unstack(fill_value=0)
    longs, shorts = by.get("long", pd.Series(dtype=float)), by.get("short", pd.Series(dtype=float))
    ax_l.bar(shorts.index, shorts, width=1 / 24 * 0.8, align="edge", color=LIQ_SHORT, label="cortos liquidados")
    ax_l.bar(longs.index, -longs, width=1 / 24 * 0.8, align="edge", color=LIQ_LONG, label="largos liquidados")
    ax_l.axhline(0, color=MUTED, lw=0.6)
    ax_l.set_ylabel("Liquidaciones/h")
    ax_l.legend(loc="upper left", frameon=False, ncol=2, fontsize=8)
    _musd(ax_l)

    ax_b.plot(book.index, book.spot_bid_05, color=UP, lw=1, label="compras (bids)")
    ax_b.plot(book.index, book.spot_ask_05, color=DOWN, lw=1, label="ventas (asks)")
    ax_b.set_ylabel("Libro spot ±0,5 %")
    ax_b.legend(loc="upper left", frameon=False, ncol=2, fontsize=8)
    _musd(ax_b)

    m = _csv(f"{SYMBOL}_metrics_5m.csv", 400_000)
    m = m[m.index >= since]
    ax_oi.plot(m.index, m.open_interest_usd, color="#c2185b", lw=1)
    ax_oi.set_ylabel("Open interest")
    from matplotlib.ticker import FuncFormatter
    ax_oi.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{ar(v / 1e9, 2)} mil M"))
    import matplotlib.dates as mdates
    ax_oi.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    ax_oi.set_xlim(since, now)
    ax_oi.set_xlabel("UTC")
    fig.tight_layout()
    return _png(fig)


def chart_liquidez() -> bytes:
    plt = _plt()
    liq = json.loads((DASH / "liquidity.json").read_text())
    est = liq["estimated"]
    price = est["price"]
    bins = pd.DataFrame(est["bins"])
    bins = bins[(bins.price > price * 0.88) & (bins.price < price * 1.12)]
    fig, ax = plt.subplots(figsize=(10, 6.5))
    h = price * est["bin_pct"] / 100 * 0.85
    for side, color, lbl in (("long", LIQ_LONG, "largos a liquidar (abajo)"), ("short", LIQ_SHORT, "cortos a liquidar (arriba)")):
        b = bins[bins.side == side]
        ax.barh(b.price, b.usd, height=h, color=color, label=lbl)
    ax.axhline(price, color="#0b0b0b", lw=1.2)
    ax.annotate(f"precio {ar(price, 0)}", (0.01, price), xycoords=("axes fraction", "data"), ha="left", va="bottom", fontsize=8)
    pools = liq.get("swing_pools", {})
    for side, color in (("above", LIQ_SHORT), ("below", LIQ_LONG)):
        for p in pools.get(side, [])[:3]:
            if abs(p["distance_pct"]) > 12:
                continue
            ax.axhline(p["price"], color=color, lw=0.9, ls="--")
            ax.annotate(f"{'máx' if side == 'above' else 'mín'} sin barrer {ar(p['price'], 0)} "
                        f"({pct(p['distance_pct'])}, {p['touches']} toques)",
                        (0.99, p["price"]), xycoords=("axes fraction", "data"), ha="right", va="bottom", fontsize=7.5, color="#52514e")
    walls = liq.get("walls", {})
    xmax = bins.usd.max() if len(bins) else 1
    for venue, mk in (("binance_spot", "s"), ("coinbase", "D")):
        v = walls.get(venue) or {}
        for side, color in (("bids", UP), ("asks", DOWN)):
            for wl in (v.get(side) or {}).get("walls", []):
                ax.scatter(xmax * 1.02, wl["price"], s=12 + wl["usd_m"] * 12, marker=mk, color=color, zorder=3,
                           clip_on=False)
    ax.scatter([], [], marker="s", color=MUTED, label="muros del libro: ■ Binance spot ◆ Coinbase (azul compra, rojo venta)")
    _thousands(ax)
    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{ar(v / 1e6, 0)} M"))
    ax.set_xlabel("US$ a liquidar por tramo de 0,5 % (estimado)")
    ax.set_title("Mapa de liquidez · dónde se concentran liquidaciones estimadas y muros del libro", loc="left")
    ax.legend(loc="lower right", frameon=False, fontsize=8)
    fig.tight_layout()
    return _png(fig)


def _heat_ax(fig, ax, img: np.ndarray, extent, label: str, log: bool) -> None:
    """log: escala log de HEAT_SPAN (libro); si no, raíz cuadrada hasta el percentil 99,5, que deja resaltar
    solo los cúmulos grandes (en el mapa de liquidaciones casi todo el mapa cae dentro de ×100 del máximo)."""
    from matplotlib.colors import LogNorm, PowerNorm
    top = img.max() or 1
    norm = LogNorm(vmin=top / HEAT_SPAN, vmax=top) if log else \
        PowerNorm(gamma=0.5, vmin=0, vmax=float(np.percentile(img[img > 0], 99.5)) if (img > 0).any() else 1)
    im = ax.imshow(np.where(img > 0, img, np.nan).T, origin="lower", aspect="auto", extent=extent,
                   cmap="inferno", norm=norm, interpolation="nearest")
    ax.set_facecolor("#0d0d10")
    ax.grid(False)
    cb = fig.colorbar(im, ax=ax, pad=0.01, fraction=0.04)
    cb.set_label(label)
    from matplotlib.ticker import FuncFormatter
    cb.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: money(v)))


def chart_mapa_calor() -> bytes:
    plt = _plt()
    import matplotlib.dates as mdates
    hm, usd = _heat(DASH / "liq_heatmap.json")
    grid = usd.sum(axis=0)[-HEAT_HOURS:]
    c0 = usd.shape[1] - len(grid)
    k = pd.DataFrame(hm["candles"])
    k.index = pd.to_datetime(k.pop("t"), unit="s", utc=True)
    k = k.iloc[c0:]
    lo_px, hi_px = k.low.min() * 0.93, k.high.max() * 1.07
    r0 = max(0, int((lo_px - hm["price0"]) // hm["bin"]))
    r1 = min(hm["rows"], int((hi_px - hm["price0"]) // hm["bin"]) + 1)
    t0 = pd.Timestamp(hm["t0"] + c0 * hm["step_s"], unit="s", tz="UTC")
    x0, x1 = mdates.date2num(t0), mdates.date2num(t0 + pd.Timedelta(hours=len(grid)))
    p0 = hm["price0"] + r0 * hm["bin"]
    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("white")
    _heat_ax(fig, ax, grid[:, r0:r1], (x0, x1, p0, hm["price0"] + r1 * hm["bin"]), "US$ a liquidar (estimado)", log=False)
    ax.vlines(k.index + pd.Timedelta(minutes=30), k.low, k.high, color="white", lw=0.6, alpha=0.9)
    ax.plot(k.index + pd.Timedelta(minutes=30), k.close, color="white", lw=0.8)
    rz = [r for r in hm.get("realized", []) if r[0] >= c0 and r[2] >= MIN_DOT_LIQ]
    for longs, color in ((1, LIQ_LONG), (0, LIQ_SHORT)):
        pts = [r for r in rz if r[3] == longs]
        if pts:
            ax.scatter([mdates.date2num(t0) + (r[0] - c0 + 0.5) / 24 for r in pts],
                       [hm["price0"] + (r[1] + 0.5) * hm["bin"] for r in pts],
                       s=[min(160, 6 + np.sqrt(r[2]) / 15) for r in pts], color=color, edgecolor="black", lw=0.3,
                       label=f"largos liquidados ≥ {money(MIN_DOT_LIQ)}" if longs else f"cortos liquidados ≥ {money(MIN_DOT_LIQ)}", zorder=3)
    ax.set_ylim(p0, hm["price0"] + r1 * hm["bin"])
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    _thousands(ax)
    ax.legend(loc="upper left", fontsize=8, facecolor="#222", labelcolor="white", framealpha=0.7)
    ax.set_title("Mapa de calor de liquidaciones estimadas · 7 días (perpetuo Binance, 1 h)", loc="left")
    fig.tight_layout()
    return _png(fig)


def chart_libro() -> bytes:
    plt = _plt()
    import matplotlib.dates as mdates
    hm, usd = _heat(DASH / "book_heatmap.json")
    grid = usd.sum(axis=0)[-BOOK_MIN:]
    c0 = usd.shape[1] - len(grid)
    mids = pd.Series([m for m in hm["mids"]["spot"][c0:]], dtype=float)
    lo_px, hi_px = mids.min() * 0.994, mids.max() * 1.006
    r0 = max(0, int((lo_px - hm["price0"]) // hm["bin"]))
    r1 = min(hm["rows"], int((hi_px - hm["price0"]) // hm["bin"]) + 1)
    t0 = pd.Timestamp(hm["t0"] + c0 * hm["step_s"], unit="s", tz="UTC")
    x0 = mdates.date2num(t0)
    x1 = x0 + len(grid) / 1440
    p0, p1 = hm["price0"] + r0 * hm["bin"], hm["price0"] + r1 * hm["bin"]
    fig, ax = plt.subplots(figsize=(10, 6))
    _heat_ax(fig, ax, grid[:, r0:r1], (x0, x1, p0, p1), "US$ en órdenes límite (tramo de US$ 10)", log=True)
    xs = x0 + (np.arange(len(mids)) + 0.5) / 1440
    ax.plot(xs, mids, color="white", lw=0.9)
    wh = [w for w in hm.get("whales", []) if w[0] >= c0 and w[2] >= MIN_DOT_WHALE]
    for buy, color, lbl in ((1, LIQ_SHORT, "ballena compra a mercado"), (0, LIQ_LONG, "ballena vende a mercado")):
        lbl += f" ≥ {money(MIN_DOT_WHALE)}"
        pts = [w for w in wh if w[3] == buy]
        if pts:
            ax.scatter([x0 + (w[0] - c0 + 0.5) / 1440 for w in pts], [hm["price0"] + (w[1] + 0.5) * hm["bin"] for w in pts],
                       s=[min(160, 6 + np.sqrt(w[2]) / 15) for w in pts], color=color, edgecolor="black", lw=0.3,
                       alpha=0.85, label=lbl, zorder=3)
    ax.set_ylim(p0, p1)
    ax.set_xlim(x0, x1)
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    _thousands(ax)
    ax.legend(loc="upper left", fontsize=8, facecolor="#222", labelcolor="white", framealpha=0.7)
    ax.set_title("Libro de órdenes Binance (spot + perpetuo) · últimas 6 h · hora UTC", loc="left")
    fig.tight_layout()
    return _png(fig)


# --- texto ---------------------------------------------------------------------------------------------

def _tf_lines(tf: str) -> list[str]:
    ind = _csv(f"{SYMBOL}_{tf}_indicators.csv", 200_000)
    r, p = ind.iloc[-1], ind.iloc[-2]
    rsi = r.rsi_14
    zone = "sobrecompra" if rsi >= 70 else "sobreventa" if rsi <= 30 else "zona media"
    hist = "a favor de suba" if r.macd_hist > 0 else "a favor de baja"
    grow = "creciendo" if abs(r.macd_hist) > abs(p.macd_hist) else "achicándose"
    pos = lambda s: "arriba" if r.close > s else "abajo"  # noqa: E731
    return [f"  {tf}: RSI 14 {ar(rsi, 1)} ({zone}) · MACD {ar(r.macd, 0)} / señal {ar(r.macd_signal, 0)}, "
            f"histograma {hist} y {grow}",
            f"      precio {pos(r.sma_50)} de SMA 50 ({ar(r.sma_50, 0)}) y {pos(r.sma_200)} de SMA 200 ({ar(r.sma_200, 0)})"
            f" · ATR 14 {ar(r.atr_14, 0)} ({ar(r.atr_14 / r.close * 100, 2)} %) · Bollinger %B {ar(r.bb_pct_b, 2)}"]


def context_text() -> str:
    parts = []

    def section(title, fn):
        try:
            lines = fn()
            if lines:
                parts.append(title + "\n" + "\n".join(lines))
        except Exception as e:  # noqa: BLE001 - una parte que falta no frena el resto
            log.info("contexto: sin %s (%s: %s)", title, type(e).__name__, e)

    section("Técnicos (vela en curso incluida)", lambda: _tf_lines("1h") + _tf_lines("4h") + _tf_lines("1d"))

    def derivados():
        out = []
        snap = _csv(f"{SYMBOL}_snapshot.csv").iloc[-1]
        out.append(f"  Funding estimado {ar(snap.est_funding_rate * 100, 4)} % cada 8 h · mark {ar(snap.mark_price, 0)}")
        m = _csv(f"{SYMBOL}_metrics_5m.csv", 400_000)
        last, ago = m.iloc[-1], m[m.index <= m.index[-1] - pd.Timedelta(hours=24)]
        oi = f"  Open interest {money(last.open_interest_usd)}"
        if len(ago):
            oi += f" ({pct((last.open_interest / ago.open_interest.iloc[-1] - 1) * 100)} en 24 h)"
        out.append(oi)
        out.append(f"  Largo/corto: cuentas {ar(last.global_account_ls_ratio, 2)} · top traders posición "
                   f"{ar(last.top_trader_position_ls_ratio, 2)} · taker compra/venta {ar(last.taker_buy_sell_ratio, 2)}")
        return out
    section("Derivados (Binance perpetuo)", derivados)

    def flujo():
        now = pd.Timestamp.now(tz="UTC")
        out = []
        b = _csv(f"{SYMBOL}_book_1m.csv", 200_000).iloc[-1]
        imb = (b.spot_bid_05 - b.spot_ask_05) / (b.spot_bid_05 + b.spot_ask_05) * 100
        out.append(f"  Libro spot ±0,5 %: compras {money(b.spot_bid_05)} · ventas {money(b.spot_ask_05)} "
                   f"(desbalance {pct(imb, 0)}) · spread {ar(b.spot_spread_bps, 3)} pb")
        lq = _csv(f"{SYMBOL}_liquidations.csv", 3_000_000)
        for h in (1, 24):
            q = lq[lq.index >= now - pd.Timedelta(hours=h)]
            out.append(f"  Liquidaciones {h} h: largos {money(q[q.side == 'long'].usd.sum())} · "
                       f"cortos {money(q[q.side == 'short'].usd.sum())}")
        w = _csv(f"{SYMBOL}_whale_trades_1m.csv", 3_000_000)
        for h in (1, 24):
            q = w[w.index >= now - pd.Timedelta(hours=h)]
            buy = sum(q[f"{m}_whale_buy_usd"].sum() for m in MARKETS)
            sell = sum(q[f"{m}_whale_sell_usd"].sum() for m in MARKETS)
            out.append(f"  Ballenas {h} h (órdenes ≥ US$ 100 mil): compras {money(buy)} · ventas {money(sell)} · "
                       f"neto {'+' if buy >= sell else ''}{money(buy - sell)}")
        return out
    section("Liquidez y flujo", flujo)

    def niveles():
        liq = json.loads((DASH / "liquidity.json").read_text())
        out = []
        price = liq["estimated"]["price"]
        bins = pd.DataFrame(liq["estimated"]["bins"])
        for side, word, cmp in (("short", "arriba (cortos)", bins.price > price), ("long", "abajo (largos)", bins.price < price)):
            b = bins[cmp & (bins.side == side) & ((bins.price / price - 1).abs() < 0.1)]
            if len(b):
                top = b.loc[b.usd.idxmax()]
                out.append(f"  Mayor cúmulo de liquidaciones estimadas {word} a 10 %: {ar(top.price, 0)} "
                           f"({pct((top.price / price - 1) * 100)}, {money(top.usd)})")
        pools = liq.get("swing_pools", {})
        for side, word in (("above", "máximo"), ("below", "mínimo")):
            if pools.get(side):
                p = pools[side][0]
                out.append(f"  {word.capitalize()} sin barrer más cercano: {ar(p['price'], 0)} "
                           f"({pct(p['distance_pct'])}, {p['touches']} toques en {'/'.join(p['timeframes'])})")
        return out
    section("Niveles", niveles)

    def sentimiento():
        out = []
        fg = _csv("fear_greed.csv").iloc[-1]
        out.append(f"  Fear & Greed {int(fg.value)} ({fg.classification})")
        dv = _csv("BTC_dvol_1h.csv", 100_000)
        out.append(f"  DVOL (volatilidad implícita Deribit) {ar(dv.close.iloc[-1], 1)} · hace 7 días "
                   f"{ar(dv.close[dv.index <= dv.index[-1] - pd.Timedelta(days=7)].iloc[-1], 1)}")
        return out
    section("Sentimiento", sentimiento)

    return "\n\n".join(parts)


# --- entrada ---------------------------------------------------------------------------------------------

def build(tf: str = "4h") -> tuple[str, list[tuple[str, str, bytes]]]:
    """-> (texto de contexto, [(nombre, título, PNG)]). Nunca lanza."""
    t = time.time()
    try:
        text = context_text()
    except Exception:  # noqa: BLE001
        log.exception("contexto: falló")
        text = ""
    images = []
    for name, title, fn in (("velas", f"Velas {tf} · RSI · MACD", lambda: chart_velas(tf)),
                            ("pulso", "Pulso de liquidez · 24 h", chart_pulso),
                            ("liquidez", "Mapa de liquidez", chart_liquidez),
                            ("mapa_calor", "Mapa de calor de liquidaciones · 7 d", chart_mapa_calor),
                            ("libro", "Mapa de calor del libro · 6 h", chart_libro)):
        try:
            images.append((name, title, fn()))
        except Exception as e:  # noqa: BLE001 - un gráfico que falla no frena el correo
            log.warning("adjuntos: sin %s (%s: %s)", name, type(e).__name__, e)
    log.info("adjuntos: %d gráficos en %.1f s", len(images), time.time() - t)
    return text, images


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "adjuntos")
    out.mkdir(parents=True, exist_ok=True)
    text, images = build(sys.argv[2] if len(sys.argv) > 2 else "4h")
    (out / "contexto.txt").write_text(text)
    for name, _, png in images:
        (out / f"{name}.png").write_bytes(png)
    print(text)
    print(f"\n{len(images)} gráficos en {out}")
