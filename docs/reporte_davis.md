# Reporte técnico: davis-sdr-greenhouse (v2.3 → v3.0, carpeta `davis_v2`)

**Fecha:** 2026-11-03 · **Equipo:** Raspberry Pi (Ubuntu, kernel 6.14 raspi, Python 3.14.4, 4 núcleos, 15 GiB) · **Dongle:** RTL2838 / R820T (`0bda:2838`) · **Receptor:** `rtldavis` 0.15 (fork lheijst) en `/usr/local/bin` · **Estación:** Davis Vantage Pro 2, transmisor ID 0, banda US

**Código analizado:** `~/Escritorio/files/davis_monitor_proyecto` (v2.3.0, 11 módulos, 87 tests) y sus datos reales en `~/capturas_davis` (270 517 tramas entre el 2026-08-26 y el 2026-11-03, 197 h de monitoreo y 1 868 líneas de log).

**Nueva versión:** `~/Escritorio/files/davis_v2` (v3.0.0). Todo lo que genera queda dentro del proyecto, en `davis_v2/datos/` (ver sección 10).

---

## 1. Resumen ejecutivo

| # | Problema | Severidad | Estado en v3 |
|---|---|---|---|
| 1 | **El servicio no arranca nunca**: `User=sibajx`, que no existe en esta máquina (`status=217/USER`), y rutas `/home/sibajx/...`. Lleva **2 201 reinicios** en bucle cada 5 s. | Crítica | Corregido (unidad nueva + instalador) |
| 2 | **Watchdog de 30 s contra rtldavis, que necesita hasta 135 s para sincronizar.** Genera bucles de reinicio: **los 65 huecos de más de 30 s del histórico contienen, todos, un reinicio del watchdog**. Son 143 min perdidos, con una mediana de 126 s por hueco. | Alta | Rediseñado: watchdog en 2 fases basado en paquetes |
| 3 | **ID de transmisor y bit de batería mal leídos** (`(b0>>1)&7` y `b0&1`). El día que la batería del ISS baje, el ID calculado pasa a 4 y **se descartan TODAS las tramas**: el sistema se queda mudo y la alerta de batería no llega nunca. | Crítica (latente) | Corregido (`b0&7`, bit 3) |
| 4 | **El paquete `0xE` no es humedad de hoja: es el contador de lluvia** (vuelcos de la cubeta). La "humedad de hoja = 13" guardada en el 100 % de las 67 633 tramas es un artefacto, y **no existía la lluvia acumulada**. | Alta (datos falsos) | Corregido: lluvia acumulada por minuto y por día |
| 4b | **El dongle RTL-SDR se desconecta del USB**: 62 reenumeraciones hoy, en 2 puertos, sin bajo voltaje. rtldavis se cuelga al perder el USB. | Alta (hardware) | v3 lo detecta, lo reinicia y alerta. **Hay que revisar el cable/conector** |
| 4c | **Tramas basura que pasan el CRC**: 4 tramas en cero (su CRC también es 0) y 4 de tipos que un ISS no emite, con "viento" de 103–290 km/h que contaminaban máximos y gráficas. *(Encontrado al construir el dashboard.)* | Media (datos falsos) | Corregido: se rechazan, filtro de plausibilidad de viento > 241 km/h, histórico re-migrado |
| 5 | **Tasa de lluvia con fórmula incorrecta** (`valor × 0.2`; el dato real es el tiempo entre vuelcos, inversamente proporcional). Daría valores absurdos con la primera lluvia. | Alta (latente) | Corregido |
| 6 | El CSV guardaba **una foto instantánea por minuto**, no promedios. Había min/max solo de temperatura y viento, y la dirección no era vectorial. | Media | Promedios, extremos, dirección vectorial, rocío, VPD y calidad |
| 7 | La base SQLite repetía en cada fila valores de otras tramas (la temperatura aparecía en las filas de humedad) y hacía 1 commit por trama (~1 400/h en la microSD). | Media | Una fila por trama solo con lo medido; commits por lotes (~240/h) |
| 8 | Logs solo en archivo (journalctl vacío), aviso de "apagado" que casi nunca salía, lógica de tareas duplicada entre TUI y headless, efectos secundarios al importar `config` y compresión que buscaba archivos que ya no existen. | Media/Baja | Corregido |

