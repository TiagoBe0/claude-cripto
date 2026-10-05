# Hipótesis para una estrategia propia (registradas el 2026-10-05, antes de ver resultados)

Instrumento: perpetuo BTCUSDT de Binance, largo y corto, sin apalancamiento en la evaluación (1x).
Horizonte: swing, de 1 a 7 días.

## Reglas comunes (no se cambian después de ver resultados)

- **Datos:** `data/research/` (`python -m investigacion.datos`).
- **Holdout:** desde **2025-10-05** no se mira hasta el final. Se usa una sola vez, con la estrategia
  ya cerrada. Si falla, se descarta; no se ajusta y se vuelve a probar.
- **Investigación:** 2020-01-01 a 2025-10-04, en dos mitades: **A = 2020-2022** y **B = 2023 a 2025-10-04**.
  Las series de posicionamiento (open interest, ratios long/short) arrancan cuando Binance las publica
  (~2021-12); en esas hipótesis la mitad A es más corta.
- **Decisión una vez por día**, al cierre de la vela diaria (00:00 UTC), con datos conocidos hasta ese
  momento. Se entra a ese precio y se sale al cierre de t + H días.
- **Costos:** 0,20 % ida y vuelta (taker 0,05 % + slippage 0,05 % por lado) más el funding real del
  período: el largo paga el funding positivo y el corto lo cobra.
- **Percentiles:** siempre contra los últimos 365 días (mínimo 180), con el valor de hoy incluido. Así
  ninguna regla usa información del futuro.
- **Base:** la misma operación (largo o corto) todos los días del período. BTC subió mucho, así que un
  largo "gana" casi siempre; lo que cuenta es la diferencia con la base del mismo lado.
- **t:** diferencia media con la base / error estándar, con eventos separados al menos H días.
- **Pasa** si en las dos mitades la diferencia con la base, neta de costos, tiene el mismo signo
  favorable, con |t| ≥ 2 en el total y al menos 20 eventos independientes. Se prueban H = 1, 3 y 7
  días (3 horizontes × 6 hipótesis × 2 lados = 36 pruebas): con tantas pruebas, una pasa por azar cada
  ~20, así que una hipótesis aislada que pasa en un solo horizonte se toma con desconfianza.

## Hipótesis

**H1 · Funding extremo, contrario.** Funding promedio de los últimos 3 días (9 cobros) en percentil
≥ 90 → **corto**; ≤ 10 → **largo**. Idea: cuando un lado paga caro por mantener la posición, está
amontonado y vulnerable a un barrido.

**H2 · Premium extremo, contrario.** Premium del perpetuo sobre el índice, promedio de 24 h, con z-score
contra los últimos 30 días ≥ +2 → **corto**; ≤ −2 → **largo**. Idea: el perp muy caro o muy barato
contra el spot es demanda apalancada que se corrige.

**H3 · Squeeze por open interest.** Cambio del open interest en 3 días en percentil ≥ 80 y precio que
**baja** en esos 3 días → **largo** (cortos entrando contra un precio que no cae más: candidatos a ser
barridos). Open interest en percentil ≥ 80, precio que **sube** y funding de 3 días en percentil ≥ 80
→ **corto** (largos apalancados persiguiendo la suba).

**H4 · Retail contra top traders.** Ratio long/short de todas las cuentas en percentil ≥ 90 y ratio de
posición de top traders que bajó en 3 días → **corto**; retail en percentil ≤ 10 y top traders que
subieron → **largo**. Idea: seguir a las cuentas grandes cuando el retail está del otro lado.

**H5 · Absorción del flujo agresor.** Proporción de volumen taker comprador del perp en 24 h en
percentil ≥ 90 y precio que no subió en esas 24 h (≤ 0) → **corto**; en percentil ≤ 10 y precio que no
bajó (≥ 0) → **largo**. Idea: mucha compra agresiva que no mueve el precio está siendo absorbida por
vendedores pasivos grandes.

