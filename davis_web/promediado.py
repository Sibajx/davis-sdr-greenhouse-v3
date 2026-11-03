"""Promediado "método Davis" (replica el registro de archivo de la consola /
WiFiLogger de Davis: "average over the archive period").

Porta el método validado por el equipo en `generar_prueba_promediado_davis.py`:

    df_sdr[...].rolling('30min', min_periods=15).mean()

es decir, una media RETROSPECTIVA de 30 min cuya ventana es (t-30min, t]
(lado derecho cerrado, igual que pandas) y que solo es válida con al menos
15 muestras de 1 min. Aquí se implementa sin pandas (biblioteca estándar) y
con tres salidas:

  * movil()     → la serie continua minuto a minuto (idéntica a la del script).
  * archivo()   → un registro por marca de reloj (p. ej. :00 y :30), como los
                  exporta la consola Davis: promedio, máximos/mínimos del
                  intervalo, dirección dominante, ráfaga máxima, lluvia.
  * metodo="integral": media ponderada en el tiempo por trapecios sobre las
                  tramas crudas (∫x dt / ∫dt). Para muestreo regular da lo
                  mismo que la media; con huecos o muestreo irregular es más
                  exacta. Los segmentos con huecos > 5 min no se integran.
"""
import bisect
import math
from typing import Dict, List, Optional

from . import consultas
from davis_monitor import protocolo

# variables promediadas: clave → (columna resumen 1-min, columna en eventos crudos)
PROMEDIABLES = {
    "temp":    ("temp_c", "temp_c"),
    "humedad": ("humedad_pct", "humedad_pct"),
    "solar":   ("radiacion_prom_w_m2", "radiacion_w_m2"),
    "uv":      ("uv_prom", "indice_uv"),
    "viento":  ("viento_prom_kmh", "viento_kmh"),
}
INTERVALOS_MIN = (5, 10, 15, 30, 60, 120)
MAX_HUECO_INTEGRAL_SEG = 300


def movil(desde: float, hasta: float, ventana_min: int = 30, min_periodos: int = 15,
          claves=("temp", "humedad", "solar", "uv")) -> dict:
    """Serie instantánea (1 min) + su media móvil retrospectiva, punto a punto."""
    claves = [k for k in claves if k in PROMEDIABLES]
    v = ventana_min * 60
    filas = consultas.filas_resumen(desde - v, hasta, [PROMEDIABLES[k][0] for k in claves])
    t_out, inst, prom = [], {k: [] for k in claves}, {k: [] for k in claves}
    # dos punteros: suma y conteo deslizantes por variable (O(n))
    suma, cuenta, j = [0.0] * len(claves), [0] * len(claves), 0
    for i, f in enumerate(filas):
        for c in range(len(claves)):
            if f[c + 1] is not None:
                suma[c] += f[c + 1]; cuenta[c] += 1
        while filas[j][0] <= f[0] - v:          # sale de la ventana (t-v, t]
            for c in range(len(claves)):
                if filas[j][c + 1] is not None:
                    suma[c] -= filas[j][c + 1]; cuenta[c] -= 1
            j += 1
        if f[0] <= desde:
            continue
        t_out.append(int(f[0]))
        for c, k in enumerate(claves):
            dec = consultas.VARIABLES[k][4]
            inst[k].append(None if f[c + 1] is None else round(f[c + 1], dec))
            prom[k].append(round(suma[c] / cuenta[c], dec + 1) if cuenta[c] >= min_periodos else None)
    return {"t": t_out, "instantaneo": inst, "promediado": prom, "ventana_min": ventana_min,
            "min_periodos": min_periodos,
            "variables": {k: consultas.variables_disponibles()[k] for k in claves}}


def _media_integral(puntos: List[tuple], ini: float, fin: float, tiempos: List[float] = None) -> Optional[float]:
    """∫x dt / ∫dt por trapecios dentro de (ini, fin]; ignora tramos con huecos grandes."""
    area = dur = 0.0
    if tiempos is not None:   # recorta con búsqueda binaria: O(log n) en vez de recorrer todo
        a = max(0, bisect.bisect_left(tiempos, ini) - 1)
        b = bisect.bisect_right(tiempos, fin) + 1
        puntos = puntos[a:b]
    for (t0, x0), (t1, x1) in zip(puntos, puntos[1:]):
        if t1 <= ini or t0 >= fin or t1 - t0 > MAX_HUECO_INTEGRAL_SEG:
            continue
        a, b = max(t0, ini), min(t1, fin)
        if b <= a:
            continue
        # interpolación lineal en los bordes recortados
        xa = x0 + (x1 - x0) * (a - t0) / (t1 - t0)
        xb = x0 + (x1 - x0) * (b - t0) / (t1 - t0)
        area += (xa + xb) / 2 * (b - a)
        dur += b - a
    return area / dur if dur > 0 else None


