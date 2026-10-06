"""Ronda 7: exposición a BTC spot según P(movimiento) del modelo ML (reglas en HIPOTESIS.md, ronda 7).

Uso:
    python -m investigacion.ronda7              # período de investigación (sin holdout)
    python -m investigacion.ronda7 --holdout    # UNA sola corrida en el holdout, con todo cerrado

Walk-forward mensual del LightGBM del panel (ml/baseline.py, sin cambios) desde 2024-10: cada hora tiene una
P(movimiento) fuera de muestra. Su percentil contra los 90 días anteriores decide la exposición a BTC spot de la
hora siguiente. El rival V usa la misma regla con la volatilidad realizada de las últimas 24 h.

Escribe data/research/ronda7_preds.csv (predicciones walk-forward) y ronda7.json (métricas).
"""

import argparse
import json
import warnings

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from investigacion.estudio import DATA, HOLDOUT, ROOT
from ml.baseline import CLASSES, feature_columns, models

H = 4  # horizonte del modelo (horas)
WF_START = "2024-10"  # primeras predicciones: solo para tener 90 días de percentil en 2025-01
PCTL_WINDOW = 90 * 24
RV_HOURS = 24
COST_SIDE = 0.001
RULES = dict(pctl=80, low=0.5)
GRID = [(p, low) for p in (70, 80, 90) for low in (0.0, 0.25, 0.5)]
RESEARCH = ("2025-01-01", str(HOLDOUT.date()))
HALVES = {"A": ("2025-01-01", "2025-06-01"), "B": ("2025-06-01", str(HOLDOUT.date()))}
HOLDOUT_END = "2026-10-05"
YEAR_H = 24 * 365


def walk_forward(df: pd.DataFrame) -> pd.DataFrame:
    label = f"label_{H}h"
    feats = feature_columns(df)
    month = df["datetime_utc"].str[:7]
    out = []
    for m in sorted(month[month >= WF_START].unique()):
        test = df[month == m]
        train = df[(df["timestamp"] < test["timestamp"].iloc[0] - H * 3_600_000) & df[label].notna()]
        model = models()["lgbm"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(train[feats], train[label].to_numpy())
            p = model.predict_proba(test[feats])[:, [list(model.classes_).index(c) for c in CLASSES]]
        out.append(pd.DataFrame({"timestamp": test["timestamp"].to_numpy(), "p_move": 1 - p[:, 1]}))
        print(f"  {m}: train {len(train):>6} filas, test {len(test):>4}", flush=True)
    return pd.concat(out, ignore_index=True)


def load() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "ml" / "dataset_1h.csv")
    preds_path = DATA / "ronda7_preds.csv"
    print(f"walk-forward ({len(feature_columns(df))} features, etiqueta label_{H}h):")
    preds = walk_forward(df)
    preds.to_csv(preds_path, index=False, float_format="%.6g")
    d = df[["timestamp", "close", f"label_{H}h", f"fwd_ret_{H}h"]].merge(preds, on="timestamp", how="left")
    d.index = pd.to_datetime(d.pop("timestamp"), unit="ms", utc=True)
    gaps = (d.index.to_series().diff() != pd.Timedelta(hours=1)).sum() - 1
    if gaps:
        print(f"ojo: {gaps} huecos en la grilla de 1 h (el retorno de la hora siguiente los cruza)")
    lr = np.log(d.close).diff()
    d["rv24"] = lr.rolling(RV_HOURS).std()
    # Percentil contra las 90 días anteriores, la hora incluida (fracción de valores <= el de hoy)
    for col in ("p_move", "rv24"):
        d[f"{col}_pctl"] = d[col].rolling(PCTL_WINDOW, min_periods=PCTL_WINDOW).rank(method="max", pct=True) * 100
    d["ret_next"] = d.close.shift(-1) / d.close - 1  # de este cierre al siguiente: lo que rige la exposición
    return d


def exposure(pctl: pd.Series, thr: float, low: float) -> pd.Series:
    """Exposición decidida al cierre de la hora: reducida si en alguna de las últimas H horas el percentil >= thr."""
    hit = (pctl >= thr).astype(float).where(pctl.notna())
    reduced = hit.rolling(H, min_periods=1).max()
    return pd.Series(np.where(reduced == 1, low, 1.0), index=pctl.index).where(pctl.notna())


