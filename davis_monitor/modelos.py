"""Estado compartido y agregación por ventana. Valores numéricos reales
(float/None), no strings: el formateo se hace solo al mostrar/guardar."""
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CAMPOS = ("temp", "humedad", "viento_vel", "viento_dir", "rafaga", "lluvia_tasa",
          "uv", "solar", "supercap_v", "celda_solar_v")


class EstadoEstacion:
    """Último valor de cada campo + cuándo se recibió (para detectar
    sensores mudos en vez de mostrar un valor viejo como si fuera actual)."""

    def __init__(self):
        self.valores: Dict[str, Optional[float]] = {c: None for c in CAMPOS}
        self.ts: Dict[str, float] = {}
        self.bateria_baja: Optional[bool] = None
        self.tx_id: Optional[int] = None
        self.lluvia_contador: Optional[int] = None
        self.lluvia_hoy_mm: float = 0.0
        self.ultimo_paquete: float = 0.0

    def actualizar(self, valores: Dict[str, Optional[float]], ahora: float) -> None:
        for k, v in valores.items():
            if k in self.valores and v is not None:
                self.valores[k] = v
                self.ts[k] = ahora

    def valor(self, campo: str, ahora: float = None, max_edad: float = None) -> Optional[float]:
        v = self.valores.get(campo)
        if v is None or max_edad is None:
            return v
        ahora = time.time() if ahora is None else ahora
        return v if (ahora - self.ts.get(campo, 0.0)) <= max_edad else None

    def edad(self, campo: str, ahora: float) -> Optional[float]:
        t = self.ts.get(campo)
        return None if t is None else round(ahora - t, 1)

    def clonar(self) -> "EstadoEstacion":
        c = EstadoEstacion()
        c.valores = dict(self.valores)
        c.ts = dict(self.ts)
        c.bateria_baja, c.tx_id = self.bateria_baja, self.tx_id
        c.lluvia_contador, c.lluvia_hoy_mm = self.lluvia_contador, self.lluvia_hoy_mm
        c.ultimo_paquete = self.ultimo_paquete
        return c


@dataclass
class EstadoSDR:
    estado: str = "Iniciando"          # Iniciando / Sincronizando / Recibiendo / Reiniciando / ERROR
    freq_mhz: Optional[float] = None
    canal: Optional[int] = None
    error_freq_hz: Optional[int] = None
    paquetes: int = 0
    # Contabilidad por RANURA (la estación transmite con periodo exacto):
    #   válidas + dañadas + sin_rastro = transmisiones esperadas (100 %)
    # y cada CRC fallido es: con_copia (ranura que sí llegó) | en ranura perdida (→ dañada) | ruido.
    descartes_crc: int = 0             # todas las tramas con CRC inválido
    crc_ruido: int = 0                 # fuera de toda ranura: falsas detecciones del demodulador (descarte correcto)
    crc_con_copia: int = 0             # en la ranura de una transmisión que SÍ llegó válida
    danadas: int = 0                   # ranuras sin trama válida pero con CRC fallido: llegó corrupta
    sin_rastro: int = 0                # ranuras sin nada (señal débil, interferencia, resincronización)
    perdidas: int = 0                  # "packet missed" que reporta rtldavis (diagnóstico de saltos)
    invalidas: int = 0                 # CRC correcto pero contenido imposible
    descartes_id: int = 0
    crc_disponible: bool = False       # rtldavis con el parche que reporta los CRC
    version_rtldavis: str = ""
    lineas_otras: int = 0
    reinicios: int = 0
    ultimo_reinicio: str = "Nunca"
    inicio_proceso: float = field(default_factory=time.time)
    ultima_linea: float = field(default_factory=time.time)
    ultima_sync_seg: Optional[float] = None   # cuánto tardó la última sincronización
    ultima_trama: str = ""
    estado_csv: str = "Esperando..."

    def clonar(self) -> "EstadoSDR":
        return EstadoSDR(**self.__dict__)


class AnalizadorTendencias:
    def __init__(self, maxlen: int = 10, umbral: float = 0.1):
        self._h = deque(maxlen=maxlen)
        self._umbral = umbral

    def registrar(self, valor: Optional[float]) -> str:
        if valor is None:
            return "─"
        if not self._h:
            self._h.append(valor)
            return "─"
        prom = sum(self._h) / len(self._h)
        self._h.append(valor)
        if valor > prom + self._umbral:
            return "▲"
        if valor < prom - self._umbral:
            return "▼"
        return "─"


class _Stat:
    __slots__ = ("n", "suma", "min", "max")

    def __init__(self):
        self.n, self.suma, self.min, self.max = 0, 0.0, math.inf, -math.inf

    def add(self, x: float) -> None:
        self.n += 1
        self.suma += x
        self.min = min(self.min, x)
        self.max = max(self.max, x)

    @property
    def prom(self) -> Optional[float]:
        return self.suma / self.n if self.n else None


class AgregadorVentana:
    """Estadística de la ventana entre dos filas del CSV (por defecto 60 s).
    v2.3 solo guardaba el último valor + min/max de temp y viento: aquí se
    guarda promedio real, extremos, dirección vectorial y lluvia caída."""

    CAMPOS_STAT = ("temp", "humedad", "viento_vel", "rafaga", "lluvia_tasa", "uv", "solar")

    def __init__(self):
        self.reiniciar()

    def reiniciar(self, ahora: float = None) -> None:
        self.inicio = time.time() if ahora is None else ahora
        self.stats = {c: _Stat() for c in self.CAMPOS_STAT}
        self._u = 0.0
        self._v = 0.0
        self._n_dir = 0
        self.lluvia_mm = 0.0
        self.paquetes = 0

    def registrar(self, valores: Dict[str, Optional[float]], lluvia_mm: float = 0.0) -> None:
        self.paquetes += 1
        self.lluvia_mm += lluvia_mm
        for k in self.CAMPOS_STAT:
            x = valores.get(k)
            if x is not None:
                self.stats[k].add(x)
        d, vel = valores.get("viento_dir"), valores.get("viento_vel")
        if d is not None and vel:  # con calma la veleta no significa nada
            r = math.radians(d)
            self._u += vel * math.sin(r)
            self._v += vel * math.cos(r)
            self._n_dir += 1

    def direccion_promedio(self) -> Optional[float]:
        if not self._n_dir or (abs(self._u) < 1e-9 and abs(self._v) < 1e-9):
            return None
        return round(math.degrees(math.atan2(self._u, self._v)) % 360)

    def resumen(self) -> Dict[str, Optional[float]]:
        out: Dict[str, Optional[float]] = {}
        for k, s in self.stats.items():
            out[f"{k}_prom"] = s.prom
            out[f"{k}_min"] = s.min if s.n else None
            out[f"{k}_max"] = s.max if s.n else None
        out["viento_dir_prom"] = self.direccion_promedio()
        out["lluvia_mm"] = round(self.lluvia_mm, 3)
        out["paquetes"] = self.paquetes
        return out


def historial(maxlen: int = 240) -> deque:
    return deque(maxlen=maxlen)


def ultimos(d: deque, n: int) -> List[float]:
    return list(d)[-n:]
