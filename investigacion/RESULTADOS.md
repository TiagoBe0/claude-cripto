# Resultados

## Ronda 1 (2026-10-05) · hipótesis H1-H6 de HIPOTESIS.md

`python -m investigacion.estudio` · período 2020-01-01 a 2025-10-04 (holdout sin tocar) · tabla completa en
`data/research/estudio.csv`.

**Pasan 2 de 33 pruebas; por azar se esperaban ~1,6.** Ninguna hipótesis aislada demuestra ventaja:

- H3 corto, H = 7: pasa (exceso +2,7 %, t 2,1) pero con 7 eventos en la mitad A y 23 independientes en total.
- H4 largo, H = 1: pasa (exceso +0,7 %, t 2,3) pero no a 3 ni a 7 días: horizonte aislado, sospechoso.
- H2 (premium) y H5 (absorción) no muestran nada; H5 casi no tiene eventos (8 y 13).

Lo que sí se repite sin llegar al umbral: las señales de **multitud corta → largo** tienen exceso positivo en
las dos mitades y en los tres horizontes:

| | H = 1 | H = 3 | H = 7 |
|---|---|---|---|
| H1 largo (funding 3 d en percentil ≤ 10) | +0,24 % (t 1,0) | +1,13 % (t 1,7) | +1,89 % (t 1,6) |
| H4 largo (retail ≤ p10 y top traders subiendo) | +0,70 % (t 2,3) | +1,10 % (t 1,5) | +0,56 % (t 0,4) |
| H6 largo (RSI diario > 65) | +0,15 % (t 1,0) | +0,56 % (t 1,3) | +1,29 % (t 1,5) |

y del lado corto, H4 (retail ≥ p90 y top traders bajando) es positivo en las dos mitades a 3 y 7 días
(+1,3 % t 1,5; +2,5 % t 1,9).

Lectura: hay una pista consistente de **posicionamiento contrario** (ir contra el lado amontonado, a favor de las
cuentas grandes), pero cada señal sola tiene pocos eventos para separarse del ruido. Cualquier hipótesis de la
ronda 2 que combine estas señales nace de mirar esta tabla: su resultado en el período de investigación va a ser
optimista por construcción, y solo el holdout puede confirmarla.

## Ronda 2 (2026-10-05) · posicionamiento contrario combinado · NO PASA

`python -m investigacion.ronda2` · reglas registradas y commiteadas antes de correrla (7f380e5).

| | A 2021-03 → 2022 | B 2023 → 2025-10-04 | Total |
|---|---|---|---|
| Retorno | −8,0 % | +11,2 % | +1,7 % |
| Sharpe | 0,08 | 0,28 | 0,19 |
| Caída máxima | −34,5 % | −38,5 % | −38,5 % |
| Trades / aciertos / en mercado | 39 / 46 % / 27 % | 89 / 53 % / 50 % | 128 / 51 % / 41 % |
| BTC comprar y mantener | −63 %, Sharpe −0,43 | +637 %, Sharpe 1,77 | +171 %, Sharpe 0,67 |
| Correlación con BTC | +0,08 | −0,13 | −0,02 |

Falla los criterios 1 (Sharpe ≥ 0,5 en cada mitad) y 2 (caída < 35 %); pasa el de robustez (7 de 9 con Sharpe
> 0 en B, pero todos bajos: entre −0,50 y 0,36). Salidas: 81 por plazo, 32 por señal contraria, 14 por stop.

Lectura: el exceso que se veía en el estudio de eventos (sobre todo del lado largo) no alcanza para pagar costos,
funding y los cortos en un mercado alcista. Aun con el sesgo a favor de haber mirado la ronda 1, no llega.
**Descartada sin ir al holdout**, como estaba registrado: no se ajusta y se vuelve a probar.

## Ronda 3 (2026-10-05) · volatilidad gestionada en el perpetuo · NO PASA

`python -m investigacion.ronda3` · reglas registradas antes de correrla (32bc546).