**H6 · Persistencia de fuerza (pista ya vista).** RSI 14 diario > 65 → **largo**. Ojo: esta salió de
la lectura de régimen del panel, o sea que ya se miró con datos de todo el período; se incluye, pero
para pasar exige además funcionar en el holdout.

## Después

1. Estudio de eventos de H1-H6 (`investigacion/estudio.py`): tabla por hipótesis, lado, horizonte y mitad.
2. Con las que pasen: una estrategia que las combine, con tamaño de posición y stop fijados de antemano,
   validada por walk-forward dentro del período de investigación.
3. Una sola corrida en el holdout.
4. Si pasa: testnet con el ejecutor (adaptado al perpetuo) durante semanas antes de pensar en plata real.

---

# Ronda 2 · posicionamiento contrario combinado (registrada el 2026-10-05, antes de correrla)

**Nace de mirar la ronda 1** (RESULTADOS.md): en el período de investigación va a salir optimista por
construcción. Lo que decide es el holdout.

## Señal

Puntaje diario al cierre (mismos datos y definiciones que la ronda 1), de −3 a +3:

| +1 (multitud corta) | −1 (multitud larga) |
|---|---|
| funding 3 d en percentil ≤ 10 | funding 3 d en percentil ≥ 90 |
| ratio L/S retail en percentil ≤ 10 y top traders (posición) subiendo en 3 d | retail en percentil ≥ 90 y top traders bajando en 3 d |
| premium z ≤ −2 | premium z ≥ +2 |

H6 (RSI) no entra: es fuerza de precio, no posicionamiento.

## Estrategia

- Puntaje ≥ +1 → **largo**; ≤ −1 → **corto**; 0 → sin señal nueva.
- Se entra al cierre diario (00:00 UTC) y se mantiene **3 días**. Si al cierre siguiente la señal del
  mismo lado se repite, el plazo vuelve a 3 días; si aparece la contraria, se da vuelta la posición.
- Tamaño: **1x** el capital (nocional = capital al entrar), sin apalancamiento.
- Stop de catástrofe a **10 %** del precio de entrada, controlado con velas de 1 h; si salta, se ejecuta a
  min(apertura, stop) en el largo y max(apertura, stop) en el corto.
- Costos: 0,10 % por lado sobre el nocional y el funding real cada 8 h mientras la posición está abierta.

## Evaluación (período de investigación; holdout sin tocar)

Mitad A = desde que hay percentiles válidos de las tres señales (~2021-03) hasta 2022; mitad B = 2023 a
2025-10-04. **Pasa** si:

1. retorno positivo y Sharpe ≥ 0,5 en **cada** mitad, neto de costos y funding;
2. al menos 30 trades y caída máxima menor a 35 %;
3. robustez: en la grilla percentil {80, 90, 95} × plazo {1, 3, 7} días, al menos 6 de las 9 combinaciones
   con Sharpe > 0 en la mitad B (la grilla se mira para ver estabilidad, **no** para elegir).

Se informa además, sin que decida: correlación con BTC, y si una cartera 50 % comprar y mantener + 50 %
estrategia mejora el Sharpe de comprar y mantener solo.

## Holdout (2025-10-05 en adelante, una sola corrida)

Pasa si el retorno es positivo, el Sharpe > 0 y la caída máxima menor a 35 %. Si no pasa, la ronda 2 se
descarta tal cual: no se ajusta y se vuelve a probar.

---

# Ronda 3 · BTC con tamaño gestionado por volatilidad (registrada el 2026-10-05, antes de correrla)

**No sale de nuestros datos:** es la idea de las "carteras gestionadas por volatilidad" (Moreira y Muir, 2017):
la volatilidad se predice mucho mejor que el retorno, y bajar la exposición cuando sube la volatilidad mejora la
relación retorno/riesgo. No intenta ganarle a BTC en retorno sino **quedarse con su suba con menos caídas**.
Siempre comprada: no hay señal de dirección. (Distinta de la cuenta G del paper trading viejo, que además
usaba una señal de momentum.)

## Estrategia

