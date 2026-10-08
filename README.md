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
- `external`: `fear_greed`, `deribit_dvol`, `coingecko_market`, `stablecoins`

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
| `BTC_market_1d.csv` | diaria | CoinGecko: `price_usd`, `market_cap_usd` y `volume_24h_usd` de BTC (todos los exchanges). La API gratis da 365 días hacia atrás |
| `stablecoins_1d.csv` | diaria | DefiLlama: oferta en unidades (≈ US$) de `stablecoins_usd` (todas las atadas al dólar), `usdt` y `usdc` |
| `crypto_market_1d.csv` | diaria | CoinMarketCap (API interna de su web, sin clave; puede cambiar sin aviso): `total_mcap_usd` y `altcoin_mcap_usd` de todo el mercado cripto, `btc_dominance`, `eth_dominance`, `volume_24h_usd` y `altcoin_volume_24h_usd`. Desde 2015 |
| `cex_flows_1h.csv` | 1 h | DefiLlama: suma de 80-90 exchanges de `inflows_24h/7d/30d_usd` (entradas netas a sus billeteras públicas, sin cambios de precio; positivo = entran) y `assets_usd` en custodia. Solo da el dato del momento: la historia arranca con el recolector. `cex_flows.json` guarda el detalle por exchange de la última foto |

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

y abrir <http://127.0.0.1:8000/dashboard.html> (`#2h`, `#1d`, `#1w`... para elegir el timeframe). Se recarga solo cada 5 min.

Timeframes: 1h, 4h y 1d son las velas bajadas de Binance; 2h, 3h y 5h se arman agrupando las de 1h (desde 2024) y 1w las de 4h
(desde 2017), todas alineadas como Binance (semanas de lunes a domingo UTC; 3h y 5h, que Binance no tiene, en una grilla
fija desde 1970). La vela en curso llega por el WebSocket de Binance; en 3h y 5h se arma con la parte ya cerrada que manda
el servidor (`partial` en `chart_<tf>.json`) más la hora en vivo. Validado: 2h y 1w dan exactamente las velas de Binance.

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
- *Proyectar medias* (interruptor arriba, 10/20/50 velas): SMA 50 y 200 hacia adelante, punteadas. La parte de la
  ventana que ya ocurrió es exacta; los cierres futuros se suponen iguales al último (línea) o yendo a ±1 desvío
  típico en el tramo (líneas tenues, desvío de las últimas 100 velas × √velas). Avisa si se cruzan dentro del tramo.
  Se calcula en el navegador, con la vela en vivo.
- *Velas posibles* (a / b / c / d, se prenden por separado para comparar; N = 10/20/50): 3 velas amarillas/negras
  después de la vela en curso, con el cuerpo y las sombras promedio de las últimas N velas cerradas (en % de la
  apertura); cada una abre donde cerró la anterior. Dirección: **a** la de la mayoría de las N velas; **b** el signo
  de la suma de los cuerpos, con el tamaño de las velas de ese lado; **c** alterna empezando contra la vela en curso;
  **d** según el MACD 12/26/9 del timeframe a la vista (línea MACD vs 0 = tendencia; histograma a favor/en contra y
  creciendo/achicándose = impulso TTT, pierde fuerza TT−T, corrección −T−TT, corrección terminando −TTT).
  No predicen: muestran el tamaño típico de las próximas velas. Se calcula en el navegador.
  *Probar en la historia*: se elige una vela del pasado (click, ◀ ▶ o flechas) y las velas posibles se calculan solo
  con lo anterior, dibujadas sobre las 3 reales; una tabla mide cada modo, cada fase del MACD y cada N con todas las
  velas del tramo a la vista (o todas las cargadas): acierto por vela, 3 de 3 y mediana del error del cierre, contra
  "siempre ▲" y "precio quieto". Medido en 1h/4h/1d (oct. 2026): 47–51 % por vela y 9–14 % las tres, como al azar.
- *Mapa de liquidez* (interruptor arriba, se recuerda en el navegador), sobre el panel de precio:
  barras horizontales con las liquidaciones **estimadas** por tramo de 0,5% hasta ±15% del precio (rojo = largos,
  azul = cortos, con el monto en los 3 tramos mayores de cada lado), líneas punteadas en los máximos y mínimos sin
  barrer y líneas naranjas en el muro más grande de cada lado de Binance y Coinbase (solo si es ≥ 3 veces un tramo
  típico). Sale de `data/dashboard/liquidity.json`, que usa las mismas funciones que el reporte (`reporte/liquidez.py`)
  y se regenera en cada corrida del cron.

### Análisis en el panel

Además del gráfico y las tarjetas, `build_dashboard.py` arma:

- **Estrategia en el gráfico** (`strategy.json` + columnas `strat_*` de los charts): EMA 1200, canal Donchian 120
  y stop trailing de la estrategia 4h sobre el precio, flechas de cada trade del backtest con su resultado y marcas
  de los trades del ejecutor. Tarjeta de rendimiento: curva de capital contra comprar y mantener desde 2018, caída,
  métricas dentro y fuera de muestra (las mismas de `estrategia/backtest.py`) y últimos trades.
