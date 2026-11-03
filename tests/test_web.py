"""Tests del dashboard: autenticación, promediado Davis y API HTTP real."""
import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

import tests.base  # noqa: F401
from davis_monitor import config
from davis_monitor.basedatos import abrir, COLS_RESUMEN, _SQL_RESUMEN
from davis_web import ajustes, auth as A, consultas, promediado
from davis_web.servidor import crear_servidor

T0 = 1788112800.0   # 2026-08-30 12:00 local (múltiplo de 30 min)


def base_sintetica(n_min=180, hueco=None):
    """Crea datos/db/eventos_davis.db con n_min filas de resumen (1/min) terminando en T0."""
    config.RUTAS.crear()
    for p in (config.RUTAS.db, config.RUTAS.db_migrada):
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(p + suf):
                os.remove(p + suf)
    c = abrir(config.RUTAS.db)
    filas = []
    for i in range(n_min):
        ts = T0 - (n_min - 1 - i) * 60
        if hueco and hueco[0] <= ts < hueco[1]:
            continue
        v = {k: None for k in COLS_RESUMEN}
        v.update(timestamp="x", ts_unix=ts, segundos=60, temp_c=20 + i * 0.01, temp_min_c=19.9 + i * 0.01,
                 temp_max_c=20.1 + i * 0.01, humedad_pct=50.0, radiacion_prom_w_m2=float(i), radiacion_max_w_m2=i + 5.0,
                 uv_prom=i / 100, uv_max=i / 100 + .1, viento_prom_kmh=3.0, viento_dir_prom=350 if i % 2 else 10,
                 rafaga_max_kmh=8.0 + (i % 7), paquetes=23, paquetes_esperados=23, calidad_pct=100.0)
        filas.append(tuple(v[k] for k in COLS_RESUMEN))
    with c:
        c.executemany(_SQL_RESUMEN, filas)
    c.close()


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.a = A.Autenticador(os.path.join(tempfile.mkdtemp(), "u.db"))
        self.a.crear_usuario("jefa", "Clave-segura-1", "admin", "Jefa")

    def test_hash(self):
        h = A.hash_clave("abc123def456")
        self.assertTrue(A.verificar_clave("abc123def456", h))
        self.assertFalse(A.verificar_clave("abc123def457", h))
        self.assertNotIn("abc123", h)

    def test_login_sesion_logout(self):
        r = self.a.iniciar_sesion("JEFA", "Clave-segura-1", "1.1.1.1")   # usuario sin distinguir mayúsculas
        s = self.a.sesion(r["token"])
        self.assertEqual((s["rol"], s["csrf"]), ("admin", r["csrf"]))
        self.a.cerrar_sesion(r["token"])
        self.assertIsNone(self.a.sesion(r["token"]))

    def test_bloqueo_tras_intentos(self):
        for _ in range(A.MAX_INTENTOS):
            with self.assertRaises(PermissionError):
                self.a.iniciar_sesion("jefa", "mala-clave-xx", "2.2.2.2")
        with self.assertRaisesRegex(PermissionError, "Demasiados"):
            self.a.iniciar_sesion("jefa", "Clave-segura-1", "2.2.2.2")   # aun con la clave correcta

    def test_validaciones(self):
        for u, c in (("x", "Clave-segura-1"), ("bien", "corta1"), ("bien", "soloLetrasAqui")):
            with self.assertRaises(ValueError):
                self.a.crear_usuario(u, c)
        with self.assertRaises(ValueError):
            self.a.crear_usuario("jefa", "Otra-clave-123")   # duplicado

    def test_no_quedar_sin_admin(self):
        uid = self.a.listar()[0]["id"]
        for f in (lambda: self.a.actualizar(uid, rol="usuario"), lambda: self.a.actualizar(uid, activo=False),
                  lambda: self.a.eliminar(uid)):
            with self.assertRaises(ValueError):
                f()

    def test_desactivar_cierra_sesiones(self):
        uid = self.a.crear_usuario("ana", "Clave-segura-2")
        r = self.a.iniciar_sesion("ana", "Clave-segura-2", "3.3.3.3")
        self.a.actualizar(uid, activo=False)
        self.assertIsNone(self.a.sesion(r["token"]))


