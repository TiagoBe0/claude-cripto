"""Cobertura sugerida y su paper trading: la regla de la ronda 8 (investigacion/HIPOTESIS.md), corriendo en vivo.

Uso (lo llama binance_extract.py en cada corrida si "cobertura" está en config.json):
    python -m estrategia.cobertura              # usa data/ y la sección "cobertura" de config.json

Regla, sin cambios respecto de la ronda 8 (se importan sus funciones):
- 100 % en BTC spot todo el tiempo.
- Corto en el perpetuo por el 50 % del capital si en alguna de las últimas 24 horas el percentil a 90 días de
  P(movimiento) del modelo ML fue ≥ 80. Decisión al cierre de cada hora, rige la hora siguiente.
- Costos 0,02 % por lado sobre la cobertura (maker), 0,10 % al comprar el spot; el corto cobra el funding.

P(movimiento) es fuera de muestra, como en la prueba: para cada mes el LightGBM del panel se entrena con todo lo
anterior a ese mes (menos 4 h) y predice el mes. El modelo de ml/live_model.py se reentrena con todo y su percentil
compara contra horas que ya vio: no sirve para esto. Los meses cerrados se guardan en cobertura_preds.csv y no
se recalculan; el mes en curso se recalcula en cada corrida (mismo train, mismo resultado para las horas pasadas).

Escribe en <data_dir>/estrategia/:
- cobertura.json        estado de ahora (cubrir o no, desde y hasta cuándo), percentil, y el paper trading desde
                        `start` contra comprar y mantener, con su curva por hora
- cobertura_preds.csv   P(movimiento) fuera de muestra por hora, desde 2024-10
- cobertura_log.csv     una fila por hora evaluada (solo se agrega), para auditar que lo ya decidido no cambie
"""

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from investigacion import ronda7 as r7, ronda8 as r8  # noqa: E402
from ml.baseline import CLASSES, feature_columns, models  # noqa: E402
from storage import Appender, iso_utc, last_timestamp  # noqa: E402

SYMBOL = "BTCUSDT"
H1 = 3_600_000
PREDS_COLUMNS = ["timestamp", "p_move", "model_month"]
LOG_COLUMNS = ["timestamp", "datetime_utc", "p_move", "pctl", "hedge", "equity", "buy_hold_equity"]
# Lo que dio la regla en la prueba (investigacion/RESULTADOS.md, ronda 8), para mostrar al lado del paper
BACKTEST = {"investigacion_2025": {"retorno_pct": 28.4, "sharpe": 1.25, "caida_max_pct": -19.6,
                                   "comprar_y_mantener": {"retorno_pct": 29.3, "sharpe": 0.99, "caida_max_pct": -30.9}},
            "holdout_2025_10_a_2026_10": {"retorno_pct": -11.3, "sharpe": -0.30, "caida_max_pct": -34.7,
                                          "comprar_y_mantener": {"retorno_pct": -29.1, "sharpe": -0.57, "caida_max_pct": -53.7}}}


