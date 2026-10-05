"""Ronda P: hipótesis sobre datos propios (ballenas, liquidaciones, libro), reglas en HIPOTESIS.md.
P5 y P6 (profundidad del libro por minuto, libro_1m.py) son la parte en vivo de la ronda 5.

Uso:
    python -m investigacion.propios       # imprime la tabla y escribe data/research/propios.json

Lo corre el cron el día 1 de cada mes. Cada prueba queda "esperando datos" hasta tener 30 eventos independientes;
si alguna pasa, avisa por correo (alertas/mailer.py) para confirmarla con los meses siguientes.
"""

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "data"
OUT = ROOT / "data" / "research" / "propios.json"
SYMBOL = "BTCUSDT"
HORIZONS = (4, 24)
COST = 0.002
MIN_IND = 30
MARKETS = ("binance_spot", "binance_perp", "coinbase")


def _read(name: str) -> pd.DataFrame | None:
    path = LIVE / name
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms")
    return df


def _pct(s: pd.Series, window_h: int, min_h: int) -> pd.Series:
    return s.rolling(window_h, min_periods=min_h).rank(pct=True) * 100


def hourly_frame() -> pd.DataFrame:
    """Una fila por hora (índice = apertura de la vela); las señales usan lo conocido a su cierre."""
    px = _read(f"{SYMBOL}_perp_1h.csv")
    d = pd.DataFrame({"close": px.close})
    # Las demás series se agregan a la hora que las contiene (minuto 00 a 59 de la vela)
    w = _read(f"{SYMBOL}_whale_trades_1m.csv")
    if w is not None:
        net = sum(w[f"{m}_whale_buy_usd"] - w[f"{m}_whale_sell_usd"] for m in MARKETS)
        d["whale_net"] = net.resample("1h").sum(min_count=1)
        d["whale_minutes"] = net.resample("1h").count()
    liq = _read(f"{SYMBOL}_liquidations.csv")
    if liq is not None:
        for side in ("long", "short"):
            d[f"liq_{side}"] = liq.usd[liq.side == side].resample("1h").sum()
        d[["liq_long", "liq_short"]] = d[["liq_long", "liq_short"]].fillna(0)
        d["liq_covered"] = d.index >= liq.index[0].floor("h")
    snap = _read(f"{SYMBOL}_snapshot.csv")
    if snap is not None:
        d["book_imb"] = snap.spot_imbalance_05pct.resample("1h").last()
    orders = _read(f"{SYMBOL}_whale_orders.csv")
    if orders is not None:
        big = orders[orders.usd >= 1_000_000]
        signed = big.usd.where(big.side == "buy", -big.usd)
        d["giant_net"] = signed.resample("1h").sum()
        d["orders_covered"] = d.index >= orders.index[0].floor("h")
    book = _read(f"{SYMBOL}_book_1m.csv")
    if book is not None:
        # promedio de los minutos de la hora; minutos sin dato (fila vacía) no cuentan
        d["depth01"] = (book.perp_bid_01 + book.perp_ask_01).resample("1h").mean()
    for hz in HORIZONS:
        d[f"ret_{hz}"] = d.close.shift(-hz) / d.close - 1
    return d


def signals(d: pd.DataFrame) -> dict:
    s = {}
    if "whale_net" in d:
        x = d.whale_net.where(d.whale_minutes >= 50)  # horas con el recolector andando casi entero
        z = (x - x.rolling(7 * 24, min_periods=3 * 24).mean()) / x.rolling(7 * 24, min_periods=3 * 24).std()
        s[("P1 ballenas", +1)], s[("P1 ballenas", -1)] = z >= 2, z <= -2
    if "liq_long" in d:
        for side, sign in (("long", +1), ("short", -1)):
            x = d[f"liq_{side}"].where(d.liq_covered)
            s[("P2 liquidaciones", sign)] = (_pct(x, 30 * 24, 7 * 24) >= 95) & (x > 0)
    if "book_imb" in d:
        p = _pct(d.book_imb, 30 * 24, 7 * 24)
        s[("P3 libro", +1)], s[("P3 libro", -1)] = p >= 90, p <= 10
    if "giant_net" in d:
        g = d.giant_net.where(d.orders_covered)
        s[("P4 orden gigante", +1)], s[("P4 orden gigante", -1)] = g > 0, g < 0
    if "depth01" in d:
        p = _pct(d.depth01, 7 * 24, 2 * 24)
        # P5 mide volatilidad: +1 = se espera más (libro fino), -1 = menos (libro grueso)
        s[("P5 libro fino → volatilidad", +1)], s[("P5 libro fino → volatilidad", -1)] = p <= 10, p >= 90
        back = (p.shift(1).rolling(6).min() <= 10) & (p >= 50)
        ch6 = d.close / d.close.shift(6) - 1
        s[("P6 vuelven los bots", +1)], s[("P6 vuelven los bots", -1)] = back & (ch6 < 0), back & (ch6 > 0)
    return {k: v.fillna(False).astype(bool) for k, v in s.items()}


