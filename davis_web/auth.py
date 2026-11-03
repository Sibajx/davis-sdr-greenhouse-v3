"""Cuentas, sesiones y auditoría del dashboard (SQLite en datos/web/usuarios.db).

Seguridad (el sitio queda expuesto a internet vía Cloudflare):
  * Contraseñas con scrypt (hashlib, N=2^14, r=8, p=1) + sal aleatoria por usuario.
  * La cookie lleva un token aleatorio de 256 bits; en la base se guarda solo
    su SHA-256 (si alguien copia la base, no puede usar las sesiones).
  * Token CSRF por sesión, exigido en toda petición que modifica algo.
  * Bloqueo: 5 intentos fallidos → 15 min, por usuario y por IP.
  * Comparaciones en tiempo constante; mismo mensaje de error para usuario
    inexistente y contraseña incorrecta.
"""
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

ROLES = ("admin", "usuario")
MIN_LARGO_CLAVE = 10
MAX_INTENTOS = 5
BLOQUEO_SEG = 15 * 60
_RE_USUARIO = re.compile(r"^[a-zA-Z0-9._-]{3,32}$")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    id INTEGER PRIMARY KEY,
    usuario TEXT UNIQUE NOT NULL COLLATE NOCASE,
    nombre TEXT NOT NULL DEFAULT '',
    rol TEXT NOT NULL CHECK (rol IN ('admin','usuario')),
    hash TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    creado REAL NOT NULL,
    ultimo_acceso REAL,
    cambiar_clave INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sesiones (
    token_sha TEXT PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    creada REAL NOT NULL,
    expira REAL NOT NULL,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    usuario TEXT,
    ip TEXT,
    accion TEXT NOT NULL,
    detalle TEXT
);
CREATE INDEX IF NOT EXISTS idx_auditoria_ts ON auditoria(ts);
"""


def hash_clave(clave: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.scrypt(clave.encode(), salt=sal, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${sal.hex()}${h.hex()}"


def verificar_clave(clave: str, guardado: str) -> bool:
    try:
        _alg, n, r, p, sal, h = guardado.split("$")
        calc = hashlib.scrypt(clave.encode(), salt=bytes.fromhex(sal), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


# hash señuelo para que "usuario inexistente" tarde lo mismo que "clave mala"
_HASH_SENUELO = hash_clave(secrets.token_hex(16))


def validar_clave_nueva(clave: str) -> Optional[str]:
    if len(clave) < MIN_LARGO_CLAVE:
        return f"La contraseña debe tener al menos {MIN_LARGO_CLAVE} caracteres"
    if clave.isdigit() or clave.isalpha():
        return "La contraseña debe combinar letras y números o símbolos"
    return None


class Autenticador:
    def __init__(self, ruta_db: str, horas_sesion: float = 12.0):
        self.ruta = ruta_db
        self.duracion = horas_sesion * 3600
        self._lock = threading.Lock()
        self._fallos: dict = {}   # clave ('u:nombre' | 'ip:x.x.x.x') → [conteo, desde]
        with self._con() as c:
            c.executescript(_ESQUEMA)

    @contextmanager
    def _con(self):
        """Conexión corta: confirma (o revierte) la transacción y SIEMPRE se cierra.
        (El `with` nativo de sqlite3 confirma pero no cierra → fuga de descriptores.)"""
        c = sqlite3.connect(self.ruta, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    # ------------------------------------------------------------ auditoría
    def auditar(self, accion: str, usuario: str = None, ip: str = None, detalle: str = None) -> None:
        with self._con() as c:
            c.execute("INSERT INTO auditoria (ts, usuario, ip, accion, detalle) VALUES (?,?,?,?,?)",
                      (time.time(), usuario, ip, accion, (detalle or "")[:500]))

    def auditoria(self, limite: int = 200) -> list:
        with self._con() as c:
            return [dict(r) for r in c.execute(
                "SELECT ts, usuario, ip, accion, detalle FROM auditoria ORDER BY id DESC LIMIT ?", (limite,))]

    # ------------------------------------------------------------ usuarios
    def crear_usuario(self, usuario: str, clave: str, rol: str = "usuario", nombre: str = "",
                      cambiar_clave: bool = False) -> int:
        if not _RE_USUARIO.match(usuario or ""):
            raise ValueError("Usuario: 3 a 32 caracteres, solo letras, números, punto, guion y guion bajo")
        if rol not in ROLES:
            raise ValueError("Rol inválido")
        err = validar_clave_nueva(clave)
        if err:
            raise ValueError(err)
        try:
            with self._con() as c:
                cur = c.execute("INSERT INTO usuarios (usuario, nombre, rol, hash, creado, cambiar_clave) "
                                "VALUES (?,?,?,?,?,?)",
                                (usuario, nombre.strip()[:80], rol, hash_clave(clave), time.time(),
                                 int(cambiar_clave)))
                return cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Ese usuario ya existe")

    def listar(self) -> list:
        with self._con() as c:
            return [dict(r) for r in c.execute(
                "SELECT id, usuario, nombre, rol, activo, creado, ultimo_acceso, cambiar_clave "
                "FROM usuarios ORDER BY usuario")]

    def obtener(self, uid: int) -> Optional[dict]:
        with self._con() as c:
            r = c.execute("SELECT id, usuario, nombre, rol, activo, cambiar_clave FROM usuarios WHERE id=?",
                          (uid,)).fetchone()
            return dict(r) if r else None

    def _admins_activos(self, c) -> int:
        return c.execute("SELECT COUNT(*) FROM usuarios WHERE rol='admin' AND activo=1").fetchone()[0]

    def actualizar(self, uid: int, rol: str = None, activo: bool = None, nombre: str = None) -> None:
        with self._con() as c:
            actual = c.execute("SELECT rol, activo FROM usuarios WHERE id=?", (uid,)).fetchone()
            if not actual:
                raise ValueError("Usuario no encontrado")
            quita_admin = actual["rol"] == "admin" and actual["activo"] and (
                (rol is not None and rol != "admin") or (activo is not None and not activo))
            if quita_admin and self._admins_activos(c) <= 1:
                raise ValueError("No se puede dejar el sistema sin ningún administrador activo")
            if rol is not None:
                if rol not in ROLES:
                    raise ValueError("Rol inválido")
                c.execute("UPDATE usuarios SET rol=? WHERE id=?", (rol, uid))
            if activo is not None:
                c.execute("UPDATE usuarios SET activo=? WHERE id=?", (int(bool(activo)), uid))
                if not activo:
                    c.execute("DELETE FROM sesiones WHERE usuario_id=?", (uid,))
            if nombre is not None:
                c.execute("UPDATE usuarios SET nombre=? WHERE id=?", (nombre.strip()[:80], uid))

    def eliminar(self, uid: int) -> None:
        with self._con() as c:
            r = c.execute("SELECT rol, activo FROM usuarios WHERE id=?", (uid,)).fetchone()
            if not r:
                raise ValueError("Usuario no encontrado")
            if r["rol"] == "admin" and r["activo"] and self._admins_activos(c) <= 1:
                raise ValueError("No se puede eliminar al último administrador")
            c.execute("DELETE FROM usuarios WHERE id=?", (uid,))

    def cambiar_clave(self, uid: int, nueva: str, forzar_cambio: bool = False) -> None:
        err = validar_clave_nueva(nueva)
        if err:
            raise ValueError(err)
        with self._con() as c:
            c.execute("UPDATE usuarios SET hash=?, cambiar_clave=? WHERE id=?",
                      (hash_clave(nueva), int(forzar_cambio), uid))
            c.execute("DELETE FROM sesiones WHERE usuario_id=?", (uid,))  # cierra todas sus sesiones

    def verificar(self, uid: int, clave: str) -> bool:
        with self._con() as c:
            r = c.execute("SELECT hash FROM usuarios WHERE id=?", (uid,)).fetchone()
        return verificar_clave(clave or "", r["hash"] if r else _HASH_SENUELO) and r is not None

    def hay_usuarios(self) -> bool:
        with self._con() as c:
            return c.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] > 0

    # ------------------------------------------------------------ login / sesiones
    def _bloqueado(self, clave: str, ahora: float) -> bool:
        f = self._fallos.get(clave)
        if not f:
            return False
        if ahora - f[1] > BLOQUEO_SEG:
            self._fallos.pop(clave, None)
            return False
        return f[0] >= MAX_INTENTOS

    def _registrar_fallo(self, clave: str, ahora: float) -> None:
        f = self._fallos.get(clave)
        if not f or ahora - f[1] > BLOQUEO_SEG:
            self._fallos[clave] = [1, ahora]
        else:
            f[0] += 1

    def iniciar_sesion(self, usuario: str, clave: str, ip: str) -> dict:
        """Devuelve {'token','csrf','usuario'} o lanza PermissionError con mensaje genérico."""
        ahora = time.time()
        ku, ki = f"u:{(usuario or '').lower()}", f"ip:{ip}"
        with self._lock:
            if self._bloqueado(ku, ahora) or self._bloqueado(ki, ahora):
                self.auditar("login_bloqueado", usuario, ip)
                raise PermissionError("Demasiados intentos. Espera 15 minutos.")
        with self._con() as c:
            r = c.execute("SELECT * FROM usuarios WHERE usuario=?", (usuario or "",)).fetchone()
        ok = verificar_clave(clave or "", r["hash"] if r else _HASH_SENUELO) and r is not None and r["activo"]
        if not ok:
            with self._lock:
                self._registrar_fallo(ku, ahora)
                self._registrar_fallo(ki, ahora)
            self.auditar("login_fallido", usuario, ip)
            raise PermissionError("Usuario o contraseña incorrectos")
        with self._lock:
            self._fallos.pop(ku, None)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self._con() as c:
            c.execute("DELETE FROM sesiones WHERE expira < ?", (ahora,))
            c.execute("INSERT INTO sesiones (token_sha, usuario_id, csrf, creada, expira, ip) VALUES (?,?,?,?,?,?)",
                      (hashlib.sha256(token.encode()).hexdigest(), r["id"], csrf, ahora, ahora + self.duracion, ip))
            c.execute("UPDATE usuarios SET ultimo_acceso=? WHERE id=?", (ahora, r["id"]))
        self.auditar("login", r["usuario"], ip)
        return {"token": token, "csrf": csrf, "usuario": self._publico(r)}

    def sesion(self, token: Optional[str]) -> Optional[dict]:
        if not token or len(token) > 100:
            return None
        ahora = time.time()
        sha = hashlib.sha256(token.encode()).hexdigest()
        with self._con() as c:
            r = c.execute("SELECT s.csrf, s.expira, u.* FROM sesiones s JOIN usuarios u ON u.id=s.usuario_id "
                          "WHERE s.token_sha=?", (sha,)).fetchone()
            if not r or r["expira"] < ahora or not r["activo"]:
                if r:
                    c.execute("DELETE FROM sesiones WHERE token_sha=?", (sha,))
                return None
            # expiración deslizante: se renueva si queda menos de la mitad
            if r["expira"] - ahora < self.duracion / 2:
                c.execute("UPDATE sesiones SET expira=? WHERE token_sha=?", (ahora + self.duracion, sha))
        d = self._publico(r)
        d["csrf"] = r["csrf"]
        return d

    def cerrar_sesion(self, token: Optional[str]) -> None:
        if token:
            with self._con() as c:
                c.execute("DELETE FROM sesiones WHERE token_sha=?", (hashlib.sha256(token.encode()).hexdigest(),))

    @staticmethod
    def _publico(r) -> dict:
        return {"id": r["id"], "usuario": r["usuario"], "nombre": r["nombre"], "rol": r["rol"],
                "cambiar_clave": bool(r["cambiar_clave"])}
