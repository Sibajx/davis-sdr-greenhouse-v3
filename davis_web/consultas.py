"""Lectura de datos para el dashboard. SIEMPRE en solo lectura (mode=ro):
el servidor web nunca puede bloquear ni modificar lo que escribe el receptor.

Fuentes, en orden: la base activa (datos/db/eventos_davis.db) y el histórico
migrado de la v2.3 (datos/db/historico_v23_migrado.db). Se consultan las dos
y se unen por tiempo, así el histórico se ve continuo.
"""
import json
import math
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from davis_monitor import config

# clave pública → (columna en `resumen`, agregación al bajar resolución, etiqueta, unidad, decimales)
VARIABLES: Dict[str, tuple] = {
    "temp":        ("temp_c",               "avg", "Temperatura",          "°C",    1),
    "temp_min":    ("temp_min_c",           "min", "Temperatura mínima",   "°C",    1),
    "temp_max":    ("temp_max_c",           "max", "Temperatura máxima",   "°C",    1),
    "humedad":     ("humedad_pct",          "avg", "Humedad relativa",     "%",     1),
    "rocio":       ("punto_rocio_c",        "avg", "Punto de rocío",       "°C",    1),
    "vpd":         ("vpd_kpa",              "avg", "Déficit de presión de vapor", "kPa", 3),
    "viento":      ("viento_prom_kmh",      "avg", "Viento promedio",      "km/h",  1),
    "viento_max":  ("viento_max_kmh",       "max", "Viento máximo",        "km/h",  1),
    "direccion":   ("viento_dir_prom",      "circ", "Dirección del viento", "°",    0),
    "rafaga":      ("rafaga_max_kmh",       "max", "Ráfaga máxima",        "km/h",  1),
    "lluvia":      ("lluvia_mm",            "sum", "Lluvia",               "mm",    2),
    "lluvia_tasa": ("lluvia_tasa_max_mm_h", "max", "Tasa de lluvia máx.",  "mm/h",  1),
    "uv":          ("uv_prom",              "avg", "Índice UV",            "UVI",   2),
    "uv_max":      ("uv_max",               "max", "Índice UV máximo",     "UVI",   2),
    "solar":       ("radiacion_prom_w_m2",  "avg", "Radiación solar",      "W/m²",  1),
    "solar_max":   ("radiacion_max_w_m2",   "max", "Radiación solar máx.", "W/m²",  1),
    "calidad":     ("calidad_pct",          "avg", "Calidad de recepción", "%",     1),
    "paquetes":    ("paquetes",             "sum", "Tramas recibidas",     "",      0),
}
VARIABLES_LLUVIA = ("lluvia", "lluvia_tasa")

# resoluciones permitidas (segundos) y elección automática por duración
RESOLUCIONES = (60, 300, 600, 900, 1800, 3600, 10800, 86400)


def variables_disponibles() -> Dict[str, dict]:
    out = {}
    for k, (_c, agg, etiqueta, unidad, dec) in VARIABLES.items():
        if k in VARIABLES_LLUVIA and not config.PLUVIOMETRO_ACTIVO:
            continue
        out[k] = {"etiqueta": etiqueta, "unidad": unidad, "decimales": dec, "agregacion": agg}
    return out


def resolucion_auto(segundos_rango: float, puntos_max: int = 1500) -> int:
    for r in RESOLUCIONES:
        if segundos_rango / r <= puntos_max:
            return r
    return RESOLUCIONES[-1]


def _bases() -> List[str]:
    r = config.RUTAS
    return [p for p in (r.db_migrada, r.db) if os.path.isfile(p)]


def _conectar(ruta: str) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{ruta}?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only=1")
    return c


