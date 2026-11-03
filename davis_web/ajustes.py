"""Ajustes del dashboard. Mismo mecanismo que davis_monitor/config.py:
variable DAVIS_<CLAVE> > config.json del proyecto > valor por defecto."""
from davis_monitor.config import _valor

HOST: str = _valor("web_host", "127.0.0.1")           # NO cambiar a 0.0.0.0: se entra por Cloudflare
PUERTO: int = _valor("web_puerto", 8090, int)
HORAS_SESION: float = _valor("web_horas_sesion", 12.0, float)
COOKIE_SEGURA: bool = _valor("web_cookie_segura", True, bool)   # Secure: solo viaja por HTTPS (y localhost)
TITULO: str = _valor("web_titulo", "Estación Davis · Invernadero")
