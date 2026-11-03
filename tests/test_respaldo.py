import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

import tests.base  # noqa: F401
from davis_monitor import config, respaldo as R
from davis_monitor.basedatos import EscritorSQLite, fila_evento
from davis_monitor.protocolo import decodificar


class TestRespaldo(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.p = mock.patch.object(R, "DIR_RESPALDOS", os.path.join(self.base, "respaldos"))
        self.p.start()
        config.RUTAS.crear()
        for f in (config.RUTAS.db, config.RUTAS.db_migrada):
            for suf in ("", "-wal", "-shm"):
                if os.path.exists(f + suf):
                    os.remove(f + suf)

    def tearDown(self):
        self.p.stop()

    def test_respaldo_en_caliente_y_restauracion(self):
        w = EscritorSQLite(config.RUTAS.db, intervalo_commit=0.05)
        lec = decodificar(bytes.fromhex("8005813339007F03"))
        for i in range(500):
            w.evento(fila_evento(time.time() + i, lec, 0.0))
        time.sleep(1.5)
        sigue = threading.Event()

        def escribir():   # el receptor sigue escribiendo mientras se respalda
            i = 0
            while not sigue.is_set():
                w.evento(fila_evento(time.time() + 1000 + i, lec, 0.0)); i += 1; time.sleep(0.002)
        t = threading.Thread(target=escribir); t.start()
        try:
            man = R.respaldar()
        finally:
            sigue.set(); t.join(); w.cerrar()
        self.assertEqual(man["archivos"]["eventos"]["integridad"], "ok")
        self.assertGreaterEqual(man["archivos"]["eventos"]["tablas"]["eventos"], 500)
        self.assertEqual(R.verificar(), [])
        # restaurar: descomprimir y abrir
        snap = R.instantaneas()[-1]
        destino = os.path.join(self.base, "restaurada.db")
        with gzip.open(os.path.join(R.DIR_RESPALDOS, snap, "eventos_davis.db.gz")) as fi, open(destino, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        n = sqlite3.connect(destino).execute("SELECT COUNT(*) FROM eventos").fetchone()[0]
        self.assertEqual(n, man["archivos"]["eventos"]["tablas"]["eventos"])

    def test_detecta_archivo_danado(self):
        EscritorSQLite(config.RUTAS.db).cerrar()
        R.respaldar()
        snap = R.instantaneas()[-1]
        ruta = os.path.join(R.DIR_RESPALDOS, snap, "eventos_davis.db.gz")
        with open(ruta, "r+b") as f:
            f.seek(30); f.write(b"\x00\xff\x00\xff")
        self.assertTrue(any("SHA-256" in p for p in R.verificar()))

    def test_retencion(self):
        os.makedirs(R.DIR_RESPALDOS)
        hoy = datetime(2026, 11, 3, 3, 0)
        for d in range(120):   # 120 días de respaldos diarios
            n = (hoy - timedelta(days=d)).strftime(R.FMT)
            os.makedirs(os.path.join(R.DIR_RESPALDOS, n))
            open(os.path.join(R.DIR_RESPALDOS, n, "manifiesto.json"), "w").write("{}")
        R.podar()
        quedan = R.instantaneas()
        self.assertLessEqual(len(quedan), R.RETENER_DIARIOS + R.RETENER_SEMANAS + R.RETENER_MESES)
        self.assertIn(hoy.strftime(R.FMT), quedan)                              # el más reciente
        self.assertIn((hoy - timedelta(days=6)).strftime(R.FMT), quedan)        # 7 diarios
        self.assertTrue(any(q.startswith("2026-07") for q in quedan))           # mensual antiguo
        self.assertGreaterEqual(len(quedan), 10)


if __name__ == "__main__":
    unittest.main()
