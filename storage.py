"""Almacenamiento incremental en CSV, común a todas las fuentes.

Cada serie es un CSV cuya primera columna es `timestamp` (ms UTC) y la
segunda `datetime_utc`. Las filas se agregan siempre en orden creciente.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def last_timestamp(csv_path: Path) -> int | None:
    """Devuelve el timestamp (ms) de la última fila del CSV, o None si no hay datos.

    Si la última línea quedó cortada (proceso matado a mitad de escritura),
    la descarta truncando el archivo hasta el último salto de línea.
    """
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return None
    with csv_path.open("r+b") as f:
        n_cols = len(f.readline().decode().rstrip("\n").split(","))
        # Leer desde el final para no recorrer archivos grandes
        size = f.seek(0, 2)
        start = max(0, size - 4096)
        f.seek(start)
        tail = f.read()
        if not tail.endswith(b"\n"):
            cut = tail.rfind(b"\n")
            f.truncate(start + cut + 1 if cut >= 0 else 0)
            tail = tail[: cut + 1] if cut >= 0 else b""
    lines = tail.decode().strip().splitlines()
    if not lines:
        return None
    fields = lines[-1].split(",")
    if len(fields) != n_cols or not fields[0].isdigit():
        return None  # solo el encabezado
    return int(fields[0])


class Appender:
    """Agrega filas a un CSV creando el encabezado si hace falta.

    Uso:
        with Appender(path, columns) as app:
            app.write(rows)   # filas sin datetime_utc: se agrega sola
    """

    def __init__(self, csv_path: Path, columns: list[str]):
        self.path = csv_path
        self.columns = columns
        self.added = 0

    def __enter__(self):
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        self._f = self.path.open("a", newline="")
        self._w = csv.writer(self._f, lineterminator="\n")
        if needs_header:
            self._w.writerow(self.columns)
            self._f.flush()  # procesos largos (liquidations_ws) pueden tardar en escribir la primera fila
        return self

    def write(self, rows) -> None:
        rows = [[r[0], iso_utc(r[0]), *r[1:]] for r in rows]
        for r in rows:
            if len(r) != len(self.columns):
                raise ValueError(f"{self.path.name}: fila con {len(r)} campos, se esperaban {len(self.columns)}")
        self._w.writerows(rows)
        self._f.flush()
        self.added += len(rows)

    def __exit__(self, *exc):
        self._f.close()


def export_json(csv_path: Path) -> Path:
    """Escribe <nombre>.json con la serie completa (valores vacíos -> null)."""
    dest = csv_path.with_suffix(".json")
    tmp = dest.with_suffix(".json.tmp")
    pd.read_csv(csv_path).to_json(tmp, orient="records", double_precision=10)
    tmp.replace(dest)
    return dest
