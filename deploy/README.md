# Despliegue en el servidor

Todo corre como usuario, sin root.

- `systemd/`: servicios de usuario. Los dos recolectores (ballenas y
  liquidaciones) y el servidor estático del panel en `127.0.0.1:8000`, que
  Cloudflare Tunnel publica en `malbecmotion.com/sbs/btc/`.

  ```bash
  cp deploy/systemd/*.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now cripto-ballenas cripto-liquidaciones cripto-dashboard
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

Las rutas absolutas de los servicios asumen
`/home/simaf/sbergamin/root/claude-cripto`.