def returns(d: pd.DataFrame, w: pd.Series, start, end) -> tuple[pd.Series, dict]:
    m = (d.index >= start) & (d.index < end)
    w, r = w[m].fillna(1.0), d.ret_next[m]
    # entra desde 0 la primera hora; después paga cada cambio de exposición
    trade = w.diff().abs().fillna(w.iloc[0])
    net = (w * r - trade * COST_SIDE).dropna()
    return net, {"exposicion_media": round(float(w.mean()), 3), "costos_pct": round(float(trade.sum() * COST_SIDE * 100), 2),
                 "cambios": int((w.diff().abs() > 0).sum())}


def metrics(net: pd.Series, extra: dict | None = None) -> dict:
    curve = (1 + net).cumprod()
    dd = curve / curve.cummax() - 1
    sharpe = net.mean() / net.std() * np.sqrt(YEAR_H) if net.std() > 0 else np.nan
    return {"retorno_pct": round(float(curve.iloc[-1] - 1) * 100, 1), "sharpe": round(float(sharpe), 2),
            "caida_max_pct": round(float(dd.min()) * 100, 1), **(extra or {})}


def evaluate(d: pd.DataFrame, start, end) -> dict:
    bh = pd.Series(1.0, index=d.index)
    e = exposure(d.p_move_pctl, RULES["pctl"], RULES["low"])
    v = exposure(d.rv24_pctl, RULES["pctl"], RULES["low"])
    out = {}
    for name, w in (("E", e), ("V", v), ("comprar_y_mantener", bh)):
        net, extra = returns(d, w, start, end)
        out[name] = metrics(net, extra)
    return out


def informative(d: pd.DataFrame, start, end) -> dict:
    x = d[(d.index >= start) & (d.index < end)].dropna(subset=["p_move", "rv24", f"fwd_ret_{H}h"])
    moved = x[f"label_{H}h"] != 0
    out = {"auc_movimiento": {"p_move": round(float(roc_auc_score(moved, x.p_move)), 3),
                              "rv24": round(float(roc_auc_score(moved, x.rv24)), 3)}}
    fwd = np.exp(x[f"fwd_ret_{H}h"]) - 1
    for col in ("p_move", "rv24"):
        q = pd.qcut(x[col], 5, labels=False)
        out[f"quintiles_{col}"] = {"ret_4h_medio_pct": [round(float(fwd[q == k].mean()) * 100, 3) for k in range(5)],
                                   "abs_4h_medio_pct": [round(float(fwd[q == k].abs().mean()) * 100, 3) for k in range(5)]}
    return out


def run(holdout: bool) -> dict:
    d = load()
    if holdout:
        res = {"holdout": evaluate(d, str(HOLDOUT.date()), HOLDOUT_END),
               "informativo": informative(d, str(HOLDOUT.date()), HOLDOUT_END)}
        e, v, bh = (res["holdout"][k] for k in ("E", "V", "comprar_y_mantener"))
        res["pasa"] = bool(e["sharpe"] >= bh["sharpe"] and e["sharpe"] > v["sharpe"] and e["caida_max_pct"] > bh["caida_max_pct"])
        return res
    res = {"total": evaluate(d, *RESEARCH), **{k: evaluate(d, *v) for k, v in HALVES.items()}}
    grid = []
    for p, low in GRID:
        net, extra = returns(d, exposure(d.p_move_pctl, p, low), *RESEARCH)
        grid.append({"percentil": p, "exposicion_reducida": low, **metrics(net, extra)})
    res["grilla"] = grid
    res["informativo"] = informative(d, *RESEARCH)
    bh_total = res["total"]["comprar_y_mantener"]
    crit = {
        "1_sharpe_E_mayor_que_comprar_y_mantener_en_cada_mitad":
            all(res[h]["E"]["sharpe"] > res[h]["comprar_y_mantener"]["sharpe"] for h in HALVES),
        "2_sharpe_E_mayor_que_V_en_cada_mitad": all(res[h]["E"]["sharpe"] > res[h]["V"]["sharpe"] for h in HALVES),
        "3_caida_E_menor_que_comprar_y_mantener": res["total"]["E"]["caida_max_pct"] > bh_total["caida_max_pct"],
        "4_grilla_6_de_9": sum(g["sharpe"] > bh_total["sharpe"] for g in grid) >= 6,
    }
    res["criterios"], res["pasa"] = crit, all(crit.values())
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--holdout", action="store_true", help="una sola corrida en el holdout")
    args = ap.parse_args()
    res = run(args.holdout)
    (DATA / ("ronda7_holdout.json" if args.holdout else "ronda7.json")).write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