def predictions(data_dir: Path, df: pd.DataFrame) -> pd.DataFrame:
    """P(movimiento) walk-forward mensual, reusando los meses cerrados ya calculados."""
    path = data_dir / "estrategia" / "cobertura_preds.csv"
    cached = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=PREDS_COLUMNS)
    label = f"label_{r7.H}h"
    feats = feature_columns(df)
    month = df["datetime_utc"].str[:7]
    current = month.iloc[-1]
    done = set(cached.model_month.astype(str)) - {current}
    parts = [cached[cached.model_month.astype(str).isin(done)]]
    for m in sorted(month[month >= r7.WF_START].unique()):
        if m in done:
            continue
        test = df[month == m]
        train = df[(df["timestamp"] < test["timestamp"].iloc[0] - r7.H * H1) & df[label].notna()]
        model = models()["lgbm"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model.fit(train[feats], train[label].to_numpy())
            p = model.predict_proba(test[feats])[:, [list(model.classes_).index(c) for c in CLASSES]]
        parts.append(pd.DataFrame({"timestamp": test["timestamp"].to_numpy(), "p_move": 1 - p[:, 1], "model_month": m}))
    out = pd.concat(parts, ignore_index=True).sort_values("timestamp")
    tmp = path.with_suffix(".tmp")
    out.to_csv(tmp, index=False, float_format="%.6g")
    tmp.replace(path)
    return out


def frame(data_dir: Path) -> pd.DataFrame:
    """Lo mismo que arman ronda7.load y ronda8.load, con las predicciones guardadas en vez del walk-forward entero."""
    df = pd.read_csv(data_dir / "ml" / "dataset_1h.csv")
    preds = predictions(data_dir, df)
    d = df[["timestamp", "close"]].merge(preds[["timestamp", "p_move"]], on="timestamp", how="left")
    d.index = pd.to_datetime(d.pop("timestamp"), unit="ms", utc=True)
    d["p_move_pctl"] = d.p_move.rolling(r7.PCTL_WINDOW, min_periods=r7.PCTL_WINDOW).rank(method="max", pct=True) * 100
    d["ret_next"] = d.close.shift(-1) / d.close - 1
    perp = pd.read_csv(data_dir / f"{SYMBOL}_perp_1h.csv", usecols=["timestamp", "close"])
    perp.index = pd.to_datetime(perp.pop("timestamp"), unit="ms", utc=True)
    pc = perp.close.reindex(d.index)
    d["perp_ret_next"] = (pc.shift(-1) / pc - 1).fillna(d.ret_next)
    f = pd.read_csv(data_dir / f"{SYMBOL}_funding.csv", usecols=["timestamp", "funding_rate"])
    f.index = pd.to_datetime(f.pop("timestamp") // H1 * H1, unit="ms", utc=True)
    f = f.funding_rate.groupby(level=0).sum()
    d["fund_next"] = f.reindex(d.index + pd.Timedelta(hours=2)).fillna(0.0).to_numpy()
    return d


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(x, nd=2):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def evaluate(d: pd.DataFrame, start: str, capital: float) -> dict:
    thr, hold = r8.RULES["thr"], r8.RULES["hold"]
    hdg = r8.hedge(d.p_move_pctl, thr, hold)
    last = d.index[-1]  # última vela de 1 h cerrada: la decisión de ahora rige la hora que está corriendo
    hit = d.p_move_pctl >= thr
    on = bool(hdg.iloc[-1] > 0)
    # Desde cuándo rige el estado de ahora (la decisión se toma al cierre de la vela, 1 h después de su apertura)
    changed = hdg.ne(hdg.shift()).cumsum()
    since = hdg[changed == changed.iloc[-1]].index[0] + pd.Timedelta(hours=1)
    state = {
        "candle_open_utc": _iso(last), "hedge": r8.HEDGE if on else 0.0, "net_exposure": 1 - (r8.HEDGE if on else 0.0),
        "since_utc": _iso(since),
        "p_move": _r(d.p_move.iloc[-1], 4), "pctl": _r(d.p_move_pctl.iloc[-1], 1),
        "hours_over_threshold_24h": int(hit.iloc[-hold:].sum()),
    }
    if on:  # se apaga a las 24 h de la última hora con percentil >= 80, si no aparece otra
        last_hit = hit[hit].index[-1]
        state["until_utc"] = _iso(last_hit + pd.Timedelta(hours=hold + 1))

    t0 = pd.Timestamp(start)
    paper = {"start_utc": _iso(t0), "capital_usd": capital}
    if d.index[d.ret_next.notna()].max() < t0:
        paper["waiting"] = True
        return {**state, "paper": paper}
    end = d.index[-1] + pd.Timedelta(hours=1)
    net, extra = r8.returns(d, hdg, t0, end, r8.COST_MAKER)
    bh, _ = r8.returns(d, pd.Series(0.0, index=d.index), t0, end, r8.COST_MAKER)
    curve, bh_curve = (1 + net).cumprod(), (1 + bh).cumprod()
    dd = lambda c: (c / c.cummax().clip(lower=1) - 1).min() * 100  # noqa: E731
    paper.update({
        "waiting": False, "hours": len(net),
        "equity_usd": _r(curve.iloc[-1] * capital), "return_pct": _r((curve.iloc[-1] - 1) * 100),
        "max_dd_pct": _r(dd(curve)), **extra,
        "buy_hold_equity_usd": _r(bh_curve.iloc[-1] * capital), "buy_hold_return_pct": _r((bh_curve.iloc[-1] - 1) * 100),
        "buy_hold_max_dd_pct": _r(dd(bh_curve)),
        # el retorno de la hora t se cobra al cierre de t + 1: la curva se fecha ahí
        "curve": {"t": [int((ts + pd.Timedelta(hours=2)).timestamp()) for ts in curve.index],
                  "equity": [round(float(x), 5) for x in curve], "buy_hold": [round(float(x), 5) for x in bh_curve]},
    })
    return {**state, "paper": paper}


def update(data_dir: Path, start: str, capital: float = 10_000) -> dict:
    out = data_dir / "estrategia"
    out.mkdir(exist_ok=True)
    d = frame(data_dir)
    state = {"generated_utc": iso_utc(int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)),
             "rules": {"hedge": r8.HEDGE, "pctl": r8.RULES["thr"], "hold_h": r8.RULES["hold"],
                       "cost_per_side": r8.COST_MAKER, "spot_entry_cost": r8.SPOT_ENTRY},
             **evaluate(d, start, capital), "backtest": BACKTEST}
    tmp = out / "cobertura.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False))
    tmp.replace(out / "cobertura.json")

    ts = int(d.index[-1].timestamp() * 1000)
    log_path = out / "cobertura_log.csv"
    if (last_timestamp(log_path) or 0) < ts:
        p = state["paper"]
        with Appender(log_path, LOG_COLUMNS) as app:
            app.write([[ts, state["p_move"], state["pctl"], state["hedge"],
                        p.get("equity_usd", ""), p.get("buy_hold_equity_usd", "")]])
    return state


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "config.json").read_text())["cobertura"]
    st = update(root / "data", cfg["start"], cfg.get("capital", 10_000))
    st["paper"].pop("curve", None)
    print(json.dumps(st, indent=2, ensure_ascii=False))
