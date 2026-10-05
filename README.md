# claude-cripto

Descarga incremental de datos de BTC a CSV y JSON, todo desde APIs públicas gratuitas (sin API key):

- velas spot de Binance + indicadores técnicos calculados localmente
- futuros perpetuos de Binance: velas, premium index, funding rate, open interest, ratios long/short, profundidad del libro
- Fear & Greed Index (alternative.me) y DVOL, la volatilidad implícita de Deribit

Módulos: `binance_extract.py` (punto de entrada y spot), `futures_data.py`, `external_data.py`,
`indicators.py`, `storage.py` (CSV incremental común).

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Uso

```bash
.venv/bin/python binance_extract.py
```

Se configura en `config.json`:

- `symbols`: pares en formato CCXT (`"BTC/USDT"`, `"SOL/USDT"`, ...)
- `timeframes`: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`
- `start_date`: desde dónde bajar historial la primera vez (`YYYY-MM-DD`, UTC)
- `spot_start_dates`: `start_date` propio para algún timeframe spot (el 4h baja desde 2017 para la estrategia)
- `strategy`: `symbol` y `timeframe` sobre los que se calcula la señal en vivo (ver Estrategia)
- `data_dir`: carpeta de salida (relativa a `config.json`)
- `export_json`: además del CSV, escribe un `.json` con la serie completa
- `indicators`: calcula indicadores técnicos después de cada descarga
- `futures`: `symbol` del perpetuo y qué series bajar (`klines`, `premium_index`, `funding_rate`, `metrics_5m`, `snapshot`)
- `external`: `fear_greed`, `deribit_dvol`

Salida: `data/BTCUSDT_1h.csv` (+ `.json`), columnas
`timestamp` (ms UTC, apertura de la vela), `datetime_utc`, `open`, `high`, `low`, `close`, `volume`.

Cada corrida retoma desde la última vela guardada y solo guarda velas cerradas.
Para rehacer una serie desde cero, borrar su CSV.

## Indicadores

Con `"indicators": true` se genera `data/BTCUSDT_1h_indicators.csv` (+ `.json`) con:

| Columna | Indicador |
|---|---|
| `sma_20`, `sma_50`, `sma_200` | Media móvil simple |
| `ema_12`, `ema_26`, `ema_50` | Media móvil exponencial |
| `macd`, `macd_signal`, `macd_hist` | MACD (12, 26, 9) |
| `rsi_14` | RSI de Wilder |
| `bb_upper`, `bb_lower`, `bb_pct_b` | Bandas de Bollinger (20, 2) y %B |
| `atr_14` | Average True Range de Wilder |
| `log_return`, `volatility_20` | Retorno logarítmico y su desvío en 20 velas (sin anualizar) |
| `obv` | On-Balance Volume (arranca en 0) |

Se recalculan completos en cada corrida (las medias exponenciales dependen de toda la historia).
Las primeras filas quedan vacías hasta tener historia suficiente.
Para recalcular sin descargar: `.venv/bin/python indicators.py data/BTCUSDT_1h.csv`.

Validado contra la librería `ta`: diferencias del orden de 1e-15.

## Futuros y sentimiento

| Archivo | Frecuencia | Columnas |
|---|---|---|
| `BTCUSDT_perp_<tf>.csv` | `timeframes` | OHLCV del perpetuo + `quote_volume` (USDT), `trades`, `taker_buy_volume`, `taker_buy_quote_volume` (compras a mercado; ventas = `volume - taker_buy_volume`) |
| `BTCUSDT_premium_<tf>.csv` | `timeframes` | `premium_open/high/low/close`: premium del perpetuo sobre el índice spot (fracción; base del funding) |
| `BTCUSDT_funding.csv` | 8 h | `funding_rate` cobrado (positivo = longs pagan a shorts), `mark_price` |
| `BTCUSDT_metrics_5m.csv` | 5 min | `open_interest` (BTC), `open_interest_usd`, `top_trader_account_ls_ratio`, `top_trader_position_ls_ratio`, `global_account_ls_ratio`, `taker_buy_sell_ratio` |
| `BTCUSDT_snapshot.csv` | 1 por corrida | `mark_price`, `index_price`, `est_funding_rate` (del período en curso), `next_funding_time`, `open_interest` en vivo, `spot_bid/ask`, `spot_spread_bps`, profundidad en USD a ±0,5% (spot) y ±0,1% (perp) con su desbalance `(bids-asks)/(bids+asks)` |
| `fear_greed.csv` | diaria | `value` 0–100, `classification` |
| `BTC_dvol_1h.csv` | 1 h | OHLC del DVOL (volatilidad implícita anualizada a 30 días, en %) |

Notas:

- La API de Binance solo guarda 30 días de open interest y ratios long/short. El historial anterior se completa
  con los archivos diarios de `data.binance.vision` (mismos datos, con más decimales). Los timestamps siguen la
  convención de la API: vision registra OI y ratios long/short 5 min antes. **Si el script deja de correr más de
  30 días, el hueco se rellena desde vision** salvo los últimos 1–2 días, que vision todavía no publicó.
- El snapshot no tiene historia: se acumula una fila por corrida desde que el cron empieza a correr.
- Huecos que vienen de la fuente: Binance no tiene métricas el 2024-02-16 desde las 13:35 y le faltan valores
  sueltos algunos días (quedan vacíos); alternative.me no publicó Fear & Greed el 2024-10-26.
- Liquidaciones: Binance eliminó el endpoint REST histórico; solo se pueden capturar en vivo por WebSocket.
- `BTCUSDT_metrics_5m.json` pesa ~80 MB y se reescribe en cada corrida; con `"export_json": false` se evita.

## Dashboard

Con `"dashboard": true`, cada corrida escribe `data/dashboard/*.json` (datos livianos por columna).
Para regenerarlos sin descargar: `.venv/bin/python build_dashboard.py`.

Para verlo (el navegador no deja leer los JSON abriendo el HTML con doble clic):

```bash
.venv/bin/python -m http.server 8000 --bind 127.0.0.1
```

y abrir <http://127.0.0.1:8000/dashboard.html> (`#1d` para velas diarias). Se recarga solo cada 5 min.

Para dejarlo siempre levantado hay un servicio de usuario de systemd
(`~/.config/systemd/user/cripto-dashboard.service`, arranca al iniciar sesión):

```bash
systemctl --user status cripto-dashboard     # ver estado
systemctl --user restart cripto-dashboard    # reiniciar
systemctl --user disable --now cripto-dashboard  # desinstalar
```

- Arriba: último snapshot (mark, funding estimado, open interest, spread, desbalance del libro), estado de la
  estrategia (se resalta si hay orden para la próxima apertura) y liquidaciones reales de las últimas 24 h.
- *Estado de las series*: avisa si alguna de BTC dejó de actualizarse (p. ej. si el cron no corre).
- Gráfico con paneles sincronizados: velas spot + SMA 50/200 y una línea con el nivel de la estrategia
  (cierre de 4 h que dispara la compra, o el stop si hay posición), volumen, RSI 14, MACD 12/26/9, funding,
  open interest, ratios long/short, DVOL contra volatilidad realizada a 30 días (anualizada) y Fear & Greed.
  En 1d el funding es el acumulado del día.
- *Mapa de liquidez* (interruptor arriba, se recuerda en el navegador), sobre el panel de precio:
  barras horizontales con las liquidaciones **estimadas** por tramo de 0,5% hasta ±15% del precio (rojo = largos,
  azul = cortos, con el monto en los 3 tramos mayores de cada lado), líneas punteadas en los máximos y mínimos sin
  barrer y líneas naranjas en el muro más grande de cada lado de Binance y Coinbase (solo si es ≥ 3 veces un tramo
  típico). Sale de `data/dashboard/liquidity.json`, que usa las mismas funciones que el reporte (`reporte/liquidez.py`)
  y se regenera en cada corrida del cron.

## Automatizar con cron

`crontab -e` y agregar (corre 2 minutos después de cada hora):

```cron
2 * * * * cd /home/santi/Documentos/claude-cripto && .venv/bin/python binance_extract.py >> extract.log 2>&1
```

## Estrategia (`estrategia/`)

`donchian_trend.pine`: ruptura de Donchian 120 + filtro EMA 1200 + stop trailing 4×ATR14, BTCUSDT spot 4h,
solo compras, costo 0,16% por lado (comisión + slippage + spread). `backtest.py` replica la misma lógica en
Python con historia desde 2017 (la baja a `data/backtest/`), separando in-sample 2018-2022 y out-of-sample 2023-hoy.

### Estrategias probadas y descartadas

`confluencia.py` / `confluencia.pine`: EMA 200 + cruce de MACD + RSI entre 50 y 70, stop 2×ATR y take 4×ATR
(la "confluencia dinámica" que circula como estrategia para armar con IA), con y sin filtro ADX > 25.
Mismos costos y misma separación in-sample / out-of-sample. **No funciona**: BTC 4h fuera de muestra da
CAGR −15% (PF 0,63; con ADX −9%, PF 0,44) y pierde también en 1d y en ETH. Acierta ~29% y con 1:2 y costos
hace falta ~39%. `python estrategia/confluencia.py` imprime la tabla completa.

### Señal en vivo

Con `"strategy"` en la config, cada corrida termina con `estrategia/live_signal.py`, que re-simula toda la
historia de `BTCUSDT_4h.csv` con el mismo código del backtest y escribe en `data/estrategia/`:

| Archivo | Contenido |
|---|---|
| `state.json` | última vela cerrada, `in_position`, `action_next_open` (`buy`/`sell`/`null`: orden para la apertura siguiente), `stop` vigente durante la próxima vela, `entry_trigger` (cierre que dispara la compra) y el trade abierto |
| `trades.csv` | trades teóricos de la simulación (se reescribe cada vez) |
| `signals_log.csv` | una fila por vela de 4h evaluada en vivo, solo se agrega: el registro del paper trading |

Como recalcula desde cero, el estado no depende de corridas anteriores. Para correrla sola:
`.venv/bin/python estrategia/live_signal.py`. Con el cron a los :02, la señal sale 2 minutos después del
cierre de cada vela de 4h (00, 04, 08, 12, 16 y 20 UTC).

### Perpetuo con apalancamiento (`perp_backtest.py`)

`python estrategia/perp_backtest.py` baja el perpetuo BTCUSDT 4h y su funding desde 2019 a `data/backtest/`
y compara reglas largo/corto con apalancamiento: costo 0,10% por lado sobre el nocional, funding cada 8 h,
liquidación verificada dentro de cada vela. Señales sobre velas spot, ejecución en la apertura siguiente del
perp. Resultado (oct-2026, costo 0,10%):

| Regla | 2019-10 a 2022: CAGR / MaxDD / Sharpe | 2023-hoy: CAGR / MaxDD / Sharpe |
|---|---|---|
| Comprar y mantener | 23% / −77% / 0,67 | 55% / −53% / 1,17 |
| C Donchian largo, vol 50% (máx 3x) | 26% / −21% / 0,98 | 19% / −26% / 0,77 |
| D Donchian largo/corto, vol 50% | 48% / −25% / 1,13 | 17% / −40% / 0,65 |
| G momentum 30/90/180 d, vol 50% (máx 2x) | 24% / −49% / 0,76 | 48% / −33% / 1,14 |

Ninguna le gana con claridad a comprar y mantener en Sharpe en los dos períodos. Los cortos y un filtro de
funding funcionaron hasta 2022 y no después. C es la más consistente (caída máxima −21/−26%); G es la
mejor desde 2023 pero la peor antes, así que elegirla sería sesgo de selección. El funding de un largo
apalancado casi permanente se come 60-95% del capital inicial en el período.

### Paper trading (`paper.py`)

Con `"paper"` en la config, cada corrida re-simula C (principal) y G (candidata) desde `paper.start`
(la primera vela posterior a la decisión, sin mirar atrás) con los datos del pipeline y escribe en
`data/estrategia/`:

| Archivo | Contenido |
|---|---|
| `paper.json` | por cuenta: capital, retorno, caída máxima, exposición (nocional / capital), orden para la próxima apertura (`target_exposure`, 0 = cerrar), stop o precio de entrada (C), funding pagado; y comprar y mantener desde el mismo inicio |
| `paper_log.csv` | una fila por vela de 4h evaluada, solo se agrega: sirve para auditar que lo simulado no cambie |

El dashboard muestra una tarjeta por cuenta, el reporte de Claude las resume y `generar.sh --si-hay-orden`
también avisa cuando C tiene una orden. G rebalancea seguido (~2 veces por semana) y no genera alertas.
Solo simula: no hay órdenes reales.

## Reporte con Claude (`reporte/`)

Python calcula los números y Claude solo los interpreta: no tiene herramientas ni puede tocar la estrategia.

- `contexto.py` junta en `data/reporte/contexto.json` precio, indicadores, derivados, sentimiento, DVOL,
  el estado de la estrategia y las series de BTC que dejaron de actualizarse.
- `prompt.md` es el system prompt con las reglas y el formato del reporte.
- `generar.sh` llama a `claude -p` (modelo `sonnet` por defecto, `REPORTE_MODEL=opus` para cambiarlo) y
  escribe `data/reporte/<fecha UTC>_diario.md` y una copia en `ultimo.md`. Con `--si-hay-orden` solo
  genera una alerta (`_alerta-buy.md` / `_alerta-sell.md`) si la estrategia tiene una orden para la
  próxima apertura, una vez por vela.

Cron (hora local, UTC−3), log en `reporte.log`:

```cron
10 1,5,9,13,17,21 * * * /home/santi/Documentos/claude-cripto/reporte/generar.sh --si-hay-orden >> /home/santi/Documentos/claude-cripto/reporte.log 2>&1
20 21 * * * /home/santi/Documentos/claude-cripto/reporte/generar.sh >> /home/santi/Documentos/claude-cripto/reporte.log 2>&1
```

## Liquidez (`reporte/liquidez.py`, `liquidations_ws.py`)

Todo gratis, sin API keys. Se suma al reporte en `liquidity`:

| Parte | Fuente | Qué es |
|---|---|---|
| `realized_liquidations` | WebSocket de Binance y Bybit | liquidaciones reales, totales de 24 h y 7 d por lado; solo desde que corre el recolector. Binance manda como máximo una por segundo (subestima cascadas) |
| `swing_pools` | velas 4h y 1d | máximos y mínimos todavía no barridos (stops encima y debajo), agrupados si están a < 0,5% |
| `order_book_walls` | Binance spot (hasta ±1%: `band_pct` dice cuánto cubrieron los 5000 niveles) y libro completo de Coinbase (±5%) | órdenes límite grandes; `x_median` = veces un tramo típico del libro |
| `estimated_liquidations` | open interest de Binance (5 min) | **modelo**: cada suba de OI abre largos y cortos con apalancamiento supuesto 10/25/50/100x; se borran al tocarse y se achican cuando baja el OI |

El recolector corre como servicio de usuario (`~/.config/systemd/user/cripto-liquidaciones.service`) y
escribe `data/BTCUSDT_liquidations.csv`:

```bash
systemctl --user status cripto-liquidaciones
journalctl --user -u cripto-liquidaciones -f
```

Linger está activado (`loginctl enable-linger santi`), así que los servicios de usuario siguen corriendo
aunque se cierre la sesión. Para volver atrás: `loginctl disable-linger santi`.

## Ballenas (`whale_trades_ws.py`)

Órdenes a mercado grandes (≥ US$ 100 mil, `MIN_USD`) en Binance spot, Binance perp y Coinbase, capturadas
por WebSocket y resumidas por minuto en `data/BTCUSDT_whale_trades_1m.csv` (compra/venta en USD y cantidad
de órdenes grandes, más el volumen taker total de cada mercado). Las órdenes que llegan partidas en varios
trades se reagrupan (Binance: mismo tiempo de ejecución y lado; Coinbase: mismo `taker_order_id`). No ve
ballenas que compran en pedacitos (TWAP, icebergs) ni movimientos on-chain.

- Dashboard: panel "Ballenas" con el neto por vela (barras) y el acumulado (línea), y un tile con la
  lectura de 1, 4 y 24 h: **comprando** / **vendiendo** si el desbalance (compra − venta) / total supera
  ±15 % (`WHALE_THRESHOLD` en `build_dashboard.py`), más el ratio long/short de los top traders de Binance
  y su cambio en 24 h.
- Reporte: sección `whales` de `contexto.json`.
- Sin historia: solo existe lo capturado mientras corre el servicio.

```bash
systemctl --user status cripto-ballenas
journalctl --user -u cripto-ballenas -f
```