**Resultado medido en hardware:** en dos pruebas en vivo, una vez sincronizada, la v3 recibió el **98.4 % y el 100 %** de las tramas esperadas, con **0 huecos ≥ 30 s** y 0 errores de CRC. La v2.3 promedió **97.8 %** en 9 días, con 65 huecos (143 min perdidos) causados por sus propios reinicios. La ganancia se nota sobre todo en tiempo largo: la v2.3 perdía en promedio un hueco de ~2 min cada 3 h. Detalle en la sección 5.

---

## 2. Por qué falla el servicio

```
$ systemctl status davis-monitor
Active: activating (auto-restart) (Result: exit-code)
Process: ExecStart=/home/sibajx/.local/bin/davis-monitor --headless (code=exited, status=217/USER)

$ journalctl -u davis-monitor
Failed to determine credentials for user 'sibajx': Unknown user
Failed at step USER spawning /home/sibajx/.local/bin/davis-monitor: Invalid argument

$ systemctl show davis-monitor -p NRestarts
NRestarts=2201
```

Causas, en orden. Aunque se corrigiera una, la siguiente también lo tumbaría:

1. **`User=sibajx` / `Group=sibajx`**: ese usuario no existe en esta Pi (el usuario es `alternas`). systemd falla antes de lanzar el programa.
2. **`WorkingDirectory=/home/sibajx/Davis_Hunter/...` y `ExecStart=/home/sibajx/.local/bin/davis-monitor`**: rutas inexistentes.
3. **El paquete no está instalado** para `alternas` (`import davis_monitor` → `ModuleNotFoundError`, tanto en el Python del sistema como en `~/Escritorio/venv`). Además, Python 3.14 de Ubuntu bloquea `pip install` global (PEP 668).
4. `Restart=on-failure` + `RestartSec=5` sin `StartLimit*`: el servicio queda en bucle infinito en lugar de marcar "failed".

**Solución en v3** (`deploy/davis-monitor.service`):
- `User=alternas` y `WorkingDirectory=/home/alternas/Escritorio/files/davis_v2`.
- `ExecStart=/usr/bin/python3 -m davis_monitor --headless`: **no hace falta pip**, porque el paquete solo usa la biblioteca estándar.
- `Type=notify` + `WatchdogSec=120`: el programa le avisa a systemd que está vivo (`sd_notify`, implementado sin dependencias). Si el bucle principal se cuelga, systemd lo reinicia.
- `StartLimitIntervalSec=900` / `StartLimitBurst=10`, `TimeoutStopSec=30` y `KillMode=control-group`.
- Logs también por stderr, así que `journalctl -u davis-monitor` ahora muestra información.
- Endurecimiento: `ProtectSystem=strict`, `ProtectHome=read-only` y escritura solo en `davis_v2/datos/`. Verificado con `systemd-analyze verify` (sin errores).

---

## 3. Errores encontrados en el código (lectura completa)

### 3.1 Protocolo Davis (`nucleo.py`, `decodificador.py`)

Validado decodificando de nuevo las **270 517 tramas reales** guardadas y comparando con referencias del protocolo (DeKay *DavisRFM69*, driver meteostick de weewx):

