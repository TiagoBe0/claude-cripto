"""Predicción en vivo de las próximas H velas de 1 h con el LightGBM del baseline.

Uso:
    python ml/live_model.py            # usa data/ml/dataset_1h.csv

En cada corrida reentrena con todas las filas ya etiquetadas (unos segundos) y
predice la última vela cerrada. Escribe en <data_dir>/ml/:

- prediction.json      P(short), P(neutral), P(long) y P(movimiento) = 1 - P(neutral) de la
                       última vela, su percentil frente a los últimos 90 días y la evaluación
                       en vivo acumulada
- predictions_log.csv  una fila por vela predicha (solo se agrega): sirve para medir el modelo
                       con datos que nunca vio, sin depender del walk-forward

Según el baseline (ml/baseline.py), P(movimiento) tiene información (AUC ~0,70) y la
dirección P(long) vs P(short) es casi azar (AUC ~0,51): el dashboard lo aclara.
"""

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import log_loss, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import Appender, iso_utc, last_timestamp  # noqa: E402

from ml.baseline import CLASSES, feature_columns, models  # noqa: E402

LOG_COLUMNS = ["timestamp", "datetime_utc", "close", "p_short", "p_neutral", "p_long"]
MIN_LIVE_EVAL = 48  # velas evaluadas antes de mostrar métricas en vivo


def live_eval(log_path: Path, df: pd.DataFrame, label: str, prior: np.ndarray) -> dict | None:
    if not log_path.exists():
        return None
    log = pd.read_csv(log_path).drop_duplicates("timestamp", keep="last")
    ev = log.merge(df[["timestamp", label]], on="timestamp").dropna(subset=[label])
    if len(ev) < MIN_LIVE_EVAL:
        return {"n": len(ev), "min_n": MIN_LIVE_EVAL}
    y, p = ev[label].to_numpy(), ev[["p_short", "p_neutral", "p_long"]].to_numpy()
    moved = y != 0
    out = {"n": len(ev), "since_utc": ev["datetime_utc"].iloc[0],
           "log_loss": round(log_loss(y, p, labels=CLASSES), 4),
           "log_loss_prior": round(log_loss(y, np.tile(prior, (len(y), 1)), labels=CLASSES), 4)}
    if 0 < moved.sum() < len(y):
        out["auc_move"] = round(roc_auc_score(moved, 1 - p[:, 1]), 3)
    if moved.sum() > 1 and len(set(y[moved])) == 2:
        d = p[moved, 2] / (p[moved, 2] + p[moved, 0])
        out["auc_direction"] = round(roc_auc_score(y[moved] == 1, d), 3)
    return out


def update(data_dir: Path, horizon: int = 4, threshold_pct: float | None = None) -> dict:
    ml_dir = data_dir / "ml"
    df = pd.read_csv(ml_dir / "dataset_1h.csv")
    label = f"label_{horizon}h"
    feats = feature_columns(df)
    train = df[df[label].notna()]
    y = train[label].to_numpy()
    prior = np.array([(y == c).mean() for c in CLASSES])

    model = models()["lgbm"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(train[feats], y)
        order = [list(model.classes_).index(c) for c in CLASSES]
        recent = df[df["timestamp"] >= df["timestamp"].iloc[-1] - 90 * 24 * 3_600_000]
        p_recent = model.predict_proba(recent[feats])[:, order]
    p = p_recent[-1]
    last = df.iloc[-1]
    ts = int(last["timestamp"])

    log_path = ml_dir / "predictions_log.csv"
    if (last_timestamp(log_path) or 0) < ts:
        with Appender(log_path, LOG_COLUMNS) as app:
            app.write([[ts, float(last["close"]), *(round(float(x), 5) for x in p)]])

    p_move = 1 - p[1]
    pred = {
        "generated_utc": iso_utc(int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)),
        "candle_open_utc": last["datetime_utc"], "close": float(last["close"]),
        "horizon_h": horizon, "threshold_pct": threshold_pct,
        "p_short": round(float(p[0]), 4), "p_neutral": round(float(p[1]), 4), "p_long": round(float(p[2]), 4),
        "p_move": round(float(p_move), 4),
        "p_move_pctile_90d": round(float((1 - p_recent[:, 1] <= p_move).mean() * 100), 1),
        "base_rate": {"short": round(float(prior[0]), 4), "neutral": round(float(prior[1]), 4),
                      "long": round(float(prior[2]), 4)},
        "trained_rows": len(train), "n_features": len(feats),
        "backtest": {"auc_move": 0.70, "auc_direction": 0.51, "ref": "ml/baseline.py, walk-forward 2025-01 a 2026-09"},
        "live": live_eval(log_path, df, label, prior),
    }
    tmp = ml_dir / "prediction.json.tmp"
    tmp.write_text(json.dumps(pred, indent=2, ensure_ascii=False))
    tmp.replace(ml_dir / "prediction.json")
    return pred


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "data"
    cfg = json.loads((root / "config.json").read_text()).get("ml", {})
    h = cfg.get("live_horizon", 4)
    print(json.dumps(update(data_dir, h, cfg.get("threshold_pct", {}).get(str(h))), indent=2, ensure_ascii=False))
