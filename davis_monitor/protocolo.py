"""Protocolo de radio Davis Vantage Pro 2 (ISS) y parser de la salida de
rtldavis. Funciones puras: sin hilos ni IO, fáciles de testear.

Formato de un paquete (8 bytes, CRC-CCITT sobre los 6 primeros):

    byte0: [7:4] tipo de paquete   [3] batería baja   [2:0] ID transmisor
    byte1: velocidad del viento (mph)
    byte2: dirección del viento
    byte3, byte4: dato propio del tipo
    byte5: depende del tipo
    byte6, byte7: CRC

Referencias usadas para las fórmulas: DeKay "DavisRFM69" (protocolo
documentado por ingeniería inversa) y el driver meteostick de weewx.

Correcciones respecto a v2.3 (ver reporte_davis.md):
  * ID = byte0 & 0x07 y batería = bit 3. Antes era (byte0>>1)&7 y bit 0:
    con batería baja (byte0 = 0x?8) el ID calculado pasaba a 4 y se
    descartaban TODAS las tramas de la estación.
  * Tipo 0xE es el CONTADOR DE LLUVIA (vuelcos de la cubeta, 7 bits,
    rueda en 128), no humedad de hoja. Por eso salía siempre 0x80.
  * Tipo 0x5 codifica el TIEMPO ENTRE VUELCOS, no mm/h directo.
  * Sentinelas de "sin dato" (temp 0xFFC, UV/solar 0x3FF) y zona muerta
    de la veleta VP2 (9 + b2*342/255).
"""
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, Optional

MPH_A_KMH = 1.609344
# El anemómetro Davis está especificado hasta ~173 mph; por encima de 150 mph
# (241 km/h) el valor es una trama corrupta que pasó el CRC, no viento real.
VIENTO_MAX_MPH = 150


class TipoPaquete(IntEnum):
    SUPERCAP = 0x2
    UV_INDEX = 0x4
    RAIN_RATE = 0x5
    SOLAR_RAD = 0x6
    SOLAR_CELL = 0x7
    TEMPERATURE = 0x8
    VIENTO_RAFAGA = 0x9
    HUMIDITY = 0xA
    RAIN_COUNTER = 0xE
    LEAF_SOIL = 0xF


@dataclass
class Lectura:
    """Resultado de decodificar una trama. `valores` solo contiene lo que
    ESTA trama midió de verdad (nada heredado de tramas anteriores)."""
    tipo: str
    tx_id: int
    bateria_baja: bool
    trama_hex: str
    valores: Dict[str, Optional[float]] = field(default_factory=dict)


def _tabla_crc():
    tabla = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        tabla.append(crc & 0xFFFF)
    return tuple(tabla)


_TABLA_CRC = _tabla_crc()


def crc_ccitt(datos: bytes) -> int:
    crc = 0
    for b in datos:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLA_CRC[((crc >> 8) ^ b) & 0xFF]
    return crc


def validar_crc(datos: bytes) -> bool:
    return len(datos) >= 8 and crc_ccitt(datos[:6]) == ((datos[6] << 8) | datos[7])


_CARDINALES = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
               "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW")


def grados_a_cardinal(grados: Optional[float]) -> str:
    if grados is None:
        return "--"
    return _CARDINALES[int((grados % 360) / 22.5 + 0.5) % 16]


def _valor10(b3: int, b4: int) -> int:
    return ((b3 << 2) | (b4 >> 6)) & 0x3FF


