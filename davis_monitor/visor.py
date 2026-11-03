"""Visor de solo lectura de estado.json (no toca el dongle ni la base)."""
import json
import time

from . import config

_CLS = "\033[2J\033[H"


def _f(v, suf="", dec=1):
    return "--" if v is None else (f"{v:.{dec}f}{suf}" if isinstance(v, (int, float)) else f"{v}{suf}")


def formatear(d: dict) -> str:
    e, s = d["estacion"], d["sdr"]
    return "\n".join([
        f"davis_monitor {d.get('version', '')} — visor (actualizado {d['actualizado']})",
        "",
        f"  Temp {_f(e['temp_c'], '°C')}  Hum {_f(e['humedad_pct'], '%')}  Rocío {_f(e['punto_rocio_c'], '°C')}"
        f"  VPD {_f(e['vpd_kpa'], ' kPa', 2)}",
        f"  Viento {_f(e['viento_kmh'], ' km/h')} {e['viento_cardinal']}  Ráfaga {_f(e['rafaga_kmh'], ' km/h')}",
        f"  Lluvia hoy {_f(e['lluvia_hoy_mm'], ' mm')}  Tasa {_f(e['lluvia_tasa_mm_h'], ' mm/h')}",
        f"  UV {_f(e['uv'])}  Radiación {_f(e['solar_w_m2'], ' W/m²')}  Batería TX {_f(e['bateria_tx'])}",
        "",
        f"  SDR {s['estado']}  {_f(s['freq_mhz'], ' MHz', 3)} canal {_f(s['canal'])}"
        f"  válidas {s['paquetes_validos']}  CRC fail {s['descartes_crc']}",
        f"  Reinicios {s['reinicios']} (último {s['ultimo_reinicio']})  Última sync {_f(s['ultima_sync_seg'], ' s')}"
        f"  Calidad {_f(s['calidad_ultima_ventana_pct'], '%')}  Sin paquetes {_f(s['segundos_sin_paquetes'], ' s', 0)}",
        "", "Ctrl+C para salir",
    ])


def ejecutar_visor(intervalo: float = 2.0) -> None:
    try:
        while True:
            try:
                with open(config.ARCHIVO_ESTADO_JSON, encoding="utf-8") as fh:
                    print(_CLS + formatear(json.load(fh)), flush=True)
            except FileNotFoundError:
                print(f"{_CLS}No existe {config.ARCHIVO_ESTADO_JSON} — ¿corre davis_monitor?", flush=True)
            except (json.JSONDecodeError, KeyError):
                pass
            time.sleep(intervalo)
    except KeyboardInterrupt:
        print("\nVisor cerrado.")
