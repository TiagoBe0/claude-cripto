"""Comprar y vender spot en Binance desde la consola.

Por defecto opera en la TESTNET de Binance (testnet.binance.vision, plata ficticia). Para operar con
plata real hay que pasar --real a propósito, y en ese modo cada orden se confirma siempre.

Las claves viven fuera del repo, en ~/.config/claude-cripto/binance.env (KEY=valor por línea):

    BINANCE_TESTNET_KEY=...       # se crean en https://testnet.binance.vision (login con GitHub)
    BINANCE_TESTNET_SECRET=...
    BINANCE_KEY=...               # cuenta real: SOLO permiso de trading spot, SIN retiros,
    BINANCE_SECRET=...            # y restringida a la IP del servidor

Uso:
    python trade.py precio
    python trade.py saldo
    python trade.py comprar 0.001                 # BTC a mercado
    python trade.py comprar 50 --usdt             # gastar 50 USDT a mercado
    python trade.py comprar 0.001 --precio 80000  # orden límite
    python trade.py vender 0.001
    python trade.py vender todo                   # todo el BTC libre
    python trade.py ordenes                       # órdenes abiertas
    python trade.py cancelar <id>                 # o "todas"
    python trade.py historial                     # últimos trades

Opciones comunes: --real, --par ETH/USDT, --sin-enviar (arma la orden y la muestra, no la manda),
--si (no pregunta; solo en testnet).

Topes en config.json, sección "trading": "symbol" (par por defecto) y "max_order_usdt" (monto máximo
por orden; una orden más grande se rechaza antes de llegar a Binance). Cada orden enviada queda en
data/trading/ordenes.jsonl.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("BINANCE_ENV", Path.home() / ".config" / "claude-cripto" / "binance.env"))
LOG_FILE = ROOT / "data" / "trading" / "ordenes.jsonl"
DEFAULTS = {"symbol": "BTC/USDT", "max_order_usdt": 100}
# Una orden límite a más de esta distancia del precio es casi seguro un dedo de más
LIMIT_WARN_PCT = 10


class TradeError(Exception):
    """Error para mostrar al usuario tal cual, sin traza."""


def _env() -> dict:
    cfg = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def _settings() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text())
    return {**DEFAULTS, **cfg.get("trading", {})}


def exchange(real: bool, private: bool = True) -> ccxt.binance:
    env = _env()
    prefix = "BINANCE_" if real else "BINANCE_TESTNET_"
    key, secret = env.get(prefix + "KEY"), env.get(prefix + "SECRET")
    if private and not (key and secret):
        raise TradeError(f"Faltan {prefix}KEY y {prefix}SECRET en {ENV_FILE}")
    # Solo mercados spot: la testnet no tiene los endpoints de futuros y load_markets fallaría
    ex = ccxt.binance({"apiKey": key, "secret": secret, "enableRateLimit": True,
                       "options": {"defaultType": "spot", "fetchMarkets": ["spot"]}})
    if not real:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def _fmt(x: float | None, d: int = 2) -> str:
    return "–" if x is None else f"{x:,.{d}f}"


def _record(entry: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _confirm(args, lines: list[str]) -> bool:
    print("\n".join(lines))
    if args.sin_enviar:
        print("(--sin-enviar: no se manda nada)")
        return False
    if args.si and not args.real:
        return True
    prompt = "Escribí REAL para enviar con plata real: " if args.real else "¿Enviar? [s/N] "
    answer = input(prompt).strip()
    return answer == "REAL" if args.real else answer.lower() in ("s", "si", "sí")


def plan_order(ex: ccxt.binance, args, settings: dict) -> dict:
    """Valida y arma la orden sin mandarla: cantidad, precio estimado y monto en la moneda cotizada."""
    market = ex.market(args.par)
    base, quote = market["base"], market["quote"]
    ticker = ex.fetch_ticker(args.par)
    ref = ticker["ask"] if args.side == "buy" else ticker["bid"]
    ref = ref or ticker["last"]
    price = float(ex.price_to_precision(args.par, args.precio)) if args.precio else None
    est = price or ref

    if args.cantidad == "todo":
        if args.side != "sell":
            raise TradeError('"todo" solo vale para vender')
        amount = ex.fetch_balance()["free"].get(base, 0) or 0
        if not amount:
            raise TradeError(f"No hay {base} libre para vender")
        by_cost = False
    else:
        try:
            value = float(args.cantidad)
        except ValueError:
            raise TradeError(f"Cantidad inválida: {args.cantidad}")
        if value <= 0:
            raise TradeError("La cantidad tiene que ser mayor que cero")
        by_cost = args.usdt
        amount = value / est if by_cost else value

    # Compra a mercado por monto: Binance la ejecuta con quoteOrderQty, sin redondear la cantidad
    market_buy_cost = by_cost and args.side == "buy" and price is None
    if market_buy_cost:
        cost = float(ex.cost_to_precision(args.par, float(args.cantidad)))
    else:
        amount = float(ex.amount_to_precision(args.par, amount))
        cost = amount * est
    if not market_buy_cost and amount <= 0:
        raise TradeError("La cantidad queda en cero al redondear al paso del mercado")

    limits = market["limits"]
    min_amount, min_cost = limits["amount"]["min"], limits["cost"]["min"]
    if not market_buy_cost and min_amount and amount < min_amount:
        raise TradeError(f"Cantidad mínima en {args.par}: {min_amount} {base}")
    if min_cost and cost < min_cost:
        raise TradeError(f"Monto mínimo por orden en {args.par}: {min_cost} {quote} (esta: {_fmt(cost)})")
    if cost > settings["max_order_usdt"]:
        raise TradeError(f"La orden ({_fmt(cost)} {quote}) supera el tope de {settings['max_order_usdt']} "
                         f"{quote} por orden (config.json → trading.max_order_usdt)")

    warnings = []
    if price is not None:
        dev = (price - ref) / ref * 100
        if abs(dev) >= LIMIT_WARN_PCT:
            warnings.append(f"⚠ El precio límite está {dev:+.1f} % del mercado ({_fmt(ref)})")
        if (args.side == "buy" and price >= ticker["ask"]) or (args.side == "sell" and price <= ticker["bid"]):
            warnings.append("⚠ El límite cruza el libro: se ejecuta ya, como orden a mercado")
    return {"symbol": args.par, "side": args.side, "type": "limit" if price else "market",
            "amount": None if market_buy_cost else amount, "cost": cost, "price": price, "ref": ref,
            "base": base, "quote": quote, "market_buy_cost": market_buy_cost, "warnings": warnings}


def cmd_order(ex, args, settings):
    p = plan_order(ex, args, settings)
    verb = "COMPRA" if p["side"] == "buy" else "VENTA"
    qty = f"{p['amount']} {p['base']}" if p["amount"] is not None else f"{p['base']} por {_fmt(p['cost'])} {p['quote']}"
    lines = [f"{'*** CUENTA REAL ***' if args.real else '[testnet]'} {verb} {p['type'].upper()} {qty} en {p['symbol']}",
             f"  precio {'límite ' + _fmt(p['price']) if p['price'] else 'de mercado ~' + _fmt(p['ref'])}"
             f" · monto ~{_fmt(p['cost'])} {p['quote']}", *("  " + w for w in p["warnings"])]
    if not _confirm(args, lines):
        print("No se envió.")
        return
    if p["market_buy_cost"]:
        order = ex.create_market_buy_order_with_cost(p["symbol"], p["cost"])
    elif p["type"] == "market":
        order = ex.create_order(p["symbol"], "market", p["side"], p["amount"])
    else:
        order = ex.create_order(p["symbol"], "limit", p["side"], p["amount"], p["price"])
    _record({"t": int(time.time()), "real": args.real, "plan": p, "order": order})
    print(f"Orden {order['id']}: {order['status']} · ejecutado {order.get('filled')} {p['base']}"
          f" a {_fmt(order.get('average'))} · {_fmt(order.get('cost'))} {p['quote']}")


def cmd_price(ex, args, settings):
    t = ex.fetch_ticker(args.par)
    print(f"{args.par}  último {_fmt(t['last'])}  compra {_fmt(t['bid'])}  venta {_fmt(t['ask'])}"
          f"  24 h {t['percentage']:+.2f} %")


def cmd_balance(ex, args, settings):
    bal = ex.fetch_balance()
    rows = [(c, bal["free"].get(c) or 0, bal["used"].get(c) or 0) for c, v in bal["total"].items() if v]
    if not rows:
        print("Sin saldo.")
    for c, free, used in sorted(rows):
        print(f"{c:>8}  libre {free:>18,.8f}  en órdenes {used:>16,.8f}")


def cmd_orders(ex, args, settings):
    orders = ex.fetch_open_orders(args.par)
    if not orders:
        print("Sin órdenes abiertas.")
    for o in orders:
        print(f"{o['id']}  {o['side']:<4} {o['type']:<6} {o['amount']} a {_fmt(o['price'])}"
              f"  ejecutado {o['filled']}  {o['datetime']}")


def cmd_cancel(ex, args, settings):
    ids = [o["id"] for o in ex.fetch_open_orders(args.par)] if args.id == "todas" else [args.id]
    if not ids:
        print("Sin órdenes abiertas.")
        return
    if args.real and not _confirm(args, [f"*** CUENTA REAL *** cancelar {len(ids)} orden(es) en {args.par}"]):
        return
    for oid in ids:
        ex.cancel_order(oid, args.par)
        print(f"Cancelada {oid}")


def cmd_history(ex, args, settings):
    trades = ex.fetch_my_trades(args.par, limit=args.n)
    if not trades:
        print("Sin trades.")
    for t in trades:
        fee = t.get("fee") or {}
        print(f"{t['datetime']}  {t['side']:<4} {t['amount']} a {_fmt(t['price'])} = {_fmt(t['cost'])}"
              f"  comisión {fee.get('cost')} {fee.get('currency') or ''}")


def main(argv=None):
    settings = _settings()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--real", action="store_true", help="operar en la cuenta real (por defecto: testnet)")
    common.add_argument("--par", default=settings["symbol"], help=f"par (por defecto {settings['symbol']})")
    ap = argparse.ArgumentParser(description="Comprar y vender spot en Binance (testnet por defecto).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("precio", parents=[common]).set_defaults(fn=cmd_price, private=False)
    sub.add_parser("saldo", parents=[common]).set_defaults(fn=cmd_balance)
    for name, side in (("comprar", "buy"), ("vender", "sell")):
        p = sub.add_parser(name, parents=[common])
        p.add_argument("cantidad", help='cantidad en la moneda base (BTC), o "todo" al vender')
        p.add_argument("--usdt", action="store_true", help="la cantidad es un monto en la moneda cotizada")
        p.add_argument("--precio", type=float, help="precio límite (sin esto: a mercado)")
        p.add_argument("--si", action="store_true", help="no pedir confirmación (solo testnet)")
        p.add_argument("--sin-enviar", action="store_true", help="armar y mostrar la orden sin mandarla")
        p.set_defaults(fn=cmd_order, side=side)
    p = sub.add_parser("ordenes", parents=[common])
    p.set_defaults(fn=cmd_orders)
    p = sub.add_parser("cancelar", parents=[common])
    p.add_argument("id", help='id de la orden, o "todas"')
    p.set_defaults(fn=cmd_cancel, si=False, sin_enviar=False)
    p = sub.add_parser("historial", parents=[common])
    p.add_argument("-n", type=int, default=20, help="cantidad de trades")
    p.set_defaults(fn=cmd_history)
    args = ap.parse_args(argv)

    try:
        # --sin-enviar sin claves: arma la orden con datos públicos (no puede usar "todo")
        private = getattr(args, "private", True) and not (getattr(args, "sin_enviar", False)
                                                          and args.cantidad != "todo")
        args.fn(exchange(args.real, private), args, settings)
    except TradeError as e:
        sys.exit(f"Error: {e}")
    except ccxt.AuthenticationError as e:
        sys.exit(f"Binance rechazó las claves ({'real' if args.real else 'testnet'}): {e}")
    except ccxt.InsufficientFunds as e:
        sys.exit(f"Saldo insuficiente: {e}")
    except ccxt.InvalidOrder as e:
        sys.exit(f"Binance rechazó la orden: {e}")
    except ccxt.NetworkError as e:
        sys.exit(f"Sin conexión con Binance: {e}")
    except KeyboardInterrupt:
        sys.exit("\nCancelado.")


if __name__ == "__main__":
    main()