- **Lectura de régimen** (`regime.json`, `estrategia/regimen.py`): estado de hoy en tendencia, momentum, RSI,
  volatilidad y distancia al máximo (desde 2017) y en Fear & Greed, funding y open interest (desde 2024), con el
  retorno 7 y 30 días después en días con el mismo estado contra la base, y si la diferencia se sostiene en
  2018-2022 y 2023-hoy. A mano: `.venv/bin/python estrategia/regimen.py`.
- **Dinero en el mercado** (`market.json`): market cap y volumen 24 h de BTC y total de stablecoins, con
  cambios a 1, 7 y 30 días y una tabla día a día de las últimas 2 semanas. El cambio de market cap es
  precio × oferta (no es plata que entra); la emisión de stablecoins es la mejor aproximación gratis.
- **Emisión de stablecoins**: creadas − quemadas por día de USDT y USDC (cambio de la oferta en unidades, así
  un desacople del dólar no cuenta como emisión), su percentil contra los 365 días anteriores, sumas de 7 y
  30 días, racha y barras de los últimos 90 días. **Emisión fuerte** / **quema fuerte** = día en el 5 % extremo
  del año (`STABLE_STRONG_PCTL` en `build_dashboard.py`). Se mide sobre USDT + USDC y no sobre el total, que
  salta cuando DefiLlama empieza a contar una stablecoin nueva. Granularidad diaria: el momento exacto de cada
  emisión requeriría leer las cadenas (Tron, Ethereum) o un servicio pago.
- **Modelo ML**: tarjeta con `data/ml/prediction.json` (P de movimiento, percentil, evaluación en vivo).
- **Laboratorio de backtest** (`lab_4h.json`, se baja al abrirlo): la estrategia reescrita en JavaScript (da los
  mismos números que Python), con parámetros a mano y un mapa de sensibilidad del Sharpe por Donchian × stop
  dentro y fuera de muestra, para ver si un resultado es robusto o un pico de sobreajuste.

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

## Libro y actividad por minuto (`libro_1m.py`, `whale_trades_ws.py`)

Para ver cuándo se retiran los market makers (bots de alta frecuencia), que no se ven directamente:

- `data/BTCUSDT_book_1m.csv` (servicio `cripto-libro`): cada minuto, profundidad en US$ del libro de Binance spot
  (±0,05 / 0,1 / 0,5 %) y perp (±0,05 / 0,1 %), spread en bps y `cover_pct` (hasta dónde llegó el libro devuelto).
- `data/BTCUSDT_activity_1m.csv` (lo escribe `whale_trades_ws.py`): por mercado y minuto, trades recibidos,
  órdenes taker reagrupadas y tamaño mediano de orden.
- Hipótesis P5 (libro fino → volatilidad) y P6 (vuelven los bots → dirección) en `investigacion/HIPOTESIS.md`,
  evaluadas cada mes con la ronda P. La versión histórica con velas (ronda 5, Amihud) no pasó: con velas se mide
  sobre todo el volumen, no el libro.

```bash
systemctl --user status cripto-libro
```

## Dataset para ML (`ml/build_dataset.py`)

Con `"ml"` en la config, cada corrida del cron reescribe `data/ml/dataset_1h.csv`: una fila por vela de 1 h
(desde 2024-01) con ~60 features y las etiquetas para entrenar un modelo que estime P(long) / P(short).

- Features: retornos y técnicos de 1 h relativos al precio, contexto 4 h / 1 d, flujo taker y basis del perp,
  OI y ratios long/short (métricas 5 m), funding, DVOL, Fear & Greed y, desde 2026-10, ballenas,
  liquidaciones y desbalance del libro (NaN antes de esa fecha).
- Sin filtración del futuro: la fila `timestamp` se conoce al cierre de la vela (`timestamp + 1 h`); las velas
  de 4 h / 1 d se unen por su hora de cierre y lo de mayor frecuencia se agrega dentro de la hora.
- Etiquetas por horizonte H (`ml.horizons`): `fwd_ret_{H}h` (log-retorno futuro) y `label_{H}h` = 1 / -1 / 0
  según supere ±`ml.threshold_pct[H]` %. Las últimas H filas quedan sin etiqueta.

A mano: `.venv/bin/python ml/build_dataset.py`.

### Baseline (`ml/baseline.py`)

`.venv/bin/python ml/baseline.py [--horizon 4]` (requiere `requirements-ml.txt`): walk-forward mensual desde
2025-01 con prior, regresión logística y LightGBM; escribe `data/ml/baseline_{preds,report}_{H}h.*`.
Resultado 2026-10-05 (H = 4 h, 15.4k horas de test): LightGBM baja el log loss 0,972 -> 0,914 y gana al prior
22/22 meses, pero solo porque predice *si* habrá movimiento (AUC 0,70); la *dirección* queda en AUC 0,51 y la
señal P(long) - P(short) pierde después de costos.
Para reevaluar cuando ballenas, liquidaciones y libro junten historia (unos 3 meses):
`.venv/bin/python ml/baseline.py --since 2026-10-04 --test-start 2026-12`. Con `--since` el filtro de
cobertura ≥ 50 % se calcula sobre ese período, así esas features entran al modelo.