| Campo | v2.3 | Correcto | Evidencia |
|---|---|---|---|
| ID transmisor | `(b0 >> 1) & 7` | `b0 & 7` | Con ID 0 y batería OK (`b0 = 0x?0`) ambos dan 0, por eso nunca se notó. Con batería baja, `b0 = 0x88` → v2.3 calcula ID **4** → descarta la trama. Test `test_bateria_baja_no_cambia_id`. |
| Batería baja | `b0 & 0x01` | `b0 & 0x08` | El bit 0 es parte del ID: la alerta de batería nunca podía dispararse correctamente. |
| Tipo `0xE` | "LEAF_MOISTURE" → `int(hex[8]+hex[6:8])/10` | **Contador de lluvia**, `b3 & 0x7F`, rueda en 128 | byte3 = `0x80` en las 67 633 tramas y la "hoja" salía siempre **13** (`0x080 = 128 → round(12.8) = 13`). Las sondas de hoja/suelo usan otra estación (tipo `0xF`). |
| Tasa de lluvia `0x5` | `int(b4_alto + b3) × 0.2` | `b3 = 0xFF` → 0; si no, `t = ((b4&0x30)<<4)|b3`, `t/16` s si `b4&0x40 == 0` (lluvia fuerte), y luego `3600/t × cubeta` | El dato es el *tiempo entre vuelcos*. Con la fórmula vieja, una lluvia leve (t grande) daba tasas enormes. |
| Dirección del viento | `b2 × 360/255` | `9 + b2 × 342/255` (VP2, con zona muerta al norte) | Diferencia media de 5.2°, máxima de 9°. `b2 = 0` (5 844 tramas) y `b2 = 255` (4 307) son los extremos de la zona muerta. Es norte válido, no "sin dato". |
| Temperatura | Sin sentinela | `0xFFC` = sensor ausente | Antes se habría registrado una temperatura falsa. |
| Solar | `< 0x3FF` | `< 0x3FE` | 0x3FE también es "sin dato". |
| Temp/Hum/UV/Solar/Viento | — | — | **Coinciden** con v2.3 en las 270 517 tramas (diferencias ≤ 0.05 por redondeo). |

### 3.2 Enlace SDR / watchdog (el problema operativo más costoso)

Observado en la salida real de `rtldavis` (pruebas `-v` en esta Pi):

```
12:44:47.88 Init channels: wait max 135 seconds for a message of each transmitter
                      ← 67 s sin imprimir NADA
12:45:54.12 TRANSMITTER 0 SEEN
```

- rtldavis arranca escuchando **solo el canal 0** (902.419 MHz). La estación salta por 51 canales cada 2.5625 s, así que pasa por el canal 0 **una vez cada ~131 s**, y rtldavis espera como máximo 135 s: **una sola oportunidad por arranque**.
- La v2.3 contaba "segundos sin *líneas*" y reiniciaba a los **30 s**. Solo ~23 % de los arranques podían sincronizar antes de que los matara (30/131). Eso coincide con lo medido: mediana de 126 s por hueco y **212 reinicios en 9 días** (74 solo el 2026-09-04).
- Cruce log ↔ base: **65 de 65** huecos ≥ 30 s contienen un "Watchdog SDR activado".
- Si la ventana de 135 s falla, rtldavis **se queda colgado en el canal 0 para siempre** (prueba 4: detectó un preámbulo a los 49 s, no lo decodificó y no volvió a imprimir nada). Ninguno de los dos extremos sirve: matarlo a los 30 s ni dejarlo indefinidamente.

Medición de arranques hoy (sin watchdog que interrumpa):

| Arranque | Primer paquete |
|---|---|
| Manual 12:44 | 67 s |
| v3 en vivo #1, #2 | nunca (300 s cada uno) |
| v3 en vivo #3 | 26 s |
| `-v` #1, #2, #3 | 64 s, 36 s, 7 s |
| `-v` #4 | nunca |

Prueba A/B de 12 arranques alternados, cortando en el primer paquete o a los 140 s (`-fc 900` = la corrección de frecuencia que el AFC de rtldavis aprende solo, ~+900 Hz, pero que no aplica durante el arranque):

