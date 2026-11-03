"""Uso:
    python3 -m davis_web                       # servidor en 127.0.0.1:8090
    python3 -m davis_web admin crear USUARIO   # crea un administrador (pide la contraseña)
    python3 -m davis_web admin clave USUARIO   # restablece la contraseña de cualquier usuario
    python3 -m davis_web admin listar
"""
import argparse
import getpass
import logging
import signal
import sys

from davis_monitor import config
from . import ajustes, __version__
from .auth import Autenticador


def _admin(a) -> None:
    config.RUTAS.crear()
    auth = Autenticador(config.RUTAS.usuarios_db)
    if a.accion == "listar":
        for u in auth.listar():
            print(f"{u['id']:>3}  {u['usuario']:<20} {u['rol']:<8} {'activo' if u['activo'] else 'INACTIVO':<9} {u['nombre']}")
        return
    clave = getpass.getpass("Contraseña nueva: ")
    if clave != getpass.getpass("Repítela: "):
        sys.exit("No coinciden.")
    try:
        if a.accion == "crear":
            auth.crear_usuario(a.usuario, clave, "admin", a.nombre or a.usuario)
            auth.auditar("admin_creado_cli", a.usuario)
            print(f"Administrador '{a.usuario}' creado.")
        else:
            u = next((x for x in auth.listar() if x["usuario"].lower() == a.usuario.lower()), None)
            if not u:
                sys.exit("No existe ese usuario.")
            auth.cambiar_clave(u["id"], clave)
            auth.auditar("clave_restablecida_cli", a.usuario)
            print("Contraseña cambiada (se cerraron sus sesiones).")
    except ValueError as e:
        sys.exit(f"Error: {e}")


def main() -> None:
    p = argparse.ArgumentParser(description="Dashboard web de la estación Davis")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    pa = sub.add_parser("admin", help="gestión de cuentas desde la terminal")
    pa.add_argument("accion", choices=("crear", "clave", "listar"))
    pa.add_argument("usuario", nargs="?")
    pa.add_argument("--nombre", default="")
    a = p.parse_args()
    if a.cmd == "admin":
        if a.accion != "listar" and not a.usuario:
            p.error("falta USUARIO")
        return _admin(a)

    from .servidor import configurar_log, crear_servidor
    configurar_log()
    log = logging.getLogger("DavisWeb")
    srv = crear_servidor()
    if not srv.RequestHandlerClass.auth.hay_usuarios():
        log.warning("No hay cuentas: crea el administrador con  python3 -m davis_web admin crear <usuario>")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    log.info(f"Dashboard {__version__} en http://{ajustes.HOST}:{ajustes.PUERTO}  (datos: {config.DIR_DATOS})")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        log.info("Dashboard detenido")


if __name__ == "__main__":
    main()
