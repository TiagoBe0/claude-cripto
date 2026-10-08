# Despliegue en el servidor

Todo corre como usuario, sin root.

- `systemd/`: servicios de usuario. Los tres recolectores (ballenas,
  liquidaciones y libro por minuto) y el servidor del panel en `127.0.0.1:8000` (`panel_server.py`: los archivos
  de `publico/` y la configuración guardada), que Cloudflare Tunnel publica en `malbecmotion.com/sbs/btc/`.

  ```bash
  cp deploy/systemd/*.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now cripto-ballenas cripto-liquidaciones cripto-libro cripto-dashboard
  loginctl enable-linger "$USER"   # que sigan corriendo sin sesión abierta
  ```

- `crontab.txt`: la extracción horaria, que además regenera los datos del
  panel (`build_dashboard.py`). Se carga con `crontab -e`.

- `publico/sbs/btc/data/dashboard` es un enlace a `data/dashboard/`. No está en
  git porque `data/` se ignora entero; hay que crearlo a mano:

  ```bash
  mkdir -p publico/sbs/btc/data
  ln -s "$PWD/data/dashboard" publico/sbs/btc/data/dashboard
  ```

- Configuración del panel guardada en el servidor: la clave va en `~/.config/claude-cripto/panel.env`
  (fuera del repo) y la configuración queda en `~/.config/claude-cripto/panel.json`. Sin ese archivo la
  dirección `config` rechaza todo y el panel guarda solo en cada navegador. La clave se relee en cada pedido.

  ```bash
  mkdir -p ~/.config/claude-cripto && chmod 700 ~/.config/claude-cripto
  echo "PANEL_CLAVE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')" > ~/.config/claude-cripto/panel.env
  chmod 600 ~/.config/claude-cripto/panel.env
  ```

Las rutas absolutas de los servicios asumen
`/home/simaf/sbergamin/root/claude-cripto`.

## Avisos por correo (`alertas/`)

Sin configuración no envían nada: cada aviso queda en `data/alertas/alertas.log` (una línea JSON por
aviso). Para que salgan por mail, crear `~/.config/claude-cripto/alertas.env` (fuera del repo):

```bash
mkdir -p ~/.config/claude-cripto && chmod 700 ~/.config/claude-cripto
cat > ~/.config/claude-cripto/alertas.env <<CONF
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=tu.cuenta@gmail.com
SMTP_PASS=contraseña-de-aplicación
ALERT_TO=tu.cuenta@gmail.com
CONF
chmod 600 ~/.config/claude-cripto/alertas.env
```

Corren por cron (ver `crontab.txt`): `alertas.velas` a los :06 de cada hora, después del extractor, y
`alertas.ballenas` cada minuto. Umbrales y su fundamento en el encabezado de cada archivo.
