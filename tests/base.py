"""Aísla cada test: config apunta a una carpeta temporal ANTES de importar."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="davis_test_")
os.environ["DAVIS_DIR_DATOS"] = _TMP
os.environ["DAVIS_WEBHOOK_URL"] = ""
_CFG = os.path.join(_TMP, "config_test.json")
with open(_CFG, "w") as _f:
    _f.write("{}")
os.environ["DAVIS_CONFIG_FILE"] = _CFG

import logging  # noqa: E402
logging.getLogger("DavisMonitor").addHandler(logging.NullHandler())
logging.getLogger("DavisMonitor").propagate = False


def trama(b0, b1, b2, b3, b4, b5=0) -> bytes:
    from davis_monitor.protocolo import crc_ccitt
    d = bytes([b0, b1, b2, b3, b4, b5])
    c = crc_ccitt(d)
    return d + bytes([c >> 8, c & 0xFF])
