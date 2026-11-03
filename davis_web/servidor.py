"""Servidor HTTP del dashboard (biblioteca estándar, ThreadingHTTPServer).

Escucha SOLO en 127.0.0.1: desde fuera se entra únicamente por el túnel de
Cloudflare (cloudflared → http://127.0.0.1:8090). Lee las bases en modo solo
lectura; nunca toca el dongle ni el receptor.
"""
import gzip
import hashlib
import json
import logging
import mimetypes
import threading
from collections import OrderedDict, deque
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

# ---- protección del sitio público (la Pi también es el receptor: no puede saturarse)
CONSULTAS_PESADAS = threading.BoundedSemaphore(3)   # series/promediado/exportar simultáneos
LIMITES = {"api": (600, 60), "pesada": (60, 60)}    # (peticiones, ventana s) por IP; el lab sale por 1 sola IP (NAT)
_HITS: dict = {}
_HITS_LOCK = threading.Lock()


def limite_superado(ip: str, tipo: str) -> bool:
    n, ventana = LIMITES[tipo]
    ahora = time.monotonic()
    with _HITS_LOCK:
        d = _HITS.setdefault((ip, tipo), deque())
        while d and ahora - d[0] > ventana:
            d.popleft()
        if len(d) >= n:
            return True
        d.append(ahora)
        if len(_HITS) > 5000:   # limpieza ocasional de IPs inactivas
            for k in [k for k, v in _HITS.items() if not v or ahora - v[-1] > 600]:
                _HITS.pop(k, None)
        return False


class CacheTTL:
    """Caché pequeña en memoria para respuestas JSON públicas (misma consulta de muchos visitantes)."""
    def __init__(self, maximo: int = 96, ttl: float = 20.0):
        self.maximo, self.ttl, self._d, self._lock = maximo, ttl, OrderedDict(), threading.Lock()

    def obtener(self, clave):
        with self._lock:
            v = self._d.get(clave)
            if v and time.monotonic() - v[0] < self.ttl:
                self._d.move_to_end(clave)
                return v[1]
            return None

    def guardar(self, clave, valor):
        with self._lock:
            self._d[clave] = (time.monotonic(), valor)
            self._d.move_to_end(clave)
            while len(self._d) > self.maximo:
                self._d.popitem(last=False)


CACHE = CacheTTL()
_ESTATICOS: dict = {}   # ruta → (mtime, contenido, gzip, etag)
COMPRIMIBLES = ("text/", "application/json", "application/javascript", "image/svg+xml")

CABECERAS_SEGURIDAD = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Strict-Transport-Security": "max-age=31536000",   # los navegadores la ignoran por http://localhost
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

    def acepta_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def enviar(self, estado: int, cuerpo: bytes, tipo: str, extra: dict = None, cache: str = "no-store",
               ya_gzip: bytes = None):
        extra = dict(extra or {})
        if len(cuerpo) > 1400 and tipo.startswith(COMPRIMIBLES) and self.acepta_gzip():
            cuerpo = ya_gzip if ya_gzip is not None else gzip.compress(cuerpo, 6)
            extra["Content-Encoding"] = "gzip"
            extra["Vary"] = "Accept-Encoding"
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", cache)
        for k, v in {**CABECERAS_SEGURIDAD, **extra}.items():
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
            if url.path.startswith("/api/"):
                pesada = url.path in RUTAS_PESADAS
                if limite_superado(self.ip(), "pesada" if pesada else "api"):
                    raise ErrorHTTP(429, "Demasiadas peticiones; espera un minuto")
            for (m, patron), fn in RUTAS.items():
                if m == metodo:
                    g = re.fullmatch(patron, url.path)
                    if g:
                        return fn(self, q, *g.groups())
            if metodo == "GET" and not url.path.startswith("/api/"):
                return self.archivo_estatico(url.path)
            raise ErrorHTTP(404, "No encontrado")
        except ErrorHTTP as e:
            self.json({"error": e.mensaje}, e.estado, extra={"Retry-After": "60"} if e.estado in (429, 503) else None)
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
        cache = "public, max-age=604800" if "/vendor/" in real else "no-cache"
        mtime = os.path.getmtime(real)
        e = _ESTATICOS.get(real)
        if not e or e[0] != mtime:
            with open(real, "rb") as f:
                datos = f.read()
            e = (mtime, datos, gzip.compress(datos, 9), '"' + hashlib.sha1(datos).hexdigest()[:16] + '"')
            _ESTATICOS[real] = e
        if self.headers.get("If-None-Match") == e[3]:
            self.send_response(304)
            self.send_header("ETag", e[3])
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        self.enviar(200, e[1], tipo, {"ETag": e[3]}, cache=cache, ya_gzip=e[2])


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


class _Pesada:
    """Máx. 3 consultas pesadas a la vez; si no hay turno en 20 s → 503 (en vez de saturar la Pi)."""
    def __enter__(self):
        if not CONSULTAS_PESADAS.acquire(timeout=20):
            raise ErrorHTTP(503, "Servidor ocupado, intenta de nuevo en unos segundos")

    def __exit__(self, *a):
        CONSULTAS_PESADAS.release()


def ep_serie(h, q):
    desde, hasta = _rango(q)
    if hasta - desde > 400 * 86400:
        raise ValueError("Rango máximo: 400 días")
    res = int(q["res"]) if q.get("res") else None
    # redondeo a 10 s: muchos visitantes del "en vivo" comparten la misma entrada de caché
    desde, hasta = round(desde / 10) * 10, round(hasta / 10) * 10
    vs = tuple(_vars(q, ("temp", "humedad")))
    clave = (desde, hasta, vs, res)
    cuerpo = CACHE.obtener(clave)
    if cuerpo is None:
        with _Pesada():
            cuerpo = json.dumps(consultas.serie(desde, hasta, vs, res), ensure_ascii=False,
                                separators=(",", ":")).encode()
        CACHE.guardar(clave, cuerpo)
    h.enviar(200, cuerpo, "application/json; charset=utf-8")


def ep_robots(h, q):
    h.enviar(200, b"User-agent: *\nDisallow: /api/\n", "text/plain; charset=utf-8", cache="public, max-age=86400")


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
    with _Pesada():
        r = promediado.movil(desde, hasta, v, max(1, v // 2))
    h.json(r)


def ep_prom_archivo(h, q):
    h.requiere()
    desde, hasta = _rango(q, 48)
    if hasta - desde > 400 * 86400:
        raise ValueError("Rango máximo: 400 días")
    metodo = q.get("metodo", "media")
    if metodo == "integral" and hasta - desde > 31 * 86400:
        raise ValueError("El método integral usa tramas crudas: máximo 31 días")
    with _Pesada():
        r = promediado.archivo(desde, hasta, int(q.get("intervalo", 30)), metodo)
    h.json(r)


def ep_exportar(h, q):
    s = h.requiere()
    desde, hasta = _rango(q)
    fuente = q.get("fuente", "minuto")
    formatos = [f for f in (q.get("formatos") or "csv").split(",") if f]
    res = int(q["res"]) if q.get("res") else None
    with _Pesada():
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
    with _Pesada():
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
    ("GET", r"/robots\.txt"): ep_robots,
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


RUTAS_PESADAS = {"/api/serie", "/api/promediado/movil", "/api/promediado/archivo", "/api/exportar",
                 "/api/vista-previa"}


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
