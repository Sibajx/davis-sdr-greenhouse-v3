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


MAX_PUNTOS = 6000        # tope por serie: protege a la Pi de consultas enormes (sitio público)
_MATH: Dict[str, bool] = {}


def _tiene_math(c: sqlite3.Connection, ruta: str) -> bool:
    if ruta not in _MATH:
        try:
            c.execute("SELECT sin(radians(1)), cos(0)").fetchone()
            _MATH[ruta] = True
        except sqlite3.OperationalError:   # SQLite compilado sin funciones matemáticas
            _MATH[ruta] = False
    return _MATH[ruta]


def _serie_sql(desde: float, hasta: float, claves: List[str], res: int) -> Dict[int, list]:
    """Agrega DENTRO de SQLite (GROUP BY cubeta): no carga cientos de miles de filas en
    Python. Cubeta = ceil(ts/res)*res → cada punto se etiqueta al final de su intervalo."""
    por_cubeta: Dict[int, list] = {}
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            math_ok = _tiene_math(c, ruta)
            exprs = []
            for k in claves:
                col, agg = VARIABLES[k][0], VARIABLES[k][1]
                if agg == "circ":
                    exprs += ([f"AVG(sin(radians({col})))", f"AVG(cos(radians({col})))"] if math_ok
                              else ["NULL", "NULL"])
                else:
                    exprs.append(f"{ {'avg': 'AVG', 'max': 'MAX', 'min': 'MIN', 'sum': 'SUM'}[agg] }({col})")
            sql = (f"SELECT CAST((ts_unix - 0.001) / {int(res)} AS INTEGER) + 1 AS b, {', '.join(exprs)} "
                   f"FROM resumen WHERE ts_unix > ? AND ts_unix <= ? GROUP BY b ORDER BY b")
            for f in c.execute(sql, (desde, hasta)):
                fila, i = [], 1
                for k in claves:
                    if VARIABLES[k][1] == "circ":
                        sn, cs = f[i], f[i + 1]
                        i += 2
                        fila.append(None if sn is None or (abs(sn) < 1e-9 and abs(cs) < 1e-9)
                                    else math.degrees(math.atan2(sn, cs)) % 360)
                    else:
                        fila.append(f[i])
                        i += 1
                por_cubeta[int(f[0]) * res] = fila   # la base activa (última) gana en la cubeta frontera
            c.close()
        except sqlite3.Error:
            continue
    return por_cubeta


def serie(desde: float, hasta: float, claves: Sequence[str], resolucion: Optional[int] = None) -> dict:
    """Series agregadas a `resolucion` segundos. Cada punto se etiqueta al FINAL de su intervalo.
    Si la resolución pedida daría más de MAX_PUNTOS puntos, se sube automáticamente."""
    claves = [k for k in claves if k in variables_disponibles()]
    res = resolucion if resolucion in RESOLUCIONES else resolucion_auto(hasta - desde)
    if (hasta - desde) / res > MAX_PUNTOS:
        res = resolucion_auto(hasta - desde, MAX_PUNTOS)
    cols = [VARIABLES[k][0] for k in claves]
    if res > 60 and claves:
        cub = _serie_sql(desde, hasta, claves, res)
        orden = sorted(cub)
        datos = {k: [_red(cub[b][i], VARIABLES[k][4]) for b in orden] for i, k in enumerate(claves)}
        return {"desde": desde, "hasta": hasta, "resolucion": res, "t": [int(b) for b in orden],
                "series": datos, "variables": {k: variables_disponibles()[k] for k in claves}}
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


# ---------------------------------------------------------------- monitor de recepción (CRC)
_SUMAS_ENLACE = ("validas", "esperadas", "danadas", "sin_rastro", "crc_fallos", "crc_ruido", "crc_con_copia",
                 "perdidas", "invalidas", "otro_tx", "errores_usb", "reinicios")


def _tiene_tabla(c: sqlite3.Connection, nombre: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)).fetchone() is not None