- Perpetuo BTCUSDT, **siempre largo**. Exposición objetivo = min(50 % / volatilidad realizada, **1,5x**).
- Volatilidad realizada: desvío de los retornos logarítmicos diarios de los últimos **30 días**, anualizado ×√365,
  medido al cierre diario (00:00 UTC).
- Rebalanceo una vez por día al cierre, solo si la exposición actual (que se mueve con el precio) se aparta del
  objetivo en más de **0,10**.
- Costos: 0,10 % por lado sobre el nocional operado y el funding real cada 8 h sobre el nocional abierto.
  Se verifica liquidación vela por vela de 1 h (con 1,5x haría falta una caída de ~65 % en el día).

## Comparación y criterios

Contra **comprar y mantener el perpetuo a 1x** pagando el mismo funding (la comparación justa); se informa
también BTC spot. Mitades A = 2020-01-01 → 2022 y B = 2023 → 2025-10-04. **Pasa** si:

1. en **cada** mitad, Sharpe mayor que el de comprar y mantener **y** caída máxima menor;
2. robustez: en la grilla ventana {14, 30, 60} días × objetivo {40, 50, 60} %, al menos 6 de 9 combinaciones con
   Sharpe mayor que comprar y mantener en **cada** mitad (no se usa para elegir).

## Holdout (una sola corrida)

Pasa si en el holdout el Sharpe es mayor o igual que el de comprar y mantener y la caída máxima menor.

---

# Ronda P · datos propios (registrada el 2026-10-05, con menos de 2 días de datos: no hay nada que mirar)

Datos que casi nadie tiene armados y que el sistema junta solo desde 2026-10-03/05: órdenes grandes (≥ US$ 100 mil)
de Binance spot, perp y Coinbase, liquidaciones de Binance y Bybit, y desbalance del libro. Se evalúan
**cada mes** (`python -m investigacion.propios`, cron del día 1) a medida que se acumulan.

## Reglas comunes

- Velas de 1 h del perpetuo (`data/BTCUSDT_perp_1h.csv`). Se decide al cierre de la hora con lo conocido hasta
  ese momento, se entra a ese precio y se sale al cierre de t + H, con **H = 4 y 24 horas**.
- Costo 0,20 % ida y vuelta. (El funding de 4 a 24 h es chico frente a eso y no se descuenta.)
- Base: la misma operación (largo o corto) en todas las horas con datos. t con eventos separados al menos H.
- **Esperando datos** mientras haya menos de **30 eventos independientes** para ese lado y horizonte.
- **Pasa** con ≥ 30 independientes, diferencia con la base positiva en la primera **y** en la segunda mitad del
  período con datos, y t ≥ 2. Con 4 hipótesis × 2 lados × 2 horizontes = 16 pruebas, una pasa por azar cada ~20:
  lo que pase se vuelve a confirmar con los meses siguientes antes de usarse.

## Hipótesis

**P1 · Flujo de ballenas, continuación.** Neto de ballenas de la hora (compras − ventas de órdenes grandes, los
tres mercados) con z-score ≥ +2 contra las horas de los últimos 7 días (mínimo 3 días) → **largo**; ≤ −2 → **corto**.

**P2 · Cascada de liquidaciones, reversión.** USD liquidado de largos en la hora en percentil ≥ 95 de los últimos
30 días (mínimo 7) → **largo**; de cortos en percentil ≥ 95 → **corto**. Idea: la venta forzada termina y el
precio rebota.

**P3 · Desbalance del libro.** Desbalance del libro spot a ±0,5 % (snapshot de la hora) en percentil ≥ 90 de los
últimos 30 días (mínimo 7) → **largo**; ≤ 10 → **corto**.

**P4 · Orden gigante.** Al menos una orden de ≥ US$ 1 M en la hora, con el neto de esas órdenes comprador →
**largo**; vendedor → **corto**.

---

# Ronda 4 · emisión de stablecoins (registrada el 2026-10-05, antes de correrla)

