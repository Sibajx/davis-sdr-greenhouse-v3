"""Respaldos automáticos de los datos (en caliente, sin detener el receptor).

    python3 -m davis_monitor.respaldo                 # respaldar ahora
    python3 -m davis_monitor.respaldo --si-hace-falta # solo si el último tiene > 23 h (para cron cada hora)
    python3 -m davis_monitor.respaldo --listar
    python3 -m davis_monitor.respaldo --verificar     # comprueba integridad de todos los respaldos

Estructura (datos/respaldos/):
    AAAA-MM-DD_HHMM/            un respaldo ("instantánea")
        eventos_davis.db.gz     base activa (copia consistente vía API de backup de SQLite)
        usuarios.db.gz          cuentas del dashboard
        csv.tar.gz              todos los CSV
        config.json             configuración de la estación (si existe)
        manifiesto.json         tamaños, SHA-256, filas por tabla, rango de fechas
    fijos/                      archivos que no cambian, guardados UNA vez (se revisa su SHA-256)
        historico_v23_migrado.db.gz, legado_v23.tar.gz

Retención: los 7 más recientes + 1 por semana (4 semanas) + 1 por mes (12 meses).
Copia extra opcional: config `dir_respaldos_extra` (p. ej. un disco USB), separada por ';'.
Restaurar: ver docs/GUIA_COMPLETA.md, sección 14.
"""
import argparse
import fcntl
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from typing import Dict, List, Optional

from . import config

logger = logging.getLogger("DavisRespaldo")
FMT = "%Y-%m-%d_%H%M"
ESPACIO_MIN_LIBRE = 1024 ** 3            # no respaldar con menos de 1 GB libre
MAX_EDAD_SEG = 23 * 3600
RETENER_DIARIOS, RETENER_SEMANAS, RETENER_MESES = 7, 4, 12

DIR_RESPALDOS = os.path.join(config.DIR_DATOS, "respaldos")
EXTRAS: List[str] = [os.path.expanduser(d.strip())
                     for d in str(config._valor("dir_respaldos_extra", "")).split(";") if d.strip()]


def _sha256(ruta: str) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _gzip(origen: str, destino: str) -> None:
    with open(origen, "rb") as fi, gzip.open(destino, "wb", compresslevel=6) as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)