def archivo(desde: float, hasta: float, intervalo_min: int = 30, metodo: str = "media",
            cobertura_min: float = 0.5) -> dict:
    """Un registro por marca de reloj múltiplo de `intervalo_min` (ventana (T-I, T])."""
    if intervalo_min not in INTERVALOS_MIN:
        raise ValueError(f"intervalo debe ser uno de {INTERVALOS_MIN}")
    if metodo not in ("media", "integral"):
        raise ValueError("metodo debe ser 'media' o 'integral'")
    I = intervalo_min * 60
    primera = math.floor(desde / I) * I + I   # registros con fin > desde (su ventana cae dentro del rango)
    cols = ["temp_c", "temp_min_c", "temp_max_c", "humedad_pct", "viento_prom_kmh", "viento_dir_prom",
            "rafaga_max_kmh", "radiacion_prom_w_m2", "radiacion_max_w_m2", "uv_prom", "uv_max", "lluvia_mm"]
    filas = consultas.filas_resumen(primera - I, hasta, cols)
    crudos: Dict[str, List[tuple]] = {}
    if metodo == "integral":
        cols_ev = [c[1] for c in PROMEDIABLES.values()]
        for ev in consultas.filas_eventos_columnas(primera - I - MAX_HUECO_INTEGRAL_SEG, hasta, cols_ev):
            for i, k in enumerate(PROMEDIABLES):
                if ev[i + 1] is not None:
                    crudos.setdefault(k, []).append((ev[0], ev[i + 1]))
    tiempos = {k: [p[0] for p in v] for k, v in crudos.items()}
    necesarias = max(1, math.ceil(intervalo_min * cobertura_min))
    registros, j = [], 0
    T = primera
    lluvia_activa = consultas.config.PLUVIOMETRO_ACTIVO
    while T <= hasta:
        ventana = []
        while j < len(filas) and filas[j][0] <= T:
            if filas[j][0] > T - I:
                ventana.append(filas[j])
            j += 1
        col = lambda n: [f[cols.index(n) + 1] for f in ventana]  # noqa: E731
        n_validas = sum(1 for x in col("temp_c") if x is not None)
        if n_validas >= necesarias:
            if metodo == "integral":
                prom = {k: _media_integral(crudos.get(k, []), T - I, T, tiempos.get(k, [])) for k in PROMEDIABLES}
            else:
                prom = {k: consultas._agregar(col(PROMEDIABLES[k][0]), "avg") for k in PROMEDIABLES}
            reg = {
                "fin": int(T),
                "temp": prom["temp"], "temp_alta": consultas._agregar(col("temp_max_c"), "max"),
                "temp_baja": consultas._agregar(col("temp_min_c"), "min"),
                "humedad": prom["humedad"],
                "rocio": protocolo.punto_rocio(prom["temp"], prom["humedad"]),
                "vpd": protocolo.vpd_kpa(prom["temp"], prom["humedad"]),
                "viento": prom["viento"],
                "direccion_dominante": consultas._agregar(col("viento_dir_prom"), "circ"),
                "rafaga": consultas._agregar(col("rafaga_max_kmh"), "max"),
                "solar": prom["solar"], "solar_alta": consultas._agregar(col("radiacion_max_w_m2"), "max"),
                "uv": prom["uv"], "uv_alto": consultas._agregar(col("uv_max"), "max"),
                "lluvia": consultas._agregar(col("lluvia_mm"), "sum") if lluvia_activa else None,
                "minutos": n_validas, "cobertura_pct": round(100.0 * n_validas / intervalo_min, 1),
            }
            for k, v in list(reg.items()):
                if isinstance(v, float):
                    reg[k] = round(v, 3 if k == "vpd" else 2)
            if reg["direccion_dominante"] is not None:
                reg["cardinal"] = protocolo.grados_a_cardinal(reg["direccion_dominante"])
            registros.append(reg)
        T += I
    return {"intervalo_min": intervalo_min, "metodo": metodo, "cobertura_min_pct": cobertura_min * 100,
            "registros": registros}


CAMPOS_ARCHIVO = [
    ("fin", "Fecha_Hora"), ("temp", "Temp_Prom_C"), ("temp_alta", "Temp_Alta_C"), ("temp_baja", "Temp_Baja_C"),
    ("humedad", "Humedad_Prom_%"), ("rocio", "Punto_Rocio_C"), ("vpd", "VPD_kPa"),
    ("viento", "Viento_Prom_kmh"), ("direccion_dominante", "Dir_Dominante_Grados"), ("cardinal", "Dir_Dominante"),
    ("rafaga", "Rafaga_Max_kmh"), ("solar", "Radiacion_Prom_W_m2"), ("solar_alta", "Radiacion_Alta_W_m2"),
    ("uv", "UV_Prom"), ("uv_alto", "UV_Alto"), ("lluvia", "Lluvia_mm"),
    ("minutos", "Minutos_Validos"), ("cobertura_pct", "Cobertura_%"),
]
