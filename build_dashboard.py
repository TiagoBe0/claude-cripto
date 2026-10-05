"""Prepara los datos del dashboard (dashboard.html) a partir de los CSV.

Escribe en <data_dir>/dashboard/:
- chart_<tf>.json para 1h, 2h, 3h, 4h, 5h, 1d y 1w (TIMEFRAMES)
                                 velas spot, SMA 50/200, volumen spot y perp, RSI, MACD,
                                 DVOL vs volatilidad realizada, Fear & Greed,
                                 funding, open interest y ratios long/short,
                                 alineados a la grilla de velas de cada timeframe
                                 y flujo neto de órdenes grandes (ballenas)
- status.json                    último snapshot, antigüedad de cada serie,
                                 estado de la estrategia y del paper trading, liquidaciones de 24 h
                                 y lectura de ballenas (comprando / vendiendo)
- liquidity.json                 mapa de liquidez (reporte/liquidez.py): liquidaciones
                                 estimadas por tramo, máximos/mínimos sin barrer y muros
- strategy.json                  estrategia 4h (estrategia/backtest.py): trades desde 2018,
                                 curva de capital contra comprar y mantener, métricas
                                 dentro y fuera de muestra y trades del ejecutor
                                 (los charts llevan además EMA 1200, canal Donchian y stop)
- lab_4h.json                    velas spot 4h desde 2017 para el laboratorio de backtest del
                                 panel (se baja solo al abrirlo)
- market.json                    diario: market cap y volumen 24 h de BTC (CoinGecko) y stablecoins
                                 (DefiLlama): oferta, emisión neta diaria de USDT y USDC y su percentil
- regime.json                    lectura de régimen (estrategia/regimen.py): estado de hoy por
                                 factor y qué pasó 7 y 30 días después en días parecidos

Los JSON van por columna ({"t": [...], "close": [...]}) en segundos UTC,
mucho más livianos que los JSON fila por fila que exporta storage.py.

Uso:
    python build_dashboard.py               # usa data/
    python build_dashboard.py otra/carpeta
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

import indicators
from estrategia import backtest, regimen
from reporte import liquidez
import whale_trades_ws
from storage import last_timestamp

SYMBOL = "BTCUSDT"
# Timeframes del panel: None = velas bajadas de Binance; si no, se arman agrupando las de ese timeframe.
# 3h y 5h no existen en Binance. 2h sale de 1h para no bajar otra serie; 1w sale de 4h, que llega
# hasta 2017 (con 1d, que arranca en 2024, la SMA 200 semanal no existiría).
TIMEFRAMES = {"1h": None, "2h": "1h", "3h": "1h", "4h": None, "5h": "1h", "1d": None, "1w": "4h"}
PERIOD_S = {"1h": 3600, "2h": 2 * 3600, "3h": 3 * 3600, "4h": 4 * 3600, "5h": 5 * 3600, "1d": 86400, "1w": 7 * 86400}
# En horas a propósito: pandas ignora `origin` con frecuencias en días ("1D", "7D") y las semanas quedarían
# empezando el día de la semana del primer dato en vez del lunes.
RULE = {"1h": "1h", "2h": "2h", "3h": "3h", "4h": "4h", "5h": "5h", "1d": "24h", "1w": "168h"}
WEEK_ORIGIN = pd.Timestamp("2017-08-14", tz="UTC")  # un lunes: las semanas de Binance empiezan el lunes 00:00 UTC
# Histórico que viaja al navegador. Los indicadores (SMA 200, volatilidad a 30 d)
# se calculan con toda la serie y recién después se recorta, así que el primer
# tramo visible no arranca con huecos. Sin recorte, el JSON de 1h pesaba 4 MB y
# se volvía a bajar entero en cada refresco.
HISTORY_DAYS = {"1h": 180, "2h": 365, "3h": 365, "4h": 730, "5h": 540, "1d": None, "1w": None}
RV_DAYS = 30  # misma ventana que el DVOL (volatilidad implícita a 30 días)
WHALE_MARKETS = whale_trades_ws.MARKETS
WHALE_WINDOWS_H = [1, 4, 24]
WHALE_THRESHOLD = 0.15  # desbalance (compra - venta) / total para decir "comprando" o "vendiendo"

# Período esperado entre filas (s) según el sufijo del archivo; lo usa el
# dashboard para marcar series que dejaron de actualizarse.
SERIES_PERIOD = [
    ("_metrics_5m", 300),
    ("_whale_trades_1m", 60),
    ("_activity_1m", 60),
    ("_book_1m", 60),
    ("_snapshot", 3600),  # una fila por corrida del cron
    ("_funding", 8 * 3600),
    ("_1h", 3600),
    ("_4h", 4 * 3600),
    ("_1d", 86400),
    ("fear_greed", 86400),
]
# Series de fuera de Binance que también se vigilan en "Estado de las series"
EXTERNAL_SERIES = ("fear_greed", "BTC_dvol_1h", "BTC_market_1d", "stablecoins_1d")


def _read(path: Path, cols: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["timestamp", *cols])
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df


def _bucket(obj, tf: str):
    """Resampler alineado a las velas de Binance. Con origin="epoch", 3h cae en 00/03/06... UTC y 5h en una
    grilla fija que no se reinicia cada día (si no, la última vela del día duraría 4 h)."""
    return obj.resample(RULE[tf], origin=WEEK_ORIGIN if tf == "1w" else "epoch")


OHLCV = ["open", "high", "low", "close", "volume"]
IND_COLS = ["sma_50", "sma_200", "rsi_14", "macd", "macd_signal", "macd_hist", "log_return"]


def _candles(data_dir: Path, tf: str) -> tuple[pd.DataFrame, dict | None]:
    """Velas cerradas con indicadores y, si el timeframe se arma agrupando, la vela en curso hasta la
    última vela chica cerrada (el panel le suma la hora en vivo)."""
    src = TIMEFRAMES[tf]
    if src is None:
        candles = _read(data_dir / f"{SYMBOL}_{tf}.csv", OHLCV)
        return candles.join(_read(data_dir / f"{SYMBOL}_{tf}_indicators.csv", IND_COLS)), None
    raw = _read(data_dir / f"{SYMBOL}_{src}.csv", OHLCV)
    g = _bucket(raw, tf)
    c = g.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    count = g.close.count()
    complete = count == PERIOD_S[tf] // PERIOD_S[src]
    partial = None
    if len(c) and not complete.iloc[-1] and count.iloc[-1] > 0:
        last = c.iloc[-1]
        partial = {"t": int(c.index[-1].timestamp()), **{k: round(float(last[k]), 3) for k in OHLCV},
                   "last_src": int(raw.index[-1].timestamp())}
    c = c[complete]  # una vela con huecos de la fuente (caída de Binance) no se muestra a medias
    df = c.reset_index(drop=True)
    df["timestamp"] = (c.index.as_unit("ms").astype("int64")).to_numpy()
    df["datetime_utc"] = ""
    ind = indicators.compute(df).set_index(c.index)
    return c.join(ind[IND_COLS]), partial


def _columns(df: pd.DataFrame, digits: dict[str, int]) -> dict:
    """DataFrame indexado por tiempo -> dict por columna, NaN -> null."""
    out = {"t": df.index.as_unit("s").astype("int64").tolist()}
    for col, nd in digits.items():
        s = df[col].round(nd).astype(object)
        out[col] = s.where(df[col].notna(), None).tolist()
    return out


# Períodos de evaluación: los mismos que imprime estrategia/backtest.py
STRATEGY_PERIODS = [("in-sample 2018-2022", "2018-01-01", "2023-01-01"),
                    ("out-of-sample 2023-hoy", "2023-01-01", None),
                    ("total 2018-hoy", "2018-01-01", None)]


def strategy_frame(data_dir: Path) -> tuple[pd.DataFrame, dict]:
    """Velas spot 4h desde 2017 con las líneas de la estrategia y la simulación completa.

    ema y don se conocen al cierre de su vela; stop es el que está vigente DURANTE la vela
    (fijado al cierre de la anterior), NaN sin posición.
    """
    # Índice sin zona horaria (UTC implícito), como en backtest.py: simulate() compara contra fechas sin zona
    df = pd.read_csv(data_dir / f"{SYMBOL}_4h.csv")
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms")
    entry, exit_, stop = backtest.signals(df, **backtest.PARAMS)
    sim = backtest.simulate(df, entry, exit_, stop, start="2018-01-01")
    p = backtest.PARAMS
    lines = pd.DataFrame({
        "strat_ema": df.close.ewm(span=p["ema_len"], adjust=False, min_periods=p["ema_len"]).mean(),
        "strat_don": df.high.rolling(p["don_len"]).max().shift(),
        "strat_stop": sim["trails"].shift(),
    }, index=df.index)
    return df.join(lines), {"sim": sim, "signals": (entry, exit_, stop)}


def strategy_lines(frame: pd.DataFrame, tf: str, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Lleva las líneas de 4h a la grilla del gráfico sin mirar el futuro."""
    lines = frame[["strat_ema", "strat_don", "strat_stop"]].tz_localize("UTC")
    if tf == "4h":
        return lines.reindex(grid)
    if tf == "1h":
        # ema y don recién se conocen al cierre de la vela de 4h; el stop rige durante ella
        known = lines[["strat_ema", "strat_don"]].copy()
        known.index = known.index + pd.Timedelta(hours=4)
        out = known.reindex(grid, method="ffill", limit=3)
        out["strat_stop"] = lines.strat_stop.reindex(grid, method="ffill", limit=3)
        return out
    # 1d: el valor al cierre del día (el stop, el vigente en la última vela de 4h del día)
    return lines.resample("1D").last().reindex(grid)


