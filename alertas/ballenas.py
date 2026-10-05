"""Avisos de ballenas, cada minuto por cron:

- Orden suelta >= BIG_ORDER_USD en Binance spot, Binance perp o Coinbase.
- Neto de ballenas (compras - ventas de órdenes >= US$ 100 mil) en los últimos 15 minutos
  >= NET_15M_USD, con una espera de NET_COOLDOWN_S entre avisos del mismo lado.
- Como mucho MAX_PER_DAY correos de ballenas por día.

Umbrales medidos el 2026-10-05 con el primer día de datos: el |neto de 15 min| tiene mediana
US$ 7 M y percentil 99 de US$ 81 M (el perpetuo de Binance mueve decenas de millones por minuto).
Con US$ 5 M llegaban ~28 avisos por día; con US$ 100 M, alrededor de uno. Recalibrar cuando haya
más historia.

Uso:
    python -m alertas.ballenas

Lee lo que escribe whale_trades_ws.py (BTCUSDT_whale_orders.csv y BTCUSDT_whale_trades_1m.csv), así
que llega con el minuto de demora con que el recolector cierra cada minuto. Si el recolector se cayó,
no hay filas nuevas y no avisa nada (el panel muestra la serie atrasada).

Todavía no hay historia suficiente para saber si estas órdenes anticipan algo: whale_orders.csv es el
registro con el que se va a medir.
"""

import json
import logging
import time
import urllib.request

import pandas as pd

from alertas import mailer
from alertas.estado import Estado
from alertas.velas import PANEL, ar

DATA = mailer.ROOT / "data"
BIG_ORDER_USD = 5_000_000
NET_15M_USD = 100_000_000
MAX_PER_DAY = 6
NET_COOLDOWN_S = 3600
MARKETS = ["binance_spot", "binance_perp", "coinbase"]
NAMES = {"binance_spot": "Binance spot", "binance_perp": "Binance perpetuo", "coinbase": "Coinbase"}

log = logging.getLogger("alertas.ballenas")


def price() -> float | None:
    try:
        with urllib.request.urlopen("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5) as r:
            return float(json.load(r)["price"])
    except Exception:  # noqa: BLE001 - el precio es un extra del correo
        return None


def money(x: float) -> str:
    return f"US$ {ar(x / 1e6, 2)} M" if abs(x) >= 1e6 else f"US$ {ar(x / 1e3, 0)} mil"


def check_orders(est: Estado) -> None:
    path = DATA / "BTCUSDT_whale_orders.csv"
    if not path.exists():
        return
    df = pd.read_csv(path)
    last = est.get("ballenas_orders_last_ts", None)
    if last is None:  # primera corrida: no avisar lo viejo
        est.set("ballenas_orders_last_ts", int(df.timestamp.max()) if len(df) else 0)
        return
    new = df[df.timestamp > last]
    if not len(new):
        return
    est.set("ballenas_orders_last_ts", int(new.timestamp.max()))
    big = new[new.usd >= BIG_ORDER_USD].sort_values("usd", ascending=False)
    if not len(big):
        return
    p = price()
    buy, sell = big[big.side == "buy"].usd.sum(), big[big.side == "sell"].usd.sum()
    lead = big.iloc[0]
    verb = "COMPRA" if lead.side == "buy" else "VENTA"
    subject = f"BTC 🐋 {verb} a mercado de {money(lead.usd)} en {NAMES.get(lead.market, lead.market)}"
    if len(big) > 1:
        subject += f" (+{len(big) - 1} más)"
    lines = [f"Órdenes a mercado de {money(BIG_ORDER_USD)} o más desde el último aviso:", ""]
    for r in big.itertuples():
        lines.append(f"  {pd.to_datetime(r.timestamp, unit='ms', utc=True):%d/%m %H:%M} UTC · "
                     f"{'compra' if r.side == 'buy' else 'venta'} · {money(r.usd)} · {NAMES.get(r.market, r.market)}")
    lines += ["", f"Total: compras {money(buy)} · ventas {money(sell)}"]
    if p:
        lines.append(f"Precio BTC ahora: {ar(p, 2)}")
    lines += ["", "Una orden grande a mercado puede ser una ballena entrando o saliendo, un fondo cubriéndose o",
              "una liquidación. Todavía no hay historia para saber si anticipa algo: se está registrando.",
              "", f"Panel: {PANEL}"]
    est.notify(f"ballena:orden:{int(big.timestamp.max())}:{int(big.usd.sum())}", "ballena", subject, "\n".join(lines))


def check_net(est: Estado) -> None:
    path = DATA / "BTCUSDT_whale_trades_1m.csv"
    if not path.exists():
        return
    df = pd.read_csv(path).tail(15)
    if not len(df) or time.time() * 1000 - df.timestamp.iloc[-1] > 5 * 60_000:
        return  # datos viejos: el recolector está caído
    buy = sum(df[f"{m}_whale_buy_usd"].sum() for m in MARKETS)
    sell = sum(df[f"{m}_whale_sell_usd"].sum() for m in MARKETS)
    net = buy - sell
    if abs(net) < NET_15M_USD:
        return
    side = "buy" if net > 0 else "sell"
    last = est.get(f"ballenas_net_{side}_at", 0)
    if time.time() - last < NET_COOLDOWN_S:
        return
    est.set(f"ballenas_net_{side}_at", int(time.time()))
    p = price()
    by_market = {m: df[f"{m}_whale_buy_usd"].sum() - df[f"{m}_whale_sell_usd"].sum() for m in MARKETS}
    word = "COMPRANDO" if net > 0 else "VENDIENDO"
    subject = f"BTC 🐋 Ballenas {word}: neto {'+' if net > 0 else '−'}{money(abs(net))} en 15 min"
    lines = [f"En los últimos 15 minutos las órdenes a mercado de US$ 100 mil o más dieron un neto de "
             f"{'+' if net > 0 else '−'}{money(abs(net))}.", "",
             f"Compras {money(buy)} · ventas {money(sell)}", ""]
    lines += [f"  {NAMES[m]}: {'+' if v >= 0 else '−'}{money(abs(v))}" for m, v in by_market.items()]
    if p:
        lines += ["", f"Precio BTC ahora: {ar(p, 2)}"]
    lines += ["", f"No se repite este aviso del mismo lado durante {NET_COOLDOWN_S // 60} minutos.",
              "", f"Panel: {PANEL}"]
    est.notify(f"ballena:neto:{side}:{int(df.timestamp.iloc[-1])}", "ballena", subject, "\n".join(lines))


def under_daily_cap(est: Estado) -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    day = est.get("ballenas_day", {})
    if day.get("date") != today:
        day = {"date": today, "sent": 0}
    est.set("ballenas_day", day)
    return day["sent"] < MAX_PER_DAY


def run() -> None:
    est = Estado("ballenas")
    # notify() pasa por acá: cuenta los correos del día y corta al llegar al tope
    notify = est.notify

    def capped(key, kind, subject, text):
        if est.seen(key):
            return False
        if not under_daily_cap(est):
            est.data["sent"][key] = int(time.time())
            mailer._record(kind, subject, text, False, "tope diario de avisos de ballenas")
            return False
        est.get("ballenas_day")["sent"] += 1
        return notify(key, kind, subject, text)

    est.notify = capped
    try:
        for fn in (check_orders, check_net):
            try:
                fn(est)
            except Exception:  # noqa: BLE001
                log.exception("alertas: falló %s", fn.__name__)
    finally:
        est.save()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
