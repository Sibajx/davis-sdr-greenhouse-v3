import unittest
from tests.base import trama
from davis_monitor import protocolo as p

# Tramas REALES capturadas en esta estación (2026-11-03) con rtldavis -tf US
REALES = {
    "8005813339007F03": ("TEMPERATURE", "temp", 27.75),
    "A002D5B81B00393A": ("HUMIDITY", "humedad", 44.0),
    "9002A708032978C5": ("VIENTO_RAFAGA", "rafaga", 12.9),
    "40048A680100577E": ("UV_INDEX", "uv", 8.32),
    "60047A600100549A": ("SOLAR_RAD", "solar", 675.0),
    "500296FF73004778": ("RAIN_RATE", "lluvia_tasa", 0.0),
    "E0029C8001004E7D": ("RAIN_COUNTER", "lluvia_contador", 0.0),
}


class TestCRC(unittest.TestCase):
    def test_reales_validas(self):
        for h in REALES:
            self.assertTrue(p.validar_crc(bytes.fromhex(h)), h)

    def test_bit_invertido_falla(self):
        d = bytearray(bytes.fromhex("8005813339007F03"))
        d[3] ^= 0x01
        self.assertFalse(p.validar_crc(bytes(d)))

    def test_corta(self):
        self.assertFalse(p.validar_crc(b"\x80\x05"))


class TestDecodificar(unittest.TestCase):
    def test_reales(self):
        for h, (tipo, campo, esperado) in REALES.items():
            l = p.decodificar(bytes.fromhex(h))
            self.assertEqual(l.tipo, tipo, h)
            self.assertAlmostEqual(l.valores[campo], esperado, places=2, msg=h)
            self.assertEqual(l.tx_id, 0)
            self.assertFalse(l.bateria_baja)

    def test_bateria_baja_no_cambia_id(self):
        # Bug v2.3: con bit de batería (0x08) el ID se calculaba como 4
        l = p.decodificar(trama(0x88, 5, 0x81, 0x33, 0x39))
        self.assertEqual(l.tx_id, 0)
        self.assertTrue(l.bateria_baja)

    def test_ids(self):
        for tx in range(8):
            self.assertEqual(p.decodificar(trama(0x80 | tx, 0, 0, 0x33, 0x39)).tx_id, tx)

    def test_temp_negativa_y_sentinela(self):
        # -10 °C = 14 °F -> raw = 140*16 = 0x08C0
        self.assertAlmostEqual(p.decodificar(trama(0x80, 0, 0, 0x08, 0xC0)).valores["temp"], -10.0, 1)
        # -40 °F (raw negativo)
        raw = (-400 * 16) & 0xFFFF
        self.assertAlmostEqual(p.decodificar(trama(0x80, 0, 0, raw >> 8, raw & 0xFF)).valores["temp"], -40.0, 1)
        self.assertNotIn("temp", p.decodificar(trama(0x80, 0, 0, 0xFF, 0xC0)).valores)

    def test_tasa_lluvia(self):
        # Lluvia ligera: bit 0x40, 100 s entre vuelcos -> 3600/100*0.2 = 7.2 mm/h
        self.assertAlmostEqual(p.decodificar(trama(0x50, 0, 0, 100, 0x41)).valores["lluvia_tasa"], 7.2)
        # Fuerte: sin bit 0x40, 160/16 = 10 s -> 72 mm/h
        self.assertAlmostEqual(p.decodificar(trama(0x50, 0, 0, 160, 0x01)).valores["lluvia_tasa"], 72.0)
        self.assertEqual(p.decodificar(trama(0x50, 0, 0, 0xFF, 0x71)).valores["lluvia_tasa"], 0.0)

    def test_contador_lluvia_7bits(self):
        self.assertEqual(p.decodificar(trama(0xE0, 0, 0, 0x85, 0x01)).valores["lluvia_contador"], 5)

    def test_uv_solar_sentinela(self):
        self.assertNotIn("uv", p.decodificar(trama(0x40, 0, 0, 0xFF, 0xC0)).valores)
        self.assertNotIn("solar", p.decodificar(trama(0x60, 0, 0, 0xFF, 0xC0)).valores)

    def test_direccion(self):
        self.assertEqual(p.decodificar(trama(0x80, 0, 0, 0x33, 0x39)).valores["viento_dir"], 9)
        self.assertEqual(p.decodificar(trama(0x80, 0, 255, 0x33, 0x39)).valores["viento_dir"], 351)
        self.assertEqual(p.grados_a_cardinal(359), "N")
        self.assertEqual(p.grados_a_cardinal(None), "--")

    def test_basura_que_pasa_el_crc(self):
        # Casos REALES del histórico (CRC válido, contenido imposible)
        for h in ("0000000000000000", "106D431E8DB3757C", "C04039E400462435"):
            self.assertTrue(p.validar_crc(bytes.fromhex(h)), h)
            self.assertIsNone(p.decodificar(bytes.fromhex(h)), h)
        for h in ("70B45310C8AAC47D", "F0AC899DDED3E9E2"):   # tipo válido, viento 289/277 km/h
            l = p.decodificar(bytes.fromhex(h))
            self.assertIsNone(l.valores["viento_vel"], h)
            self.assertIsNone(l.valores["viento_dir"], h)
        self.assertEqual(p.decodificar(trama(0x80, 150, 0, 0x33, 0x39)).valores["viento_vel"], 241.4)


class TestLineas(unittest.TestCase):
    def test_clasificar(self):
        self.assertEqual(p.clasificar_linea("12:46:04.374871 8005813339007F03 5 0 0 0 0 msg.ID=0")[0], "paquete")
        c, d = p.clasificar_linea("12:46:04.3749 Hop: {ChannelIdx:47 ChannelFreq:926001611 FreqError:-12 Transmitter:0}")
        self.assertEqual((c, d), ("hop", (47, 926001611, -12)))
        self.assertEqual(p.clasificar_linea("12:44:47.88 Init channels: wait max 135 seconds")[0], "init")
        self.assertEqual(p.clasificar_linea("12:45:54.12 TRANSMITTER 0 SEEN")[0], "sync")
        self.assertEqual(p.clasificar_linea("12:44:47.31 rtldavis.go VERSION=0.15")[0], "otro")
        self.assertEqual(p.clasificar_linea("usb_claim_interface error -6")[0], "usb_error")
        self.assertEqual(p.clasificar_linea("13:35:03.7 cb transfer status: 1, canceling...")[0], "usb_error")
        self.assertEqual(p.clasificar_linea("15:10:52.674135 input/output error")[0], "usb_error")


class TestDerivados(unittest.TestCase):
    def test_rocio_vpd(self):
        self.assertAlmostEqual(p.punto_rocio(25.0, 50.0), 13.9, 0)
        self.assertAlmostEqual(p.vpd_kpa(25.0, 50.0), 1.584, 2)
        self.assertIsNone(p.punto_rocio(None, 50))


if __name__ == "__main__":
    unittest.main()
