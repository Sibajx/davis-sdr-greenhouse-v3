import tests.base  # noqa: F401  (aislar config antes de importar)
import os
import csv
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.base import trama
from davis_monitor import config
from davis_monitor.nucleo import NucleoMonitoreo
from davis_monitor.almacenamiento import ENCABEZADO


class Notif:
    def __init__(self):
        self.msgs = []

    def enviar(self, m, *a, **k):
        self.msgs.append(m)

    def cerrar(self, *a, **k):
        pass


def nuevo():
    d = tempfile.mkdtemp(prefix="davis_nucleo_")
    return NucleoMonitoreo(d, notificador=Notif()), d


class TestProcesamiento(unittest.TestCase):
    def test_trama_valida_y_crc(self):
        n, _ = nuevo()
        self.assertIsNotNone(n.procesar_linea("12:00:00.1 8005813339007F03 1 0 0 0 0 msg.ID=0"))
        self.assertIsNone(n.procesar_linea("12:00:00.1 8005813339007F04 1 0 0 0 0 msg.ID=0"))
        self.assertEqual((n.sdr.paquetes, n.sdr.descartes_crc), (1, 1))
        self.assertAlmostEqual(n.est.valores["temp"], 27.75)
        n.finalizar()

    def test_contabilidad_por_ranuras(self):
        """válidas + dañadas + sin rastro = esperadas; los CRC fuera de ranura son ruido."""
        n, _ = nuevo()
        P = config.periodo_tx_seg()
        V = "8005813339007F03 1 0 0 0 0 msg.ID=0"
        n.procesar_linea("x rtldavis.go VERSION=0.15+crc", 999.0)
        self.assertTrue(n.sdr.crc_disponible)
        n.procesar_linea("x CRC ERROR 0102 ok=0", 999.5)                # antes de sincronizar → ruido
        t0 = 1000.0
        n.procesar_linea("12:00:00.1 " + V, t0)                          # ranura 0: válida
        n.procesar_linea("x CRC ERROR 0102 ok=1", t0)                    # copia dañada en el mismo bloque
        n.procesar_linea("x CRC ERROR 0102 ok=0", t0 + P + 0.002)        # ranura 1: llegó dañada
        n.procesar_linea("x CRC ERROR 0102 ok=0", t0 + 1.9 * P)          # entre ranuras → ruido
        # ranura 2: nada (sin rastro)
        n.procesar_linea("12:00:07.8 " + V, t0 + 3 * P + 0.002)          # ranura 3: válida
        n.procesar_trama(bytes(8), t0 + 3 * P + 0.5)                     # inválida (CRC ok, ceros)
        s = n.sdr
        self.assertEqual((s.paquetes, s.danadas, s.sin_rastro), (2, 1, 1))
        self.assertEqual((s.descartes_crc, s.crc_con_copia, s.crc_ruido, s.invalidas), (4, 1, 2, 1))
        n._cerrar_enlace(t0 + 60)
        e = n.enlace_ultima
        self.assertEqual(e["esperadas"], e["validas"] + e["danadas"] + e["sin_rastro"])
        self.assertEqual(e["esperadas"], 4)   # 2 válidas + 1 dañada + 1 sin rastro
        n._cerrar_enlace(t0 + 120)            # ventana siguiente: contadores en cero
        self.assertEqual(n.enlace_ultima["crc_fallos"], 0)
        n.finalizar()

    def test_otro_transmisor(self):
        n, _ = nuevo()
        self.assertIsNone(n.procesar_trama(trama(0x83, 0, 0, 0x33, 0x39)))
        self.assertEqual(n.sdr.descartes_id, 1)
        n.finalizar()

    def test_lluvia_con_vuelta_del_contador(self):
        n, _ = nuevo()
        for c in (125, 126, 127, 0, 2):     # 1+1+1+2 = 5 vuelcos
            n.procesar_trama(trama(0xE0, 0, 0, 0x80 | c, 0x01))
        self.assertAlmostEqual(n.est.lluvia_hoy_mm, 5 * config.TAMANO_CUBETA_MM)
        n.procesar_trama(trama(0xE0, 0, 0, 0x80 | 60, 0x01))  # salto absurdo: ignorado
        self.assertAlmostEqual(n.est.lluvia_hoy_mm, 5 * config.TAMANO_CUBETA_MM)
        n.finalizar()

    def test_lluvia_persiste_tras_reinicio(self):
        n, d = nuevo()
        for c in (10, 11, 13):
            n.procesar_trama(trama(0xE0, 0, 0, 0x80 | c, 0x01))
        n.finalizar()
        n2 = NucleoMonitoreo(d, notificador=Notif())
        self.assertAlmostEqual(n2.est.lluvia_hoy_mm, 3 * config.TAMANO_CUBETA_MM)
        n2.finalizar()

    def test_pluviometro_fuera_de_servicio(self):
        n, d = nuevo()
        t0 = time.time()
        with mock.patch.object(config, "PLUVIOMETRO_ACTIVO", False):
            for c in (1, 2, 3):
                n.procesar_trama(trama(0xE0, 0, 0, 0x80 | c, 0x01), t0)
            n.procesar_trama(trama(0x50, 0, 0, 0xFF, 0x71), t0)
            n.procesar_trama(trama(0x80, 0, 0, 0x33, 0x39), t0)
            n.procesar_trama(trama(0xA0, 0, 0, 0xB8, 0x1B), t0)
            n._guardar_ventana(t0 + 60)
        n.finalizar()
        self.assertEqual(n.est.lluvia_hoy_mm, 0.0)
        self.assertIsNone(n.est.valores["lluvia_tasa"])
        c = sqlite3.connect(n.rutas.db)
        self.assertIsNone(c.execute("SELECT lluvia_mm FROM resumen").fetchone()[0])   # NULL, no 0
        self.assertEqual(c.execute("SELECT COUNT(*) FROM eventos").fetchone()[0], 6)   # tramas crudas sí

    def test_alerta_bateria(self):
        n, _ = nuevo()
        n.procesar_trama(trama(0x80, 0, 0, 0x33, 0x39))
        n.procesar_trama(trama(0x88, 0, 0, 0x33, 0x39))
        self.assertTrue(n.est.bateria_baja)
        self.assertTrue(any("Batería baja" in m for m in n.notif.msgs))
        n.finalizar()