| Argumentos | Sincronizó | Tiempos (s) |
|---|---|---|
| `-tf US -fc 900` | 6/6 | 56, 129, 117, 128, 129, 129 |
| `-tf US` (actual) | 5/6 | 129, FALLO, 129, 129, 129, 129 |

- Muchos arranques sincronizan **a los ~129 s, al final de la ventana de 135 s**. Los arranques consecutivos quedan "en fase" con el ciclo de saltos de 131 s. Por eso cualquier watchdog de sincronización menor de ~135 s corta arranques que iban a lograrlo, y 150 s es el valor correcto.
- `-fc 900` no muestra una diferencia significativa con 12 muestras. Se deja el valor por defecto y se documenta como experimento opcional (`"rtldavis_args": "-tf US -fc 900"`).
- Tasa de falla observada en total: 4 de 20 arranques. **Corrección posterior (ver 3.4): esas fallas coinciden con desconexiones físicas del dongle USB**, no con mala recepción de RF. Con el watchdog de v3, un arranque fallido cuesta como máximo 150 s y no un tiempo indefinido.

### 3.4 El dongle se desconecta del USB (hallazgo de hardware)

El kernel registró **62 reenumeraciones del RTL-SDR (`0bda:2838`) solo el 2026-11-03**, en dos puertos distintos (2-1 y 4-1). En el mismo periodo, el teclado y el mouse se conectaron una sola vez cada uno, y la Pi **no reporta bajo voltaje** (`get_throttled = 0`). Así que no es la fuente ni el bus: es **el dongle, su conector o su cable/extensión** (o lo están moviendo físicamente). A las 15:00 el dongle ya no estaba conectado y en su puerto apareció un teclado.

| Hora de la falla | Desconexiones USB en esa ventana |
|---|---|
| v3 en vivo #1 y #2 (12:55–13:05), "no sincronizó" | 12:55:22, 12:55:58, 12:57:59, 12:59:54, 13:00:17, 13:00:20 |
| `rtldavis -v` #4 (13:34), "colgado" | 13:33:43, 13:35:18–13:38:02 (11 eventos) |

Al perder el USB, rtldavis imprime `cb transfer status: 1, canceling...` y **se queda colgado** sin salir. Si arranca sin dongle, imprime `input/output error` y sale con código 1. La v2.3 no distinguía ninguno de los dos casos. **v3:** reconoce ambos mensajes, mata rtldavis al instante (sin esperar al watchdog), cuenta `errores_usb` (heartbeat y `estado.json`), aplica backoff de hasta 60 s mientras el dongle no esté y manda **una alerta "no puede abrir el dongle"** tras 5 fallos seguidos. Cuando el dongle vuelve, se recupera solo.

**v3:** watchdog **en dos fases**, basado en *paquetes válidos* (no en líneas):
- **Sincronizando:** si en `watchdog_sync_seg = 150` s no llega el primer paquete, se reinicia. Es una ventana completa de 135 s más margen; esperar más es inútil porque rtldavis ya no se recupera.
- **Sincronizado:** si pasan `watchdog_sin_paquetes_seg = 90` s sin paquetes (~35 seguidos perdidos; con 2 % de pérdida aleatoria eso no ocurre por azar), se reinicia. rtldavis tardaría 51 paquetes (~131 s) en notarlo por sí solo.
- Cada sincronización queda medida (`última_sync` en el log, `ultima_sync_seg` en `estado.json`). Esto cubre el pendiente #3 de la documentación de v2.3: "tiempo de resincronización no cronometrado".

### 3.3 Otros defectos de código

