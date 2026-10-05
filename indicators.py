"""Indicadores técnicos calculados a partir de las velas OHLCV guardadas.

Uso directo (recalcula sobre los CSV ya descargados, sin ir a Binance):
    python indicators.py data/BTCUSDT_1h.csv [data/BTCUSDT_1d.csv ...]

Convenciones (las mismas que TradingView):
- RSI y ATR usan el suavizado de Wilder (EMA con alfa = 1/n).
- Bollinger usa desvío estándar poblacional (ddof=0).
- Las primeras filas de cada indicador quedan vacías hasta tener historia
  suficiente (p. ej. SMA 200 recién aparece en la vela 200).
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd


def wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False, min_periods=n).mean()


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Devuelve un DataFrame con timestamp, datetime_utc, close y los indicadores."""
    close, high, low, volume = df["close"], df["high"], df["low"], df["volume"]
    out = df[["timestamp", "datetime_utc", "close"]].copy()

    # Tendencia
    for n in (20, 50, 200):
        out[f"sma_{n}"] = close.rolling(n).mean()
    for n in (12, 26, 50):
        out[f"ema_{n}"] = ema(close, n)

    # MACD (12, 26, 9)
    macd = ema(close, 12) - ema(close, 26)
    out["macd"] = macd
    out["macd_signal"] = ema(macd, 9)
    out["macd_hist"] = macd - out["macd_signal"]

    # RSI 14
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), 14)
    loss = wilder(-delta.clip(upper=0), 14)
    out["rsi_14"] = 100 - 100 / (1 + gain / loss)

    # Bandas de Bollinger (20, 2)
    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    out["bb_upper"] = mid + 2 * std
    out["bb_lower"] = mid - 2 * std
    out["bb_pct_b"] = (close - out["bb_lower"]) / (out["bb_upper"] - out["bb_lower"])

    # Volatilidad
    prev_close = close.shift()
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    true_range.iloc[0] = high.iloc[0] - low.iloc[0]
    out["atr_14"] = wilder(true_range, 14)
    log_ret = np.log(close / prev_close)
    out["log_return"] = log_ret
    out["volatility_20"] = log_ret.rolling(20).std()  # desvío de retornos por vela, sin anualizar

    # Volumen
    out["obv"] = (np.sign(delta).fillna(0) * volume).cumsum()

    return out


def write_indicators(csv_path: Path, export_json: bool = False) -> Path:
    df = pd.read_csv(csv_path)
    out = compute(df)
    dest = csv_path.with_name(f"{csv_path.stem}_indicators.csv")
    tmp = dest.with_suffix(".csv.tmp")
    out.to_csv(tmp, index=False, float_format="%.8g")
    tmp.replace(dest)
    if export_json:
        tmp = dest.with_suffix(".json.tmp")
        out.to_json(tmp, orient="records", double_precision=10)
        tmp.replace(dest.with_suffix(".json"))
    return dest


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        print(write_indicators(Path(arg), export_json=True))