class TestPromediado(unittest.TestCase):
    def test_movil_igual_a_rolling_de_pandas(self):
        """Equivalente a df.rolling('30min', min_periods=15).mean(): ventana (t-30min, t]."""
        base_sintetica(200, hueco=(T0 - 100 * 60, T0 - 80 * 60))
        r = promediado.movil(T0 - 120 * 60, T0, 30, 15, ("solar",))
        filas = consultas.filas_resumen(T0 - 200 * 60, T0, ["radiacion_prom_w_m2"])
        for t, p in zip(r["t"], r["promediado"]["solar"]):
            vent = [f[1] for f in filas if t - 1800 < f[0] <= t]
            esperado = sum(vent) / len(vent) if len(vent) >= 15 else None
            if esperado is None:
                self.assertIsNone(p)
            else:
                self.assertAlmostEqual(p, esperado, places=1)
        self.assertTrue(any(p is None for p in r["promediado"]["solar"]))   # el hueco deja minutos sin promedio

    def test_archivo_alineado_y_cobertura(self):
        base_sintetica(180)
        a = promediado.archivo(T0 - 3 * 3600, T0, 30, "media")
        self.assertTrue(all(r["fin"] % 1800 == 0 for r in a["registros"]))
        ult = a["registros"][-1]
        self.assertEqual(ult["fin"], T0)
        self.assertEqual(ult["minutos"], 30)
        self.assertAlmostEqual(ult["solar"], sum(range(150, 180)) / 30, places=2)
        self.assertEqual(ult["temp_alta"], round(20.1 + 179 * 0.01, 2))
        self.assertIn(round(ult["direccion_dominante"]) % 360, (0, 360))   # 350° y 10° → norte, no 180°

    def test_integral_sin_tramas_es_none(self):
        base_sintetica(60)
        a = promediado.archivo(T0 - 3600, T0, 30, "integral")
        self.assertIsNone(a["registros"][-1]["temp"])

    def test_media_integral_trapecios(self):
        pts = [(0, 0.0), (10, 10.0), (20, 10.0)]
        self.assertAlmostEqual(promediado._media_integral(pts, 0, 20), 7.5)
        self.assertAlmostEqual(promediado._media_integral(pts, 0, 20, [0, 10, 20]), 7.5)


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ajustes.COOKIE_SEGURA = False   # urllib no reenvía cookies Secure por http://; en producción va por HTTPS
        base_sintetica(240)
        cls.auth = A.Autenticador(os.path.join(tempfile.mkdtemp(), "u.db"))
        cls.auth.crear_usuario("admin1", "Clave-segura-1", "admin")
        cls.srv = crear_servidor("127.0.0.1", 0, cls.auth)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        ajustes.COOKIE_SEGURA = True

    def pedir(self, ruta, cuerpo=None, metodo=None, jar=None, csrf=None):
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar if jar is not None else CookieJar()))
        h = {"Content-Type": "application/json"}
        if csrf:
            h["X-CSRF-Token"] = csrf
        req = urllib.request.Request(self.url + ruta, data=None if cuerpo is None else json.dumps(cuerpo).encode(),
                                     headers=h, method=metodo)
        try:
            with op.open(req, timeout=20) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    def login(self):
        jar = CookieJar()
        st, h, b = self.pedir("/api/login", {"usuario": "admin1", "clave": "Clave-segura-1"}, jar=jar)
        self.assertEqual(st, 200)
        self.assertIn("HttpOnly", h["Set-Cookie"])
        return jar, json.loads(b)["usuario"]["csrf"]

    def test_publico_y_cabeceras(self):
        st, h, b = self.pedir("/")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertIn(b"echarts.min.js", b)
        st, _, b = self.pedir(f"/api/serie?desde={T0 - 3600}&hasta={T0}&vars=temp,direccion&res=300")
        d = json.loads(b)
        self.assertEqual((st, d["resolucion"], len(d["t"])), (200, 300, 12))
        self.assertEqual(json.loads(self.pedir("/api/estado")[2])["conexion"], "fuera_de_linea")

    def test_privado_requiere_sesion(self):
        for r in ("/api/promediado/archivo", "/api/exportar", "/api/usuarios", "/api/vista-previa"):
            self.assertEqual(self.pedir(r)[0], 401, r)

    def test_path_traversal(self):
        st, _, b = self.pedir("/../../davis_monitor/config.py")
        self.assertEqual(st, 200)
        self.assertNotIn(b"_valor", b)   # devuelve index.html, nunca archivos fuera de static/

    def test_flujo_admin_y_descarga(self):
        jar, csrf = self.login()
        st, _, b = self.pedir("/api/usuarios", {"usuario": "tecnico", "clave": "Clave-tecnico-9"}, jar=jar)
        self.assertEqual(st, 403)                                      # sin CSRF
        st, _, _ = self.pedir("/api/usuarios", {"usuario": "tecnico", "clave": "Clave-tecnico-9"}, jar=jar, csrf=csrf)
        self.assertEqual(st, 201)
        st, h, b = self.pedir(f"/api/exportar?fuente=minuto&desde={T0 - 3600}&hasta={T0}&vars=temp&formatos=csv,txt",
                              jar=jar)
        self.assertEqual((st, h["Content-Type"]), (200, "application/zip"))
        st, h, b = self.pedir(f"/api/exportar?fuente=promediado&desde={T0 - 7200}&hasta={T0}&intervalo=30&formatos=csv",
                              jar=jar)
        self.assertEqual(st, 200)
        self.assertEqual(len(b.decode("utf-8-sig").strip().splitlines()), 1 + 4)   # 4 registros de 30 min
        eventos = json.loads(self.pedir("/api/auditoria", jar=jar)[2])["eventos"]
        self.assertTrue(any(e["accion"] == "descarga" for e in eventos))
        # un usuario normal no puede administrar
        jar2 = CookieJar()
        self.pedir("/api/login", {"usuario": "tecnico", "clave": "Clave-tecnico-9"}, jar=jar2)
        self.assertEqual(self.pedir("/api/usuarios", jar=jar2)[0], 403)
        self.assertEqual(self.pedir(f"/api/exportar?fuente=minuto&desde={T0 - 600}&hasta={T0}&formatos=csv",
                                    jar=jar2)[0], 200)


if __name__ == "__main__":
    unittest.main()
