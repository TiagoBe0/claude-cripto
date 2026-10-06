"""Ronda 8: BTC spot siempre, cubierto con un corto en el perpetuo según P(movimiento) del ML (HIPOTESIS.md, ronda 8).

Uso:
    python -m investigacion.ronda8              # período de investigación (sin holdout)
    python -m investigacion.ronda8 --holdout    # UNA sola corrida en el holdout, con todo cerrado

La señal es la de la ronda 7 sin cambios (walk-forward del LightGBM y percentil a 90 días). Lo que cambia es cómo
se baja la exposición: un corto en el perpetuo (0,02 % por lado y cobra el funding positivo) en vez de vender spot
(0,10 %), y con permanencia de 24 h en vez de 4 h.

Retorno de la hora siguiente a la decisión, como fracción del capital:
    spot − cobertura × retorno del perpetuo + cobertura × funding cobrado en esa hora − |Δ cobertura| × costo

Escribe data/research/ronda8.json (o ronda8_holdout.json).
"""

import argparse
import json

import numpy as np
import pandas as pd

from investigacion import ronda7 as r7
from investigacion.estudio import DATA, HOLDOUT, ROOT

HEDGE = 0.5
COST_MAKER, COST_TAKER = 0.0002, 0.0005
SPOT_ENTRY = 0.001  # comprar el spot la primera hora, igual que comprar y mantener
RULES = dict(pctl=80, hold=24)
GRID = [(p, hold) for p in (70, 80, 90) for hold in (12, 24, 48)]


def load() -> pd.DataFrame:
    d = r7.load()
    perp = pd.read_csv(ROOT / "data" / "BTCUSDT_perp_1h.csv", usecols=["timestamp", "close"])
    perp.index = pd.to_datetime(perp.pop("timestamp"), unit="ms", utc=True)
    pc = perp.close.reindex(d.index)
    d["perp_ret_next"] = pc.shift(-1) / pc - 1
    missing = d.perp_ret_next.isna() & d.ret_next.notna()
    if missing.any():  # sin vela del perpetuo: se usa el spot (la base de una hora es despreciable)
        print(f"ojo: {int(missing.sum())} horas sin perpetuo, se usa el retorno spot")
        d.loc[missing, "perp_ret_next"] = d.ret_next[missing]
    f = pd.read_csv(ROOT / "data" / "BTCUSDT_funding.csv", usecols=["timestamp", "funding_rate"])
    f.index = pd.to_datetime(f.pop("timestamp") // 3_600_000 * 3_600_000, unit="ms", utc=True)
    f = f.funding_rate.groupby(level=0).sum()
    # La fila de la hora t se decide a las t + 1 h y rige hasta t + 2 h: cobra el funding de las t + 2 h
    d["fund_next"] = f.reindex(d.index + pd.Timedelta(hours=2)).fillna(0.0).to_numpy()
    return d


def hedge(pctl: pd.Series, thr: float, hold: int) -> pd.Series:
    """Cobertura decidida al cierre de la hora: activa si en alguna de las últimas `hold` horas el percentil >= thr."""
    hit = (pctl >= thr).astype(float).where(pctl.notna())
    on = hit.rolling(hold, min_periods=1).max() == 1
    return pd.Series(np.where(on, HEDGE, 0.0), index=pctl.index)


def returns(d: pd.DataFrame, hdg: pd.Series, start, end, cost: float) -> tuple[pd.Series, dict]:
    m = (d.index >= start) & (d.index < end)
    x, h = d[m], hdg[m]
    trade = h.diff().abs().fillna(h.iloc[0])
    fund = h * x.fund_next
    net = x.ret_next - h * x.perp_ret_next + fund - trade * cost
    net.iloc[0] -= SPOT_ENTRY
    net = net.dropna()
    return net, {"cobertura_media": round(float(h.mean()), 3), "cambios": int((h.diff().abs() > 0).sum()),
                 "costos_pct": round(float(trade.sum() * cost * 100), 2), "funding_cobrado_pct": round(float(fund.sum() * 100), 2)}


def evaluate(d: pd.DataFrame, start, end, cost: float = COST_MAKER) -> dict:
    out = {}
    for name, h in (("E", hedge(d.p_move_pctl, **RULES)), ("V", hedge(d.rv24_pctl, **RULES)),
                    ("comprar_y_mantener", pd.Series(0.0, index=d.index))):
        net, extra = returns(d, h, start, end, cost)
        out[name] = r7.metrics(net, extra)
    return out


def run(holdout: bool) -> dict:
    d = load()
    if holdout:
        res = {"holdout": evaluate(d, str(HOLDOUT.date()), r7.HOLDOUT_END)}
        e, v, bh = (res["holdout"][k] for k in ("E", "V", "comprar_y_mantener"))
        res["pasa"] = bool(e["sharpe"] >= bh["sharpe"] and e["sharpe"] > v["sharpe"] and e["caida_max_pct"] > bh["caida_max_pct"])
        return res
    res = {"total": evaluate(d, *r7.RESEARCH), **{k: evaluate(d, *v) for k, v in r7.HALVES.items()},
           "total_taker": evaluate(d, *r7.RESEARCH, cost=COST_TAKER)}
    grid = []
    for p, hold in GRID:
        net, extra = returns(d, hedge(d.p_move_pctl, p, hold), *r7.RESEARCH, COST_MAKER)
        grid.append({"percentil": p, "permanencia_h": hold, **r7.metrics(net, extra)})
    res["grilla"] = grid
    bh = res["total"]["comprar_y_mantener"]
    crit = {
        "1_sharpe_E_mayor_que_comprar_y_mantener_en_cada_mitad":
            all(res[h]["E"]["sharpe"] > res[h]["comprar_y_mantener"]["sharpe"] for h in r7.HALVES),
        "2_sharpe_E_mayor_que_V_en_cada_mitad": all(res[h]["E"]["sharpe"] > res[h]["V"]["sharpe"] for h in r7.HALVES),
        "3_caida_E_menor_que_comprar_y_mantener": res["total"]["E"]["caida_max_pct"] > bh["caida_max_pct"],
        "4_con_taker_sharpe_E_mayor_que_comprar_y_mantener":
            res["total_taker"]["E"]["sharpe"] > res["total_taker"]["comprar_y_mantener"]["sharpe"],
        "5_grilla_6_de_9": sum(g["sharpe"] > bh["sharpe"] for g in grid) >= 6,
    }
    res["criterios"], res["pasa"] = crit, all(crit.values())
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--holdout", action="store_true", help="una sola corrida en el holdout")
    args = ap.parse_args()
    res = run(args.holdout)
    (DATA / ("ronda8_holdout.json" if args.holdout else "ronda8.json")).write_text(json.dumps(res, indent=2, ensure_ascii=False))
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
