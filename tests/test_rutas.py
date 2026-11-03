import gzip
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import tests.base  # noqa: F401
from davis_monitor import config
from davis_monitor.rutas import Rutas
from davis_monitor.almacenamiento import GestorCSV, ENCABEZADO


class TestRutas(unittest.TestCase):
    def test_estructura(self):
        r = Rutas(tempfile.mkdtemp()).crear()
        for sub in ("csv", "db", "logs", "estado", "exportaciones"):
            self.assertTrue(os.path.isdir(os.path.join(r.base, sub)), sub)
        self.assertEqual(r.csv_del_dia(datetime(2026, 11, 3)),
                         os.path.join(r.base, "csv", "2026", "11", "historial_davis_2026-11-03.csv"))
        self.assertEqual(r.exportacion("x.csv"), os.path.join(r.base, "exportaciones", "x.csv"))
        self.assertEqual(r.exportacion("/tmp/y.csv"), "/tmp/y.csv")

    def test_por_defecto_dentro_del_proyecto(self):
        self.assertEqual(Rutas(os.path.join(config.DIR_PROYECTO, "datos")).db,
                         os.path.join(config.DIR_PROYECTO, "datos", "db", "eventos_davis.db"))

    def test_csv_mensual_y_compresion(self):
        r = Rutas(tempfile.mkdtemp()).crear()
        g = GestorCSV(r)
        viejo, hoy = datetime.now() - timedelta(days=40), datetime.now()
        g.escribir(viejo, ["1"] * (len(ENCABEZADO) - 1))
        g.escribir(hoy, ["2"] * (len(ENCABEZADO) - 1))
        self.assertTrue(os.path.isfile(r.csv_del_dia(viejo)))
        hechos = g.comprimir_antiguos(7)
        self.assertEqual(hechos, [r.csv_del_dia(viejo) + ".gz"])
        self.assertFalse(os.path.exists(r.csv_del_dia(viejo)))
        self.assertTrue(os.path.isfile(r.csv_del_dia(hoy)))  # el de hoy nunca se toca
        with gzip.open(hechos[0], "rt") as f:
            self.assertEqual(f.readline().strip(), ",".join(ENCABEZADO))


if __name__ == "__main__":
    unittest.main()
