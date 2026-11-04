"""Configuración centralizada de davis_monitor v3 (carpeta davis_v2).

Precedencia (mayor a menor):
    1. Variable de entorno DAVIS_<NOMBRE>
    2. Archivo de configuración: DAVIS_CONFIG_FILE, o config.json / config.yaml
       en el directorio de trabajo, o config.json junto al paquete.
    3. Valor por defecto.

Diferencia clave con v2.3: importar este módulo YA NO crea carpetas ni
configura logging (efectos secundarios al importar = tests que ensucian el
log de producción). Eso ahora lo hace `preparar_entorno()`, que llama
__main__ de forma explícita.
"""
import os
import sys
import json
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("DavisMonitor")

try:
    import yaml  # opcional
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False

_DIR_PAQUETE = os.path.dirname(os.path.abspath(__file__))
_DIR_PROYECTO = os.path.dirname(_DIR_PAQUETE)


def _localizar_archivo_config() -> str:
    explicita = os.environ.get("DAVIS_CONFIG_FILE")
    if explicita:
        return explicita
    for base in (os.getcwd(), _DIR_PROYECTO):
        if _HAS_YAML and os.path.isfile(os.path.join(base, "config.yaml")):
            return os.path.join(base, "config.yaml")
        if os.path.isfile(os.path.join(base, "config.json")):
            return os.path.join(base, "config.json")
    return ""


def _cargar_archivo_config() -> dict:
    ruta = _localizar_archivo_config()
    if not ruta:
        return {}
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            if ruta.endswith((".yaml", ".yml")) and _HAS_YAML:
                datos = yaml.safe_load(f)
            else:
                datos = json.load(f)
        return datos if isinstance(datos, dict) else {}
    except Exception as e:  # noqa: BLE001 - nunca tumbar el arranque por el config
        print(f"[davis_monitor] No se pudo leer '{ruta}': {e}", file=sys.stderr)
        return {}


ARCHIVO_CONFIG_USADO = _localizar_archivo_config()
_CONFIG_ARCHIVO = _cargar_archivo_config()
_ADVERTENCIAS = []


def _valor(nombre: str, default, tipo=str):
    """nombre en minúsculas = clave del archivo; DAVIS_<NOMBRE> = variable de entorno."""
    env = "DAVIS_" + nombre.upper()
    crudo, origen = default, "default"
    if nombre in _CONFIG_ARCHIVO:
        crudo, origen = _CONFIG_ARCHIVO[nombre], "archivo"
    if env in os.environ:
        crudo, origen = os.environ[env], "entorno"
    if origen == "default":
        return default
    try:
        if tipo is bool and isinstance(crudo, str):
            return crudo.strip().lower() in ("1", "true", "sí", "si", "yes", "on")
        return tipo(crudo)
    except (TypeError, ValueError):
        _ADVERTENCIAS.append(f"Valor inválido {env}/{nombre} ({origen}): {crudo!r}; uso {default!r}")
        return default


# === Directorios ===
# Todo lo generado vive DENTRO del proyecto, en <proyecto>/datos (ver rutas.py).
# Se puede mover con dir_datos / DAVIS_DIR_DATOS (ruta relativa = relativa al
# proyecto). DAVIS_DIR_CAPTURAS se sigue aceptando por compatibilidad.
from .rutas import Rutas  # noqa: E402

DIR_PROYECTO: str = _DIR_PROYECTO
_dir = _valor("dir_datos", "") or _valor("dir_capturas", "") or "datos"
_dir = os.path.expanduser(_dir)
DIR_DATOS: str = _dir if os.path.isabs(_dir) else os.path.join(_DIR_PROYECTO, _dir)
RUTAS = Rutas(DIR_DATOS)
DIR_CAPTURAS = DIR_DATOS  # alias de compatibilidad
LOG_FILE: str = RUTAS.log
ARCHIVO_ESTADO_JSON: str = RUTAS.estado
ARCHIVO_DB: str = RUTAS.db
NIVEL_LOG: int = getattr(logging, str(_valor("log_level", "INFO")).upper(), logging.INFO)