Idea: USDT y USDC se crean cuando alguien deposita dólares en Tether o Circle y se queman cuando los retira.
Emisión neta = dólares nuevos disponibles para comprar cripto ("pólvora seca"). Si llegan antes de usarse,
una emisión fuerte debería anticipar suba de BTC en días o semanas, y una quema, baja.

Qué se vio antes de registrar (para juzgar el sesgo): la distribución de la emisión neta diaria de USDT + USDC
de 2024 a hoy (mediana +62 M, percentiles 1 y 99 de −978 M y +1.824 M) y la tabla y las barras del panel de los
últimos ~90 días, que caen **en el holdout**. Nunca se cruzó la emisión con el precio.

## Datos y reglas

- Oferta diaria **en unidades** de USDT + USDC (DefiLlama, `data/research/stablecoins_1d.csv`,
  `python -m investigacion.datos`). Emisión neta del día = diferencia de la oferta con el día anterior.
  Se usa la suma USDT + USDC (más del 80 % del total) y no el total, que salta cuando DefiLlama suma una
  stablecoin nueva.
- **Un día de demora a propósito:** no está claro a qué hora del día arma DefiLlama el dato fechado D, así que la
  decisión al cierre del día D solo usa datos fechados ≤ D (nunca D + 1). Puede costar algo de ventaja; evita
  mirar el futuro.
- Todo lo demás, igual que la ronda 1: perpetuo BTCUSDT a 1x, decisión al cierre diario, costo 0,20 % ida y
  vuelta más el funding real, percentiles contra los últimos 365 días (mínimo 180) con el de hoy incluido, base =
  la misma operación todos los días, t con eventos separados al menos H días, mitades **A = 2020-2022** y
  **B = 2023 a 2025-10-04**, holdout desde **2025-10-05** sin tocar.
- Horizontes más largos que en la ronda 1, porque la plata nueva tarda en usarse: **H = 7, 14 y 30 días**.
- **Pasa** igual que en la ronda 1: diferencia con la base, neta de costos, con el mismo signo favorable en las
  dos mitades, t ≥ 2 en el total y al menos 20 eventos independientes. 3 hipótesis × 2 lados × 3 horizontes =
  18 pruebas: por azar se espera ~1. Una que pase en un solo horizonte se toma con desconfianza.

## Hipótesis

**S1 · Día de emisión fuerte.** Emisión neta del día en percentil ≥ 95 → **largo**; ≤ 5 (quema fuerte) → **corto**.

**S2 · Régimen de liquidez.** Emisión neta de 30 días en percentil ≥ 80 → **largo**; ≤ 20 → **corto**.

**S3 · Pólvora seca sin usar.** Emisión neta de 7 días en percentil ≥ 80 y BTC que no subió en esos 7 días (≤ 0)
→ **largo** (entró plata que todavía no empujó el precio). Emisión de 7 días en percentil ≤ 20 y BTC que no bajó
(≥ 0) → **corto**.

## Holdout (una sola corrida)

Solo si alguna pasa: se corre una vez en el holdout con las mismas reglas. Pasa si la diferencia con la base
tiene el mismo signo favorable con al menos 10 eventos independientes. Si no, se descarta sin ajustar.

---

# Ronda 5 · liquidez: cuando los market makers se retiran (registrada el 2026-10-05, antes de correrla)

Los bots de alta frecuencia que hacen de market makers ponen la mayor parte de las órdenes límite cerca del
precio. No se ven directamente: se ve su efecto. Cuando se retiran, cada dólar operado mueve más el precio
(libro fino). Lo esperable, y lo que dice la literatura, es que eso anticipe **volatilidad**; la pregunta abierta
es si anticipa **dirección**. Nada de esta ronda se miró antes de registrarla.

## Parte histórica (velas de 1 h del perpetuo, 2019 en adelante)

- **Iliquidez del día (Amihud):** promedio de las 24 horas del día de |retorno logarítmico de la hora| /
  volumen de la hora en US$ (`quote_volume`). Alta = libro fino. Percentil contra los últimos 365 días (mínimo
  180), hoy incluido. Se usa el percentil y no el valor porque el volumen de BTC creció mucho desde 2019.