def _spaced(idx: pd.DatetimeIndex, hz: int) -> pd.DatetimeIndex:
    keep, last = [], None
    for t in idx:
        if last is None or (t - last) >= pd.Timedelta(hours=hz):
            keep.append(t)
            last = t
    return pd.DatetimeIndex(keep)


def evaluate(d: pd.DataFrame, sig: pd.Series, side: int, hz: int, covered: pd.Series, vol: bool = False) -> dict:
    # vol: la variable es |retorno| (sin costos); side +1 espera más movimiento que la base, -1 menos
    net = side * d[f"ret_{hz}"].abs() if vol else side * d[f"ret_{hz}"] - COST
    base = net[covered].dropna()
    ev = net[sig & covered].dropna()
    ind = net[_spaced(ev.index, hz)] if len(ev) else ev
    out = {"n": int(len(ev)), "n_ind": int(len(ind))}
    if len(ind) < MIN_IND:
        return {**out, "estado": f"esperando datos ({len(ind)}/{MIN_IND})"}
    mid = base.index[len(base) // 2]
    halves = [(ev[ev.index < mid].mean() - base[base.index < mid].mean()),
              (ev[ev.index >= mid].mean() - base[base.index >= mid].mean())]
    excess = ev.mean() - base.mean()
    t = excess / (ind.std(ddof=1) / np.sqrt(len(ind)))
    ok = all(pd.notna(h) and h > 0 for h in halves) and t >= 2
    return {**out, "exceso_%": round(float(excess) * 100, 3), "t": round(float(t), 2),
            "mitades_%": [None if pd.isna(h) else round(float(h) * 100, 3) for h in halves],
            "estado": "PASA (confirmar con los meses siguientes)" if ok else "no pasa"}


def run() -> dict:
    d = hourly_frame()
    rows = []
    for (name, side), sig in signals(d).items():
        # Horas cubiertas por la fuente de esa hipótesis (desde su primer dato)
        col = {"P1": "whale_net", "P2": "liq_long", "P3": "book_imb", "P4": "giant_net",
               "P5": "depth01", "P6": "depth01"}[name[:2]]
        vol = name.startswith("P5")
        lado = ("percentil ≤ 10 (+)" if side > 0 else "percentil ≥ 90 (−)") if vol else "largo" if side > 0 else "corto"
        first = d[col].first_valid_index()
        covered = pd.Series(d.index >= first, index=d.index) if first is not None else pd.Series(False, index=d.index)
        for hz in HORIZONS:
            rows.append({"hipotesis": name, "lado": lado, "H_horas": hz,
                         "datos_desde": None if first is None else str(first), **evaluate(d, sig, side, hz, covered, vol)})
    out = {"generated": int(time.time()), "rows": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    prev = json.loads(OUT.read_text()) if OUT.exists() else {"rows": []}
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    for r in rows:
        print(f"{r['hipotesis']:28s} {r['lado']:18s} H={r['H_horas']:2d} h  eventos {r['n']:4d} ({r['n_ind']:3d} indep.)  {r['estado']}"
              + (f"  exceso {r['exceso_%']:+.2f} % t {r['t']:.1f} mitades {r['mitades_%']}" if "t" in r else ""))
    # Aviso solo cuando una prueba pasa por primera vez
    was = {(r["hipotesis"], r["lado"], r["H_horas"]) for r in prev["rows"] if r["estado"].startswith("PASA")}
    new = [r for r in rows if r["estado"].startswith("PASA") and (r["hipotesis"], r["lado"], r["H_horas"]) not in was]
    if new:
        from alertas import mailer
        text = "\n".join(f"{r['hipotesis']} {r['lado']} a {r['H_horas']} h: exceso {r['exceso_%']:+.2f} %, t {r['t']}, "
                         f"{r['n_ind']} eventos independientes" for r in new)
        mailer.send("investigacion", "Investigación BTC: una hipótesis de datos propios pasó", text
                    + "\n\nHay que confirmarla con los meses siguientes antes de usarla (ver investigacion/HIPOTESIS.md).")
    return out


if __name__ == "__main__":
    run()
