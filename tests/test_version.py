"""Las versiones declaradas en el repo deben coincidir con davis_monitor.__version__."""
import re
from pathlib import Path

from davis_monitor import __version__
from davis_monitor.nucleo import VERSION

RAIZ = Path(__file__).resolve().parent.parent


def _buscar(archivo: str, patron: str) -> str:
    m = re.search(patron, (RAIZ / archivo).read_text(encoding="utf-8"), re.M)
    assert m, f"no se encontró la versión en {archivo}"
    return m.group(1)


def test_versiones_coinciden():
    assert VERSION == __version__
    assert _buscar("pyproject.toml", r'^version = "([^"]+)"') == __version__
    assert _buscar("CITATION.cff", r'^version: "([^"]+)"') == __version__
    assert _buscar("CHANGELOG.md", r"^## (\S+) ") == __version__