| Archivo | Defecto | Impacto |
|---|---|---|
| `nucleo.py` | `select()` sobre un `TextIOWrapper` con buffer + `readline()`: si llegan 2 líneas juntas, la segunda queda en el buffer de Python y `select` no la ve hasta la siguiente escritura. | Líneas `Hop:` retrasadas ~2.5 s. |
| `nucleo.py` | El lock global se mantenía **durante el INSERT + commit SQLite** de cada trama. | La TUI y el loop principal esperaban al disco. |
| `nucleo.py` | `timeout_counter == WATCHDOG` (igualdad exacta) y el contador no se reiniciaba entre procesos. | Avisos perdidos o confusos. |
| `__main__.py` | El handler de señal llamaba `sys.exit()` y `finalizar()` se ejecutaba 2 veces. El aviso "apagado" salía en un hilo *daemon* que moría con el proceso. | La notificación de apagado casi nunca llegaba. |
| `nucleo.py` | Al morir Python con `kill -9`, `rtldavis` quedaba huérfano con el dongle tomado. | El siguiente arranque falla con `usb_claim_interface`. v3: `PR_SET_PDEATHSIG` + `KillMode=control-group`. |
| `config.py` | Importar el módulo creaba carpetas y configuraba logging (por eso los tests ensuciaban el log de producción, el "fix" de 2.3.0 duplicado en cada test). Solo había handler de archivo. | journalctl vacío. Tests frágiles. |
| `tui.py` / `headless.py` | El bloque de tareas periódicas (watchdog, CSV, JSON, heartbeat, compresión, alertas) estaba **copiado** en ambos. | El mismo riesgo de desincronización que el CHANGELOG 2.2 corrigió solo para las alertas. |
| `almacenamiento.py` | Comprime `eventos_davis_*.csv`, que ya no existen desde el cambio a SQLite. `historial_*` no se comprimía por defecto. | La compresión no hacía nada. |
| `almacenamiento.py` | Leía la primera línea del CSV en **cada** escritura. | E/S innecesaria cada minuto. |
| `nucleo.py` | Valores guardados como *strings* ("27.5", "--") y convertidos a float en cada uso. | Código frágil y `_a_float_seguro` por todos lados. |
| `nucleo.py` | `verificar_datos_completos` exigía **todos** los campos, incluida la hoja (que no existe). | Siempre se esperaban los 300 s de timeout para empezar a guardar. |
| Alertas | HELADA sin histéresis: con la temperatura oscilando en 2.0 ↔ 2.1 °C llega una notificación por cada cruce. No había alerta de "estación sin datos". | Spam, o silencio cuando más importa. |
| CSV | Dirección del viento instantánea. El promedio aritmético de 350° y 10° da 180° (S): un error típico. | Datos de viento poco útiles. |

---

## 4. Análisis de los datos históricos (`~/capturas_davis/eventos_davis.db`)

- **270 517 tramas**, 197 h efectivas, **0 % de errores de CRC**, 1 trama de otro ID: la señal llega limpia.
- Distribución por tipo: TEMPERATURE 67 639 · RAIN_RATE 67 633 · RAIN_COUNTER (ex "hoja") 67 633 · HUMIDITY 27 025 · UV 13 544 · SOLAR 13 519 · RÁFAGA 13 516 · 8 desconocidas. Es el patrón normal de un ISS VP2 con sensores UV y solar.
- **Eficiencia de recepción v2.3: 97.8 %** (sesión de 7 días: 230 940 de 236 148 esperadas). Casi toda la pérdida está en los huecos de reinicio (143 min).
- **Lluvia: 0 mm en 9 días.** El contador quedó fijo en `0x80` y la tasa siempre en "seco" (`0xFF`). Puede ser que no haya llovido, pero si llovió en esas fechas, **el pluviómetro está desconectado, tapado o con el cable dañado.** Ver la recomendación 1.
- **Migración:** `python3 -m davis_monitor.migrar` re-decodificó todo el histórico con el protocolo corregido en **16 s** → `datos/db/historico_v23_migrado.db` (11 740 minutos de resumen con promedios, rocío y VPD). La base original se abre en **solo lectura** y no se toca.

Extracto del histórico migrado (tabla `resumen`):

