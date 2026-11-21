"""Alertas por Telegram: credenciales desde archivo, formato del mensaje y que el token nunca salga al log."""
import logging
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import tests.base  # noqa: F401
from davis_monitor import alertas, config

TOKEN = "123456:TOKEN-DE-PRUEBA-xyz"


def _archivo_env(contenido: str) -> str:
    ruta = os.path.join(tempfile.mkdtemp(), "telegram.env")
    with open(ruta, "w") as f:
        f.write(contenido)
    return ruta


class TestTelegram(unittest.TestCase):
    def setUp(self):
        self._viejo = (config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.TELEGRAM_ENV)
        config.TELEGRAM_TOKEN = config.TELEGRAM_CHAT_ID = ""
        config.TELEGRAM_ENV = _archivo_env(f'# bot\nTOKEN="{TOKEN}"\nCHAT_ID=987654\n')

    def tearDown(self):
        config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID, config.TELEGRAM_ENV = self._viejo

    def test_credenciales_desde_archivo(self):
        self.assertEqual(alertas.credenciales_telegram(), (TOKEN, "987654"))

    def test_sin_credenciales_no_hay_canal(self):
        config.TELEGRAM_ENV = "/no/existe/telegram.env"
        self.assertEqual(alertas.credenciales_telegram(), ("", ""))
        self.assertFalse(alertas.hay_canal())

    def test_mensaje_a_telegram(self):
        enviadas = []

        def falso_urlopen(req, timeout):
            enviadas.append(req)
            return mock.MagicMock()
        with mock.patch("urllib.request.urlopen", falso_urlopen):
            alertas._post("Advertencia de helada: 1.5°C", "urgent")
            alertas._post("Sistema Davis iniciado", "low")
        url = enviadas[0].full_url
        self.assertTrue(url.startswith("https://api.telegram.org/bot") and url.endswith("/sendMessage"))
        datos = urllib.parse.parse_qs(enviadas[0].data.decode())
        self.assertEqual(datos["chat_id"], ["987654"])
        self.assertTrue(datos["text"][0].startswith("[davis-monitor] "))
        self.assertIn("helada", datos["text"][0])
        self.assertNotIn("disable_notification", datos)                       # urgente: suena
        self.assertEqual(urllib.parse.parse_qs(enviadas[1].data.decode())["disable_notification"], ["true"])

    def test_el_token_no_sale_en_el_log(self):
        n = alertas.Notificador(reintentos=1)
        with self.assertLogs("DavisMonitor", level=logging.WARNING) as log, \
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(f"falló {TOKEN}")):
            n.enviar("prueba")
            n.cerrar()
        self.assertTrue(log.output)
        self.assertFalse(any(TOKEN in linea for linea in log.output))

    def test_ntfy_ya_no_se_usa(self):
        self.assertFalse(hasattr(config, "WEBHOOK_FORMATO"))


if __name__ == "__main__":
    unittest.main()