class TestTick(unittest.TestCase):
    def test_csv_sqlite_estado(self):
        n, d = nuevo()
        t0 = time.time()
        for _ in range(3):
            n.procesar_trama(trama(0x80, 5, 0x80, 0x33, 0x39), t0)
            n.procesar_trama(trama(0xA0, 5, 0x80, 0xB8, 0x1B), t0)
        n._guardar_ventana(t0 + 60)
        n.escribir_estado_json(t0 + 60)
        n.finalizar()
        from datetime import datetime
        from davis_monitor.rutas import Rutas
        r = Rutas(d)
        ruta_csv = r.csv_del_dia(datetime.fromtimestamp(t0 + 60))
        self.assertTrue(ruta_csv.startswith(os.path.join(d, "csv", f"{datetime.now():%Y}")))
        with open(ruta_csv) as f:
            filas = list(csv.reader(f))
        self.assertEqual(filas[0], ENCABEZADO)
        fila = dict(zip(ENCABEZADO, filas[1]))
        self.assertEqual(fila["Humedad_%"], "44.0")
        self.assertEqual(fila["Paquetes"], "6")
        self.assertEqual(len(filas[1]), len(ENCABEZADO))
        c = sqlite3.connect(r.db)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM eventos").fetchone()[0], 6)
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM resumen").fetchone()[0], 1)
        # La fila de humedad NO arrastra la temperatura de otra trama
        self.assertIsNone(c.execute("SELECT temp_c FROM eventos WHERE tipo_paquete='HUMIDITY'").fetchone()[0])
        with open(r.estado) as f:
            self.assertEqual(json.load(f)["estacion"]["humedad_pct"], 44.0)

    def test_helada_histeresis(self):
        n, _ = nuevo()
        t = time.time()
        n.est.actualizar({"temp": 1.5}, t)
        self.assertEqual(n.evaluar_alertas(t), "HELADA")
        n.est.actualizar({"temp": 2.5}, t)   # dentro de la histéresis: sigue activa
        self.assertEqual(n.evaluar_alertas(t), "HELADA")
        n.est.actualizar({"temp": 3.5}, t)
        self.assertIsNone(n.evaluar_alertas(t))
        self.assertEqual(sum("Advertencia de helada" in m for m in n.notif.msgs), 1)
        n.finalizar()

    def test_watchdog_fase_sincronizacion(self):
        n, _ = nuevo()
        n.proc = mock.Mock(); n.proc.poll.return_value = None
        n.sdr.inicio_proceso = 1000.0
        with mock.patch.object(n, "forzar_reinicio_sdr") as f:
            n.tick(1000.0 + 120)   # sincronizando 120 s: NO se toca (v2.3 lo mataba a los 30 s)
            f.assert_not_called()
            n.tick(1000.0 + config.WATCHDOG_SYNC_SEG + 1)
            f.assert_called_once()
        n.proc = None
        n.finalizar()

    def test_watchdog_fase_sincronizado(self):
        n, _ = nuevo()
        n.proc = mock.Mock(); n.proc.poll.return_value = None
        n.sdr.inicio_proceso = 1000.0
        n.procesar_trama(trama(0x80, 0, 0, 0x33, 0x39), 1100.0)   # sincronizó a los 100 s
        with mock.patch.object(n, "forzar_reinicio_sdr") as f:
            n.tick(1100.0 + 30)    # pérdidas normales: nada
            f.assert_not_called()
            n.tick(1100.0 + config.WATCHDOG_SIN_PAQUETES_SEG + 1)
            f.assert_called_once()
        n.proc = None
        n.finalizar()