| Día | T mín | T máx | HR prom | VPD mín–máx (kPa) | Ráfaga máx | Radiación máx | Calidad prom |
|---|---|---|---|---|---|---|---|
| 08-29 | 16.8 | 30.3 | 57.6 | 0.35–3.08 | 29.0 | 979 | 99.2 % |
| 09-01 | 17.7 | 30.9 | 73.8 | 0.27–2.75 | 33.8 | 1007 | 98.3 % |
| 09-02 | 16.1 | 32.4 | 61.2 | 0.21–3.45 | 24.1 | 1257 | 98.7 % |
| 09-03 | 16.0 | 33.0 | 55.2 | 0.38–3.66 | 33.8 | 844 | 99.1 % |

(VPD > 1.5 kPa al mediodía casi todos los días: dato útil para el manejo del invernadero.)

---

## 5. Pruebas realizadas

| Prueba | Resultado |
|---|---|
| Suite v2.3 (`unittest`) | 87/87 OK. Pero no detecta ninguno de los errores de protocolo porque los tests validaban contra las mismas fórmulas. |
| Suite v3 (`python3 -m unittest discover -s tests -t .`) | **39/39 OK** (~13 s): CRC, tramas reales de esta estación, batería/ID, sentinelas, lluvia con vuelta de contador, persistencia de la lluvia tras reinicio, histéresis, watchdog por fase, rtldavis falso (lectura, relanzamiento, proceso que ignora SIGTERM, binario inexistente), SQLite por lotes y migración. |
| Re-decodificación de las 270 517 tramas | Coincidencia total en temp/hum/UV/solar/viento. Errores confirmados en hoja, lluvia y dirección. |
| `rtldavis -v` × 4 + manual | Mecanismo de sincronización caracterizado (sección 3.2). |
| **v3 en vivo, 21 min headless** (watchdog de 300 s, primera versión) | Arranque OK. Dos ventanas de sincronización fallidas (rtldavis colgado en el canal 0), **reiniciadas solas por el watchdog**. Tercera: sincronizó en 25.9 s. Después: **246 tramas, 100.0 % de eficiencia, 0 huecos, hueco máximo 2.6 s, 0 CRC**. Calidad del 100 % en cada minuto del CSV. Apagado por SIGTERM en 75 ms, sin rtldavis huérfano, WAL vaciado. |
| **v3 en vivo, 21 min headless (versión final, watchdog en 2 fases)** | Sincronizó **al primer intento** (109 s), **0 reinicios**, 442 tramas, **98.4 % de eficiencia**, **0 huecos** (máximo 5.1 s = 1 trama suelta perdida), 0 CRC. Minutos completos al 96–100 % (el primer minuto parcial, en el que ocurrió la sincronización, da 48 %). Apagado limpio, sin huérfano. |
| Recursos | Python ~35 MB RSS y 0.3 % de CPU. rtldavis ~23 MB y 1.8 % de CPU. Memoria extra de v3: historial de 240 puntos + colas (< 2 MB). |

---

## 6. Qué cambió en v3 (y por qué rinde más)

```
rtldavis ──stdout──▶ [hilo SDR] for linea in stdout ──▶ protocolo.decodificar() ──▶ estado (lock corto)
                                                                  │
                                            cola ──▶ [hilo SQLiteWriter] lotes cada 15 s
[hilo principal] tick() 1/s: watchdog 2 fases · CSV alineado al minuto · estado.json · heartbeat
                              · alertas con histéresis · compresión · sd_notify WATCHDOG=1
                                            cola ──▶ [hilo Webhook] reintentos, se vacía al apagar
```

