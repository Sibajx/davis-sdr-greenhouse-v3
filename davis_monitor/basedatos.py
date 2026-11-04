"""SQLite: una fila por trama (solo lo que esa trama midió) + una fila
por ventana de resumen (espejo del CSV, consultable con SQL).

Escritura en un hilo dedicado con cola y commits agrupados cada
INTERVALO_COMMIT_SEG: el hilo del SDR nunca espera a disco, y en una
Raspberry Pi con microSD se pasa de ~1400 commits/hora a ~240.

CLI:
    python -m davis_monitor.basedatos resumen
    python -m davis_monitor.basedatos calidad [--dias 7]
    python -m davis_monitor.basedatos exportar [--salida x.csv] [--desde ..] [--hasta ..] [--tabla resumen]
    (sin ruta, los CSV exportados quedan en datos/exportaciones/)
"""
import os
import csv
import queue
import sqlite3
import logging
import argparse
import threading
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger("DavisMonitor")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,          -- hora local 'YYYY-MM-DD HH:MM:SS.mmm'
    ts_unix REAL NOT NULL,
    tipo_paquete TEXT NOT NULL,
    tx_id INTEGER,
    bateria_baja INTEGER,
    viento_kmh REAL,
    viento_dir_grados REAL,
    temp_c REAL,
    humedad_pct REAL,
    rafaga_kmh REAL,
    lluvia_tasa_mm_h REAL,
    lluvia_contador INTEGER,
    lluvia_mm REAL,                   -- lluvia caída detectada en ESTA trama
    indice_uv REAL,
    radiacion_w_m2 REAL,
    trama_hex TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eventos_ts ON eventos(ts_unix);
