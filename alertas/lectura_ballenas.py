"""Contexto de un neto de ballenas de 15 min: dónde está el precio en el rango y si la venta parece absorbida.

Hipótesis (estudio de los correos de octubre de 2026, 4 casos): una venta neta grande en el techo del rango
siguió de largo; en el piso, con el perpetuo vendiendo y el spot comprando y el funding negativo, marcó el
agotamiento de la caída. La usan alertas/ballenas.py (en vivo) e investigacion/ballenas_neto.py (backtest),
con las mismas reglas, fijadas antes de mirar resultados:

- Rango: mínimo y máximo de las últimas RANGE_DAYS velas diarias, incluida la de hoy. Posición 0 = piso, 1 = techo.
- Piso: posición <= 0,25 o %B de Bollinger (20, 2) de 4 h <= 0,10, con el precio de ahora como cierre de la vela
  en curso. Techo: posición >= 0,75 o %B 4 h >= 0,90.
- Divergencia: neto del perpetuo de Binance con el signo del total y neto spot (Binance spot + Coinbase) del
  signo contrario o cero.
- Funding: el estimado del período en curso (snapshot más reciente) negativo para una venta, o más del doble del
  0,01 % de base para una compra.
- Absorción = venta neta en el piso + divergencia + funding negativo. Takers (relación compra/venta del
  perpetuo, promedio de 15 min) <= 0,6 y libro spot ±0,5 % con más compras que ventas suman a favor, pero no se
  exigen. Agotamiento comprador = el espejo, compra neta en el techo; no tiene casos todavía.

Todo sale de lo que ya escriben los recolectores: book_1m (precio y libro), snapshot (funding), metrics_5m
(takers), 1d y 4h (rango y Bollinger).
"""

import io
from pathlib import Path

import numpy as np
import pandas as pd

SYMBOL = "BTCUSDT"
RANGE_DAYS = 21  # tres semanas: con 30 días entraba el piso de 75k de septiembre y no el rango 80-87k de octubre
FLOOR, CEIL = 0.25, 0.75
BB_FLOOR, BB_CEIL = 0.10, 0.90
TAKER_SELL, TAKER_BUY = 0.6, 1 / 0.6
FUNDING_HIGH = 0.0002  # el doble del 0,01 % por 8 h de base de Binance
SPOT = ["binance_spot", "coinbase"]


def _read(path: Path, tail_bytes: int | None) -> pd.DataFrame:
    if tail_bytes is None or path.stat().st_size <= tail_bytes:
        df = pd.read_csv(path)
    else:
        with path.open("rb") as f:
            header = f.readline()
            f.seek(path.stat().st_size - tail_bytes)
            f.readline()  # la primera línea puede estar cortada
            df = pd.read_csv(io.BytesIO(header + f.read()))
    df.index = pd.to_datetime(df.pop("timestamp"), unit="ms", utc=True)
    return df.drop(columns="datetime_utc", errors="ignore").sort_index()


def load(data: Path, live: bool = True) -> dict[str, pd.DataFrame]:
    """Series que hacen falta. live=True lee solo el final de los archivos grandes."""
    t = (lambda n: n) if live else (lambda n: None)
    return {"book": _read(data / f"{SYMBOL}_book_1m.csv", t(200_000)),
            "snap": _read(data / f"{SYMBOL}_snapshot.csv", t(50_000)),
            "metrics": _read(data / f"{SYMBOL}_metrics_5m.csv", t(400_000)),
            "d1": _read(data / f"{SYMBOL}_1d.csv", None),
            "h4": _read(data / f"{SYMBOL}_4h.csv", t(200_000) if live else None)}


def _asof(df: pd.DataFrame, at: pd.Timestamp) -> pd.Series | None:
    df = df[df.index <= at]
    return df.iloc[-1] if len(df) else None


