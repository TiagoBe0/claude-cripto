"""Backtest de la lectura del neto de ballenas (alertas/lectura_ballenas.py): ¿la venta en el piso con divergencia
perpetuo/spot y funding negativo marca un agotamiento, y la del techo sigue de largo?

Uso:
    python -m investigacion.ballenas_neto            # usa data/
    python -m investigacion.ballenas_neto otra/carpeta

Rearma todos los netos de 15 min que habrían disparado el aviso, con la misma regla de alertas/ballenas.py
(|neto| >= US$ 100 M, una hora de espera por lado), aunque el tope diario los haya dejado sin correo. A cada uno le
pone la lectura de ese momento y mide el precio spot (mid de book_1m) a 1, 4 y 24 h y el máximo y el mínimo en 24 h.

"A favor" es el retorno con el signo del neto: positivo = el precio siguió para donde empujaban las ballenas.
La hipótesis dice que la absorción da "a favor" negativo (rebote) y la venta en el techo, positivo.
La historia de whale_trades_1m arranca el 2026-10-04: con pocas semanas los grupos tienen un puñado de casos y las
medias no significan nada todavía. Correrlo de nuevo cada tanto.

Escribe data/research/ballenas_neto.csv.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alertas import ballenas, lectura_ballenas as lb  # noqa: E402

HORIZONS_H = [1, 4, 24]


def events(w: pd.DataFrame) -> pd.DataFrame:
    """Netos de 15 filas que habrían avisado, con la espera de una hora por lado."""
    nets = {m: (w[f"{m}_whale_buy_usd"] - w[f"{m}_whale_sell_usd"]).rolling(15).sum() for m in ballenas.MARKETS}
    total = sum(nets.values())
    rows, last = [], {}
    for t in total.index[total.abs() >= ballenas.NET_15M_USD]:
        side = "buy" if total[t] > 0 else "sell"
        if side in last and t - last[side] < pd.Timedelta(seconds=ballenas.NET_COOLDOWN_S):
            continue
        last[side] = t
        rows.append({"t": t, **{m: nets[m][t] for m in ballenas.MARKETS}})
    return pd.DataFrame(rows)


def main() -> None:
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    s = lb.load(data, live=False)
    w = lb._read(data / f"{lb.SYMBOL}_whale_trades_1m.csv", None)
    mid = s["book"].spot_mid
    out = []
    for e in events(w).itertuples(index=False):
        # la fila del minuto t cierra en t + 1 min: el aviso sale recién ahí
        at = e.t + pd.Timedelta(minutes=1)
        f = lb.features(s, at, {m: getattr(e, m) for m in ballenas.MARKETS})
        sign = 1 if f["net_usd"] > 0 else -1
        row = {"t": at, **f}
        for h in HORIZONS_H:
            fut = mid[mid.index >= at + pd.Timedelta(hours=h)]
            row[f"a_favor_{h}h_pct"] = ((fut.iloc[0] / f["price"] - 1) * 100 * sign
                                        if len(fut) and fut.index[0] - at < pd.Timedelta(hours=h, minutes=10) else np.nan)
        win = mid[(mid.index > at) & (mid.index <= at + pd.Timedelta(hours=24))]
        if len(win) and win.index[-1] >= at + pd.Timedelta(hours=23):
            row["max_24h_pct"] = (win.max() / f["price"] - 1) * 100
            row["min_24h_pct"] = (win.min() / f["price"] - 1) * 100
        out.append(row)
    df = pd.DataFrame(out)
    (data / "research").mkdir(exist_ok=True)
    df.to_csv(data / "research" / "ballenas_neto.csv", index=False, float_format="%.6g")

    cols = [f"a_favor_{h}h_pct" for h in HORIZONS_H]
    pd.set_option("display.width", 200, "display.max_rows", 200)
    print(f"{len(df)} netos desde {df.t.min():%d/%m %H:%M} hasta {df.t.max():%d/%m %H:%M} UTC\n")
    g = df.groupby("tag")[cols]
    print("Retorno a favor del neto (%), media y casos:")
    print(pd.concat([g.mean().round(2), g.count().iloc[:, [0]].rename(columns={cols[0]: "n"})], axis=1).to_string())
    print("\nCasos:")
    show = ["t", "tag", "net_usd", "price", "range_pos", "bb_pct_b_4h", "perp_usd", "spot_usd", "funding",
            "taker_ratio", "book_imbalance", *cols, "min_24h_pct", "max_24h_pct"]
    v = df[[c for c in show if c in df]].copy()
    v["t"] = v.t.dt.strftime("%d/%m %H:%M")
    for c in ("net_usd", "perp_usd", "spot_usd"):
        v[c] = (v[c] / 1e6).round(0)
    v["funding"] = (v.funding * 100).round(4)
    print(v.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