CREATE TABLE IF NOT EXISTS resumen (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    ts_unix REAL NOT NULL,
    segundos REAL,
    temp_c REAL, temp_min_c REAL, temp_max_c REAL,
    humedad_pct REAL, punto_rocio_c REAL, vpd_kpa REAL,
    viento_prom_kmh REAL, viento_max_kmh REAL, viento_dir_prom REAL,
    rafaga_max_kmh REAL,
    lluvia_mm REAL, lluvia_hoy_mm REAL, lluvia_tasa_max_mm_h REAL,
    uv_prom REAL, uv_max REAL, radiacion_prom_w_m2 REAL, radiacion_max_w_m2 REAL,
    bateria_baja INTEGER,
    paquetes INTEGER, paquetes_esperados INTEGER, calidad_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_resumen_ts ON resumen(ts_unix);
-- calidad del enlace de radio por ventana (contadores del intervalo, no acumulados)
-- Por ranura de transmisión: validas + danadas + sin_rastro = esperadas (100 %).
CREATE TABLE IF NOT EXISTS enlace (
    id INTEGER PRIMARY KEY,
    ts_unix REAL NOT NULL,
    segundos REAL,
    validas INTEGER,          -- transmisiones decodificadas bien
    esperadas INTEGER,        -- ranuras resueltas (válidas + dañadas + sin rastro)
    danadas INTEGER,          -- ranuras sin válida pero con CRC fallido: llegó corrupta
    sin_rastro INTEGER,       -- ranuras sin nada
    crc_fallos INTEGER,       -- tramas descartadas por CRC (todas)
    crc_ruido INTEGER,        -- ...fuera de toda ranura (falsas detecciones del demodulador)
    crc_con_copia INTEGER,    -- ...en una ranura que sí llegó válida
    perdidas INTEGER,         -- "packet missed" de rtldavis (diagnóstico)
    invalidas INTEGER,        -- CRC correcto pero contenido imposible
    otro_tx INTEGER,          -- tramas de otro transmisor
    errores_usb INTEGER,
    reinicios INTEGER,        -- reinicios de rtldavis (watchdog / USB)
    error_freq_hz REAL,       -- desvío de frecuencia medio que reporta rtldavis
    crc_disponible INTEGER    -- 1 si rtldavis tiene el parche que reporta los CRC
);
CREATE INDEX IF NOT EXISTS idx_enlace_ts ON enlace(ts_unix);
"""

_COLS_EVENTO = ("timestamp", "ts_unix", "tipo_paquete", "tx_id", "bateria_baja", "viento_kmh",
                "viento_dir_grados", "temp_c", "humedad_pct", "rafaga_kmh", "lluvia_tasa_mm_h",
                "lluvia_contador", "lluvia_mm", "indice_uv", "radiacion_w_m2", "trama_hex")
_SQL_EVENTO = f"INSERT INTO eventos ({','.join(_COLS_EVENTO)}) VALUES ({','.join('?' * len(_COLS_EVENTO))})"
COLS_RESUMEN = ("timestamp", "ts_unix", "segundos", "temp_c", "temp_min_c", "temp_max_c",
                "humedad_pct", "punto_rocio_c", "vpd_kpa", "viento_prom_kmh", "viento_max_kmh",
                "viento_dir_prom", "rafaga_max_kmh", "lluvia_mm", "lluvia_hoy_mm",
                "lluvia_tasa_max_mm_h", "uv_prom", "uv_max", "radiacion_prom_w_m2",
                "radiacion_max_w_m2", "bateria_baja", "paquetes", "paquetes_esperados", "calidad_pct")
COLS_ENLACE = ("ts_unix", "segundos", "validas", "esperadas", "danadas", "sin_rastro", "crc_fallos", "crc_ruido",
               "crc_con_copia", "perdidas", "invalidas", "otro_tx", "errores_usb", "reinicios", "error_freq_hz",
               "crc_disponible")
_SQL_ENLACE = f"INSERT INTO enlace ({','.join(COLS_ENLACE)}) VALUES ({','.join('?' * len(COLS_ENLACE))})"
_SQL_RESUMEN = f"INSERT INTO resumen ({','.join(COLS_RESUMEN)}) VALUES ({','.join('?' * len(COLS_RESUMEN))})"


def abrir(ruta_db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(ruta_db, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    # `enlace` de la versión de desarrollo (sin ranuras, unos minutos del 3-nov-2026): se rehace
    cols = {r[1] for r in conn.execute("PRAGMA table_info(enlace)")}
    if cols and "danadas" not in cols:
        conn.execute("DROP TABLE enlace")
    conn.executescript(ESQUEMA)
    conn.commit()
    return conn


def lluvia_acumulada_desde(ruta_db: str, desde_unix: float) -> float:
    """Para no perder la lluvia del día al reiniciar el servicio."""
    if not os.path.isfile(ruta_db):
        return 0.0
    try:
        conn = abrir(ruta_db)
        r = conn.execute("SELECT COALESCE(SUM(lluvia_mm),0) FROM eventos WHERE ts_unix >= ?",
                         (desde_unix,)).fetchone()[0]
        conn.close()
        return float(r or 0.0)
    except sqlite3.Error as e:
        logger.warning(f"No se pudo leer lluvia acumulada: {e}")
        return 0.0


class EscritorSQLite:
    """Hilo único dueño de la conexión. Productores solo hacen queue.put()."""

    def __init__(self, ruta_db: str, intervalo_commit: float = 15.0, max_lote: int = 500):
        self.ruta_db = ruta_db
        self.intervalo = intervalo_commit
        self.max_lote = max_lote
        self._cola: "queue.Queue" = queue.Queue(maxsize=50000)
        self._fin = threading.Event()
        self.filas_escritas = 0
        self.errores = 0
        abrir(ruta_db).close()  # crea esquema / falla temprano si la ruta no sirve
        self._hilo = threading.Thread(target=self._bucle, name="SQLiteWriter", daemon=True)
        self._hilo.start()

    def evento(self, fila: tuple) -> None:
        self._put(("e", fila))

    def resumen(self, fila: tuple) -> None:
        self._put(("r", fila))

    def enlace(self, fila: tuple) -> None:
        self._put(("l", fila))

    def _put(self, item) -> None:
        try:
            self._cola.put_nowait(item)
        except queue.Full:
            self.errores += 1
            if self.errores % 1000 == 1:
                logger.error("Cola SQLite llena: se están perdiendo filas (¿disco lento/lleno?)")

    def _vaciar(self, conn, lote) -> None:
        ev = [f for t, f in lote if t == "e"]
        rs = [f for t, f in lote if t == "r"]
        en = [f for t, f in lote if t == "l"]
        try:
            with conn:
                if ev:
                    conn.executemany(_SQL_EVENTO, ev)
                if rs:
                    conn.executemany(_SQL_RESUMEN, rs)
                if en:
                    conn.executemany(_SQL_ENLACE, en)
            self.filas_escritas += len(lote)
        except sqlite3.Error as e:
            self.errores += 1
            logger.exception(f"Error escribiendo lote SQLite ({len(lote)} filas): {e}")

    def _bucle(self) -> None:
        conn = abrir(self.ruta_db)
        lote, ultimo = [], time.monotonic()
        try:
            while True:
                try:
                    lote.append(self._cola.get(timeout=1.0))
                except queue.Empty:
                    pass
                fin = self._fin.is_set()
                if lote and (fin or len(lote) >= self.max_lote
                             or time.monotonic() - ultimo >= self.intervalo):
                    if fin:  # drenar todo lo pendiente
                        while True:
                            try:
                                lote.append(self._cola.get_nowait())
                            except queue.Empty:
                                break
                    self._vaciar(conn, lote)
                    lote, ultimo = [], time.monotonic()
                if fin and self._cola.empty() and not lote:
                    break
        finally:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except sqlite3.Error:
                pass
            conn.close()

    def cerrar(self, timeout: float = 10.0) -> None:
        self._fin.set()
        self._hilo.join(timeout)


def fila_evento(ts: float, lectura, lluvia_mm: float) -> tuple:
    v = lectura.valores
    return (
        datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3], ts, lectura.tipo,
        lectura.tx_id, int(lectura.bateria_baja), v.get("viento_vel"), v.get("viento_dir"),
        v.get("temp"), v.get("humedad"), v.get("rafaga"), v.get("lluvia_tasa"),
        None if v.get("lluvia_contador") is None else int(v["lluvia_contador"]),
        lluvia_mm if lectura.tipo == "RAIN_COUNTER" else None,
        v.get("uv"), v.get("solar"), lectura.trama_hex,
    )


# ============================================================ CLI
def _exportar(ruta_db, tabla, desde, hasta, salida, tipo) -> int:
    conn = sqlite3.connect(ruta_db)
    cond, par = [], []
    if desde:
        cond.append("timestamp >= ?"); par.append(desde)
    if hasta:
        cond.append("timestamp <= ?"); par.append(hasta)
    if tipo and tabla == "eventos":
        cond.append("tipo_paquete = ?"); par.append(tipo)
    where = f"WHERE {' AND '.join(cond)}" if cond else ""
    cur = conn.execute(f"SELECT * FROM {tabla} {where} ORDER BY ts_unix", par)
    n = 0
    with open(salida, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([d[0] for d in cur.description])
        for fila in cur:  # streaming: no carga todo en RAM
            w.writerow(fila); n += 1
    conn.close()
    return n


def calidad(ruta_db: str, dias: float = 7, periodo: float = 2.5625) -> dict:
    """Eficiencia de recepción y huecos, a partir de los timestamps."""
    conn = sqlite3.connect(ruta_db)
    desde = time.time() - dias * 86400
    ts = [r[0] for r in conn.execute("SELECT ts_unix FROM eventos WHERE ts_unix>=? ORDER BY ts_unix", (desde,))]
    conn.close()
    if len(ts) < 2:
        return {"paquetes": len(ts)}
    huecos = [b - a for a, b in zip(ts, ts[1:])]
    sesion = sum(g for g in huecos if g < 900)  # >15 min = programa apagado
    esperadas = sesion / periodo
    return {
        "paquetes": len(ts),
        "horas_monitoreadas": round(sesion / 3600, 2),
        "eficiencia_pct": round(100 * (len(ts) - 1) / esperadas, 2) if esperadas else None,
        "huecos_30s_15min": sum(1 for g in huecos if 30 <= g < 900),
        "minutos_perdidos_en_huecos": round(sum(g for g in huecos if 30 <= g < 900) / 60, 1),
        "hueco_max_seg": round(max(g for g in huecos if g < 900), 1) if any(g < 900 for g in huecos) else None,
    }


def _main_cli() -> None:
    from . import config
    p = argparse.ArgumentParser(description="Consulta/exporta la base de davis_monitor v3")
    p.add_argument("--db", default=config.ARCHIVO_DB)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resumen")
    pc = sub.add_parser("calidad"); pc.add_argument("--dias", type=float, default=7)
    pe = sub.add_parser("exportar")
    pe.add_argument("--tabla", choices=("eventos", "resumen"), default="eventos")
    pe.add_argument("--desde"); pe.add_argument("--hasta"); pe.add_argument("--tipo")
    pe.add_argument("--salida", help="nombre o ruta del CSV (por defecto en datos/exportaciones/)")
    a = p.parse_args()
    if not os.path.isfile(a.db):
        raise SystemExit(f"No existe la base de datos: {a.db}")
    if a.cmd == "resumen":
        c = sqlite3.connect(a.db)
        print("Eventos:", c.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM eventos").fetchone())
        print("Resumen:", c.execute("SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM resumen").fetchone())
        for t, n in c.execute("SELECT tipo_paquete, COUNT(*) FROM eventos GROUP BY 1 ORDER BY 2 DESC"):
            print(f"  {t:<16} {n}")
        print("Lluvia total (mm):", c.execute("SELECT ROUND(COALESCE(SUM(lluvia_mm),0),2) FROM eventos").fetchone()[0])
    elif a.cmd == "calidad":
        for k, v in calidad(a.db, a.dias, config.periodo_tx_seg()).items():
            print(f"{k:<28} {v}")
    else:
        nombre = a.salida or f"{a.tabla}_{(a.desde or 'inicio')[:10]}_{(a.hasta or 'fin')[:10]}.csv"
        salida = config.RUTAS.crear().exportacion(nombre)
        print(f"Exportadas {_exportar(a.db, a.tabla, a.desde, a.hasta, salida, a.tipo)} filas a {salida}")


if __name__ == "__main__":
    _main_cli()
