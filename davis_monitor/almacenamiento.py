"""CSV de resumen diario (legible por humanos / Excel / pipeline del paper)
y compresión de CSV antiguos."""
import os
import csv
import glob
import gzip
import re
import shutil
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Sequence

from .rutas import Rutas

logger = logging.getLogger("DavisMonitor")

ENCABEZADO = [
    "Fecha_Hora", "Temp_C", "Temp_Min_C", "Temp_Max_C", "Humedad_%", "Punto_Rocio_C", "VPD_kPa",
    "Viento_Prom_kmh", "Viento_Max_kmh", "Direccion_Prom_Grados", "Cardinal", "Rafaga_Max_kmh",
    "Lluvia_mm", "Lluvia_Hoy_mm", "Lluvia_Tasa_Max_mm_h", "UV_Prom", "UV_Max",
    "Radiacion_Prom_W_m2", "Radiacion_Max_W_m2", "Bateria_TX", "Paquetes", "Paquetes_Esperados",
    "Calidad_%",
]
_PATRON_FECHA = re.compile(r"(\d{4}-\d{2}-\d{2})(?:_v\d+)?\.csv$")


def _fmt(v, dec: int = 1) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.{dec}f}"
    return str(v)


class GestorCSV:
    """Escribe en datos/csv/AAAA/MM/historial_davis_AAAA-MM-DD.csv."""

    def __init__(self, rutas: Rutas):
        self.rutas = rutas
        self._archivo_actual: Optional[str] = None
        self._fecha_actual: Optional[str] = None

    def _ruta_para(self, cuando: datetime) -> str:
        os.makedirs(self.rutas.carpeta_csv(cuando), exist_ok=True)
        ruta = self.rutas.csv_del_dia(cuando)
        if os.path.isfile(ruta):
            try:
                with open(ruta, "r", encoding="utf-8") as f:
                    if f.readline().strip() != ",".join(ENCABEZADO):
                        # Archivo de otra versión: no mezclar columnas
                        ruta = self.rutas.csv_del_dia(cuando, "_v3")
            except OSError:
                pass
        return ruta

    def escribir(self, cuando: datetime, valores: Sequence) -> None:
        fecha = cuando.strftime("%Y-%m-%d")
        if fecha != self._fecha_actual:
            self._archivo_actual, self._fecha_actual = self._ruta_para(cuando), fecha
        ruta = self._archivo_actual
        nuevo = not os.path.isfile(ruta)
        try:
            with open(ruta, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if nuevo:
                    w.writerow(ENCABEZADO)
                w.writerow([cuando.strftime("%Y-%m-%d %H:%M:%S")] + list(valores))
        except OSError as e:
            logger.exception(f"Error escribiendo CSV {ruta}: {e}")

    def comprimir_antiguos(self, dias_retener: int, incluir_historial: bool = True) -> List[str]:
        limite = datetime.now().date() - timedelta(days=dias_retener)
        if not incluir_historial:
            return []
        hechos: List[str] = []
        for patron in ("historial_davis_*.csv",):
            for ruta in glob.glob(os.path.join(self.rutas.csv, "**", patron), recursive=True):
                m = _PATRON_FECHA.search(os.path.basename(ruta))
                if not m:
                    continue
                try:
                    if datetime.strptime(m.group(1), "%Y-%m-%d").date() >= limite:
                        continue
                except ValueError:
                    continue
                destino = ruta + ".gz"
                if os.path.isfile(destino):
                    continue
                tmp = destino + ".tmp"
                try:
                    with open(ruta, "rb") as fi, gzip.open(tmp, "wb") as fo:
                        shutil.copyfileobj(fi, fo)
                    os.replace(tmp, destino)
                    os.remove(ruta)
                    hechos.append(destino)
                except OSError as e:
                    logger.exception(f"Error comprimiendo {ruta}: {e}")
                    if os.path.exists(tmp):
                        os.remove(tmp)
        if hechos:
            logger.info(f"Comprimidos {len(hechos)} CSV antiguos")
        return hechos


__all__ = ["GestorCSV", "ENCABEZADO", "_fmt"]