- Reglas comunes de la ronda 1: perpetuo a 1x, decisión al cierre diario, costo 0,20 % más funding, base = la
  misma operación todos los días, t con eventos separados al menos H días, mitades **A = 2020-2022** y
  **B = 2023 a 2025-10-04**, holdout desde **2025-10-05** sin tocar. **H = 1, 3 y 7 días.**
- **Pasa** (las tres hipótesis): diferencia con la base del signo esperado en las dos mitades, |t| ≥ 2 en el
  total y al menos 20 eventos independientes. 3 hipótesis × 2 lados × 3 horizontes = 18 pruebas (~1 por azar).

**L1 · Libro fino → más volatilidad (no es una regla de trading: sirve para el tamaño de la posición).**
Variable: log(volatilidad realizada de los próximos H días / la de los últimos 7 días), con el desvío de los
retornos horarios. Como la volatilidad se agrupa y vuelve a su media, la base no son todos los días sino los días
del **mismo tercil de volatilidad pasada** (percentil de la volatilidad de 7 días: < 33, 33-67, > 67) y de la misma
mitad. Iliquidez en percentil ≥ 80 → se espera diferencia **positiva**; ≤ 20 → **negativa**.

**L2 · Movimiento sin liquidez, reversión.** Iliquidez en percentil ≥ 90 y BTC que bajó en el día → **largo**;
iliquidez ≥ 90 y BTC que subió → **corto**. Idea: un movimiento hecho con poca liquidez exagera y se corrige.

**L3 · Vuelven los bots.** El máximo del percentil de iliquidez de los 3 días anteriores ≥ 90 y hoy ≤ 50 (la
liquidez volvió), con BTC que bajó en esos 3 días → **largo**; que subió → **corto**. Idea: cuando los market
makers vuelven, el pánico (o la euforia) terminó.

## Parte en vivo (se suma a la ronda P, evaluación mensual en `investigacion/propios.py`)

Datos nuevos desde el 2026-10-05, capturados por minuto: profundidad del libro de Binance spot y perp a ±0,05,
±0,1 y ±0,5 % y spread (`libro_1m.py`), y cantidad de trades y de órdenes por mercado (`whale_trades_ws.py`).
Mismas reglas de la ronda P (velas de 1 h del perp, H = 4 y 24 horas, ≥ 30 eventos independientes, positivo en
las dos mitades del período con datos y t ≥ 2).

**P5 · Libro fino → volatilidad.** Profundidad del perp a ±0,1 % (compra + venta, promedio de los minutos de la
hora) en percentil ≤ 10 de los últimos 7 días (mínimo 2) → |retorno| de las próximas H horas mayor que la base
(sin costos: mide volatilidad, no dirección). Se informa también el lado opuesto (≥ 90 → menor).

**P6 · Vuelven los bots, en vivo.** Profundidad de la hora en percentil ≥ 50 después de que el mínimo de las 6
horas anteriores estuvo ≤ 10, con BTC que bajó en esas 6 horas → **largo**; que subió → **corto**.

---

# Ronda 6 · medias móviles proyectadas (registrada el 2026-10-05, antes de correrla)

Sale del indicador "Proyectar medias" del panel. La SMA de N dentro de k velas promedia N cierres, de los que
N − k ya ocurrieron: con el precio quieto en el cierre de hoy P, el cambio proyectado es exacto,

    SMA_N(t + k) − SMA_N(t) = k · (P − promedio de los k cierres más viejos de la ventana) / N.

O sea, la media proyectada sube si el precio de hoy está por encima de los cierres que van a salir del promedio.
**Advertencia que hay que tener presente al leer el resultado:** eso es un **momentum** disfrazado. Para la SMA 200
a 20 días compara P con los cierres de hace 181 a 200 días, y para la SMA 50 a 10 días, con los de hace 41 a 50
días. Si algo pasa, la pregunta siguiente es si agrega algo sobre el retorno simple a 6 meses o 6 semanas. Lo que
se pone a prueba es la idea que dio origen al indicador: que proyectar las medias ayuda a anticipar el precio.