class TestLectorReal(unittest.TestCase):
    """Lanza un rtldavis falso (script Python) para probar el ciclo de vida."""

    def _falso(self, cuerpo):
        d = tempfile.mkdtemp()
        ruta = os.path.join(d, "rtldavis_falso")
        with open(ruta, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys, time\n{cuerpo}\n")
        os.chmod(ruta, 0o755)
        return ruta

    def test_lee_y_reinicia(self):
        ruta = self._falso(
            "print('12:00:00.0 Init channels: wait max 135 seconds', flush=True)\n"
            "print('12:00:01.0 8005813339007F03 1 0 0 0 0 msg.ID=0', flush=True)\n"
            "print('12:00:01.0 Hop: {ChannelIdx:5 ChannelFreq:905000000 FreqError:3 Transmitter:0}', flush=True)\n")
        n, _ = nuevo()
        with mock.patch.object(config, "RTLDAVIS_CMD", ruta), mock.patch.object(config, "RTLDAVIS_ARGS", ""), \
                mock.patch.object(config, "BACKOFF_INICIAL_SEG", 0.1):
            n.iniciar_lector()
            fin = time.time() + 10
            while n.sdr.paquetes < 2 and time.time() < fin:   # 2 paquetes = se relanzó tras terminar
                time.sleep(0.05)
            n.finalizar()
        self.assertGreaterEqual(n.sdr.paquetes, 2)
        self.assertEqual(n.sdr.canal, 5)

    def test_usb_colgado_se_mata_al_instante(self):
        ruta = self._falso("print('12:00:00.0 cb transfer status: 1, canceling...', flush=True)\ntime.sleep(60)")
        n, _ = nuevo()
        with mock.patch.object(config, "RTLDAVIS_CMD", ruta), mock.patch.object(config, "RTLDAVIS_ARGS", ""), \
                mock.patch.object(config, "BACKOFF_INICIAL_SEG", 30):
            n.iniciar_lector()
            fin = time.time() + 8
            while n.errores_usb == 0 and time.time() < fin:
                time.sleep(0.05)
            time.sleep(0.5)
            self.assertIsNone(n.proc)   # ya fue terminado, sin esperar 150 s
            n.finalizar()
        self.assertEqual(n.sdr.estado, "ERROR USB")

    def test_binario_inexistente(self):
        n, _ = nuevo()
        with mock.patch.object(config, "RTLDAVIS_CMD", "/no/existe/rtldavis"):
            n.lector_rtldavis_worker()
        self.assertTrue(n.hilo_sdr_detenido.is_set())
        n.finalizar()

    def test_finalizar_mata_proceso_colgado(self):
        ruta = self._falso("import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)")
        n, _ = nuevo()
        with mock.patch.object(config, "RTLDAVIS_CMD", ruta), mock.patch.object(config, "RTLDAVIS_ARGS", ""):
            n.iniciar_lector()
            fin = time.time() + 5
            while n.proc is None and time.time() < fin:
                time.sleep(0.05)
            p = n.proc
            t = time.time(); n.finalizar()
        self.assertIsNotNone(p.poll())
        self.assertLess(time.time() - t, 12)


if __name__ == "__main__":
    unittest.main()