# === Estación / protocolo ===
# Monitor de CRC: un CRC fallido a ± esta tolerancia del instante en que tocaba una
# transmisión (periodo exacto del ISS) se cuenta como transmisión DAÑADA; fuera, es ruido.
# Medido (3-nov-2026): las válidas caen a ≤ 2.2 ms de su ranura y el ruido es plano a
# ~4.2 CRC/s → con ±5 ms solo ~4 % de las ranuras perdidas se confundiría con "dañada".
TOLERANCIA_RANURA_SEG: float = _valor("tolerancia_ranura_seg", 0.005, float)
TX_ID_OBJETIVO: int = _valor("tx_id_objetivo", 0, int)          # 0..7 (consola Davis: ID-1)
# Cubeta del pluviómetro: 0.2 mm (versión métrica) o 0.254 mm (0.01", versión US)
TAMANO_CUBETA_MM: float = _valor("tamano_cubeta_mm", 0.2, float)
# false = pluviómetro dañado/desconectado: la lluvia se guarda VACÍA (NULL), no
# como "0 mm" (que sería un dato falso de "no llovió"). Las tramas crudas se
# siguen guardando en SQLite, así que se puede recalcular cuando se repare.
PLUVIOMETRO_ACTIVO: bool = _valor("pluviometro_activo", True, bool)
# Sin sonda de hoja/suelo conectada la estación no manda ese dato: no se
# exige para "datos completos".
CAMPOS_REQUERIDOS: str = _valor("campos_requeridos", "temp,humedad,viento_vel")
MAX_EDAD_DATO_SEG: float = _valor("max_edad_dato_seg", 600.0, float)

# === Enlace SDR ===
RTLDAVIS_CMD: str = _valor("rtldavis_cmd", "rtldavis")
RTLDAVIS_ARGS: str = _valor("rtldavis_args", "-tf US")
# Watchdog en DOS fases (medido en esta estación, ver reporte_davis.md):
#  * Sincronizando: rtldavis escucha el canal 0 hasta 135 s; la estación pasa
#    por ese canal una vez cada 51 saltos (~131 s). Hay UNA oportunidad por
#    arranque: si en 150 s no llegó nada, reintentar es lo más rápido.
#    (v2.3 mataba el proceso a los 30 s: solo ~23 % de los intentos podían lograrlo.)
#  * Sincronizado: con ~2 % de pérdida aleatoria, 90 s sin paquetes (~35
#    seguidos) = sincronía perdida con seguridad; rtldavis tardaría 51
#    paquetes (~131 s) en darse cuenta solo.
WATCHDOG_SYNC_SEG: float = _valor("watchdog_sync_seg", 150.0, float)
WATCHDOG_SIN_PAQUETES_SEG: float = _valor("watchdog_sin_paquetes_seg", 90.0, float)
FALLO_RAPIDO_SEG: float = _valor("fallo_rapido_seg", 10.0, float)
BACKOFF_INICIAL_SEG: float = _valor("backoff_inicial_seg", 2.0, float)
BACKOFF_MAX_SEG: float = _valor("backoff_max_seg", 60.0, float)

# === Persistencia / observabilidad ===
INTERVALO_CSV_SEG: float = _valor("intervalo_csv_seg", 60.0, float)
INTERVALO_COMMIT_SEG: float = _valor("intervalo_commit_seg", 15.0, float)
INTERVALO_HEARTBEAT_SEG: float = _valor("intervalo_heartbeat_seg", 900.0, float)
INTERVALO_ESTADO_JSON_SEG: float = _valor("intervalo_estado_json_seg", 5.0, float)
GUARDAR_TRAMAS_SQLITE: bool = _valor("guardar_tramas_sqlite", True, bool)
DIAS_RETENER_SIN_COMPRIMIR: int = _valor("dias_retener_sin_comprimir", 7, int)
COMPRIMIR_HISTORIAL_RESUMEN: bool = _valor("comprimir_historial_resumen", True, bool)
INTERVALO_COMPRESION_SEG: float = _valor("intervalo_compresion_seg", 3600.0, float)
LOG_A_STDERR: bool = _valor("log_a_stderr", True, bool)

