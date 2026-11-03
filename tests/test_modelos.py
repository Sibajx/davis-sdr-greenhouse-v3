import unittest
import tests.base  # noqa: F401
from davis_monitor.modelos import AgregadorVentana, EstadoEstacion, AnalizadorTendencias


class TestAgregador(unittest.TestCase):
    def test_promedios(self):
        a = AgregadorVentana()
        for t in (20.0, 22.0, 24.0):
            a.registrar({"temp": t})
        r = a.resumen()
        self.assertEqual((r["temp_prom"], r["temp_min"], r["temp_max"], r["paquetes"]), (22.0, 20.0, 24.0, 3))
        self.assertIsNone(r["humedad_prom"])

    def test_direccion_vectorial_cruza_norte(self):
        a = AgregadorVentana()
        a.registrar({"viento_vel": 10, "viento_dir": 350})
        a.registrar({"viento_vel": 10, "viento_dir": 10})
        self.assertIn(a.direccion_promedio(), (0, 360))  # promedio aritmético daría 180 (S): error

    def test_calma_sin_direccion(self):
        a = AgregadorVentana()
        a.registrar({"viento_vel": 0.0, "viento_dir": 90})
        self.assertIsNone(a.direccion_promedio())

    def test_lluvia(self):
        a = AgregadorVentana()
        a.registrar({}, 0.2); a.registrar({}, 0.4)
        self.assertAlmostEqual(a.resumen()["lluvia_mm"], 0.6)


class TestEstado(unittest.TestCase):
    def test_edad(self):
        e = EstadoEstacion()
        e.actualizar({"temp": 20.0, "uv": None}, 100.0)
        self.assertEqual(e.valor("temp", 200.0, 600), 20.0)
        self.assertIsNone(e.valor("temp", 1000.0, 600))
        self.assertIsNone(e.valores["uv"])
        c = e.clonar(); c.valores["temp"] = 0
        self.assertEqual(e.valores["temp"], 20.0)

    def test_tendencia(self):
        t = AnalizadorTendencias()
        self.assertEqual(t.registrar(10), "─")
        self.assertEqual(t.registrar(11), "▲")
        self.assertEqual(t.registrar(None), "─")


if __name__ == "__main__":
    unittest.main()