Corrección antes de juzgar: la primera corrida comparaba contra un comprar y mantener del perpetuo **sin
rebalancear nunca**, que terminaba liquidado en mayo de 2021 (el funding se comía el margen y la exposición subía
sola a 1,42x). Eso no es "a 1x": se reemplazó por el perpetuo a 1x rebalanceado con la misma banda. Contra el rival
débil "pasaba"; contra el justo, no.

| | Volatilidad gestionada | Perp 1x rebalanceado | BTC spot |
|---|---|---|---|
| A 2020-2022: retorno / Sharpe / caída | +2,7 % / 0,31 / −76 % | +22,6 % / 0,49 / −81 % | +129,8 % / 0,76 / −77 % |
| B 2023-2025: retorno / Sharpe / caída | +665 % / 1,66 / −32 % | +521 % / 1,59 / −31 % | +637 % / 1,77 / −28 % |

Falla los dos criterios: en A rinde menos que el perpetuo a 1x, y en B mejora el Sharpe apenas (1,66 contra 1,59)
con una caída algo mayor. La grilla tampoco: 0 de 9 en A, 6 de 9 en B.

**Hallazgo:** en las dos mitades lo mejor es BTC spot. El funding del perpetuo es un costo estructural para quien
está comprado: en 2020-2022 el mismo BTC dio +130 % en spot y +23 % en el perpetuo. Para exposición compradora,
el perpetuo no conviene; solo tiene sentido para cortos o para apalancamiento corto en el tiempo.

## Ronda 4 (2026-10-05) · emisión de stablecoins · NO PASA

`python -m investigacion.ronda4` · reglas registradas antes de correrla (0ad393e) · tabla en `data/research/ronda4.csv`.

**Pasan 0 de 18 pruebas** (por azar se esperaba ~0,9). Excesos sobre la base, netos de costos y funding:

| | H = 7 | H = 14 | H = 30 |
|---|---|---|---|
| S1 largo (día de emisión ≥ p95) | +0,40 % (t 0,4) | +0,46 % (t 0,3) | −0,77 % (t −0,3) |
| S1 corto (día de quema ≤ p5) | +0,22 % (t 0,2) | +0,23 % (t 0,1) | +2,12 % (t 0,7), B 18 eventos |
| S2 largo (emisión 30 d ≥ p80) | +0,56 % (t 0,6) | +1,40 % (t 0,9) | +3,56 % (t 1,1) |
| S2 corto (emisión 30 d ≤ p20) | +1,30 % (t 0,8) | +2,23 % (t 0,7) | +7,01 % (t 1,2), B −24 % con 2 eventos |
| S3 largo (pólvora seca) | −0,21 % | −1,02 % | −0,65 % |
| S3 corto | +1,89 % (t 1,4), B −0,75 % | +0,89 % | −0,21 % |

Lectura:

- **El día de emisión fuerte (S1) no anticipa nada**: el exceso está cerca de cero en todos los horizontes. Que
  Tether o Circle emitan mucho un día no es una señal de compra.
- **La "pólvora seca" (S3) tampoco**: emisión sin suba previa da excesos negativos del lado largo.
- **Lo único consistente es S2 largo**: cuando la emisión de 30 días está alta, BTC rinde más que la base en las
  dos mitades y en los tres horizontes, creciendo con el plazo (+0,6 / +1,4 / +3,6 %). Pero con t ≤ 1,1 no se
  distingue del ruido, y probablemente sea el mismo fenómeno que "el mercado alcista": en las subas entra plata
  y se emiten stablecoins, o sea que la emisión acompaña al precio más que adelantarlo.
- Del lado corto, S2 casi no tiene eventos en B (la oferta de stablecoins creció casi todo 2023-2025).

**Descartada sin ir al holdout.** La emisión de stablecoins queda como dato de contexto en el panel (cuánta liquidez
nueva hay), no como señal de entrada.

## Ronda 5, parte histórica (2026-10-05) · liquidez de Amihud · NO PASA

`python -m investigacion.ronda5` · reglas registradas antes de correrla (bae2178) · tabla en `data/research/ronda5.csv`.

**Pasan 0 de 18 pruebas** (~0,9 por azar).