def enlace(desde: float, hasta: float, resolucion: Optional[int] = None) -> dict:
    """Contabilidad de la recepción por intervalo.

    Por RANURA de transmisión (la estación transmite con periodo exacto):
        válidas + dañadas (llegó con CRC fallido) + sin rastro = esperadas → 100 %
    Efectividad de decodificación = válidas ÷ (válidas + dañadas): de lo que llegó
    al receptor, cuánto se decodificó. Los CRC fuera de ranura (ruido), las copias
    dañadas de una transmisión que sí llegó, las inválidas y las de otro transmisor
    son DESCARTES CORRECTOS: no cuestan datos.

    Antes de la tabla `enlace` (receptor < 3.2) solo hay válidas/esperadas
    (tabla `resumen`): esos intervalos no tienen desglose (null).
    """
    res = resolucion or resolucion_auto(hasta - desde, 600)
    cubos: Dict[int, dict] = {}
    seg_con_datos = 0.0
    for ruta in _bases():
        try:
            c = _conectar(ruta)
            for b, val, esp, seg in c.execute(
                    "SELECT CAST(ts_unix / ? AS INTEGER), SUM(paquetes), SUM(paquetes_esperados), SUM(segundos) "
                    "FROM resumen WHERE ts_unix >= ? AND ts_unix < ? GROUP BY 1", (res, desde, hasta)):
                seg_con_datos += seg or 0
                d = cubos.setdefault(b, {"validas": 0, "esperadas": 0})
                d["validas"] += val or 0
                d["esperadas"] += esp or 0
            if _tiene_tabla(c, "enlace"):
                sumas = ", ".join(f"SUM({k})" for k in _SUMAS_ENLACE)
                for fila in c.execute(
                        f"SELECT CAST(ts_unix / ? AS INTEGER), {sumas}, AVG(error_freq_hz), MIN(crc_disponible) "
                        "FROM enlace WHERE ts_unix >= ? AND ts_unix < ? GROUP BY 1", (res, desde, hasta)):
                    d = dict(zip(_SUMAS_ENLACE, (x or 0 for x in fila[1:1 + len(_SUMAS_ENLACE)])))
                    d["error_freq_hz"] = _red(fila[-2], 0)
                    d["detallado"] = bool(fila[-1])   # todo el intervalo con rtldavis parchado
                    cubos[fila[0]] = d                # la tabla enlace manda sobre resumen en ese intervalo
            c.close()
        except sqlite3.Error:
            continue

    nombres = _SUMAS_ENLACE + ("error_freq_hz", "entrega_pct", "efectividad_pct")
    t, series = [], {k: [] for k in nombres}
    tot = {k: 0 for k in _SUMAS_ENLACE}
    det = {k: 0 for k in _SUMAS_ENLACE}
    crc_desde = None
    for b in sorted(cubos):
        d = cubos[b]
        detallado = d.get("detallado", False)
        t.append(b * res)
        for k in _SUMAS_ENLACE:
            v = d.get(k, 0)
            series[k].append(v if (detallado or k in ("validas", "esperadas")) else None)
            tot[k] += v or 0
            if detallado:
                det[k] += v or 0
        if detallado and crc_desde is None:
            crc_desde = b * res
        series["error_freq_hz"].append(d.get("error_freq_hz"))
        esp = d.get("esperadas") or 0
        series["entrega_pct"].append(_red(min(100.0, 100.0 * d["validas"] / esp), 1) if esp else None)
        llegaron = d["validas"] + d.get("danadas", 0)
        series["efectividad_pct"].append(_red(100.0 * d["validas"] / llegaron, 2) if detallado and llegaron else None)

    def pct(a, b):
        return _red(100.0 * a / b, 2) if b else None

    contabilidad = None
    if det["esperadas"]:
        e = det["esperadas"]
        correctos = det["crc_ruido"] + det["crc_con_copia"] + det["invalidas"] + det["otro_tx"]
        contabilidad = {
            "esperadas": e, "validas": det["validas"], "danadas": det["danadas"], "sin_rastro": det["sin_rastro"],
            "pct": {"validas": pct(det["validas"], e), "danadas": pct(det["danadas"], e),
                    "sin_rastro": pct(det["sin_rastro"], e)},
            "efectividad_pct": pct(det["validas"], det["validas"] + det["danadas"]),
            "entrega_pct": pct(det["validas"], e),
            "descartes": {"crc_fallos": det["crc_fallos"], "crc_ruido": det["crc_ruido"],
                          "crc_con_copia": det["crc_con_copia"], "crc_en_danadas":
                          max(0, det["crc_fallos"] - det["crc_ruido"] - det["crc_con_copia"]),
                          "invalidas": det["invalidas"], "otro_tx": det["otro_tx"], "correctos": correctos},
        }
    duracion = max(1.0, min(hasta, time.time()) - desde)
    cobertura = min(100.0, 100.0 * seg_con_datos / duracion)
    return {"desde": desde, "hasta": hasta, "resolucion": res, "t": t, "series": series,
            "totales": dict(tot, entrega_pct=pct(min(tot["validas"], tot["esperadas"]), tot["esperadas"]),
                            cobertura_pct=_red(cobertura, 1),
                            horas_sin_datos=_red((duracion - min(duracion, seg_con_datos)) / 3600, 1)),
            "contabilidad": contabilidad, "crc_desde": crc_desde}
