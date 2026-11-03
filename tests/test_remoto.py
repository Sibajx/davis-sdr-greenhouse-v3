import json
import os
import time
import unittest

import tests.base  # noqa: F401
from davis_monitor import config
from davis_monitor.remoto import NucleoRemoto


class TestNucleoRemoto(unittest.TestCase):
    def test_lee_estado_del_servicio_sin_dongle(self):
        config.RUTAS.crear()
        with open(config.ARCHIVO_ESTADO_JSON, "w") as f:
            json.dump({"estacion": {"temp_c": 1.5, "humedad_pct": 80, "viento_kmh": 3.2, "viento_dir_grados": 90,
                                    "bateria_tx": "OK", "edad_seg": {"temp": 4}},
                       "sdr": {"estado": "Recibiendo", "paquetes_validos": 42, "segundos_sin_paquetes": 1}}, f)
        n = NucleoRemoto()
        self.assertEqual(n.tick(time.time()), "HELADA")        # 1.5 °C ≤ umbral
        e, s, *_ = n.obtener_instantanea()
        self.assertEqual((e.valores["temp"], e.valores["humedad"], s.paquetes, e.bateria_baja), (1.5, 80, 42, False))
        self.assertIsNone(n.iniciar_lector())                  # nunca abre rtldavis
        os.utime(config.ARCHIVO_ESTADO_JSON, (time.time() - 300, time.time() - 300))
        n._leer_estado(time.time())
        self.assertTrue(n.sdr.estado.startswith("FUERA DE LÍNEA"))


if __name__ == "__main__":
    unittest.main()
