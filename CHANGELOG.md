# Changelog

Continúa el historial de [davis-sdr-greenhouse](https://github.com/Sibajx/davis-sdr-greenhouse) (versiones ≤ 2.3.0, la del artículo).

## 3.0.0 — 2026-11-03 · davis_web 1.0.0

### Corregido (protocolo)
- ID del transmisor = `b0 & 7` y batería baja = bit 3. En v2.3 eran `(b0>>1)&7` y bit 0: con batería baja se descartaban todas las tramas.
- El paquete `0xE` es el **contador de lluvia** (7 bits, rueda en 128), no humedad de hoja. El "13" de hoja de v2.3 era un artefacto.
- Tasa de lluvia (`0x5`): se calcula a partir del tiempo entre vuelcos, no con `valor × 0.2`.
- Dirección del viento con la zona muerta de la VP2 (`9 + b2·342/255`). Se agregaron los sentinelas de "sin dato" (temperatura `0xFFC`, UV y radiación `0x3FF`).
- Se rechazan las tramas que pasan el CRC con contenido imposible: 8 bytes en cero y tipos que un ISS no emite. Filtro de viento > 150 mph.

### Corregido (operación)
- Servicio systemd: usuario inexistente (`status=217/USER`) y dependencia de `pip install`.
- Watchdog en dos fases: 150 s durante la sincronización y 90 s ya sincronizado. Antes era "30 s sin líneas" y mataba a rtldavis durante su ventana de sincronización de 135 s.
- Detección inmediata de la pérdida de USB (rtldavis se cuelga con `cb transfer status`).
- Apagado ordenado: la notificación de apagado se envía de verdad, y rtldavis no queda huérfano (`PR_SET_PDEATHSIG`).
- Los logs también van a journalctl. `config` ya no tiene efectos secundarios al importarse.

### Agregado
- CSV por minuto alineado al reloj: promedios y extremos, punto de rocío, VPD, dirección vectorial, lluvia del minuto y del día, y calidad de recepción.
- SQLite con hilo escritor y lotes cada 15 s, y tabla `resumen` además de `eventos`.
- `pluviometro_activo`: con el pluviómetro dañado, la lluvia se guarda vacía en vez de "0 mm".
- Toda la salida va a `datos/` (CSV organizados por año y mes), con compresión automática.
- `migrar`: re-decodifica la base de v2.3 con el protocolo corregido.
- **davis_web**: dashboard en vivo e histórico, promediado tipo consola Davis (método del paper e integral), consultor de datos (CSV, TXT, PNG), cuentas y roles, bitácora y túnel de Cloudflare.
- Lanzador `davis` (+ acceso directo de escritorio): arranca los servicios y abre la TUI conectada al servicio, sin usar el dongle.
- 55 pruebas automáticas y CI en GitHub Actions.
