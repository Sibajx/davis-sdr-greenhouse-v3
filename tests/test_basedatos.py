import os
import sqlite3
import tempfile
import time
import unittest

import tests.base  # noqa: F401
from davis_monitor.basedatos import EscritorSQLite, calidad, fila_evento, lluvia_acumulada_desde
from davis_monitor.protocolo import decodificar


class TestSQLite(unittest.TestCase):
    def test_lote_y_calidad(self):
        ruta = os.path.join(tempfile.mkdtemp(), "x.db")
        w = EscritorSQLite(ruta, intervalo_commit=60)
        lec = decodificar(bytes.fromhex("8005813339007F03"))
        t0 = time.time() - 1000
        for i in range(100):
            w.evento(fila_evento(t0 + i * 2.5625, lec, 0.0))
        w.cerrar()  # debe vaciar la cola aunque no haya pasado el intervalo
        self.assertEqual(sqlite3.connect(ruta).execute("SELECT COUNT(*) FROM eventos").fetchone()[0], 100)
        self.assertGreater(calidad(ruta, 1)["eficiencia_pct"], 99)

    def test_lluvia_acumulada(self):
        ruta = os.path.join(tempfile.mkdtemp(), "x.db")
        w = EscritorSQLite(ruta)
        lec = decodificar(bytes.fromhex("E0029C8001004E7D"))
        w.evento(fila_evento(time.time(), lec, 0.4))
        w.evento(fila_evento(time.time() - 3 * 86400, lec, 9.0))
        w.cerrar()
        self.assertAlmostEqual(lluvia_acumulada_desde(ruta, time.time() - 3600), 0.4)


if __name__ == "__main__":
    unittest.main()


class TestMigrar(unittest.TestCase):
    def test_migra_base_v23(self):
        from davis_monitor.migrar import migrar
        from tests.base import trama
        d = tempfile.mkdtemp()
        vieja = os.path.join(d, "v23.db")
        c = sqlite3.connect(vieja)
        c.execute("CREATE TABLE eventos (id INTEGER PRIMARY KEY, timestamp TEXT, trama_hex TEXT)")
        filas = [("2026-08-26 10:00:00.000", "8005813339007F03"),
                 ("2026-08-26 10:00:02.562", trama(0xE0, 0, 0, 0x80 | 3, 1).hex().upper()),
                 ("2026-08-26 10:00:05.125", trama(0xE0, 0, 0, 0x80 | 5, 1).hex().upper()),
                 ("2026-08-26 10:01:05.125", "8005813339007F04")]  # CRC malo
        c.executemany("INSERT INTO eventos (timestamp, trama_hex) VALUES (?,?)", filas)
        c.commit(); c.close()
        st = migrar(vieja, os.path.join(d, "v3.db"))
        self.assertEqual((st["migradas"], st["crc_invalido"]), (3, 1))
        self.assertAlmostEqual(st["lluvia_mm"], 0.4)
