"""Dataset horario para entrenar modelos que estiman P(long) / P(short) de BTC.

Uso:
    python ml/build_dataset.py              # usa data/ y config.json
    python ml/build_dataset.py otra/carpeta

Junta todas las series que guarda el extractor en una grilla de 1 h y escribe
<data_dir>/ml/dataset_1h.csv (una fila por vela de 1 h, se reescribe entera en
cada corrida; las fuentes son append-only, así que el resultado es determinista).

Convención de tiempo (para no filtrar información del futuro):
- `timestamp` es la apertura de la vela de 1 h; la fila se "conoce" en su cierre,
  t_close = timestamp + 1 h. Toda feature usa solo datos con hora <= t_close.
- Las velas de 4 h / 1 d se unen por su hora de cierre, no de apertura.
- Las métricas de 5 m, ballenas y liquidaciones se agregan dentro de [t, t_close).

Etiquetas, para cada horizonte H de config["ml"]["horizons"]:
- fwd_ret_{H}h = log(close[t+H] / close[t])   (retorno futuro, queda NaN al final)
- label_{H}h   = 1 long si fwd_ret > umbral, -1 short si < -umbral, 0 neutral.
  El umbral (en %) sale de config["ml"]["threshold_pct"]; el entrenamiento puede
  redefinir las clases a partir de fwd_ret sin regenerar el CSV.

Las fuentes en vivo (ballenas, liquidaciones, libro) recién empiezan en 2026-10;
antes quedan en NaN. Se mantienen igual para que el histórico crezca solo.
"""

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
H1 = 3_600_000
DEFAULTS = {"symbol": "BTCUSDT", "horizons": [4, 24], "threshold_pct": {"4": 0.5, "24": 1.5}}


def read(data_dir: Path, name: str) -> pd.DataFrame | None:
    path = data_dir / name
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = pd.read_csv(path).drop(columns="datetime_utc", errors="ignore")
    return df.drop_duplicates("timestamp", keep="last").sort_values("timestamp").reset_index(drop=True)


def asof(base: pd.DataFrame, other: pd.DataFrame | None, cols: dict[str, str], on: str = "timestamp",
         tolerance: int | None = None) -> pd.DataFrame:
    """Une a cada fila el último valor de `other` con other[on] <= t_close (cols: origen -> destino)."""
    if other is None:
        for dst in cols.values():
            base[dst] = np.nan
        return base
    right = other[[on, *cols]].rename(columns={on: "_key", **cols}).sort_values("_key")
    merged = pd.merge_asof(base.sort_values("t_close"), right, left_on="t_close", right_on="_key",
                           direction="backward", tolerance=tolerance)
    return merged.drop(columns="_key")


