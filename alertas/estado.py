"""Memoria de los avisos: qué se avisó ya y cuántos correos salieron en la última hora.

Cada aviso tiene una clave única (p. ej. "vela:1d:1791072000:Martillo"). Si la clave ya está, no se
repite: el chequeo de velas corre cada hora y vería la misma vela cerrada varias veces.
El tope por hora evita una lluvia de correos en una cascada de ballenas.

Cada chequeo usa su propio archivo (estado_<nombre>.json): ballenas corre cada minuto y velas cada
hora, y con un archivo compartido uno podía pisar lo que acababa de guardar el otro.

Dos corridas del mismo chequeo (cron más una a mano) se ordenan con un lock desde que leen el estado
hasta que lo guardan: sin él compartían el .tmp y una fallaba al renombrarlo, o repetían un aviso.
"""

import fcntl
import json
import os
import tempfile
import time

from alertas import mailer

STATE_DIR = mailer.ROOT / "data" / "alertas"
MAX_PER_HOUR = 12
KEEP_DAYS = 60


class Estado:
    def __init__(self, name: str):
        self.path = path = STATE_DIR / f"estado_{name}.json"
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # se suelta en save() o al terminar el proceso
        self._lock = open(STATE_DIR / f"estado_{name}.lock", "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("sent", {})      # clave -> epoch
        self.data.setdefault("recent", [])    # epochs de correos enviados (tope por hora)

    def seen(self, key: str) -> bool:
        return key in self.data["sent"]

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def notify(self, key: str, kind: str, subject: str, text: str) -> bool:
        """Avisa una vez por clave. Pasado el tope por hora, lo marca como visto y solo lo registra."""
        if self.seen(key):
            return False
        now = time.time()
        self.data["recent"] = [t for t in self.data["recent"] if now - t < 3600]
        self.data["sent"][key] = int(now)
        if len(self.data["recent"]) >= MAX_PER_HOUR:
            mailer._record(kind, subject, text, False, "tope de correos por hora")
            return False
        self.data["recent"].append(int(now))
        return mailer.send(kind, subject, text)

    def save(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        self.data["sent"] = {k: t for k, t in self.data["sent"].items() if t >= cutoff}
        fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=self.path.stem, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)
        self._lock.close()