def respaldar_sqlite(origen: str, destino_gz: str) -> Dict:
    """Copia consistente con la API de backup (funciona con el receptor escribiendo),
    verifica la copia con integrity_check y la comprime."""
    with tempfile.TemporaryDirectory(dir=os.path.dirname(destino_gz)) as tmp:
        copia = os.path.join(tmp, "copia.db")
        src = sqlite3.connect(f"file:{origen}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(copia)
        try:
            src.backup(dst, pages=4096, sleep=0.01)   # por bloques: no bloquea al escritor
        finally:
            src.close()
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            dst.close()
            raise RuntimeError(f"integrity_check falló en la copia de {origen}: {ok}")
        info = {"integridad": ok, "tablas": {}}
        for (t,) in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            info["tablas"][t] = dst.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        try:
            a, b = dst.execute("SELECT MIN(timestamp), MAX(timestamp) FROM eventos").fetchone()
            info["rango_eventos"] = [a, b]
        except sqlite3.Error:
            pass
        dst.close()
        _gzip(copia, destino_gz)
    info.update(archivo=os.path.basename(destino_gz), bytes=os.path.getsize(destino_gz), sha256=_sha256(destino_gz))
    return info


def _tar(origen_dir: str, destino: str, nombre: str) -> Dict:
    with tarfile.open(destino, "w:gz", compresslevel=6) as t:
        t.add(origen_dir, arcname=nombre)
    return {"archivo": os.path.basename(destino), "bytes": os.path.getsize(destino), "sha256": _sha256(destino)}


def _fijos(manifiesto: Dict) -> None:
    """Histórico migrado y legado: no cambian; se guardan una vez y solo se rehacen si cambia su origen."""
    d = os.path.join(DIR_RESPALDOS, "fijos")
    os.makedirs(d, exist_ok=True)
    reg_path = os.path.join(d, "fijos.json")
    reg = json.load(open(reg_path)) if os.path.isfile(reg_path) else {}
    r = config.RUTAS
    if os.path.isfile(r.db_migrada):
        firma = f"{os.path.getsize(r.db_migrada)}:{os.path.getmtime(r.db_migrada)}"
        if reg.get("historico", {}).get("firma") != firma or not os.path.isfile(os.path.join(d, "historico_v23_migrado.db.gz")):
            info = respaldar_sqlite(r.db_migrada, os.path.join(d, "historico_v23_migrado.db.gz"))
            reg["historico"] = dict(info, firma=firma)
            logger.info("Histórico migrado respaldado (fijo)")
    if os.path.isdir(r.legado) and not os.path.isfile(os.path.join(d, "legado_v23.tar.gz")):
        reg["legado"] = _tar(r.legado, os.path.join(d, "legado_v23.tar.gz"), "legado_v23")
        logger.info("Legado v2.3 respaldado (fijo)")
    with open(reg_path, "w") as f:
        json.dump(reg, f, indent=1)
    manifiesto["fijos"] = {k: {"archivo": v.get("archivo"), "sha256": v.get("sha256")} for k, v in reg.items()}


def instantaneas(base: str = None) -> List[str]:
    base = base or DIR_RESPALDOS
    if not os.path.isdir(base):
        return []
    out = []
    for n in os.listdir(base):
        try:
            datetime.strptime(n, FMT)
            if os.path.isfile(os.path.join(base, n, "manifiesto.json")):
                out.append(n)
        except ValueError:
            continue
    return sorted(out)


def edad_ultimo() -> Optional[float]:
    s = instantaneas()
    return None if not s else time.time() - datetime.strptime(s[-1], FMT).timestamp()


def podar(base: str = None) -> List[str]:
    """Conserva 7 más recientes + el más reciente de cada una de las últimas 4 semanas
    + el más reciente de cada uno de los últimos 12 meses. Devuelve los borrados."""
    base = base or DIR_RESPALDOS
    s = instantaneas(base)
    conservar = set(s[-RETENER_DIARIOS:])
    semanas, meses = {}, {}
    for n in reversed(s):
        d = datetime.strptime(n, FMT)
        semanas.setdefault(d.strftime("%G-W%V"), n)
        meses.setdefault(d.strftime("%Y-%m"), n)
    conservar |= set(list(semanas.values())[:RETENER_SEMANAS]) | set(list(meses.values())[:RETENER_MESES])
    borrados = [n for n in s if n not in conservar]
    for n in borrados:
        shutil.rmtree(os.path.join(base, n), ignore_errors=True)
    return borrados


def respaldar() -> Dict:
    os.makedirs(DIR_RESPALDOS, exist_ok=True)
    libre = shutil.disk_usage(DIR_RESPALDOS).free
    if libre < ESPACIO_MIN_LIBRE:
        raise RuntimeError(f"Solo hay {libre / 1024 ** 3:.1f} GB libres; no se respalda (mínimo 1 GB)")
    nombre = datetime.now().strftime(FMT)
    final = os.path.join(DIR_RESPALDOS, nombre)
    if os.path.isdir(final):
        raise RuntimeError(f"Ya existe un respaldo de este minuto: {nombre}")
    tmp = final + ".parcial"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    t0 = time.time()
    r = config.RUTAS
    man = {"creado": datetime.now().isoformat(timespec="seconds"), "version": 1, "archivos": {}}
    try:
        if os.path.isfile(r.db):
            man["archivos"]["eventos"] = respaldar_sqlite(r.db, os.path.join(tmp, "eventos_davis.db.gz"))
        if os.path.isfile(r.usuarios_db):
            man["archivos"]["usuarios"] = respaldar_sqlite(r.usuarios_db, os.path.join(tmp, "usuarios.db.gz"))
        if os.path.isdir(r.csv):
            man["archivos"]["csv"] = _tar(r.csv, os.path.join(tmp, "csv.tar.gz"), "csv")
        cfg = os.path.join(config.DIR_PROYECTO, "config.json")
        if os.path.isfile(cfg):
            shutil.copy2(cfg, os.path.join(tmp, "config.json"))
            man["archivos"]["config"] = {"archivo": "config.json", "sha256": _sha256(cfg)}
        _fijos(man)
        man["segundos"] = round(time.time() - t0, 1)
        man["bytes_total"] = sum(v.get("bytes", 0) for v in man["archivos"].values())
        with open(os.path.join(tmp, "manifiesto.json"), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=1)
        os.replace(tmp, final)   # el respaldo solo "existe" cuando está completo
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    borrados = podar()
    man["podados"] = borrados
    for extra in EXTRAS:
        try:
            if not os.path.isdir(extra):
                logger.warning(f"Destino extra no disponible (¿disco desconectado?): {extra}")
                continue
            for sub in (nombre, "fijos"):
                destino = os.path.join(extra, "davis_respaldos", sub)
                if sub == "fijos":
                    shutil.copytree(os.path.join(DIR_RESPALDOS, "fijos"), destino, dirs_exist_ok=True)
                elif not os.path.isdir(destino):
                    shutil.copytree(final, destino)
            podar(os.path.join(extra, "davis_respaldos"))
            logger.info(f"Copiado también a {extra}")
        except OSError as e:
            logger.error(f"No se pudo copiar a {extra}: {e}")
    logger.info(f"Respaldo {nombre} OK: {man['bytes_total'] / 1024 ** 2:.1f} MB en {man['segundos']} s; "
                f"eventos={man['archivos'].get('eventos', {}).get('tablas')}; podados={len(borrados)}")
    return man


def verificar(base: str = None) -> List[str]:
    """Comprueba SHA-256 y que cada .gz se pueda descomprimir. Devuelve la lista de problemas."""
    base = base or DIR_RESPALDOS
    problemas = []
    for n in instantaneas(base):
        man = json.load(open(os.path.join(base, n, "manifiesto.json")))
        for k, v in man["archivos"].items():
            ruta = os.path.join(base, n, v["archivo"])
            if not os.path.isfile(ruta):
                problemas.append(f"{n}/{v['archivo']}: falta")
            elif _sha256(ruta) != v["sha256"]:
                problemas.append(f"{n}/{v['archivo']}: SHA-256 no coincide (archivo dañado)")
            elif ruta.endswith(".gz"):
                try:
                    with gzip.open(ruta) as f:
                        while f.read(1 << 20):
                            pass
                except (OSError, EOFError) as e:
                    problemas.append(f"{n}/{v['archivo']}: gzip ilegible ({e})")
    return problemas


def _configurar_log() -> None:
    config.RUTAS.crear()
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(os.path.join(config.RUTAS.logs, "respaldo.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logger.addHandler(fh)
        logger.addHandler(sh)


def main() -> None:
    p = argparse.ArgumentParser(description="Respaldos de los datos de la estación Davis")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--si-hace-falta", action="store_true", help="solo si el último respaldo tiene más de 23 h")
    g.add_argument("--listar", action="store_true")
    g.add_argument("--verificar", action="store_true")
    a = p.parse_args()
    _configurar_log()
    if a.listar:
        for n in instantaneas():
            m = json.load(open(os.path.join(DIR_RESPALDOS, n, "manifiesto.json")))
            ev = m["archivos"].get("eventos", {})
            print(f"{n}  {m.get('bytes_total', 0) / 1024 ** 2:7.1f} MB  eventos={ev.get('tablas', {}).get('eventos')}  "
                  f"hasta {(ev.get('rango_eventos') or [None, None])[1]}")
        e = edad_ultimo()
        print(f"Último respaldo: hace {e / 3600:.1f} h" if e is not None else "Sin respaldos todavía")
        print(f"Carpeta: {DIR_RESPALDOS}" + (f"  · extra: {', '.join(EXTRAS)}" if EXTRAS else ""))
        return
    if a.verificar:
        prob = verificar()
        print("\n".join(prob) if prob else f"OK: {len(instantaneas())} respaldos verificados (SHA-256 + gzip)")
        sys.exit(1 if prob else 0)
    # evitar dos respaldos simultáneos (cron + manual)
    os.makedirs(DIR_RESPALDOS, exist_ok=True)
    with open(os.path.join(DIR_RESPALDOS, ".lock"), "w") as candado:
        try:
            fcntl.flock(candado, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Ya hay un respaldo en curso.")
            return
        if a.si_hace_falta:
            e = edad_ultimo()
            if e is not None and e < MAX_EDAD_SEG:
                return
        try:
            respaldar()
        except Exception as ex:  # noqa: BLE001
            logger.error(f"RESPALDO FALLIDO: {ex}")
            try:
                from .alertas import Notificador
                n = Notificador()
                n.enviar(f"Davis: falló el respaldo automático ({ex})", "high", "floppy_disk,warning")
                n.cerrar()
            except Exception:  # noqa: BLE001
                pass
            sys.exit(1)


if __name__ == "__main__":
    main()
