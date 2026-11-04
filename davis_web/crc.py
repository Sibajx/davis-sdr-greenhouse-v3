"""Monitor de CRC en la terminal:  python3 -m davis_web.crc [horas]   (o: davis crc [horas])"""
import json
import sys
import time

from . import consultas


def _n(x):
    return "--" if x is None else f"{x:,}".replace(",", " ")


def main(horas: float = 24.0) -> None:
    hasta = time.time()
    d = consultas.enlace(hasta - horas * 3600, hasta)
    tot, c = d["totales"], d["contabilidad"]
    try:
        sdr = consultas.estado_en_vivo()["datos"]["sdr"]
    except (TypeError, KeyError):
        sdr = {}
    print(f"\nRecepción · últimas {horas:g} h   (ahora: {sdr.get('estado', '--')}, rtldavis {sdr.get('version_rtldavis') or '?'})")
    print(f"  Cobertura del periodo   {tot['cobertura_pct']} %   ({tot['horas_sin_datos']} h sin datos)")
    print(f"  Errores USB / reinicios {tot['errores_usb']} / {tot['reinicios']}")
    if not c:
        print(f"  Entrega (PDR)           {tot['entrega_pct']} %   ({_n(tot['validas'])} de {_n(tot['esperadas'])})")
        print("  Sin desglose de CRC en este rango (requiere rtldavis 0.15+crc; ver deploy/rtldavis/).\n")
        return
    p = c["pct"]
    print(f"\n  Transmisiones esperadas {_n(c['esperadas']):>10}   100 %")
    print(f"    válidas               {_n(c['validas']):>10}   {p['validas']} %")
    print(f"    dañadas (CRC)         {_n(c['danadas']):>10}   {p['danadas']} %")
    print(f"    no llegaron           {_n(c['sin_rastro']):>10}   {p['sin_rastro']} %")
    print(f"\n  Efectividad de decodificación  {c['efectividad_pct']} %   (válidas ÷ lo que llegó)")
    print(f"  Entrega (PDR)                  {c['entrega_pct']} %")
    x = c["descartes"]
    print(f"\n  Fallos de CRC {_n(x['crc_fallos'])}: {_n(x['crc_en_danadas'])} en transmisiones dañadas · "
          f"{_n(x['crc_ruido'])} ruido · {_n(x['crc_con_copia'])} copias de válidas")
    print(f"  Descartes correctos {_n(x['correctos'])} (ruido + copias + {_n(x['invalidas'])} inválidas + {_n(x['otro_tx'])} otro TX)\n")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 24.0)