Qué se vio antes de registrar (para juzgar el sesgo):

- El **estudio de eventos** (`estrategia/eventos.py`) ya midió los cruces dorado y de la muerte **reales** y el
  cierre que cruza la SMA 200, sobre spot diario desde 2017 **incluido el período que acá es holdout**: no
  mostraron ventaja. M3 es la versión anticipada de esos cruces, así que nace con esa información.
- La lectura de régimen del panel (cierre contra SMA 200 diaria, 2017 en adelante, también con el holdout) dio
  "no concluyente".
- La proyección de hoy en el panel (1d, precio quieto en 85.872 el 2026-10-05: SMA 50 +5,2 % y SMA 200 +2,4 % en
  20 velas), que cae en el holdout. Nunca se cruzó la proyección con el retorno siguiente.

## Datos y reglas

- **Medias sobre cierres diarios spot** (00:00 UTC) armados con las velas de 4 h de Binance desde 2017
  (`data/BTCUSDT_4h.csv`), las mismas que mira el panel. Así la SMA 200 ya es válida en 2018 y los percentiles
  tienen un año de historia cuando arranca el período.
- Proyección con el **precio quieto** en el cierre del día de decisión (la línea punteada del panel; el rango de
  ±1 desvío no entra porque es simétrico y no dice nada de dirección).
- Todo lo demás, igual que la ronda 1: perpetuo BTCUSDT a 1x, decisión al cierre diario, costo 0,20 % ida y
  vuelta más el funding real, percentiles contra los últimos 365 días (mínimo 180) con el de hoy incluido, base =
  la misma operación todos los días, t con eventos separados al menos H días, mitades **A = 2020-2022** y
  **B = 2023 a 2025-10-04**, holdout desde **2025-10-05** sin tocar.
- Horizontes del orden del tramo proyectado: **H = 7, 14 y 30 días**.
- **Pasa** igual que en la ronda 1: diferencia con la base, neta de costos, con el mismo signo favorable en las dos
  mitades, |t| ≥ 2 en el total y al menos 20 eventos independientes. 3 hipótesis × 2 lados × 3 horizontes =
  18 pruebas: por azar se espera ~1. Una que pase en un solo horizonte se toma con desconfianza.

## Hipótesis

**M1 · La SMA 200 va a subir.** Cambio proyectado de la SMA 200 a 20 días, en % de la SMA de hoy, en percentil
≥ 80 → **largo**; ≤ 20 → **corto**.

**M2 · La SMA 50 va a subir.** Lo mismo con la SMA 50 a 10 días: percentil ≥ 80 → **largo**; ≤ 20 → **corto**.

**M3 · Cruce anunciado.** Hoy la SMA 50 está debajo de la 200 y, con el precio quieto, la cruza hacia arriba dentro
de los próximos 20 días → **largo** (cruce dorado anunciado); arriba y la cruza hacia abajo → **corto** (cruce de
la muerte anunciado). Evento = el **primer** día en que aparece el cruce anunciado (el día anterior no lo anunciaba).
Si el cruce real ocurre antes de los 20 días, el evento sigue contando: lo que se mide es si anticiparlo sirve.

## Se informa, sin que decida

- Para lo que pase: la misma prueba con el retorno simple (P / cierre de hace 190 días para M1, de hace 45 días
  para M2) en percentil ≥ 80 / ≤ 20. Si da lo mismo, la proyección no agrega nada sobre el momentum.
- Para M3: cuántos de los cruces anunciados terminaron en un cruce real dentro de 20 días, y la diferencia de
  resultado contra entrar el día del cruce real (lo del estudio de eventos).

## Holdout (una sola corrida)

Solo si alguna pasa: se corre una vez en el holdout con las mismas reglas. Pasa si la diferencia con la base tiene
el mismo signo favorable con al menos 10 eventos independientes. Si no, se descarta sin ajustar.
