"""Interfaz de texto (curses). Solo dibuja: la lógica vive en NucleoMonitoreo.tick()."""
import curses
import time
from datetime import datetime

from . import config, protocolo
from .nucleo import NucleoMonitoreo

_BLOQUES = "▁▂▃▄▅▆▇█"


def sparkline(datos, ancho=20) -> str:
    datos = list(datos)[-ancho:]
    if not datos:
        return "─" * ancho
    lo, hi = min(datos), max(datos)
    if hi == lo:
        return _BLOQUES[0] * len(datos)
    return "".join(_BLOQUES[int((v - lo) / (hi - lo) * 7)] for v in datos)


def f(v, dec=1, ancho=6) -> str:
    return f"{'--':>{ancho}}" if v is None else f"{v:>{ancho}.{dec}f}"


class MotorInterfazTUI:
    def __init__(self, nucleo: NucleoMonitoreo):
        self.core = nucleo

    @staticmethod
    def put(scr, y, x, txt, attr=0):
        try:
            scr.addstr(y, x, txt, attr)
        except curses.error:
            pass

    def caja(self, scr, y, x, alto, ancho, titulo):
        c = curses.color_pair(1)
        self.put(scr, y, x, "╭" + "─" * (ancho - 2) + "╮", c)
        for i in range(1, alto - 1):
            self.put(scr, y + i, x, "│", c)
            self.put(scr, y + i, x + ancho - 1, "│", c)
        self.put(scr, y + alto - 1, x, "╰" + "─" * (ancho - 2) + "╯", c)
        self.put(scr, y, x + 3, f" {titulo} ", curses.color_pair(5) | curses.A_BOLD)

    def ejecutar(self, scr) -> None:
        curses.curs_set(0)
        scr.nodelay(True)
        curses.start_color()
        curses.use_default_colors()
        for i, col in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_RED,
                                 curses.COLOR_YELLOW, curses.COLOR_MAGENTA, curses.COLOR_WHITE), 1):
            curses.init_pair(i, col, -1)
        B = curses.A_BOLD
        self.core.iniciar_lector()
        ultimo_tick, alerta = 0.0, None

        while not self.core.shutdown_event.is_set():
            ahora = time.time()
            if ahora - ultimo_tick >= 1.0:
                alerta = self.core.tick(ahora)
                ultimo_tick = ahora
            if scr.getch() in (ord('q'), ord('Q')):
                break
            e, s, fl_t, fl_v, h_t, h_v = self.core.obtener_instantanea()
            v = e.valores
            scr.erase()
            alto, ancho = scr.getmaxyx()
            if alto < 24 or ancho < 90:
                self.put(scr, 1, 1, "Terminal pequeña (mín. 90x24). 'q' para salir.", curses.color_pair(3))
                scr.refresh()
                time.sleep(0.3)
                continue
            w = (ancho - 4) // 2
            xr = w + 3
            up = int(ahora - self.core.hora_inicio)
            titulo = (f" 📡 ESTACIÓN DAVIS VP2 │ {datetime.now():%Y-%m-%d %H:%M:%S} │ "
                      f"UPTIME {up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d} ")
            self.put(scr, 0, max(0, (ancho - len(titulo)) // 2), titulo,
                     curses.color_pair(4) | B | curses.A_REVERSE)

            self.caja(scr, 2, 1, 8, w, "VIENTO")
            self.put(scr, 3, 3, f"Velocidad : {f(v['viento_vel'])} km/h {fl_v}", curses.color_pair(2) | B)
            self.put(scr, 4, 3, f"Dirección : {f(v['viento_dir'], 0)} °  {protocolo.grados_a_cardinal(v['viento_dir'])}")
            self.put(scr, 5, 3, f"Ráfaga    : {f(v['rafaga'])} km/h", curses.color_pair(3) | B)
            self.put(scr, 6, 3, f"Tendencia : {sparkline(h_v, min(30, w - 18))}", curses.color_pair(4))

            self.caja(scr, 2, xr, 8, w, "TERMOHIGROMETRÍA")
            t, hu = v["temp"], v["humedad"]
            self.put(scr, 3, xr + 2, f"Temperatura : {f(t)} °C {fl_t}", curses.color_pair(2) | B)
            self.put(scr, 4, xr + 2, f"Humedad     : {f(hu)} %", curses.color_pair(1) | B)
            self.put(scr, 5, xr + 2, f"Punto rocío : {f(protocolo.punto_rocio(t, hu))} °C   "
                                     f"VPD {f(protocolo.vpd_kpa(t, hu), 2, 5)} kPa")
            self.put(scr, 6, xr + 2, f"Tendencia   : {sparkline(h_t, min(30, w - 18))}", curses.color_pair(4))
            if alerta:
                self.put(scr, 7, xr + 2, f"⚠ ALERTA: {alerta}", curses.color_pair(3) | B)

            self.caja(scr, 10, 1, 6, w, "LLUVIA")
            if not config.PLUVIOMETRO_ACTIVO:
                self.put(scr, 11, 3, "Pluviómetro FUERA DE SERVICIO (config)", curses.color_pair(3) | B)
            self.put(scr, 12 if not config.PLUVIOMETRO_ACTIVO else 11, 3,
                     f"Hoy        : {f(e.lluvia_hoy_mm, 1)} mm" if config.PLUVIOMETRO_ACTIVO else "",
                     curses.color_pair(1) | B)
            if config.PLUVIOMETRO_ACTIVO:
                self.put(scr, 12, 3, f"Tasa       : {f(v['lluvia_tasa'], 1)} mm/h")
            self.put(scr, 13, 3, f"Contador   : {'--' if e.lluvia_contador is None else e.lluvia_contador}"
                                 f"  (cubeta {config.TAMANO_CUBETA_MM} mm)")

            self.caja(scr, 10, xr, 6, w, "RADIACIÓN")
            self.put(scr, 11, xr + 2, f"Índice UV : {f(v['uv'])}", curses.color_pair(5) | B)
            self.put(scr, 12, xr + 2, f"Solar     : {f(v['solar'])} W/m²", curses.color_pair(4) | B)
            bat = "--" if e.bateria_baja is None else ("BAJA" if e.bateria_baja else "OK")
            self.put(scr, 13, xr + 2, f"Batería TX: {bat}",
                     curses.color_pair(3 if e.bateria_baja else 2) | B)

            pw = w * 2 + 2
            self.caja(scr, 16, 1, 7, pw, "ENLACE RF (SDR)")
            freq = "--" if s.freq_mhz is None else f"{s.freq_mhz:.3f}"
            self.put(scr, 17, 3, f"Estado: {s.estado} │ {freq} MHz │ canal {s.canal if s.canal is not None else '-'}"
                                 f" │ última sync {s.ultima_sync_seg if s.ultima_sync_seg is not None else '--'} s",
                     curses.color_pair(1) | B)
            self.put(scr, 18, 3, f"Válidas {s.paquetes} │ CRC fail {s.descartes_crc} │ Otro TX {s.descartes_id}"
                                 f" │ Reinicios {s.reinicios} (último {s.ultimo_reinicio})")
            self.put(scr, 19, 3, f"{s.estado_csv}", curses.color_pair(2))
            self.put(scr, 20, 3, f"Última trama: {s.ultima_trama}", curses.A_DIM)
            self.put(scr, 22, 3, "q = salir", curses.color_pair(3) | B)
            scr.refresh()
            time.sleep(0.25)