**Más datos y mejores**
- Watchdog en dos fases: sin bucles de reinicio y recuperación más rápida cuando se pierde la sincronía.
- Lluvia real: contador → mm por trama, por minuto y del día. Persiste entre reinicios (se recalcula desde SQLite al arrancar). Se ignoran los saltos absurdos del contador.
- CSV por minuto **alineado al reloj** (hh:mm:00), con **23 columnas**: temp prom/mín/máx, HR, **punto de rocío**, **VPD**, viento prom/máx, **dirección vectorial**, cardinal, ráfaga máx, lluvia del minuto, lluvia del día, tasa máx, UV prom/máx, radiación prom/máx, batería, paquetes recibidos/esperados y **calidad %**.
- SQLite: tabla `eventos` (una fila por trama, **solo** lo que esa trama midió: sin valores arrastrados) y tabla `resumen` (espejo del CSV, consultable con SQL).
- Edad de cada dato en `estado.json`. Un sensor mudo deja de mostrarse como actual después de 10 min.

**Más robusto**
- Lector bloqueante en su hilo; el lock nunca espera a disco ni a red.
- SQLite con un hilo escritor dueño de la conexión: ~240 commits/h en vez de ~1 400 (menos desgaste de la microSD), `executemany` y `wal_checkpoint` al cerrar.
- Apagado ordenado: se guarda la ventana parcial, se vacían las colas de SQLite y webhook, y rtldavis recibe `terminate` → `kill` si no responde en 5 s.
- `PR_SET_PDEATHSIG`: rtldavis muere si Python muere.
- Alertas: helada con histéresis (+1 °C), tormenta con histéresis, **"sin datos de la estación > 15 min" y "recepción restablecida"**, batería con cooldown, reinicios repetidos.
- `config.py` sin efectos al importar. Todas las claves nuevas son configurables (`config.example.json`).

**Herramientas**
- `--replay archivo.txt`: procesa una captura de rtldavis sin dongle.
- `python3 -m davis_monitor.basedatos calidad --dias N`: eficiencia, huecos y minutos perdidos.
- `python3 -m davis_monitor.migrar`: recupera el histórico de v2.3 con el protocolo corregido.

**Compatibilidad**: mismos comandos (`--headless`, `--visor`, `--test-webhook`, TUI), mismas variables `DAVIS_*` para webhook/umbrales/rtldavis y mismo formato ntfy/json. Cambian el esquema del CSV y de SQLite, por eso v3 escribe en `davis_v2/datos/` y **no mezcla** con los datos viejos (copiados aparte en `datos/legado_v23/`). Si existe un CSV del día con otro encabezado, se escribe a `..._v3.csv` en lugar de corromperlo.

---

## 7. Cómo dejarlo corriendo

```bash
cd ~/Escritorio/files/davis_v2
python3 -m unittest discover -s tests -t .        # 39 tests, ~14 s
sudo bash deploy/instalar_servicio.sh               # detiene el servicio roto, respalda la unidad vieja,
                                                    # instala la nueva, crea datos/ y lo arranca
journalctl -u davis-monitor -f                      # logs en vivo
python3 -m davis_monitor --visor                    # pantalla en vivo, en paralelo al servicio
python3 -m davis_monitor.basedatos calidad --dias 1
```

Notificaciones push: descomenta en la unidad `Environment=DAVIS_WEBHOOK_URL=https://ntfy.sh/<tu-tema>` y `DAVIS_WEBHOOK_FORMATO=ntfy`, o crea `config.json` a partir de `config.example.json`. Después: `sudo systemctl restart davis-monitor`.

> Importante: no abras la TUI (`python3 -m davis_monitor`) mientras el servicio corre: los dos pelean por el dongle. Para mirar, usa `--visor`.

---

## 8. Recomendaciones (fuera del código)

