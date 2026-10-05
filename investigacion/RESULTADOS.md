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