def chart_data(data_dir: Path, tf: str, strat: pd.DataFrame | None = None) -> dict:
    candles, partial = _candles(data_dir, tf)
    # Volumen del perpetuo en la grilla spot, para el perfil de volumen (spot + perp)
    src = TIMEFRAMES[tf] or tf
    perp = _read(data_dir / f"{SYMBOL}_perp_{src}.csv", ["volume"]).volume
    if TIMEFRAMES[tf]:
        perp = _bucket(perp, tf).sum(min_count=1)
    candles["perp_volume"] = perp.reindex(candles.index)

    # Volatilidad realizada a 30 días, anualizada (cripto opera 24/7: 8760 h o 365 d por año)
    n = RV_DAYS * 86400 // PERIOD_S[tf]
    per_year = 365 * 86400 / PERIOD_S[tf]
    candles["rv_30d"] = candles.pop("log_return").rolling(n).std() * np.sqrt(per_year) * 100
    dvol = _read(data_dir / "BTC_dvol_1h.csv", ["close"]).close
    if tf != "1h":
        dvol = _bucket(dvol, tf).last()  # cierre de la vela
    candles["dvol"] = dvol.reindex(candles.index)
    # Fear & Greed es diario (00:00 UTC): en 1h cada valor se extiende a las horas de su día
    fng = _read(data_dir / "fear_greed.csv", ["value"]).value
    if PERIOD_S[tf] > 86400:
        candles["fng"] = _bucket(fng, tf).last().reindex(candles.index)  # semanal: el último día de la semana
    else:
        candles["fng"] = fng.reindex(candles.index, method="ffill", limit=PERIOD_S["1d"] // PERIOD_S[tf] * 2)

    # Todo se lleva a la grilla de velas: si una serie tuviera tiempos fuera
    # de ella, el eje del gráfico intercalaría barras vacías entre velas.
    funding = _read(data_dir / f"{SYMBOL}_funding.csv", ["funding_rate"])
    if PERIOD_S[tf] >= 86400:
        funding = _bucket(funding, tf).sum(min_count=1)  # funding acumulado del día o de la semana
    else:
        funding.index = funding.index.floor(RULE[tf])  # vienen con +1 ms a veces (floor usa la misma grilla epoch)
    metrics = _read(data_dir / f"{SYMBOL}_metrics_5m.csv",
                    ["open_interest_usd", "top_trader_position_ls_ratio", "global_account_ls_ratio"])
    metrics = _bucket(metrics, tf).last()  # niveles: el último valor de cada vela
    derivs = funding.join(metrics, how="outer").dropna(how="all")
    # Las métricas llegan hasta hace 5 min; se cortan en la última vela
    # cerrada para no mostrar la hora (o el día) en curso.
    derivs = derivs[derivs.index <= candles.index[-1]]

    strat_cols = {}
    if strat is not None and tf in ("1h", "4h", "1d"):  # las líneas de la estrategia 4h, solo en la grilla nativa
        candles = candles.join(strategy_lines(strat, tf, candles.index))
        strat_cols = {"strat_ema": 2, "strat_don": 2, "strat_stop": 2}

    if HISTORY_DAYS[tf]:
        start = candles.index[-1] - pd.Timedelta(days=HISTORY_DAYS[tf])
        candles = candles[candles.index >= start]
        derivs = derivs[derivs.index >= start]

    whales = whale_candles(data_dir, tf, candles.index)

    return {
        "timeframe": tf,
        "partial": partial,
        "candles": _columns(candles, {"open": 2, "high": 2, "low": 2, "close": 2,
                                      "volume": 3, "perp_volume": 3, "sma_50": 2, "sma_200": 2, "rsi_14": 2,
                                      "macd": 2, "macd_signal": 2, "macd_hist": 2,
                                      "rv_30d": 2, "dvol": 2, "fng": 0, **strat_cols}),
        "derivs": _columns(derivs, {"funding_rate": 8, "open_interest_usd": 0,
                                    "top_trader_position_ls_ratio": 4, "global_account_ls_ratio": 4}),
        "whales": None if whales is None else _columns(whales, {c: 0 for c in whales.columns}),
    }


def _iso(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x, nd: int):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def strategy_data(data_dir: Path, frame: pd.DataFrame, extra: dict) -> dict:
    sim = extra["sim"]
    close = frame.close
    trades = [{"entry_t": _iso(t["entry_time"]), "entry_price": _num(t["entry_price"], 2),
               "exit_t": _iso(t["exit_time"]) if "exit_time" in t else None,
               "exit_price": _num(t.get("exit_price"), 2), "reason": t.get("exit_reason"),
               "ret_pct": _num(t["ret"] * 100, 2) if "ret" in t else
               _num((close.iloc[-1] * (1 - backtest.COST) / (t["entry_price"] * (1 + backtest.COST)) - 1) * 100, 2)}
              for t in sim["trades"]]
    # Curva diaria (base 100) de la estrategia y de comprar y mantener desde el mismo inicio
    curve = sim["curve"].resample("1D").last().dropna()
    bh = close[close.index >= curve.index[0]].resample("1D").last().reindex(curve.index)
    bh = bh / close[close.index >= sim["curve"].index[0]].iloc[0]
    dd = curve / curve.cummax() - 1
    metrics = []
    for name, start, end in STRATEGY_PERIODS:
        m = backtest.period_stats(frame, *extra["signals"], start=start, end=end)
        metrics.append({"period": name, **{k: _num(v, 4) for k, v in m.items()}})
    executor = None
    ex_path = data_dir / "trading" / "ejecutor.json"
    if ex_path.exists():
        e = json.loads(ex_path.read_text())
        executor = {"real": e.get("real"), "trades": e.get("trades") or [], "position": e.get("position")}
    return {"params": backtest.PARAMS, "cost_per_side": backtest.COST, "trades": trades,
            "curve": {"t": curve.index.as_unit("s").astype("int64").tolist(),
                      "strategy": [round(float(v) * 100, 2) for v in curve],
                      "buy_hold": [_num(v * 100, 2) for v in bh], "drawdown": [round(float(v) * 100, 2) for v in dd]},
            "metrics": metrics, "executor": executor}


def _whale_minutes(data_dir: Path) -> pd.DataFrame | None:
    path = data_dir / f"{SYMBOL}_whale_trades_1m.csv"
    if not path.exists() or last_timestamp(path) is None:
        return None
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df.drop(columns="datetime_utc")


def whale_candles(data_dir: Path, tf: str, grid: pd.DatetimeIndex) -> pd.DataFrame | None:
    """Flujo neto de ballenas (compra - venta, USD) por vela: total y por mercado."""
    df = _whale_minutes(data_dir)
    if df is None:
        return None
    out = pd.DataFrame(index=df.index)
    for m in WHALE_MARKETS:
        out[m] = df[f"{m}_whale_buy_usd"] - df[f"{m}_whale_sell_usd"]
    out = _bucket(out, tf).sum(min_count=1)
    out["net"] = out[WHALE_MARKETS].sum(axis=1, min_count=1)
    # Solo velas cerradas y dentro de la grilla de velas spot
    return out[out.index.isin(grid)].dropna(how="all")


def whale_status(data_dir: Path) -> dict | None:
    """Lectura de ballenas en ventanas de 1, 4 y 24 h, más el posicionamiento de top traders."""
    df = _whale_minutes(data_dir)
    if df is None:
        return None
    end = df.index[-1] + pd.Timedelta(minutes=1)
    windows = []
    for h in WHALE_WINDOWS_H:
        w = df[df.index >= end - pd.Timedelta(hours=h)]
        buy = sum(w[f"{m}_whale_buy_usd"].sum() for m in WHALE_MARKETS)
        sell = sum(w[f"{m}_whale_sell_usd"].sum() for m in WHALE_MARKETS)
        total = sum(w[f"{m}_buy_usd"].sum() + w[f"{m}_sell_usd"].sum() for m in WHALE_MARKETS)
        imb = (buy - sell) / (buy + sell) if buy + sell else 0.0
        windows.append({
            "hours": h, "minutes": len(w), "buy_usd": round(float(buy), 0), "sell_usd": round(float(sell), 0),
            "net_usd": round(float(buy - sell), 0), "imbalance": round(float(imb), 4),
            "share_of_volume": round(float((buy + sell) / total), 4) if total else None,
            "n_orders": int(sum(w[f"{m}_whale_{s}_n"].sum() for m in WHALE_MARKETS for s in ("buy", "sell"))),
            "by_market": {m: round(float(w[f"{m}_whale_buy_usd"].sum() - w[f"{m}_whale_sell_usd"].sum()), 0)
                          for m in WHALE_MARKETS},
            "reading": "comprando" if imb >= WHALE_THRESHOLD else "vendiendo" if imb <= -WHALE_THRESHOLD
                       else "neutral",
        })
    # Top traders de Binance (mayores cuentas por margen): ratio long/short de sus posiciones y cambio en 24 h
    top = None
    mpath = data_dir / f"{SYMBOL}_metrics_5m.csv"
    if mpath.exists():
        m = _read(mpath, ["top_trader_position_ls_ratio", "global_account_ls_ratio"]).dropna()
        if len(m):
            now, prev = m.iloc[-1], m[m.index <= m.index[-1] - pd.Timedelta(hours=24)]
            top = {"ratio": round(float(now.top_trader_position_ls_ratio), 4),
                   "retail_ratio": round(float(now.global_account_ls_ratio), 4),
                   "change_24h": round(float(now.top_trader_position_ls_ratio
                                             - prev.top_trader_position_ls_ratio.iloc[-1]), 4) if len(prev) else None}
    return {"since": int(df.index[0].timestamp()), "min_usd": whale_trades_ws.MIN_USD, "threshold": WHALE_THRESHOLD,
            "windows": windows, "top_traders": top}


def day_stats(data_dir: Path) -> dict | None:
    """Precio, variación, máximo, mínimo y volumen de las últimas 24 velas de 1h.

    Es el respaldo de la franja de arriba del panel: el navegador la pisa con el
    ticker en vivo de Binance, pero si el WebSocket no conecta se ve esto.
    """
    path = data_dir / f"{SYMBOL}_1h.csv"
    if not path.exists():
        return None
    c = _read(path, ["open", "high", "low", "close", "volume"]).tail(24)
    if len(c) < 24:
        return None
    first, last = c.open.iloc[0], c.close.iloc[-1]
    return {"t": int(c.index[-1].timestamp()) + 3600, "price": round(float(last), 2),
            "change_pct": round(float((last - first) / first * 100), 3),
            "high": round(float(c.high.max()), 2), "low": round(float(c.low.min()), 2),
            "volume": round(float(c.volume.sum()), 3)}


def status_data(data_dir: Path) -> dict:
    series = []
    for csv_path in sorted(data_dir.glob("*.csv")):
        if csv_path.stem.endswith("_indicators"):
            continue  # misma frescura que sus velas
        if not (csv_path.stem.startswith(SYMBOL) or csv_path.stem in EXTERNAL_SERIES):
            continue  # otros símbolos: restos de configuraciones viejas, ya no se actualizan
        period = next((p for suffix, p in SERIES_PERIOD if suffix in csv_path.stem), None)
        last = last_timestamp(csv_path)
        series.append({"name": csv_path.stem, "last": last // 1000 if last else None, "period": period})

    snap = None
    snap_path = data_dir / f"{SYMBOL}_snapshot.csv"
    if snap_path.exists():
        rows = pd.read_csv(snap_path)
        if len(rows):
            row = rows.iloc[-1]
            snap = {k: (None if pd.isna(v) else v.item() if hasattr(v, "item") else v) for k, v in row.items()}
    strategy = None
    state_path = data_dir / "estrategia" / "state.json"
    if state_path.exists():
        strategy = json.loads(state_path.read_text())
    paper = None
    paper_path = data_dir / "estrategia" / "paper.json"
    if paper_path.exists():
        paper = json.loads(paper_path.read_text())
    ml = None
    ml_path = data_dir / "ml" / "prediction.json"
    if ml_path.exists():
        ml = json.loads(ml_path.read_text())
    executor = None
    ex_path = data_dir / "trading" / "ejecutor.json"
    if ex_path.exists() and _config().get("trading", {}).get("ejecutor", {}).get("enabled"):
        e = json.loads(ex_path.read_text())
        executor = {k: e.get(k) for k in ("real", "capital_usdt", "equity_usdt", "return_pct", "position", "updated")}
        executor["last_trade"] = e["trades"][-1] if e.get("trades") else None
        executor["n_trades"] = len(e.get("trades") or [])
        executor["last_event"] = e["events"][-1] if e.get("events") else None
    liq = None
    liq_path = data_dir / f"{SYMBOL}_liquidations.csv"
    if liq_path.exists() and liq_path.stat().st_size > 0:
        rows = pd.read_csv(liq_path)
        w = rows[rows.timestamp >= (time.time() - 86400) * 1000]
        liq = {"since": int(rows.timestamp.iloc[0]) // 1000 if len(rows) else None, "events_24h": len(w),
               "long_usd_24h": round(float(w.usd[w.side == "long"].sum()), 2),
               "short_usd_24h": round(float(w.usd[w.side == "short"].sum()), 2)}
    return {"generated": int(time.time()), "series": series, "snapshot": snap,
            "strategy": strategy, "paper": paper, "executor": executor, "ml": ml, "liquidations": liq, "whales": whale_status(data_dir),
            "day": day_stats(data_dir)}


def liquidity_data(data_dir: Path) -> dict:
    """Cada parte por separado: si una falla (p. ej. sin red para los libros), las otras se muestran igual."""
    out = {"generated": int(time.time()),
           "model": "OI de Binance 30 días, apalancamiento supuesto "
                    + ", ".join(f"{l}x {p:.0%}" for l, p in liquidez.LEVERAGE_MIX.items())}
    try:
        price, bins = liquidez.estimated_bins(data_dir)
        out["estimated"] = {"price": round(price, 2), "bin_pct": 0.5,
                            "bins": bins.round({"price": 2, "usd": 0}).to_dict(orient="records")}
    except (OSError, KeyError, IndexError, ValueError) as e:
        out["estimated"] = {"error": f"{type(e).__name__}: {e}"}
    try:
        out["swing_pools"] = liquidez.swing_pools(data_dir)
    except (OSError, KeyError, IndexError, ValueError) as e:
        out["swing_pools"] = {"error": f"{type(e).__name__}: {e}"}
    out["walls"] = liquidez.order_book_walls()  # maneja sus propios errores de red
    return out


STABLE_STRONG_PCTL = 95  # emisión neta por encima (o quema por debajo de 100 - esto) = día fuerte
MARKET_DAYS = 400  # lo que viaja al navegador; la API gratis de CoinGecko da 365 días


def market_data(data_dir: Path) -> dict | None:
    """Series diarias para ver si entra o sale dinero: market cap, volumen y stablecoins.

    Cada fila es un día cerrado (timestamp = apertura del día, como las velas de 1d).
    """
    mpath, spath = data_dir / "BTC_market_1d.csv", data_dir / "stablecoins_1d.csv"
    if not mpath.exists() and not spath.exists():
        return None
    parts = []
    if mpath.exists():
        parts.append(_read(mpath, ["price_usd", "market_cap_usd", "volume_24h_usd"]))
    if spath.exists():
        parts.append(_read(spath, ["stablecoins_usd", "usdt", "usdc"]))
    df = pd.concat(parts, axis=1).sort_index()
    for col in ("price_usd", "market_cap_usd", "volume_24h_usd", "stablecoins_usd", "usdt", "usdc"):
        if col not in df:
            df[col] = np.nan
    # promedio de 30 días del volumen, para decir si un día movió más o menos que lo normal
    df["volume_avg_30d"] = df.volume_24h_usd.rolling(30, min_periods=20).mean()
    # Emisión neta (creadas - quemadas) por día. La señal usa USDT + USDC (más del 80 % del total):
    # el total salta cada vez que DefiLlama empieza a contar una stablecoin nueva.
    major = df.usdt + df.usdc
    df["usdt_net"], df["usdc_net"], df["stable_net"] = df.usdt.diff(), df.usdc.diff(), df.stablecoins_usd.diff()
    df["major_net"] = major.diff()
    df["major_net_7d"], df["major_net_30d"] = major.diff(7), major.diff(30)
    # Percentil del día contra los 365 anteriores: Tether emite en bloques, la distribución
    # tiene colas largas y un desvío estándar exageraría los días comunes.
    df["major_net_pctl"] = df.major_net.rolling(366, min_periods=90).apply(
        lambda w: (w[:-1] < w[-1]).mean() * 100 + (w[:-1] == w[-1]).mean() * 50, raw=True)
    df = df.tail(MARKET_DAYS)
    return {"generated": int(time.time()),
            **_columns(df, {"price_usd": 2, "market_cap_usd": 0, "volume_24h_usd": 0,
                            "volume_avg_30d": 0, "stablecoins_usd": 0, "usdt_net": 0, "usdc_net": 0,
                            "stable_net": 0, "major_net": 0, "major_net_7d": 0, "major_net_30d": 0,
                            "major_net_pctl": 0}),
            "stable_strong_pctl": STABLE_STRONG_PCTL}


def _write(dest: Path, obj: dict) -> None:
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), allow_nan=False))
    tmp.replace(dest)