def features(s: dict[str, pd.DataFrame], at: pd.Timestamp, by_market: dict[str, float],
             price: float | None = None) -> dict:
    """Lectura al momento `at` (fin de la ventana de 15 min) de un neto con su detalle por mercado."""
    net = sum(by_market.values())
    sign = 1 if net > 0 else -1
    b = _asof(s["book"], at)
    p = price if price is not None else (float(b.spot_mid) if b is not None else np.nan)
    out = {"net_usd": net, "price": p,
           "perp_usd": by_market.get("binance_perp", 0.0), "spot_usd": sum(by_market.get(m, 0.0) for m in SPOT)}

    d1 = s["d1"][(s["d1"].index > at - pd.Timedelta(days=RANGE_DAYS)) & (s["d1"].index <= at)]
    lo, hi = min(d1.low.min(), p), max(d1.high.max(), p)
    out.update(range_low=lo, range_high=hi, range_pos=(p - lo) / (hi - lo) if hi > lo else np.nan)

    # %B de 4 h con la vela en curso cerrando al precio de ahora (indicators.py: SMA 20 ± 2 desvíos, ddof 0)
    h4 = s["h4"].close[s["h4"].index <= at - pd.Timedelta(hours=4)].iloc[-19:]
    closes = pd.concat([h4, pd.Series([p])], ignore_index=True)
    mid, sd = closes.mean(), closes.std(ddof=0)
    out["bb_pct_b_4h"] = (p - (mid - 2 * sd)) / (4 * sd) if len(closes) == 20 and sd > 0 else np.nan

    snap = _asof(s["snap"], at)
    out["funding"] = float(snap.est_funding_rate) if snap is not None and at - snap.name < pd.Timedelta(hours=3) else np.nan
    m = s["metrics"].taker_buy_sell_ratio
    m = m[(m.index > at - pd.Timedelta(minutes=20)) & (m.index <= at)]
    out["taker_ratio"] = float(m.mean()) if len(m) else np.nan
    out["book_imbalance"] = (float((b.spot_bid_05 - b.spot_ask_05) / (b.spot_bid_05 + b.spot_ask_05))
                             if b is not None and at - b.name < pd.Timedelta(minutes=10) else np.nan)

    pos, bb = out["range_pos"], out["bb_pct_b_4h"]
    floor = pos <= FLOOR or bb <= BB_FLOOR
    ceil = pos >= CEIL or bb >= BB_CEIL
    out["zone"] = "piso" if floor and not ceil else "techo" if ceil and not floor else "medio"
    out["divergence"] = bool(np.sign(out["perp_usd"]) == sign and np.sign(out["spot_usd"]) != sign)
    # venta con los cortos pagando (funding negativo) o compra con los largos pagando caro; NaN da False
    out["funding_against"] = bool(out["funding"] < 0 if sign < 0 else out["funding"] > FUNDING_HIGH)
    extras = [out["taker_ratio"] <= TAKER_SELL if sign < 0 else out["taker_ratio"] >= TAKER_BUY,
              out["book_imbalance"] * sign < 0]
    out["extras"] = int(sum(bool(x) for x in extras))
    core = out["divergence"] and out["funding_against"]
    out["tag"] = ("absorcion" if sign < 0 and out["zone"] == "piso" and core else
                  "agotamiento" if sign > 0 and out["zone"] == "techo" and core else
                  f"{'venta' if sign < 0 else 'compra'}_{out['zone']}")
    return out


def ar(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


TITLES = {"absorcion": "🧽 posible ABSORCIÓN", "agotamiento": "posible AGOTAMIENTO comprador"}


def text(f: dict) -> list[str]:
    """Líneas para el correo."""
    sell = f["net_usd"] < 0
    ok = lambda b: "sí" if b else "no"  # noqa: E731
    head = {"absorcion": "Lectura: posible ABSORCIÓN. Venta en el piso del rango, el perpetuo vende mientras el spot "
                         "compra y el funding está negativo: en los casos vistos marcó el final de la caída.",
            "agotamiento": "Lectura: posible AGOTAMIENTO comprador. Compra en el techo del rango, el perpetuo compra "
                           "mientras el spot vende y el funding está alto: el espejo de la absorción, todavía sin casos.",
            }.get(f["tag"], {
                ("venta", "techo"): "Lectura: venta en el techo del rango. En los casos vistos el movimiento siguió.",
                ("venta", "piso"): "Lectura: venta en el piso del rango, pero sin la firma completa de absorción.",
            }.get(("venta" if sell else "compra", f["zone"]), f"Lectura: {'venta' if sell else 'compra'} en la "
                  f"zona {'media' if f['zone'] == 'medio' else f['zone']} del rango."))
    nan = lambda x, d=2: "s/d" if x != x else ar(x, d)  # noqa: E731
    return [head, "",
            f"  Rango de {RANGE_DAYS} días {ar(f['range_low'], 0)} – {ar(f['range_high'], 0)}: el precio está al "
            f"{nan(f['range_pos'] * 100, 0)} % (0 = piso) · Bollinger %B 4 h {nan(f['bb_pct_b_4h'])} → {f['zone']}",
            f"  Perpetuo {'+' if f['perp_usd'] >= 0 else '−'}US$ {ar(abs(f['perp_usd']) / 1e6, 1)} M · spot "
            f"{'+' if f['spot_usd'] >= 0 else '−'}US$ {ar(abs(f['spot_usd']) / 1e6, 1)} M → divergencia {ok(f['divergence'])}",
            f"  Funding estimado {nan(f['funding'] * 100, 4)} % → en contra del neto {ok(f['funding_against'])}",
            f"  A favor, no exigidos ({f['extras']} de 2): takers {nan(f['taker_ratio'])} · "
            f"libro spot ±0,5 % {nan(f['book_imbalance'] * 100, 0)} %",
            "",
            "Hipótesis armada con 4 casos: no es una regla. Su backtest está en investigacion/ballenas_neto.py."]
