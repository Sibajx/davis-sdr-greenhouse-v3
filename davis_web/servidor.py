"""Servidor HTTP del dashboard (biblioteca estándar, ThreadingHTTPServer).

Escucha SOLO en 127.0.0.1: desde fuera se entra únicamente por el túnel de
Cloudflare (cloudflared → http://127.0.0.1:8090). Lee las bases en modo solo
lectura; nunca toca el dongle ni el receptor.
"""
import json
import logging
import mimetypes
import os
import re
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import parse_qs, urlparse

from davis_monitor import config
from . import __version__, ajustes, consultas, exportar, promediado
from .auth import Autenticador

logger = logging.getLogger("DavisWeb")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "davis_sesion"
MAX_CUERPO = 64 * 1024

CABECERAS_SEGURIDAD = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


class ErrorHTTP(Exception):
    def __init__(self, estado: int, mensaje: str):
        super().__init__(mensaje)
        self.estado, self.mensaje = estado, mensaje


class Manejador(BaseHTTPRequestHandler):
    server_version = "DavisWeb"
    sys_version = ""
    auth: Autenticador = None   # se asigna al crear el servidor

    # ------------------------------------------------------------ utilidades
    def log_message(self, fmt, *args):   # silenciar el log por petición de http.server
        pass

    def ip(self) -> str:
        # Detrás del túnel la conexión viene de 127.0.0.1; la IP real la pone Cloudflare.
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return self.headers.get("CF-Connecting-IP") or self.client_address[0]
        return self.client_address[0]

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def sesion(self):
        if not hasattr(self, "_sesion"):
            self._sesion = self.auth.sesion(self.token())
        return self._sesion

    def requiere(self, rol: str = None) -> dict:
        s = self.sesion()
        if not s:
            raise ErrorHTTP(401, "Inicia sesión para acceder")
        if rol == "admin" and s["rol"] != "admin":
            raise ErrorHTTP(403, "Solo administradores")
        return s

    def verificar_csrf(self, s: dict) -> None:
        origen = self.headers.get("Origin")
        if origen and urlparse(origen).netloc != self.headers.get("Host"):
            raise ErrorHTTP(403, "Origen no permitido")
        if self.headers.get("X-CSRF-Token") != s["csrf"]:
            raise ErrorHTTP(403, "Token CSRF inválido; recarga la página")

    def cuerpo_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_CUERPO:
            raise ErrorHTTP(413, "Petición demasiado grande")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError
            return d
        except ValueError:
            raise ErrorHTTP(400, "JSON inválido")

    def enviar(self, estado: int, cuerpo: bytes, tipo: str, extra: dict = None, cache: str = "no-store"):
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", cache)
        for k, v in {**CABECERAS_SEGURIDAD, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def json(self, datos, estado: int = 200, extra: dict = None):
        self.enviar(estado, json.dumps(datos, ensure_ascii=False, separators=(",", ":")).encode(),
                    "application/json; charset=utf-8", extra)

    def cookie_sesion(self, token: str, max_age: int) -> str:
        seg = "; Secure" if ajustes.COOKIE_SEGURA else ""
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{seg}"

    # ------------------------------------------------------------ despacho
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    def do_DELETE(self):
        self._despachar("DELETE")

    def _despachar(self, metodo: str):
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        t0 = time.time()
        try:
            for (m, patron), fn in RUTAS.items():
                if m == metodo:
                    g = re.fullmatch(patron, url.path)
                    if g:
                        return fn(self, q, *g.groups())
            if metodo == "GET":
                return self.archivo_estatico(url.path)
            raise ErrorHTTP(404, "No encontrado")
        except ErrorHTTP as e:
            self.json({"error": e.mensaje}, e.estado)
        except ValueError as e:
            self.json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error en {metodo} {url.path}: {e}")
            self.json({"error": "Error interno"}, 500)
        finally:
            dt = time.time() - t0
            if dt > 2:
                logger.info(f"{metodo} {url.path} tardó {dt:.1f} s")

    def archivo_estatico(self, ruta: str):
        if ruta in ("/", "/index.html"):
            ruta = "/index.html"
        real = os.path.realpath(os.path.join(STATIC, ruta.lstrip("/")))
        if not real.startswith(STATIC + os.sep) or not os.path.isfile(real):
            # rutas del SPA → index
            real = os.path.join(STATIC, "index.html")
        tipo = mimetypes.guess_type(real)[0] or "application/octet-stream"
        if tipo.startswith("text/") or tipo in ("application/javascript",):
            tipo += "; charset=utf-8"
        cache = "public, max-age=86400" if "/vendor/" in real else "no-cache"
        with open(real, "rb") as f:
            self.enviar(200, f.read(), tipo, cache=cache)


# ================================================================ endpoints
def _rango(q, defecto_horas=24):
    hasta = consultas.parse_fecha(q["hasta"], fin_de_dia=True) if q.get("hasta") else time.time()
    desde = consultas.parse_fecha(q["desde"]) if q.get("desde") else hasta - defecto_horas * 3600
    if hasta <= desde:
        raise ValueError("El fin debe ser posterior al inicio")
    return desde, hasta


def _vars(q, defecto):
    v = [x for x in (q.get("vars") or "").split(",") if x]
    return v or list(defecto)


def ep_estado(h, q):
    h.json(consultas.estado_en_vivo())


def ep_meta(h, q):
    h.json({"version": __version__, "titulo": ajustes.TITULO, "variables": consultas.variables_disponibles(),
            "rango": consultas.rango_disponible(), "pluviometro_activo": config.PLUVIOMETRO_ACTIVO,
            "intervalos_promediado": promediado.INTERVALOS_MIN, "resoluciones": consultas.RESOLUCIONES,
            "max_dias": exportar.MAX_DIAS})


def ep_serie(h, q):
    desde, hasta = _rango(q)
    if hasta - desde > 400 * 86400:
        raise ValueError("Rango máximo: 400 días")
    res = int(q["res"]) if q.get("res") else None
    h.json(consultas.serie(desde, hasta, _vars(q, ("temp", "humedad")), res))


def ep_sesion(h, q):
    s = h.sesion()
    h.json({"usuario": {k: v for k, v in s.items()} if s else None})


def ep_login(h, q):
    d = h.cuerpo_json()
    origen = h.headers.get("Origin")
    if origen and urlparse(origen).netloc != h.headers.get("Host"):
        raise ErrorHTTP(403, "Origen no permitido")
    try:
        r = h.auth.iniciar_sesion(str(d.get("usuario", ""))[:64], str(d.get("clave", ""))[:256], h.ip())
    except PermissionError as e:
        raise ErrorHTTP(401, str(e))
    u = dict(r["usuario"], csrf=r["csrf"])
    h.json({"usuario": u}, extra={"Set-Cookie": h.cookie_sesion(r["token"], int(h.auth.duracion))})


def ep_logout(h, q):
    s = h.sesion()
    if s:
        h.verificar_csrf(s)
        h.auth.cerrar_sesion(h.token())
        h.auth.auditar("logout", s["usuario"], h.ip())
    h.json({"ok": True}, extra={"Set-Cookie": h.cookie_sesion("", 0)})


def ep_cambiar_mi_clave(h, q):
    s = h.requiere()
    h.verificar_csrf(s)
    d = h.cuerpo_json()
    if not h.auth.verificar(s["id"], str(d.get("actual", ""))[:256]):
        h.auth.auditar("cambio_clave_fallido", s["usuario"], h.ip())
        raise ErrorHTTP(400, "La contraseña actual no es correcta")
    h.auth.cambiar_clave(s["id"], str(d.get("nueva", "")))
    h.auth.auditar("cambio_clave_propia", s["usuario"], h.ip())
    h.json({"ok": True, "mensaje": "Contraseña cambiada. Vuelve a iniciar sesión."},
           extra={"Set-Cookie": h.cookie_sesion("", 0)})


def ep_prom_movil(h, q):
    h.requiere()
    desde, hasta = _rango(q, 48)
    if hasta - desde > 31 * 86400:
        raise ValueError("Rango máximo para la serie móvil: 31 días")
    v = int(q.get("ventana", 30))
    if v not in promediado.INTERVALOS_MIN:
        raise ValueError(f"ventana debe ser una de {promediado.INTERVALOS_MIN}")
    h.json(promediado.movil(desde, hasta, v, max(1, v // 2)))


def ep_prom_archivo(h, q):
    h.requiere()
    desde, hasta = _rango(q, 48)
    if hasta - desde > 400 * 86400:
        raise ValueError("Rango máximo: 400 días")
    metodo = q.get("metodo", "media")
    if metodo == "integral" and hasta - desde > 31 * 86400:
        raise ValueError("El método integral usa tramas crudas: máximo 31 días")
    h.json(promediado.archivo(desde, hasta, int(q.get("intervalo", 30)), metodo))


def ep_exportar(h, q):
    s = h.requiere()
    desde, hasta = _rango(q)
    fuente = q.get("fuente", "minuto")
    formatos = [f for f in (q.get("formatos") or "csv").split(",") if f]
    res = int(q["res"]) if q.get("res") else None
    nombre, tipo, datos = exportar.generar(fuente, desde, hasta, _vars(q, consultas.variables_disponibles()),
                                           formatos, res, int(q.get("intervalo", 30)), q.get("metodo", "media"))
    h.auth.auditar("descarga", s["usuario"], h.ip(),
                   f"{fuente} {exportar._fecha(desde)} → {exportar._fecha(hasta)} {','.join(formatos)} "
                   f"({len(datos) // 1024} KB)")
    h.enviar(200, datos, tipo, {"Content-Disposition": f'attachment; filename="{nombre}"'})


def ep_vista_previa(h, q):
    h.requiere()
    desde, hasta = _rango(q)
    res = int(q["res"]) if q.get("res") else None
    enc, filas, meta = exportar.tabla(q.get("fuente", "minuto"), desde, hasta,
                                      _vars(q, consultas.variables_disponibles()), res,
                                      int(q.get("intervalo", 30)), q.get("metodo", "media"))
    h.json({"encabezados": enc, "filas": filas[:200], "total": len(filas), "meta": meta})


# ---- administración
def ep_usuarios(h, q):
    h.requiere("admin")
    h.json({"usuarios": h.auth.listar()})


def ep_crear_usuario(h, q):
    s = h.requiere("admin")
    h.verificar_csrf(s)
    d = h.cuerpo_json()
    uid = h.auth.crear_usuario(str(d.get("usuario", "")).strip(), str(d.get("clave", "")),
                               str(d.get("rol", "usuario")), str(d.get("nombre", "")),
                               bool(d.get("cambiar_clave", True)))
    h.auth.auditar("usuario_creado", s["usuario"], h.ip(), f"{d.get('usuario')} ({d.get('rol', 'usuario')})")
    h.json({"id": uid}, 201)


def ep_editar_usuario(h, q, uid):
    s = h.requiere("admin")
    h.verificar_csrf(s)
    d = h.cuerpo_json()
    h.auth.actualizar(int(uid), d.get("rol"), d.get("activo"), d.get("nombre"))
    h.auth.auditar("usuario_editado", s["usuario"], h.ip(), f"id={uid} {d}")
    h.json({"ok": True})


def ep_clave_usuario(h, q, uid):
    s = h.requiere("admin")
    h.verificar_csrf(s)
    d = h.cuerpo_json()
    h.auth.cambiar_clave(int(uid), str(d.get("clave", "")), forzar_cambio=True)
    h.auth.auditar("clave_restablecida", s["usuario"], h.ip(), f"id={uid}")
    h.json({"ok": True})


def ep_borrar_usuario(h, q, uid):
    s = h.requiere("admin")
    h.verificar_csrf(s)
    if int(uid) == s["id"]:
        raise ErrorHTTP(400, "No puedes eliminar tu propia cuenta")
    h.auth.eliminar(int(uid))
    h.auth.auditar("usuario_eliminado", s["usuario"], h.ip(), f"id={uid}")
    h.json({"ok": True})


def ep_auditoria(h, q):
    h.requiere("admin")
    h.json({"eventos": h.auth.auditoria(int(q.get("limite", 200)))})


RUTAS = {
    ("GET", r"/api/estado"): ep_estado,
    ("GET", r"/api/meta"): ep_meta,
    ("GET", r"/api/serie"): ep_serie,
    ("GET", r"/api/sesion"): ep_sesion,
    ("POST", r"/api/login"): ep_login,
    ("POST", r"/api/logout"): ep_logout,
    ("POST", r"/api/cuenta/clave"): ep_cambiar_mi_clave,
    ("GET", r"/api/promediado/movil"): ep_prom_movil,
    ("GET", r"/api/promediado/archivo"): ep_prom_archivo,
    ("GET", r"/api/exportar"): ep_exportar,
    ("GET", r"/api/vista-previa"): ep_vista_previa,
    ("GET", r"/api/usuarios"): ep_usuarios,
    ("POST", r"/api/usuarios"): ep_crear_usuario,
    ("POST", r"/api/usuarios/(\d+)"): ep_editar_usuario,
    ("POST", r"/api/usuarios/(\d+)/clave"): ep_clave_usuario,
    ("DELETE", r"/api/usuarios/(\d+)"): ep_borrar_usuario,
    ("GET", r"/api/auditoria"): ep_auditoria,
}


def configurar_log() -> None:
    config.RUTAS.crear()
    lg = logging.getLogger("DavisWeb")
    if lg.handlers:
        return
    lg.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(config.RUTAS.logs, "davis_web.log"), maxBytes=2 * 1024 * 1024,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    lg.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    lg.addHandler(sh)


def crear_servidor(host: str = None, puerto: int = None, auth: Autenticador = None) -> ThreadingHTTPServer:
    config.RUTAS.crear()
    Manejador.auth = auth or Autenticador(config.RUTAS.usuarios_db, ajustes.HORAS_SESION)
    srv = ThreadingHTTPServer((host or ajustes.HOST, ajustes.PUERTO if puerto is None else puerto), Manejador)
    srv.daemon_threads = True
    return srv