def _config() -> dict:
    path = Path(__file__).with_name("config.json")
    return json.loads(path.read_text()) if path.exists() else {}


def build(data_dir: Path) -> Path:
    out_dir = data_dir / "dashboard"
    out_dir.mkdir(exist_ok=True)
    strat = extra = None
    # La estrategia 4h (Donchian) va al panel solo si está activa en config.json ("strategy" en la raíz;
    # pausada, queda bajo "pausado"). Si no, se borran sus archivos para no servir datos congelados.
    if "strategy" in _config():
        try:
            strat, extra = strategy_frame(data_dir)
        except (OSError, KeyError, ValueError) as e:  # sin velas 4h: los gráficos salen igual, sin la estrategia
            print(f"estrategia: {type(e).__name__}: {e}")
    else:
        for name in ("strategy.json", "lab_4h.json"):
            (out_dir / name).unlink(missing_ok=True)
    for tf in PERIOD_S:
        _write(out_dir / f"chart_{tf}.json", chart_data(data_dir, tf, strat))
    if strat is not None:
        _write(out_dir / "strategy.json", strategy_data(data_dir, strat, extra))
        lab = strat[["open", "high", "low", "close"]]
        _write(out_dir / "lab_4h.json", {"params": backtest.PARAMS, "cost_per_side": backtest.COST,
                                         "t": lab.index.as_unit("s").astype("int64").tolist(),
                                         **{k[0]: lab[k].round(2).tolist() for k in lab.columns}})
    try:
        _write(out_dir / "regime.json", regimen.regime(data_dir))
    except (OSError, KeyError, ValueError) as e:
        print(f"régimen: {type(e).__name__}: {e}")
    market = market_data(data_dir)
    if market is not None:
        _write(out_dir / "market.json", market)
    _write(out_dir / "status.json", status_data(data_dir))
    _write(out_dir / "liquidity.json", liquidity_data(data_dir))
    return out_dir


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("data")
    print(build(d))