### Predicción en vivo (`ml/live_model.py`)

Con `ml.live_horizon` en la config (requiere `requirements-ml.txt`; si no está, solo falla este paso), cada
corrida reentrena el LightGBM con todo lo etiquetado (~4 s) y predice la última vela cerrada:
`data/ml/prediction.json` (P(short/neutral/long), P(movimiento) = 1 − P(neutral) y su percentil en 90 días) y
`data/ml/predictions_log.csv` (una fila por vela, solo se agrega). Con ≥ 48 velas ya etiquetadas,
`prediction.json` incluye la evaluación en vivo (log loss vs prior, AUC de movimiento y de dirección). En el
dashboard es la tarjeta "Modelo: movimiento fuerte en 4 h": en alerta cuando el percentil es ≥ 80.

### Cobertura sugerida (`estrategia/cobertura.py`)

La regla de la ronda 8 (`investigacion/`), la única que pasó investigación y holdout: 100 % en BTC spot y un
corto del 50 % en el perpetuo mientras en alguna de las últimas 24 h el percentil a 90 días de P(movimiento) fue
≥ 80. Con `"cobertura"` en la config, cada corrida:

- calcula P(movimiento) **fuera de muestra** como en la prueba: para cada mes, el LightGBM entrenado con todo lo
  anterior (no el de `live_model.py`, que ya vio las horas contra las que compara el percentil). Los meses cerrados
  quedan en `data/estrategia/cobertura_preds.csv`; el mes en curso se recalcula (~3 s);
- re-simula el paper trading desde `cobertura.start` con las funciones de `investigacion/ronda8.py` (spot, corto,
  costo maker 0,02 % por lado y funding real) contra comprar y mantener: `data/estrategia/cobertura.json` y
  `cobertura_log.csv` (una fila por hora, solo se agrega).

Corriendo el paper desde el inicio del holdout reproduce exacto el resultado de la ronda 8 (−11,3 %, Sharpe −0,30).
En el dashboard es la tarjeta "Cobertura sugerida" y `alertas/velas.py` manda un correo cuando se activa o se apaga.
Solo simula: no hay órdenes reales.

## Operar en Binance desde la consola (`trade.py`)

Compra y venta spot. **Por defecto va a la testnet** (testnet.binance.vision, plata ficticia); la cuenta
real se usa solo con `--real`, y ahí cada orden pide escribir `REAL` para salir.

```bash
.venv/bin/python trade.py precio
.venv/bin/python trade.py saldo
.venv/bin/python trade.py comprar 0.001                 # BTC a mercado
.venv/bin/python trade.py comprar 50 --usdt             # gastar 50 USDT a mercado
.venv/bin/python trade.py comprar 0.001 --precio 80000  # límite
.venv/bin/python trade.py vender todo                   # todo el BTC libre
.venv/bin/python trade.py ordenes
.venv/bin/python trade.py cancelar <id|todas>
.venv/bin/python trade.py historial
```

- Claves en `~/.config/claude-cripto/binance.env` (fuera del repo, permisos 600). La key real tiene que
  tener **solo permiso de trading spot, sin retiros**, y restringida a la IP del servidor.
- `config.json` → `trading.max_order_usdt`: tope por orden; una orden mayor se rechaza antes de enviarla.
- `--sin-enviar` arma la orden y la muestra sin mandarla (funciona sin claves).
- Antes de enviar valida mínimo de Binance, paso de cantidad y precio, y avisa si un límite está a más
  de 10 % del mercado o cruza el libro.
- Cada orden enviada queda en `data/trading/ordenes.jsonl`.

### Ejecutor automático (`estrategia/ejecutor.py`)

La estrategia 4h (`live_signal.py`) operando sola en Binance spot, dentro de la corrida horaria del cron.
Config en `config.json` → `trading.ejecutor`: `enabled`, `real` (por defecto `false` = testnet) y
`capital_usdt` (subcuenta virtual: compra con ese capital, no con el saldo entero).

- Compra / vende a mercado con la orden de la vela (una sola vez por vela) y deja un `STOP_LOSS` en
  Binance al stop vigente, que se mueve cuando sube. El stop salta en el momento, sin esperar al cron.
- Si la estrategia queda afuera y el ejecutor no, vende; al revés no entra tarde: espera una señal nueva.
- Con `state.json` viejo no manda órdenes (el stop ya puesto sigue protegiendo).
- Estado en `data/trading/ejecutor.json`, órdenes en `data/trading/ordenes.jsonl`, aviso por correo de cada
  compra y venta, y tarjeta en el dashboard. A mano: `.venv/bin/python -m estrategia.ejecutor [--estado]`.