def decodificar(datos: bytes, cubeta_mm: float = 0.2) -> Optional[Lectura]:
    """Decodifica 8 bytes con CRC ya validado. Devuelve None si no es una
    trama ISS reconocible."""
    if len(datos) < 8:
        return None
    # 8 bytes en cero tienen CRC 0 → "válidos" para el CRC, pero son ruido
    # (4 casos en el histórico de agosto-septiembre).
    if not any(datos[:8]):
        return None
    b0, b1, b2, b3, b4, b5 = datos[:6]
    tipo_num = b0 >> 4
    try:
        tipo = TipoPaquete(tipo_num)
        nombre = tipo.name
    except ValueError:
        # Tipos que un ISS no emite (0x0, 0x1, 0x3, 0xB, 0xC, 0xD): basura que
        # pasó el CRC. En el histórico traían "viento" de 103-175 km/h.
        return None

    viento_ok = b1 <= VIENTO_MAX_MPH
    v: Dict[str, Optional[float]] = {
        "viento_vel": round(b1 * MPH_A_KMH, 1) if viento_ok else None,
        # VP2: la veleta tiene una zona muerta en el norte (351°-9°). 0 y 255
        # son los extremos de esa zona (en esta estación ambos son frecuentes:
        # 5844 y 4307 tramas), así que 0 es un norte válido, no "sin dato".
        "viento_dir": round(9 + b2 * 342.0 / 255.0) % 360 if viento_ok else None,
    }

    if tipo == TipoPaquete.TEMPERATURE:
        if ((b3 << 4) | (b4 >> 4)) != 0xFFC:
            raw = (b3 << 8) | b4
            if raw >= 0x8000:
                raw -= 0x10000
            v["temp"] = round(((raw / 160.0) - 32.0) * 5.0 / 9.0, 2)
    elif tipo == TipoPaquete.HUMIDITY:
        hum = (((b4 >> 4) << 8) | b3) / 10.0
        if 0 < hum <= 100.5:
            v["humedad"] = min(hum, 100.0)
    elif tipo == TipoPaquete.RAIN_RATE:
        if b3 == 0xFF:
            v["lluvia_tasa"] = 0.0
        else:
            entre_vuelcos = ((b4 & 0x30) << 4) | b3
            if entre_vuelcos:
                seg = entre_vuelcos / 16.0 if (b4 & 0x40) == 0 else float(entre_vuelcos)
                v["lluvia_tasa"] = round(3600.0 / seg * cubeta_mm, 2)
    elif tipo == TipoPaquete.RAIN_COUNTER:
        v["lluvia_contador"] = float(b3 & 0x7F)
    elif tipo == TipoPaquete.VIENTO_RAFAGA:
        if b3 <= VIENTO_MAX_MPH:
            v["rafaga"] = round(b3 * MPH_A_KMH, 1)
    elif tipo == TipoPaquete.UV_INDEX:
        raw = _valor10(b3, b4)
        if raw != 0x3FF:
            v["uv"] = round(raw / 50.0, 2)
    elif tipo == TipoPaquete.SOLAR_RAD:
        raw = _valor10(b3, b4)
        if raw < 0x3FE:
            v["solar"] = round(raw * 1.757936, 1)
    elif tipo == TipoPaquete.SUPERCAP:
        v["supercap_v"] = round(_valor10(b3, b4) / 100.0, 2)
    elif tipo == TipoPaquete.SOLAR_CELL:
        v["celda_solar_v"] = round(_valor10(b3, b4) / 300.0, 2)

    return Lectura(nombre, b0 & 0x07, bool(b0 & 0x08), datos[:8].hex().upper(), v)


# ---------------------------------------------------------------- rtldavis
# Ejemplos reales (lheijst/rtldavis 0.15):
#   12:46:04.374871 8005813339007F03 5 0 0 0 0 msg.ID=0
#   12:46:04.374900 Hop: {ChannelIdx:47 ChannelFreq:926001611 FreqError:0 Transmitter:0}
#   12:44:47.882901 Init channels: wait max 135 seconds for a message of each transmitter
#   12:45:54.125069 TRANSMITTER 0 SEEN
REGEX_PAQUETE = re.compile(r'^\d{2}:\d{2}:\d{2}\.\d+\s+([0-9A-Fa-f]{16})\b')
REGEX_HOP = re.compile(r'Hop: \{ChannelIdx:(\d+) ChannelFreq:(\d+) FreqError:(-?\d+)')


def clasificar_linea(linea: str):
    """Devuelve (clase, datos):
       ('paquete', bytes) | ('hop', (canal, freq_hz, error_hz)) |
       ('init', None) | ('sync', None) | ('usb_error', linea) | ('otro', linea)"""
    m = REGEX_PAQUETE.match(linea)
    if m:
        return "paquete", bytes.fromhex(m.group(1))
    m = REGEX_HOP.search(linea)
    if m:
        return "hop", (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if "Init channels" in linea:
        return "init", None
    if "SEEN" in linea and "TRANSMITTER" in linea:
        return "sync", None
    low = linea.lower()
    # Fallas de USB vistas en esta Pi: al desconectarse el dongle rtldavis
    # imprime "cb transfer status: 1, canceling..." y se queda COLGADO; al
    # arrancar sin dongle imprime "input/output error" y sale con código 1.
    if any(t in low for t in ("usb_claim_interface", "no supported devices", "failed to open",
                               "cb transfer status", "input/output error", "no device")):
        return "usb_error", linea
    return "otro", linea


# ---------------------------------------------------------------- derivados
def punto_rocio(temp_c: Optional[float], hum: Optional[float]) -> Optional[float]:
    """Magnus-Tetens (Alduchov & Eskridge). Válido -40..50 °C."""
    if temp_c is None or not hum:
        return None
    import math
    a, b = 17.625, 243.04
    g = math.log(hum / 100.0) + a * temp_c / (b + temp_c)
    return round(b * g / (a - g), 1)


def vpd_kpa(temp_c: Optional[float], hum: Optional[float]) -> Optional[float]:
    """Déficit de presión de vapor (kPa): indicador clave en invernadero
    (transpiración / riesgo de hongos). Tetens."""
    if temp_c is None or hum is None:
        return None
    import math
    es = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
    return round(es * (1 - hum / 100.0), 3)
