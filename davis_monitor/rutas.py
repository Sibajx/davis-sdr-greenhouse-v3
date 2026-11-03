"""Único lugar donde se decide DÓNDE se guarda cada cosa.

Todo lo que genera el programa vive dentro del proyecto, en `datos/`:

    datos/
    ├── csv/AAAA/MM/historial_davis_AAAA-MM-DD.csv   resumen por minuto (uno por día;
    │                                                 los viejos se comprimen a .csv.gz ahí mismo)
    ├── db/eventos_davis.db                          SQLite: tramas + resumen (base activa)
    ├── db/historico_v23_migrado.db                  histórico de v2.3 re-decodificado
    ├── logs/davis_monitor.log(.1 … .5)              log rotativo
    ├── estado/estado.json                           último estado en vivo (cada 5 s)
    ├── exportaciones/                               salidas de `basedatos exportar`
    ├── web/usuarios.db                              cuentas, sesiones y auditoría del dashboard
    └── legado_v23/                                  copia de los datos originales de v2.3

Ningún otro módulo arma rutas a mano: todos piden aquí.
"""
import os
from datetime import datetime


class Rutas:
    def __init__(self, base: str):
        self.base = os.path.abspath(os.path.expanduser(base))
        self.csv = os.path.join(self.base, "csv")
        self.db_dir = os.path.join(self.base, "db")
        self.logs = os.path.join(self.base, "logs")
        self.estado_dir = os.path.join(self.base, "estado")
        self.exportaciones = os.path.join(self.base, "exportaciones")
        self.legado = os.path.join(self.base, "legado_v23")
        self.web_dir = os.path.join(self.base, "web")
        self.usuarios_db = os.path.join(self.web_dir, "usuarios.db")

        self.db = os.path.join(self.db_dir, "eventos_davis.db")
        self.db_migrada = os.path.join(self.db_dir, "historico_v23_migrado.db")
        self.log = os.path.join(self.logs, "davis_monitor.log")
        self.estado = os.path.join(self.estado_dir, "estado.json")

    def crear(self) -> "Rutas":
        for d in (self.csv, self.db_dir, self.logs, self.estado_dir, self.exportaciones, self.web_dir):
            os.makedirs(d, exist_ok=True)
        return self

    def carpeta_csv(self, fecha: datetime) -> str:
        return os.path.join(self.csv, f"{fecha:%Y}", f"{fecha:%m}")

    def csv_del_dia(self, fecha: datetime, sufijo: str = "") -> str:
        return os.path.join(self.carpeta_csv(fecha), f"historial_davis_{fecha:%Y-%m-%d}{sufijo}.csv")

    def exportacion(self, nombre: str) -> str:
        """Nombre suelto → dentro de exportaciones/; ruta explícita → se respeta."""
        if os.path.dirname(nombre):
            return os.path.abspath(os.path.expanduser(nombre))
        return os.path.join(self.exportaciones, nombre)