# === Alertas ===
WEBHOOK_URL: str = _valor("webhook_url", "")
WEBHOOK_TIMEOUT_SEG: float = _valor("webhook_timeout_seg", 5.0, float)
WEBHOOK_FORMATO: str = _valor("webhook_formato", "json")
COOLDOWN_ALERTA_BATERIA_SEG: float = _valor("cooldown_alerta_bateria_seg", 1800.0, float)
UMBRAL_HELADA_C: float = _valor("umbral_helada_c", 2.0, float)
HISTERESIS_HELADA_C: float = _valor("histeresis_helada_c", 1.0, float)
UMBRAL_TORMENTA_MM_H: float = _valor("umbral_tormenta_mm_h", 25.0, float)
ALERTA_SIN_DATOS_SEG: float = _valor("alerta_sin_datos_seg", 900.0, float)


def _validar() -> list:
    p = list(_ADVERTENCIAS)
    if not 0 <= TX_ID_OBJETIVO <= 7:
        p.append(f"tx_id_objetivo={TX_ID_OBJETIVO} fuera de 0-7")
    if WATCHDOG_SYNC_SEG < 140:
        p.append(f"watchdog_sync_seg={WATCHDOG_SYNC_SEG} < 140: rtldavis espera hasta 135 s "
                 "en el canal 0; se cortarán sincronizaciones que iban a lograrse")
    if WATCHDOG_SIN_PAQUETES_SEG < 30:
        p.append(f"watchdog_sin_paquetes_seg={WATCHDOG_SIN_PAQUETES_SEG} < 30: reinicios por pérdidas normales")
    if BACKOFF_MAX_SEG < BACKOFF_INICIAL_SEG:
        p.append("backoff_max_seg < backoff_inicial_seg")
    if WEBHOOK_URL and not WEBHOOK_URL.startswith(("http://", "https://")):
        p.append(f"webhook_url={WEBHOOK_URL!r} no empieza con http(s)://")
    if WEBHOOK_FORMATO not in ("json", "ntfy"):
        p.append(f"webhook_formato={WEBHOOK_FORMATO!r} debe ser 'json' o 'ntfy'")
    for nombre in ("intervalo_csv_seg", "intervalo_commit_seg", "intervalo_estado_json_seg",
                   "intervalo_heartbeat_seg"):
        if globals()[nombre.upper()] <= 0:
            p.append(f"{nombre} debe ser > 0")
    return p


def periodo_tx_seg(tx_id: int = None) -> float:
    """Davis ISS transmite cada (41 + id)/16 s: id 0 → 2.5625 s."""
    tx = TX_ID_OBJETIVO if tx_id is None else tx_id
    return (41 + tx) / 16.0


def preparar_entorno(log_a_stderr: bool = None) -> None:
    """Crea la carpeta de capturas y configura logging (archivo rotativo +
    stderr para que journalctl muestre algo). Idempotente."""
    RUTAS.crear()
    raiz = logging.getLogger("DavisMonitor")
    if getattr(raiz, "_davis_configurado", False):
        return
    raiz.setLevel(NIVEL_LOG)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(threadName)s: %(message)s')
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    raiz.addHandler(fh)
    if LOG_A_STDERR if log_a_stderr is None else log_a_stderr:
        sh = logging.StreamHandler(sys.stderr)
        # journald ya pone la fecha; formato corto
        sh.setFormatter(logging.Formatter('[%(levelname)s] %(threadName)s: %(message)s'))
        raiz.addHandler(sh)
    raiz.propagate = False
    raiz._davis_configurado = True
    for adv in _validar():
        raiz.warning(f"Config sospechosa: {adv}")


def resumen_config() -> dict:
    return {
        "archivo_config": ARCHIVO_CONFIG_USADO or None,
        "dir_datos": DIR_DATOS,
        "tx_id": TX_ID_OBJETIVO,
        "rtldavis": f"{RTLDAVIS_CMD} {RTLDAVIS_ARGS}",
        "watchdog_sync_seg": WATCHDOG_SYNC_SEG,
        "watchdog_sin_paquetes_seg": WATCHDOG_SIN_PAQUETES_SEG,
        "cubeta_mm": TAMANO_CUBETA_MM,
        "pluviometro_activo": PLUVIOMETRO_ACTIVO,
        "webhook": bool(WEBHOOK_URL),
    }