def per_hour(df: pd.DataFrame | None, agg: dict) -> pd.DataFrame | None:
    """Agrega una serie de mayor frecuencia en la vela de 1 h que la contiene."""
    if df is None:
        return None
    df = df.assign(timestamp=df["timestamp"] // H1 * H1)
    return df.groupby("timestamp").agg(**agg).reset_index()


def build(data_dir: Path, cfg: dict) -> pd.DataFrame:
    sym = cfg["symbol"]
    spot = read(data_dir, f"{sym}_1h.csv")
    ind = read(data_dir, f"{sym}_1h_indicators.csv")
    df = spot.merge(ind.drop(columns="close"), on="timestamp", how="left")
    df["t_close"] = df["timestamp"] + H1
    close = df["close"]

    # Precio y técnicos de 1 h (todo relativo al precio para que sea estacionario)
    lc = np.log(close)
    for n in (1, 4, 24, 72, 168):
        df[f"ret_{n}h"] = lc.diff(n)
    for n in (20, 50, 200):
        df[f"dist_sma{n}"] = close / df[f"sma_{n}"] - 1
    df["dist_ema50"] = close / df["ema_50"] - 1
    df["macd_hist_n"] = df["macd_hist"] / close
    df["atr_n"] = df["atr_14"] / close
    df["range_n"] = (df["high"] - df["low"]) / close
    df["vol_rel_24h"] = np.log(df["volume"] / df["volume"].rolling(24).mean())
    df["hour_sin"] = np.sin(2 * np.pi * (df["timestamp"] // H1 % 24) / 24)
    df["hour_cos"] = np.cos(2 * np.pi * (df["timestamp"] // H1 % 24) / 24)
    dow = (df["timestamp"] // (24 * H1) + 3) % 7  # 1970-01-01 fue jueves -> lunes = 0
    df["dow_sin"] = np.sin(2 * np.pi * dow / 7)
    df["dow_cos"] = np.cos(2 * np.pi * dow / 7)

    # Contexto de 4 h y 1 d, unido por hora de cierre de esas velas
    for tf, ms in (("4h", 4 * H1), ("1d", 24 * H1)):
        hi = read(data_dir, f"{sym}_{tf}_indicators.csv")
        if hi is not None:
            hi = hi.assign(timestamp=hi["timestamp"] + ms,
                           dist_sma200=hi["close"] / hi["sma_200"] - 1,
                           dist_sma50=hi["close"] / hi["sma_50"] - 1,
                           macd_hist_n=hi["macd_hist"] / hi["close"])
        df = asof(df, hi, {"rsi_14": f"rsi_14_{tf}", "bb_pct_b": f"bb_pct_b_{tf}",
                           "dist_sma50": f"dist_sma50_{tf}", "dist_sma200": f"dist_sma200_{tf}",
                           "macd_hist_n": f"macd_hist_n_{tf}", "volatility_20": f"volatility_20_{tf}"})

    # Perpetuo: flujo taker, actividad y basis
    perp = read(data_dir, f"{sym}_perp_1h.csv")
    if perp is not None:
        perp = perp.assign(perp_close=perp["close"],
                           perp_taker_buy_ratio=perp["taker_buy_quote_volume"] / perp["quote_volume"],
                           perp_trades_rel_24h=np.log(perp["trades"] / perp["trades"].rolling(24).mean()),
                           perp_qvol_rel_24h=np.log(perp["quote_volume"] / perp["quote_volume"].rolling(24).mean()))
        df = df.merge(perp[["timestamp", "perp_close", "perp_taker_buy_ratio", "perp_trades_rel_24h",
                            "perp_qvol_rel_24h"]], on="timestamp", how="left")
        df["basis"] = df["perp_close"] / close - 1
        df = df.drop(columns="perp_close")
    prem = read(data_dir, f"{sym}_premium_1h.csv")
    if prem is not None:
        df = df.merge(prem[["timestamp", "premium_close"]], on="timestamp", how="left")

    # Métricas de 5 m: OI y ratios long/short, al cierre de la hora
    m = per_hour(read(data_dir, f"{sym}_metrics_5m.csv"), {
        "oi_usd": ("open_interest_usd", "last"),
        "top_acc_ls": ("top_trader_account_ls_ratio", "last"),
        "top_pos_ls": ("top_trader_position_ls_ratio", "last"),
        "global_ls": ("global_account_ls_ratio", "last"),
        "taker_bs_mean": ("taker_buy_sell_ratio", "mean"),
        "taker_bs_last": ("taker_buy_sell_ratio", "last"),
    })
    if m is not None:
        df = df.merge(m, on="timestamp", how="left")
        oi = np.log(df["oi_usd"].where(df["oi_usd"] > 0))  # Binance a veces publica OI = 0
        df["oi_chg_1h"], df["oi_chg_24h"] = oi.diff(1), oi.diff(24)
        df["oi_rel_close"] = oi - lc  # OI en monedas, en log
        for c in ("top_acc_ls", "top_pos_ls", "global_ls"):
            df[f"{c}_chg_24h"] = np.log(df[c]).diff(24)
        df = df.drop(columns="oi_usd")

    # Funding (cada 8 h), DVOL y Fear & Greed: último valor conocido al cierre
    df = asof(df, read(data_dir, f"{sym}_funding.csv"), {"funding_rate": "funding_rate"})
    dvol = read(data_dir, "BTC_dvol_1h.csv")
    if dvol is not None:
        dvol = dvol.assign(timestamp=dvol["timestamp"] + H1, dvol_chg_24h=np.log(dvol["close"]).diff(24))
    df = asof(df, dvol, {"close": "dvol", "dvol_chg_24h": "dvol_chg_24h"})
    df["dvol_minus_rv"] = df["dvol"] - df["ret_1h"].rolling(24 * 30).std() * np.sqrt(24 * 365) * 100
    df = asof(df, read(data_dir, "fear_greed.csv"), {"value": "fear_greed"})

    # Fuentes en vivo (desde 2026-10): ballenas, liquidaciones y libro de órdenes
    w = read(data_dir, f"{sym}_whale_trades_1m.csv")
    if w is not None:
        cols = lambda kind, side: [c for c in w.columns if c.endswith(f"_{kind}{side}_usd")]  # noqa: E731
        w = w.assign(wb=w[cols("whale_", "buy")].sum(axis=1), ws=w[cols("whale_", "sell")].sum(axis=1),
                     tb=w[cols("", "buy")].sum(axis=1), ts=w[cols("", "sell")].sum(axis=1))
    w = per_hour(w, {"wb": ("wb", "sum"), "ws": ("ws", "sum"), "tb": ("tb", "sum"), "ts": ("ts", "sum"),
                     "whale_minutes": ("wb", "size")})
    if w is not None:
        w = w.assign(whale_net_usd=w["wb"] - w["ws"],
                     whale_imbalance=(w["wb"] - w["ws"]) / (w["wb"] + w["ws"]).replace(0, np.nan),
                     flow_imbalance=(w["tb"] - w["ts"]) / (w["tb"] + w["ts"]).replace(0, np.nan))
        df = df.merge(w[["timestamp", "whale_minutes", "whale_net_usd", "whale_imbalance", "flow_imbalance"]],
                      on="timestamp", how="left")
    liq = read(data_dir, f"{sym}_liquidations.csv")
    if liq is not None:
        liq = liq.assign(liq_long=liq["usd"].where(liq["side"] == "long", 0),
                         liq_short=liq["usd"].where(liq["side"] == "short", 0))
        liq = per_hour(liq, {"liq_long_usd": ("liq_long", "sum"), "liq_short_usd": ("liq_short", "sum")})
        df = df.merge(liq, on="timestamp", how="left")
        # El servicio graba desde su primer evento: antes de eso es "sin dato", después "cero liquidaciones"
        live = df["t_close"] > liq["timestamp"].min()
        for c in ("liq_long_usd", "liq_short_usd"):
            df[c] = df[c].where(~live, df[c].fillna(0))
    df = asof(df, read(data_dir, f"{sym}_snapshot.csv"),
              {"spot_spread_bps": "spot_spread_bps", "spot_imbalance_05pct": "spot_imbalance_05pct",
               "perp_imbalance_01pct": "perp_imbalance_01pct"}, tolerance=2 * H1)

    # Etiquetas
    for h in cfg["horizons"]:
        thr = np.log1p(float(cfg["threshold_pct"][str(h)]) / 100)
        fwd = lc.shift(-h) - lc
        df[f"fwd_ret_{h}h"] = fwd
        df[f"label_{h}h"] = np.select([fwd > thr, fwd < -thr], [1, -1], 0).astype(float)
        df.loc[fwd.isna(), f"label_{h}h"] = np.nan

    drop = ["t_close", "open", "high", "low", "volume", "sma_20", "sma_50", "sma_200", "ema_12", "ema_26",
            "ema_50", "macd", "macd_signal", "macd_hist", "bb_upper", "bb_lower", "atr_14", "log_return", "obv"]
    df = df.sort_values("timestamp").drop(columns=drop)
    df.insert(1, "datetime_utc", pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df.reset_index(drop=True)


def update(data_dir: Path, cfg: dict | None = None) -> Path:
    cfg = {**DEFAULTS, **(cfg or {})}
    df = build(data_dir, cfg)
    out_dir = data_dir / "ml"
    out_dir.mkdir(exist_ok=True)
    dest = out_dir / "dataset_1h.csv"
    tmp = dest.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, float_format="%.8g")
    tmp.replace(dest)
    return dest


if __name__ == "__main__":
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    cfg = json.loads((ROOT / "config.json").read_text()).get("ml", {})
    dest = update(data_dir, cfg)
    df = pd.read_csv(dest)
    print(f"{dest}: {len(df)} filas x {df.shape[1]} columnas, {df['datetime_utc'].iloc[0]} -> {df['datetime_utc'].iloc[-1]}")
