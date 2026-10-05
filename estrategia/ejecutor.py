"""Ejecuta la estrategia donchian_trend (live_signal.py) en Binance spot: testnet por defecto.

Uso:
    python -m estrategia.ejecutor            # una corrida (el cron la hace dentro de binance_extract.py)
    python -m estrategia.ejecutor --estado   # muestra el estado sin tocar nada

Config en config.json → trading.ejecutor:
    "enabled": true, "real": false, "capital_usdt": 1000

Opera una subcuenta virtual de `capital_usdt`: compra con todo ese capital (más lo ganado o menos
lo perdido), no con el saldo entero de la cuenta. La posición que lleva es la suya, guardada en
data/trading/ejecutor.json; lo que se compre o venda a mano con trade.py no la toca.

En cada corrida, después de que live_signal.py escribió state.json:

1. Si había un stop en el exchange y se ejecutó, cierra la posición con ese precio.
2. Orden de la estrategia para la vela en curso ("buy" / "sell"), una sola vez por vela:
   compra o venta a mercado, como el backtest en la apertura de la vela siguiente a la señal.
3. Si tiene posición y la estrategia está afuera sin compra pendiente (la simulación vio el
   stop y el exchange no, p. ej. por diferencias de precio), vende para quedar igual.
   Al revés no: si la estrategia está adentro y el ejecutor no, espera una señal de compra
   nueva en vez de entrar tarde.
4. Mantiene un STOP_LOSS en el exchange al stop vigente: salta en el momento, como el backtest
   (que ejecuta a min(apertura, stop)), sin esperar al cron. Si el precio ya está debajo, vende.

Si state.json es viejo (el extractor no corrió), no manda órdenes: el stop que ya está en el
exchange sigue protegiendo la posición. Cada orden va a data/trading/ordenes.jsonl y se avisa
por correo (alertas/mailer.py).
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import ccxt
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import trade  # noqa: E402
from alertas import mailer  # noqa: E402

log = logging.getLogger("ejecutor")
SYMBOL = "BTC/USDT"
CANDLE = pd.Timedelta(hours=4)
STALE_AFTER = pd.Timedelta(minutes=45)  # tras el cierre de la vela siguiente; el cron corre cada hora
MAX_TRADES_KEPT = 20


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _iso(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class Executor:
    def __init__(self, data_dir: Path, cfg: dict):
        self.data_dir = data_dir
        self.real = bool(cfg.get("real", False))
        self.capital = float(cfg.get("capital_usdt", 1000))
        self.path = data_dir / "trading" / "ejecutor.json"
        self.s = self._load()
        self.ex = None

    # ---------- estado propio ----------
    def _load(self) -> dict:
        if self.path.exists():
            s = json.loads(self.path.read_text())
            if s.get("real") != self.real:
                raise RuntimeError(f"{self.path} es de la cuenta {'real' if s.get('real') else 'testnet'}: "
                                   "moverlo antes de cambiar trading.ejecutor.real")
            return s
        return {"real": self.real, "capital_usdt": self.capital, "cash_usdt": self.capital, "position": None,
                "last_signal_candle": None, "trades": [], "events": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=2, ensure_ascii=False, default=str))
        tmp.replace(self.path)

    def _event(self, text: str, notify: bool = False) -> None:
        log.info(text)
        self.s["events"] = (self.s["events"] + [{"t": int(time.time()), "text": text}])[-MAX_TRADES_KEPT:]
        self._save()
        if notify:
            mode = "REAL" if self.real else "testnet"
            mailer.send("ejecutor", f"[{mode}] Ejecutor BTC: {text}", text)

    def _record(self, kind: str, order: dict) -> None:
        trade._record({"t": int(time.time()), "real": self.real, "source": "ejecutor", "kind": kind, "order": order})

    # ---------- órdenes ----------
    def _filled_base(self, order: dict) -> float:
        """BTC recibido: lo ejecutado menos la comisión si se cobró en BTC."""
        qty = float(order.get("filled") or 0)
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            if fee and fee.get("currency") == "BTC" and fee.get("cost"):
                qty -= float(fee["cost"])
        return qty

    def _cancel_stop(self) -> dict | None:
        """Cancela el stop; devuelve la orden si resulta que ya se había ejecutado."""
        pos = self.s["position"]
        oid = pos and pos.get("stop_order_id")
        if not oid:
            return None
        try:
            self.ex.cancel_order(oid, SYMBOL)
        except ccxt.OrderNotFound:
            o = self.ex.fetch_order(oid, SYMBOL)
            if o["status"] == "closed":
                return o
        pos["stop_order_id"] = pos["stop_price"] = None
        self._save()
        return None

    def buy(self, reason: str) -> None:
        cost = float(self.ex.cost_to_precision(SYMBOL, self.s["cash_usdt"]))
        min_cost = self.ex.market(SYMBOL)["limits"]["cost"]["min"] or 0
        if cost < min_cost:
            self._event(f"sin capital para comprar ({cost} USDT < mínimo {min_cost})", notify=True)
            return
        order = self.ex.create_market_buy_order_with_cost(SYMBOL, cost)
        self._record("compra", order)
        qty = float(self.ex.amount_to_precision(SYMBOL, self._filled_base(order)))
        spent = float(order.get("cost") or cost)
        self.s["cash_usdt"] = round(self.s["cash_usdt"] - spent, 8)
        self.s["position"] = {"qty": qty, "entry_price": order.get("average"), "entry_cost_usdt": spent,
                              "entry_time": _iso(_now()), "entry_order_id": order["id"],
                              "stop_order_id": None, "stop_price": None}
        self._event(f"COMPRA {qty} BTC a {order.get('average')} ({spent:.2f} USDT) · {reason}", notify=True)

    def _close(self, price: float, proceeds: float, reason: str) -> None:
        pos = self.s["position"]
        self.s["cash_usdt"] = round(self.s["cash_usdt"] + proceeds, 8)
        ret = proceeds / pos["entry_cost_usdt"] - 1 if pos["entry_cost_usdt"] else None
        self.s["trades"] = (self.s["trades"] + [{
            "entry_time": pos["entry_time"], "entry_price": pos["entry_price"], "exit_time": _iso(_now()),
            "exit_price": price, "qty": pos["qty"], "ret_pct": None if ret is None else round(ret * 100, 2),
            "reason": reason}])[-MAX_TRADES_KEPT:]
        self.s["position"] = None
        self._event(f"VENTA {pos['qty']} BTC a {price} ({proceeds:.2f} USDT, "
                    f"{'–' if ret is None else f'{ret * 100:+.2f} %'}) · {reason}", notify=True)

    def sell(self, reason: str) -> None:
        stop_fill = self._cancel_stop()
        if stop_fill:  # el stop se ejecutó entre la consulta y la cancelación
            self._close(stop_fill["average"], stop_fill["cost"], "stop en el exchange")
            return
        pos = self.s["position"]
        free = self.ex.fetch_balance()["free"].get("BTC") or 0
        qty = float(self.ex.amount_to_precision(SYMBOL, min(pos["qty"], free)))
        if qty < pos["qty"]:
            log.warning("BTC libre (%s) menor que la posición (%s): vendo lo que hay", free, pos["qty"])
        order = self.ex.create_order(SYMBOL, "market", "sell", qty)
        self._record("venta", order)
        self._close(order.get("average"), float(order.get("cost") or 0), reason)

    def ensure_stop(self, price: float) -> None:
        pos = self.s["position"]
        price = float(self.ex.price_to_precision(SYMBOL, price))
        if pos.get("stop_order_id") and pos.get("stop_price") == price:
            return
        bid = self.ex.fetch_ticker(SYMBOL)["bid"]
        if bid and price >= bid:
            self.sell(f"precio {bid} ya debajo del stop {price}")
            return
        stop_fill = self._cancel_stop()
        if stop_fill:
            self._close(stop_fill["average"], stop_fill["cost"], "stop en el exchange")
            return
        order = self.ex.create_order(SYMBOL, "market", "sell", pos["qty"], None, {"triggerPrice": price})
        self._record("stop", order)
        old = pos.get("stop_price")
        pos["stop_order_id"], pos["stop_price"] = order["id"], price
        self._event(f"stop {'movido de ' + str(old) + ' a' if old else 'puesto en'} {price}")

    def check_stop(self) -> None:
        pos = self.s["position"]
        if not pos or not pos.get("stop_order_id"):
            return
        o = self.ex.fetch_order(pos["stop_order_id"], SYMBOL)
        if o["status"] == "closed":
            self._record("stop ejecutado", o)
            self._close(o.get("average"), float(o.get("cost") or 0), "stop en el exchange")
        elif o["status"] in ("canceled", "expired", "rejected"):
            self._event(f"el stop {pos['stop_order_id']} quedó {o['status']}: se vuelve a poner")
            pos["stop_order_id"] = pos["stop_price"] = None
            self._save()

    # ---------- corrida ----------
    def run(self) -> dict:
        st = json.loads((self.data_dir / "estrategia" / "state.json").read_text())
        candle = st["last_candle_open_utc"]
        age = _now() - (pd.Timestamp(candle) + CANDLE)
        self.ex = trade.exchange(self.real)
        self.check_stop()
        if age > CANDLE + STALE_AFTER:
            self._event(f"state.json viejo (vela {candle}): no se mandan órdenes")
            return self.summary()

        action = st.get("action_next_open")
        if self.s["last_signal_candle"] != candle:
            if action == "buy" and not self.s["position"]:
                self.buy(f"señal de la vela {candle}")
            elif action == "sell" and self.s["position"]:
                self.sell(f"cierre bajo la EMA, vela {candle}")
            self.s["last_signal_candle"] = candle
            self._save()
        if self.s["position"] and not st["in_position"] and action != "buy":
            self.sell("la estrategia está afuera (stop de la simulación)")
        if self.s["position"] and st["in_position"] and st.get("stop"):
            self.ensure_stop(st["stop"])
        return self.summary()

    def summary(self) -> dict:
        pos = self.s["position"]
        price = None
        if self.ex:
            try:
                price = self.ex.fetch_ticker(SYMBOL)["last"]
            except ccxt.BaseError:
                pass
        value = (pos["qty"] * price if pos and price else pos["entry_cost_usdt"] if pos else 0)
        equity = self.s["cash_usdt"] + value
        self.s.update({"updated": int(time.time()), "price": price, "equity_usdt": round(equity, 2),
                       "return_pct": round((equity / self.s["capital_usdt"] - 1) * 100, 2)})
        self._save()
        return self.s


def update(data_dir: Path, cfg: dict) -> dict:
    return Executor(data_dir, cfg).run()


def main() -> None:
    ap = argparse.ArgumentParser(description="Ejecutor de la estrategia 4h en Binance spot.")
    ap.add_argument("--estado", action="store_true", help="mostrar el estado sin operar")
    ap.add_argument("data_dir", nargs="?", default=ROOT / "data", type=Path)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads((ROOT / "config.json").read_text()).get("trading", {}).get("ejecutor", {})
    ex = Executor(args.data_dir, cfg)
    print(json.dumps(ex.s if args.estado else ex.run(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
