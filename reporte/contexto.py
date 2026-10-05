"""Junta en un JSON compacto los datos que necesita el reporte de Claude.

Uso:
    python reporte/contexto.py               # usa data/, escribe data/reporte/contexto.json
    python reporte/contexto.py otra/carpeta

Todos los números se calculan acá; Claude solo los interpreta. Así el reporte
no depende de que el modelo haga cuentas sobre CSV de decenas de MB y cada
cifra se puede rastrear hasta este archivo.
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from build_dashboard import status_data, whale_status  # noqa: E402
from reporte import liquidez  # noqa: E402

SYMBOL = "BTCUSDT"
H, D5 = 1, 12  # filas por hora en series de 1 h y de 5 min


def _num(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), nd)


def _read(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.timestamp, unit="ms", utc=True)
    return df


def _iso(ts) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _change(s: pd.Series, hours: int, per_hour: int) -> float | None:
    n = hours * per_hour
    return _num((s.iloc[-1] / s.iloc[-n - 1] - 1) * 100) if len(s) > n else None


def _pctl(s: pd.Series, n: int) -> float | None:
    """Percentil (0-100) del último valor dentro de las últimas n filas."""
    w = s.dropna().iloc[-n:]
    return _num((w < w.iloc[-1]).mean() * 100, 0) if len(w) else None


def price(d: Path) -> dict:
    h, day = _read(d / f"{SYMBOL}_1h.csv"), _read(d / f"{SYMBOL}_1d.csv")
    ind = _read(d / f"{SYMBOL}_1d_indicators.csv").iloc[-1]
    ind4 = _read(d / f"{SYMBOL}_4h_indicators.csv").iloc[-1]
    c = h.close.iloc[-1]
    last90 = day.iloc[-90:]
    return {
        "last_1h_candle_open_utc": _iso(h.index[-1]),
        "close": _num(c),
        "change_pct": {k: _change(h.close, n, H) for k, n in
                       [("24h", 24), ("7d", 168), ("30d", 720), ("90d", 2160), ("365d", 8760)]},
        "max_since_2024": {"price": _num(day.high.max()), "date": str(day.high.idxmax().date()),
                           "distance_pct": _num((c / day.high.max() - 1) * 100)},
        "range_90d": {"low": _num(last90.low.min()), "low_date": str(last90.low.idxmin().date()),
                      "high": _num(last90.high.max()), "high_date": str(last90.high.idxmax().date())},
        "daily": {"sma_50": _num(ind.sma_50), "sma_200": _num(ind.sma_200), "rsi_14": _num(ind.rsi_14, 1),
                  "macd_hist": _num(ind.macd_hist), "bb_pct_b": _num(ind.bb_pct_b),
                  "atr_14": _num(ind.atr_14), "atr_pct": _num(ind.atr_14 / c * 100),
                  "realized_vol_20d_annual_pct": _num(ind.volatility_20 * np.sqrt(365) * 100, 1)},
        "4h": {"rsi_14": _num(ind4.rsi_14, 1), "macd_hist": _num(ind4.macd_hist), "bb_pct_b": _num(ind4.bb_pct_b)},
        "volume_7d_vs_prev_30d": _num(day.volume.iloc[-7:].mean() / day.volume.iloc[-37:-7].mean()),
    }


def derivatives(d: Path) -> dict:
    f = _read(d / f"{SYMBOL}_funding.csv").funding_rate
    m = _read(d / f"{SYMBOL}_metrics_5m.csv")
    prem = _read(d / f"{SYMBOL}_premium_1h.csv").premium_close
    last = m.iloc[-1]
    day_ago = m.iloc[-24 * D5 - 1] if len(m) > 24 * D5 else m.iloc[0]
    week_ago = m.iloc[-168 * D5 - 1] if len(m) > 168 * D5 else m.iloc[0]
    return {
        "metrics_time_utc": _iso(m.index[-1]),
        "funding_last_3_pct": [_num(x * 100, 4) for x in f.iloc[-3:]],
        "funding_30d_annualized_pct": _num(f.iloc[-90:].mean() * 3 * 365 * 100),
        "funding_pctl_1y": _pctl(f, 3 * 365),
        "premium_24h_avg_pct": _num(prem.iloc[-24:].mean() * 100, 3),
        "open_interest_btc": _num(last.open_interest, 0),
        "open_interest_usd_bn": _num(last.open_interest_usd / 1e9),
        "open_interest_change_pct": {k: _change(m.open_interest, n, D5) for k, n in
                                     [("24h", 24), ("7d", 168), ("30d", 720)]},
        "open_interest_usd_pctl_1y": _pctl(m.open_interest_usd, 365 * 24 * D5),
        "top_trader_position_ls": {"now": _num(last.top_trader_position_ls_ratio, 3),
                                   "7d_ago": _num(week_ago.top_trader_position_ls_ratio, 3)},
        "global_account_ls": {"now": _num(last.global_account_ls_ratio, 3),
                              "7d_ago": _num(week_ago.global_account_ls_ratio, 3)},
        # >1: dominan las compras a mercado. Mediana de las filas de 5 min: el promedio
        # de ratios sobrepesa los valores altos.
        "taker_buy_sell_24h": _num(m.taker_buy_sell_ratio.loc[day_ago.name:].median(), 3),
    }


def sentiment(d: Path) -> dict:
    g = _read(d / "fear_greed.csv")
    v = _read(d / "BTC_dvol_1h.csv").close
    return {
        "fear_greed": {"now": int(g.value.iloc[-1]), "label": g.classification.iloc[-1],
                       "7d_ago": int(g.value.iloc[-8]), "30d_avg": _num(g.value.iloc[-30:].mean(), 0)},
        "dvol_implied_vol_pct": {"now": _num(v.iloc[-1], 1), "7d_ago": _num(v.iloc[-169], 1),
                                 "pctl_1y": _pctl(v, 8760)},
    }


def market_snapshot(d: Path) -> dict | None:
    p = d / f"{SYMBOL}_snapshot.csv"
    if not p.exists():
        return None
    s = pd.read_csv(p).iloc[-1]
    return {"time_utc": s.datetime_utc, "spot_spread_bps": _num(s.spot_spread_bps, 4),
            "est_funding_pct": _num(s.est_funding_rate * 100, 4),
            "spot_depth_usd_m_05pct": {"bids": _num(s.spot_bid_depth_usd_05pct / 1e6, 1),
                                       "asks": _num(s.spot_ask_depth_usd_05pct / 1e6, 1)},
            "perp_imbalance_01pct": _num(s.perp_imbalance_01pct, 3)}


def strategy(d: Path) -> dict | None:
    p = d / "estrategia" / "state.json"
    if not p.exists():
        return None
    state = json.loads(p.read_text())
    trades = pd.read_csv(d / "estrategia" / "trades.csv")
    closed = trades.dropna(subset=["exit_time"])
    recent = closed[closed.exit_time >= _iso(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=365))]
    state["last_trades"] = trades.tail(4).replace({np.nan: None}).to_dict(orient="records")
    state["last_365d"] = {"trades": len(recent), "wins": int((recent.ret_pct > 0).sum()),
                          "compounded_return_pct": _num(((1 + recent.ret_pct / 100).prod() - 1) * 100)}
    return state


def paper(d: Path) -> dict | None:
    p = d / "estrategia" / "paper.json"
    return json.loads(p.read_text()) if p.exists() else None


def stale_series(d: Path) -> list[dict]:
    """Series de BTC atrasadas más de 2 períodos (mínimo 2 corridas del cron horario) + 15 min.

    Las series de otros símbolos se ignoran: pueden ser restos de configuraciones viejas.
    """
    now = time.time()
    out = []
    for s in status_data(d)["series"]:
        if not (s["name"].startswith(SYMBOL) or s["name"] in ("fear_greed", "BTC_dvol_1h")):
            continue
        if s["period"] and s["last"] and now - s["last"] > max(2 * s["period"], 2 * 3600) + 900:
            out.append({"series": s["name"], "hours_behind": _num((now - s["last"]) / 3600, 1)})
    return out


def build(d: Path) -> dict:
    ctx = {"generated_utc": _iso(pd.Timestamp.now(tz="UTC")), "symbol": SYMBOL,
           "stale_series": stale_series(d)}
    for name, fn in [("price", price), ("derivatives", derivatives), ("sentiment", sentiment),
                     ("market_snapshot", market_snapshot), ("strategy", strategy), ("paper", paper),
                     ("whales", whale_status)]:
        try:
            ctx[name] = fn(d)
        except (OSError, KeyError, IndexError, ValueError) as e:
            ctx[name] = {"error": f"{type(e).__name__}: {e}"}
    ctx["liquidity"] = liquidez.build(d)
    return ctx


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    out = d / "reporte"
    out.mkdir(exist_ok=True)
    tmp = out / "contexto.json.tmp"
    tmp.write_text(json.dumps(build(d), indent=1, ensure_ascii=False))
    tmp.replace(out / "contexto.json")
    print(out / "contexto.json")
