"""Avisos por correo: ballenas, cruces de medias, patrones de velas, órdenes de la estrategia C y cobertura sugerida.

- mailer.py   SMTP con la config de ~/.config/claude-cripto/alertas.env. Sin SMTP no envía y solo
              registra el aviso en data/alertas/alertas.log. Nunca lanza: un aviso que falla no
              puede cortar la extracción.
- estado.py   qué se avisó ya (para no repetir) y tope de correos por hora.
- velas.py    cada hora, después de la extracción: patrones de velas 4h y 1d, cruces de medias 1d,
              orden de la estrategia C y cuando la cobertura sugerida se activa o se apaga.
- ballenas.py cada minuto (cron): órdenes >= US$ 1 M y neto de ballenas de 15 min >= US$ 5 M.
"""
