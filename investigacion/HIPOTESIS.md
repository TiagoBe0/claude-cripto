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
