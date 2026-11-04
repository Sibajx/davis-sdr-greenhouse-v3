# Changelog

## 3.2.0 — 2026-11-03 · davis_web 1.2.0

### Agregado
- **Monitor de recepción y CRC** (pestaña *Recepción*, `davis crc`, tabla `enlace`):
  - Contabilidad **por ranura de transmisión**. La estación transmite cada 2.5625 s exactos, así que cada transmisión esperada es *válida*, *dañada* (hubo una trama con CRC fallido en su ranura) o *no llegó*. Las tres suman el 100 %.
  - Efectividad de decodificación = válidas ÷ (válidas + dañadas).
  - Los CRC fallidos fuera de ranura (ruido que imita una trama) y las copias dañadas de una transmisión que sí llegó válida se cuentan como *descartes correctos*.
  - También se registran la cobertura del periodo, los errores USB, los reinicios y el desvío de frecuencia por minuto.
- Parche para rtldavis (`deploy/rtldavis/`, GPL-3.0): reporta cada trama que descarta por CRC (`CRC ERROR <hex> ok=N`). Sin el parche, rtldavis las descarta en silencio y el contador de CRC de v2.3/v3.1 siempre marcaba 0.

### Mejorado (dashboard)
- Navegación más clara: pestañas con icono, una línea de ayuda por sección y pestañas privadas que explican qué contienen.
- Tarjetas "en vivo" sin huecos (brújula integrada), "toca para ampliar", hace cuánto llegó la última trama y explicación de VPD y UV.
- Histórico: variables agrupadas por tema y consulta automática al cambiar las fechas.
- Móvil: las pestañas ya no se cortan y las leyendas no se enciman con las gráficas.

### Seguridad / privacidad
- **PNG solo con sesión.** La imagen lleva al pie quién la descargó y cuándo.
- Medidas disuasorias contra capturas: marca de agua (con el usuario si hay sesión), impresión deshabilitada, se borra el portapapeles con ImprPant, sin menú contextual sobre las gráficas, y las secciones privadas se ocultan cuando la ventana pierde el foco. Un sitio web **no puede** impedir una captura del sistema operativo o una foto con otro dispositivo.

## 3.1.0 — 2026-11-03

### Agregado
- Respaldos automáticos diarios en caliente (`davis_monitor.respaldo`): verificados con integrity_check + SHA-256, retención de 7 diarios + 4 semanales + 12 mensuales, copia opcional a un disco extra, `davis respaldo [listar|verificar]`.

### Mejorado (dashboard, ahora público)
- Agregación de series **dentro de SQLite** (GROUP BY): 40 consultas pesadas simultáneas pasaron de 50 s / 369 MB a 0.3 s / 48 MB.
- Caché de respuestas (20 s), máximo 3 consultas pesadas simultáneas (503 si se satura), límite por IP (429), tope de 6 000 puntos por serie.
- gzip (ECharts: 1 MB → 333 KB), ETag/304, HSTS, `robots.txt`, 404 real en `/api/*`.

### Corregido
- Tarjetas de VPD y Enlace SDR que parpadeaban como "dato viejo" cada 5 s (`classList.toggle` con `undefined`).

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