**L1 dio al revés de lo esperado.** Contra días con la misma volatilidad pasada, los días de iliquidez alta
(≥ p80) fueron seguidos de **menos** volatilidad, no de más: −7,4 % a 1 día (t −2,1) y −9,3 % a 7 días (t −2,5),
negativo en las dos mitades a 1 y 7 días. Y los días de iliquidez baja (≤ p20), de algo más (+4,2 % a 1 día,
t 2,7, positivo en las dos mitades). Como estaba registrado esperando el signo contrario, **no pasa**, y no se
da vuelta la hipótesis después de verla.

Lectura probable, sin probar: la iliquidez de Amihud con velas de 1 h está dominada por el **volumen** (el
denominador), no por la retirada de market makers. Los días de poco volumen (fines de semana, feriados) dan
iliquidez alta y son días tranquilos, seguidos de días tranquilos; los de mucho volumen, al revés. O sea que con
velas no se mide lo que se quería medir: hace falta ver el libro directamente, que es lo que junta la parte en
vivo (`libro_1m.py`, hipótesis P5 y P6).

**L2 y L3** (dirección) tienen pocos eventos (menos de 20 por mitad) y excesos que cambian de signo entre mitades:
nada. Con este proxy, la liquidez tampoco da dirección.

Si se quisiera estudiar el patrón de L1 ("poco volumen hoy → poca volatilidad después"), sería una hipótesis nueva,
registrada aparte y con el día de la semana como control.

## Ronda 6 (2026-10-05) · medias móviles proyectadas · NO PASA

`python -m investigacion.ronda6` · reglas registradas antes de correrla (6f2ef52) · código verificado contra un
cálculo a fuerza bruta (0099e2c) · tabla en `data/research/ronda6.csv`.

**Pasan 0 de 18 pruebas** (~0,9 por azar). Excesos sobre la base, netos de costos y funding:

| | H = 7 | H = 14 | H = 30 |
|---|---|---|---|
| M1 largo (SMA 200 proyectada sube, ≥ p80) | +1,94 % (t 1,9) · A +4,7 / B −0,1 | +3,97 % (t 1,8) · B −0,4 | +8,0 % (t 1,3) · B −1,6 |
| M1 corto (≤ p20) | −0,24 % | −0,45 % | −1,40 % |
| M2 largo (SMA 50 proyectada sube) | +1,10 % (t 1,1) | +2,46 % (t 1,2) | +5,11 % (t 1,1) · B −1,0 |
| M2 corto | +0,16 % | +0,56 % | +2,42 % (t 0,5) |
| M3 largo (cruce dorado anunciado) | +1,48 % (t 0,7) | +1,32 % | +0,14 % |
| M3 corto (cruce de la muerte anunciado) | −3,10 % | −2,76 % | +1,71 % |

Lectura:

- **M1 largo es lo más cerca de pasar, y es momentum.** Exceso positivo en los tres horizontes con t ≈ 1,8-1,9,
  pero todo viene de 2020-2022 (+4,7 % a 7 días); en 2023-2025 es cero o negativo. Y el retorno simple a 190 días
  en percentil ≥ 80 da casi lo mismo (+1,73 % t 1,8 · A +3,7 / B +0,1 a 7 días; +3,32 % a 14). Como se registró,
  la proyección con el precio quieto es un momentum disfrazado y **no agrega nada sobre él**. Lo mismo M2 frente
  al retorno a 45 días.
- **Del lado corto no hay nada**: que la media vaya a bajar no anticipa bajas por encima de la base.
- **M3 tiene muy pocos eventos** (8 dorados y 9 de la muerte desde 2020, 7 independientes) para decir algo, y los
  excesos cambian de signo entre mitades. Lo único firme es descriptivo: **13 de los 17 cruces anunciados se dieron
  de verdad dentro de 20 días**, con una anticipación mediana de 15 a 17 días. El indicador del panel sí avisa
  antes del cruce; lo que no hay es ventaja en operarlo (tampoco operando el cruce real: −4,6 % a 7 días del
  lado largo, en línea con el estudio de eventos).

**Descartada sin ir al holdout.** "Proyectar medias" queda en el panel como ayuda de lectura (hacia dónde empujan
las velas que salen del promedio y si se acerca un cruce), no como predicción del precio.
