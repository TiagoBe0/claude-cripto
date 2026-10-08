"""Servidor del panel: los archivos de publico/ (como `python -m http.server`) y la configuración guardada.

La configuración del panel (paneles, timeframe, zoom, velas posibles…) se guarda en el servidor para que valga en
cualquier compu o celular. Leerla o cambiarla pide la clave de ~/.config/claude-cripto/panel.env (PANEL_CLAVE=…),
que viaja en el encabezado X-Clave; el panel es público y sin clave la dirección no existe para nadie.

    GET /sbs/btc/config  → la configuración guardada ({} si todavía no hay)
    PUT /sbs/btc/config  → la reemplaza (objeto JSON de texto → texto, hasta 64 kB)

Uso (lo arranca el servicio cripto-dashboard): python panel_server.py --port 8000 --bind 127.0.0.1
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONF_DIR = Path.home() / ".config" / "claude-cripto"
ENV_FILE = Path(os.environ.get("PANEL_ENV", CONF_DIR / "panel.env"))
STORE = Path(os.environ.get("PANEL_STORE", CONF_DIR / "panel.json"))
CONFIG_PATH = "/sbs/btc/config"
MAX_BYTES = 64 * 1024


def read_clave() -> str | None:
    """PANEL_CLAVE del archivo de entorno (se relee en cada pedido: cambiarla no pide reiniciar)."""
    try:
        for line in ENV_FILE.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "PANEL_CLAVE" and value.strip():
                return value.strip()
    except OSError:
        pass
    return None


class Handler(SimpleHTTPRequestHandler):
    def _send_json(self, code: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        clave = read_clave()
        sent = self.headers.get("X-Clave", "")
        return bool(clave) and hmac.compare_digest(sent.encode(), clave.encode())

    def _is_config(self) -> bool:
        return self.path.split("?", 1)[0] == CONFIG_PATH

    def do_GET(self) -> None:
        if not self._is_config():
            return super().do_GET()
        if not self._authorized():
            return self._send_json(403, {"error": "clave incorrecta"})
        try:
            conf = json.loads(STORE.read_text())
        except (OSError, ValueError):
            conf = {}
        self._send_json(200, conf)

    def do_PUT(self) -> None:
        if not self._is_config():
            return self.send_error(405)
        if not self._authorized():
            return self._send_json(403, {"error": "clave incorrecta"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if not 0 < size <= MAX_BYTES:
            return self._send_json(413, {"error": "configuración vacía o de más de 64 kB"})
        try:
            conf = json.loads(self.rfile.read(size))
        except ValueError:
            return self._send_json(400, {"error": "no es JSON"})
        if not isinstance(conf, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in conf.items()):
            return self._send_json(400, {"error": "tiene que ser un objeto de texto → texto"})
        # Escritura atómica: si se corta a la mitad queda la anterior
        STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(conf, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(STORE)
        self._send_json(200, {"ok": True})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--directory", default=str(ROOT / "publico"))
    args = ap.parse_args()
    handler = partial(Handler, directory=args.directory)
    with ThreadingHTTPServer((args.bind, args.port), handler) as srv:
        print(f"Panel en http://{args.bind}:{args.port}/ (archivos de {args.directory})", flush=True)
        srv.serve_forever()


if __name__ == "__main__":
    main()
