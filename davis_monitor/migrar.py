"""Re-decodifica la base de v2.3 (eventos_davis.db, columna trama_hex) con
el protocolo corregido y genera una base v3 nueva (eventos + resumen por
minuto). La base original se abre en SOLO LECTURA: nunca se modifica.

    python3 -m davis_monitor.migrar            # origen datos/legado_v23/ o ~/capturas_davis
                                               # destino datos/db/historico_v23_migrado.db
"""
import argparse
import math
import os
import sqlite3
from datetime import datetime

from . import protocolo
from .basedatos import abrir, fila_evento, _SQL_EVENTO, _SQL_RESUMEN
from .modelos import AgregadorVentana
from .nucleo import MAX_VUELCOS_POR_TRAMA


def migrar(origen: str, destino: str, cubeta_mm: float = 0.2, tx_id: int = 0, ventana: float = 60.0) -> dict:
    if os.path.exists(destino):
        raise SystemExit(f"Ya existe {destino}; bórralo o elige otro destino.")
    src = sqlite3.connect(f"file:{origen}?mode=ro", uri=True)
    dst = abrir(destino)
    periodo = (41 + tx_id) / 16.0
    stats = {"leidas": 0, "migradas": 0, "crc_invalido": 0, "otro_tx": 0, "lluvia_mm": 0.0, "minutos": 0}
    agg, bucket = AgregadorVentana(), None
    ultimos = {}
    prev_cont = None
    lote_e, lote_r = [], []
    dia_actual, lluvia_dia = None, 0.0

    def cerrar_bucket(b):
        r = agg.resumen()
        if not r["paquetes"]:
            return
        temp = r["temp_prom"] if r["temp_prom"] is not None else ultimos.get("temp")
        hum = r["humedad_prom"] if r["humedad_prom"] is not None else ultimos.get("humedad")
        esperados = max(1, round(ventana / periodo))
        fin = (b + 1) * ventana
        lote_r.append((datetime.fromtimestamp(fin).strftime("%Y-%m-%d %H:%M:%S"), fin, ventana,
                       temp, r["temp_min"], r["temp_max"], hum, protocolo.punto_rocio(temp, hum),
                       protocolo.vpd_kpa(temp, hum), r["viento_vel_prom"], r["viento_vel_max"],
                       r["viento_dir_prom"], r["rafaga_max"], r["lluvia_mm"], round(lluvia_dia, 3),
                       r["lluvia_tasa_max"], r["uv_prom"], r["uv_max"], r["solar_prom"], r["solar_max"],
                       None, r["paquetes"], esperados, round(min(100.0, 100.0 * r["paquetes"] / esperados), 1)))
        stats["minutos"] += 1

    for ts_txt, trama in src.execute("SELECT timestamp, trama_hex FROM eventos ORDER BY id"):
        stats["leidas"] += 1
        datos = bytes.fromhex(trama)
        if not protocolo.validar_crc(datos):
            stats["crc_invalido"] += 1
            continue
        lec = protocolo.decodificar(datos, cubeta_mm)
        if lec is None or lec.tx_id != tx_id:
            stats["otro_tx"] += 1
            continue
        ts = datetime.strptime(ts_txt, "%Y-%m-%d %H:%M:%S.%f").timestamp()
        dia = ts_txt[:10]
        if dia != dia_actual:
            dia_actual, lluvia_dia = dia, 0.0
        mm = 0.0
        c = lec.valores.get("lluvia_contador")
        if c is not None:
            if prev_cont is not None:
                d = (int(c) - prev_cont) % 128
                mm = d * cubeta_mm if d <= MAX_VUELCOS_POR_TRAMA else 0.0
            prev_cont = int(c)
        lluvia_dia += mm
        stats["lluvia_mm"] += mm
        b = math.floor(ts / ventana)
        if bucket is not None and b != bucket:
            cerrar_bucket(bucket)
            agg.reiniciar()
        bucket = b
        agg.registrar(lec.valores, mm)
        ultimos.update({k: v for k, v in lec.valores.items() if v is not None})
        lote_e.append(fila_evento(ts, lec, mm))
        stats["migradas"] += 1
        if len(lote_e) >= 5000:
            with dst:
                dst.executemany(_SQL_EVENTO, lote_e)
                dst.executemany(_SQL_RESUMEN, lote_r)
            lote_e, lote_r = [], []
    if bucket is not None:
        cerrar_bucket(bucket)
    with dst:
        dst.executemany(_SQL_EVENTO, lote_e)
        dst.executemany(_SQL_RESUMEN, lote_r)
    dst.close()
    src.close()
    stats["lluvia_mm"] = round(stats["lluvia_mm"], 2)
    return stats


def main() -> None:
    from . import config
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    legado = os.path.join(config.RUTAS.legado, "eventos_davis.db")
    p.add_argument("--origen", default=legado if os.path.isfile(legado)
                   else os.path.expanduser("~/capturas_davis/eventos_davis.db"))
    p.add_argument("--destino", default=config.RUTAS.db_migrada)
    p.add_argument("--cubeta-mm", type=float, default=config.TAMANO_CUBETA_MM)
    a = p.parse_args()
    os.makedirs(os.path.dirname(a.destino) or ".", exist_ok=True)
    for k, v in migrar(a.origen, a.destino, a.cubeta_mm, config.TX_ID_OBJETIVO).items():
        print(f"{k:<14} {v}")
    print(f"Base migrada: {a.destino}")


if __name__ == "__main__":
    main()
