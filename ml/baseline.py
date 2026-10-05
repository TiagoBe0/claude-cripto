"""Baseline walk-forward: ¿las features de dataset_1h.csv predicen las próximas H velas?

Uso:
    python ml/baseline.py              # label_4h, test mensual desde 2025-01
    python ml/baseline.py --horizon 24 --test-start 2025-06
    python ml/baseline.py --since 2026-10-04 --test-start 2026-12   # solo el período con
        ballenas, liquidaciones y libro: así esas features pasan el filtro de cobertura

Compara tres modelos que dan P(short), P(neutral), P(long):
- prior     frecuencia de cada clase en el train (lo que hay que superar)
- logistic  regresión logística multinomial (features estandarizadas, NaN -> mediana)
- lgbm      LightGBM chico y regularizado (maneja NaN solo)

Walk-forward: para cada mes de test se entrena con todo lo anterior, descartando las
últimas H horas del train (su etiqueta mira dentro del mes de test: sería filtración).
Las features con menos de 50 % de datos (ballenas, liquidaciones, libro) quedan afuera
hasta que junten historia.

Métrica principal: log loss (mide la calidad de las probabilidades, no solo el acierto).
Además: acierto, y retorno medio de las próximas H horas según la señal P(long) - P(short),
para ver si la probabilidad se traduce en algo operable.

Escribe <data_dir>/ml/baseline_preds_{H}h.csv y baseline_report_{H}h.json.
"""

import argparse
import json
import warnings
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parent.parent
CLASSES = np.array([-1.0, 0.0, 1.0])  # short, neutral, long
COST_ROUNDTRIP = 0.001  # ~0,05 %/lado taker en perp


def feature_columns(df: pd.DataFrame) -> list[str]:
    skip = {"timestamp", "datetime_utc", "close"}
    cols = [c for c in df.columns if c not in skip and not c.startswith(("fwd_ret_", "label_"))]
    return [c for c in cols if df[c].notna().mean() >= 0.5]


def models() -> dict:
    return {
        "logistic": make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                                  LogisticRegression(C=0.05, max_iter=2000)),
        "lgbm": lgb.LGBMClassifier(n_estimators=300, learning_rate=0.02, num_leaves=15, min_child_samples=200,
                                   subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0,
                                   verbose=-1, n_jobs=4),
    }


def walk_forward(df: pd.DataFrame, feats: list[str], h: int, test_start: str) -> pd.DataFrame:
    label = f"label_{h}h"
    df = df[df[label].notna()].reset_index(drop=True)
    month = df["datetime_utc"].str[:7]
    out = []
    for m in sorted(month[month >= test_start].unique()):
        test = df[month == m]
        train = df[df["timestamp"] < test["timestamp"].iloc[0] - h * 3_600_000]
        y = train[label].to_numpy()
        prior = np.array([(y == c).mean() for c in CLASSES])
        pred = test[["timestamp", "datetime_utc", label, f"fwd_ret_{h}h"]].copy()
        pred[["prior_short", "prior_neutral", "prior_long"]] = np.tile(prior, (len(test), 1))
        for name, model in models().items():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model.fit(train[feats], y)
                p = model.predict_proba(test[feats])
            p = p[:, [list(model.classes_).index(c) for c in CLASSES]]
            pred[[f"{name}_short", f"{name}_neutral", f"{name}_long"]] = p
        out.append(pred)
        print(f"  {m}: train {len(train):>6} filas, test {len(test):>4}", flush=True)
    return pd.concat(out, ignore_index=True)


def evaluate(pred: pd.DataFrame, h: int) -> dict:
    y, fwd = pred[f"label_{h}h"].to_numpy(), pred[f"fwd_ret_{h}h"].to_numpy()
    month = pred["datetime_utc"].str[:7]
    rep = {}
    for name in ("prior", "logistic", "lgbm"):
        p = pred[[f"{name}_short", f"{name}_neutral", f"{name}_long"]].to_numpy()
        r = {"log_loss": log_loss(y, p, labels=CLASSES),
             "accuracy": accuracy_score(y, CLASSES[p.argmax(1)])}
        if name != "prior":
            q = pred[["prior_short", "prior_neutral", "prior_long"]].to_numpy()
            by_month = [log_loss(y[mk], q[mk], labels=CLASSES) - log_loss(y[mk], p[mk], labels=CLASSES)
                        for mk in (month == m for m in month.unique())]
            r["meses_mejor_que_prior"] = f"{sum(d > 0 for d in by_month)}/{len(by_month)}"
            # Señal: P(long) - P(short). Retorno medio de las H horas siguientes por quintil de la señal
            s = p[:, 2] - p[:, 0]
            q5 = pd.qcut(s, 5, labels=False, duplicates="drop")
            r["fwd_ret_medio_pct_por_quintil"] = [round(float(fwd[q5 == k].mean()) * 100, 4) for k in range(5)]
            # Operar solo cuando la señal es fuerte: long si s > 0.1, short si s < -0.1, neto de costos
            pos = np.select([s > 0.1, s < -0.1], [1.0, -1.0], 0.0)
            act = pos != 0
            r["señal_fuerte_pct_horas"] = round(float(act.mean()) * 100, 1)
            if act.any():
                r["señal_fuerte_ret_medio_neto_pct"] = round(float((pos[act] * fwd[act] - COST_ROUNDTRIP).mean()) * 100, 4)
                r["señal_fuerte_acierto_dir"] = round(float((np.sign(fwd[act]) == pos[act]).mean()), 4)
        rep[name] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()}
    return rep


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--horizon", type=int, default=4)
    ap.add_argument("--test-start", default="2025-01")
    ap.add_argument("--since", help="descarta filas anteriores a esta fecha (p. ej. 2026-10-04)")
    args = ap.parse_args()

    df = pd.read_csv(args.data_dir / "ml" / "dataset_1h.csv")
    if args.since:
        df = df[df["datetime_utc"] >= args.since].reset_index(drop=True)
    feats = feature_columns(df)
    print(f"{len(feats)} features, horizonte {args.horizon} h, test desde {args.test_start}")
    pred = walk_forward(df, feats, args.horizon, args.test_start)
    rep = {"horizon_h": args.horizon, "test_start": args.test_start, "since": args.since, "n_test": len(pred), "features": feats,
           **evaluate(pred, args.horizon)}

    out = args.data_dir / "ml"
    tag = f"{args.horizon}h" + (f"_since{args.since}" if args.since else "")
    pred.to_csv(out / f"baseline_preds_{tag}.csv", index=False, float_format="%.6g")
    (out / f"baseline_report_{tag}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False))

    print(json.dumps({k: v for k, v in rep.items() if k != "features"}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