0. **PRIORIDAD: fijar el dongle USB** (sección 3.4). Conéctalo directo a un puerto USB 2.0 de la Pi (o a un hub con alimentación propia) con un cable de extensión corto y de buena calidad, sujeto para que no se mueva. Revisa si el conector del dongle está flojo; si sigue reenumerándose, cambia el dongle. Para verificar: `journalctl -k -f | grep -i usb` no debe mostrar `USB disconnect` durante horas.
1. **Revisar el pluviómetro**: vierte agua lentamente en el embudo. Cada vuelco debe subir `lluvia_hoy_mm` 0.2 mm en `--visor`. Si no sube, revisa el cable y el conector en el ISS. Si es la versión US (0.01"), configura `tamano_cubeta_mm: 0.254`.
2. **Sonda de hoja/suelo**: no hay ninguna transmitiendo (ningún paquete `0xF` válido). Si se quiere ese dato hace falta la estación Leaf & Soil de Davis, que transmite con su propio ID (`-tr`).
3. **Antena**: el arranque depende de recibir **una** trama en el canal 0 (902.4 MHz, borde inferior de la banda). Una antena de 915 MHz bien orientada (1/4 de onda ≈ 8.2 cm, vertical, lejos de la Pi y de USB 3) sube la probabilidad de sincronizar al primer intento y reduce el ~2 % de pérdida en régimen.
4. **Retención**: la base vieja crecía ~5 MB/día (≈ 1.8 GB/año). Con `guardar_tramas_sqlite: false` solo se guarda el resumen por minuto (~0.5 MB/día) si el disco aprieta.
5. Una vez validada la v3 unos días, se puede borrar o archivar `~/capturas_davis` (la migración ya está en `historico_v23_migrado.db`).

## 9. Archivos entregados (`~/Escritorio/files/davis_v2`)

```
davis_monitor/  config.py rutas.py protocolo.py modelos.py nucleo.py basedatos.py almacenamiento.py
                alertas.py headless.py tui.py visor.py migrar.py __main__.py
tests/          test_protocolo.py test_modelos.py test_nucleo.py test_basedatos.py base.py
deploy/         davis-monitor.service instalar_servicio.sh 99-rtlsdr.rules
docs/           reporte_davis.md
datos/          (generado; ver sección 10)
config.example.json  pyproject.toml  README.md  .gitignore
```

El proyecto original (`~/Escritorio/files/davis_monitor_proyecto`) y sus datos (`~/capturas_davis`) **no fueron modificados**.

## 10. Organización de los resultados

Todo lo que genera el programa queda **dentro del proyecto**, en `datos/`. Ningún módulo arma rutas por su cuenta: todas salen de `davis_monitor/rutas.py`, así que el lugar de cada archivo no depende del directorio desde donde se lance el programa.

```
davis_v2/datos/
├── csv/2026/11/historial_davis_2026-11-03.csv   1 fila/min; un archivo por día, carpetas por año/mes
│                                                 (los de > 7 días se comprimen a .csv.gz en su carpeta)
├── db/eventos_davis.db                          SQLite activa (tramas + resumen)
├── db/historico_v23_migrado.db                  histórico v2.3 re-decodificado
├── logs/davis_monitor.log (.1 … .5)             log rotativo, máx. 30 MB
├── estado/estado.json                           estado en vivo (visor/dashboards)
├── exportaciones/                               CSV de `basedatos exportar`
└── legado_v23/                                  copia intacta de ~/capturas_davis
```

- **Por qué así:** cada tipo de dato en su carpeta. Los CSV diarios agrupados por mes evitan carpetas con cientos de archivos (en un año serían 365). Las bases quedan separadas de los textos, y para respaldar basta copiar `datos/`.
- El servicio systemd solo tiene permiso de escritura en `datos/`: el código no se puede modificar por accidente desde el programa.
- `datos/` está en `.gitignore` (salvo `datos/LEEME.md`, que explica cada carpeta), así que los resultados no se suben al repositorio de GitHub.
- Para mover los datos a otro disco: `"dir_datos": "/media/usb/davis"` en `config.json`. Se mantiene la misma estructura interna.
- Los datos de las pruebas de hoy se movieron de `~/capturas_davis_v2` (carpeta eliminada) a esta estructura. `~/capturas_davis` (v2.3) se **copió** a `legado_v23/` sin tocar el original.