def filas_resumen(desde: float, hasta: float, columnas: Sequence[str]) -> List[tuple]:
    """Filas por minuto (ts_unix, *columnas) de todas las bases, ordenadas y sin duplicados."""
    cols = ", ".join(columnas)
    filas: Dict[int, tuple] = {}
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            for f in c.execute(f"SELECT ts_unix, {cols} FROM resumen WHERE ts_unix > ? AND ts_unix <= ? "
                               "ORDER BY ts_unix", (desde, hasta)):
                filas[int(f[0] // 60)] = f   # una por minuto: la base activa (última) gana
            c.close()
        except sqlite3.Error:
            continue
    return [filas[k] for k in sorted(filas)]


def filas_eventos(desde: float, hasta: float, limite: int = 2_000_000) -> List[sqlite3.Row]:
    out = []
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            c.row_factory = sqlite3.Row
            out.extend(c.execute("SELECT * FROM eventos WHERE ts_unix > ? AND ts_unix <= ? ORDER BY ts_unix LIMIT ?",
                                 (desde, hasta, limite)).fetchall())
            c.close()
        except sqlite3.Error:
            continue
    out.sort(key=lambda r: r["ts_unix"])
    return out[:limite]


def filas_eventos_columnas(desde: float, hasta: float, columnas: Sequence[str]) -> List[tuple]:
    """Tuplas (ts_unix, *columnas) de las tramas: más liviano que filas_eventos para cálculos."""
    cols = ", ".join(columnas)
    out = []
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            out.extend(c.execute(f"SELECT ts_unix, {cols} FROM eventos WHERE ts_unix > ? AND ts_unix <= ? "
                                 "ORDER BY ts_unix", (desde, hasta)).fetchall())
            c.close()
        except sqlite3.Error:
            continue
    out.sort(key=lambda r: r[0])
    return out


def _agregar(valores: List[float], agg: str) -> Optional[float]:
    v = [x for x in valores if x is not None]
    if not v:
        return None
    if agg == "avg":
        return sum(v) / len(v)
    if agg == "max":
        return max(v)
    if agg == "min":
        return min(v)
    if agg == "sum":
        return sum(v)
    if agg == "circ":
        s = sum(math.sin(math.radians(x)) for x in v)
        c = sum(math.cos(math.radians(x)) for x in v)
        return None if abs(s) < 1e-9 and abs(c) < 1e-9 else math.degrees(math.atan2(s, c)) % 360
    return None


def serie(desde: float, hasta: float, claves: Sequence[str], resolucion: Optional[int] = None) -> dict:
    """Series agregadas a `resolucion` segundos. Cada punto se etiqueta al FINAL de su intervalo."""
    claves = [k for k in claves if k in variables_disponibles()]
    res = resolucion if resolucion in RESOLUCIONES else resolucion_auto(hasta - desde)
    cols = [VARIABLES[k][0] for k in claves]
    filas = filas_resumen(desde, hasta, cols) if cols else []
    t, datos = [], {k: [] for k in claves}
    if res == 60:
        for f in filas:
            t.append(int(f[0]))
            for i, k in enumerate(claves):
                datos[k].append(_red(f[i + 1], VARIABLES[k][4]))
    else:
        cubeta, acum = None, None
        for f in filas + [None]:
            b = None if f is None else math.ceil(f[0] / res) * res
            if b != cubeta and acum is not None:
                t.append(int(cubeta))
                for i, k in enumerate(claves):
                    datos[k].append(_red(_agregar(acum[i], VARIABLES[k][1]), VARIABLES[k][4]))
                acum = None
            if f is None:
                break
            if acum is None:
                acum, cubeta = [[] for _ in claves], b
            for i in range(len(claves)):
                acum[i].append(f[i + 1])
    return {"desde": desde, "hasta": hasta, "resolucion": res, "t": t, "series": datos,
            "variables": {k: variables_disponibles()[k] for k in claves}}


def _red(v, dec):
    return None if v is None else round(v, dec)


def rango_disponible() -> dict:
    mn, mx = None, None
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            a, b = c.execute("SELECT MIN(ts_unix), MAX(ts_unix) FROM resumen").fetchone()
            c.close()
            if a is not None:
                mn = a if mn is None else min(mn, a)
                mx = b if mx is None else max(mx, b)
        except sqlite3.Error:
            continue
    return {"desde": mn, "hasta": mx}


def estado_en_vivo() -> dict:
    """estado.json + diagnóstico de 'en línea'. Nunca lee el .tmp: el receptor
    escribe ahí y hace os.replace() atómico, así que estado.json siempre está completo."""
    ruta = config.RUTAS.estado
    try:
        edad = time.time() - os.path.getmtime(ruta)
        with open(ruta, encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return {"conexion": "fuera_de_linea", "motivo": "No hay estado.json: el receptor no está corriendo",
                "edad_archivo_seg": None, "datos": None}
    sdr = datos.get("sdr", {})
    if edad > 60:
        conexion, motivo = "fuera_de_linea", f"El receptor no actualiza el estado hace {edad / 60:.0f} min"
    elif sdr.get("estado") != "Recibiendo" or (sdr.get("segundos_sin_paquetes") or 0) > 120:
        conexion, motivo = "sincronizando", f"Receptor activo, enlace: {sdr.get('estado')}"
    else:
        conexion, motivo = "en_linea", "Recibiendo datos de la estación"
    return {"conexion": conexion, "motivo": motivo, "edad_archivo_seg": round(edad, 1), "datos": datos,
            "pluviometro_activo": config.PLUVIOMETRO_ACTIVO}


def parse_fecha(txt: str, fin_de_dia: bool = False) -> float:
    """'2026-11-03', '2026-11-03T14:30', '2026-11-03 14:30:00' o epoch → epoch (hora local)."""
    txt = (txt or "").strip()
    if not txt:
        raise ValueError("fecha vacía")
    try:
        return float(txt)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(txt, fmt)
            if fmt == "%Y-%m-%d" and fin_de_dia:
                d = d.replace(hour=23, minute=59, second=59)
            return d.timestamp()
        except ValueError:
            continue
    raise ValueError(f"fecha inválida: {txt!r}")
