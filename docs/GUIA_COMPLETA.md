# davis_monitor v3 — Guía completa: comandos y funcionamiento del sistema

*Estación meteorológica Davis Vantage Pro 2 → RTL-SDR (NooElec) → rtldavis → davis_monitor.
Proyecto en `~/Escritorio/files/davis_v2`. Versión 3.0.0, noviembre de 2026.*

Este documento reemplaza a `INSTALACION.md`, `README_MONITOR.md` y `DOCUMENTACION_TECNICA.md` de la v2.3 y retoma lo que sigue siendo válido de ellos. El análisis de errores y la comparación de resultados están en `docs/reporte_davis.md`.

## Índice

1. [Qué es y cómo está armado](#1-qué-es-y-cómo-está-armado)
2. [Estructura de la carpeta](#2-estructura-de-la-carpeta)
3. [Checklist: dejarlo corriendo como servicio](#3-checklist-dejarlo-corriendo-como-servicio)
4. [Guía de comandos](#4-guía-de-comandos)
5. [Configuración](#5-configuración)
6. [Los datos que produce](#6-los-datos-que-produce)
7. [Cómo funciona por dentro](#7-cómo-funciona-por-dentro)
8. [El protocolo Davis](#8-el-protocolo-davis)
9. [Alertas](#9-alertas)
10. [Solución de problemas](#10-solución-de-problemas)
11. [Instalación desde cero (otra máquina)](#11-instalación-desde-cero-otra-máquina)
12. [Estado actual del hardware y pendientes](#12-estado-actual-del-hardware-y-pendientes)
13. [Dashboard web](#13-dashboard-web)
14. [Respaldos](#14-respaldos)

---

## 1. Qué es y cómo está armado

`davis_monitor` captura la telemetría de una estación **Davis Vantage Pro 2** escuchando directamente su radio (915 MHz, banda US/México), sin la consola de Davis. Lo hace con un dongle RTL-SDR de bajo costo, para investigación en agricultura e invernadero.

```
 Estación Davis (ISS)  ── radio 902-928 MHz, salta por 51 canales, 1 paquete cada 2.5625 s
        │
        ▼
 Dongle RTL-SDR (NooElec, chip RTL2832U + R820T, USB 0bda:2838)
        │  USB
        ▼
 rtldavis  (programa externo en Go, fork lheijst)  — demodula la radio y sigue los saltos
        │  texto por stdout:  "15:20:01.123 8005813339007F03 5 0 0 0 0 msg.ID=0"
        ▼
 davis_monitor  (este proyecto, Python, solo biblioteca estándar)
        │   valida CRC → decodifica el protocolo → agrega por minuto → guarda → alerta
        ├──▶ datos/csv/AAAA/MM/historial_davis_AAAA-MM-DD.csv   (1 fila/minuto, para Excel/paper)
        ├──▶ datos/db/eventos_davis.db                          (SQLite: cada trama + resúmenes)
        ├──▶ datos/estado/estado.json                           (estado en vivo, cada 5 s)
        ├──▶ datos/logs/davis_monitor.log                       (log + journalctl)
        └──▶ notificaciones push (ntfy) — opcional
```

**Reparto del trabajo:** `rtldavis` resuelve la parte de radio (demodulación FSK y seguimiento del salto de frecuencia). `davis_monitor` empieza donde `rtldavis` termina, en el texto hexadecimal: entiende qué significa cada byte, valida, agrega, guarda, vigila que el enlace siga vivo y avisa.

**Modos de uso:**

| Modo | Para qué |
|---|---|
| Servicio (`--headless` vía systemd) | Operación normal, desatendida, arranca sola con la Pi |
| Interfaz de texto (TUI) | Ver todo en pantalla. **No** se usa con el servicio corriendo (pelean por el dongle) |
| Visor (`--visor`) | Ver el estado en vivo **mientras corre el servicio** (solo lee `estado.json`) |

---

## 2. Estructura de la carpeta

```
davis_v2/
├── davis_monitor/            código
│   ├── __main__.py           punto de entrada y opciones de línea de comandos
│   ├── config.py             toda la configuración (archivo / variables de entorno / defaults)
│   ├── rutas.py              dónde se guarda cada cosa (único lugar)
│   ├── protocolo.py          CRC, decodificación Davis, lectura de líneas de rtldavis, rocío/VPD
│   ├── modelos.py            estado en memoria, agregador por minuto, tendencias
│   ├── nucleo.py             el corazón: lanza/vigila rtldavis, procesa tramas, tareas periódicas
│   ├── basedatos.py          SQLite (escritura por lotes) + comandos de consulta/exportación
│   ├── almacenamiento.py     CSV diario + compresión de CSV viejos
│   ├── alertas.py            notificaciones (ntfy / json) con cola y reintentos
│   ├── headless.py           bucle sin pantalla (servicio)
│   ├── tui.py                interfaz de texto (curses)
│   ├── visor.py              visor de solo lectura
│   └── migrar.py             re-decodifica la base de la v2.3 con el protocolo corregido
├── davis_web/                dashboard web (sección 13)
├── tests/                    55 pruebas automáticas
├── deploy/
│   ├── davis-monitor.service unidad systemd
│   ├── instalar_servicio.sh  instalador (sudo)
│   └── 99-rtlsdr.rules       permisos del dongle (udev)
├── docs/
│   ├── GUIA_COMPLETA.md      este documento
│   └── reporte_davis.md      análisis de errores v2.3 → v3 y pruebas en hardware
├── datos/                    TODO lo generado (ver sección 6 y datos/LEEME.md)
├── config.json               configuración ACTIVA de esta estación
├── config.example.json       todas las claves posibles, con sus valores por defecto
├── README.md
└── pyproject.toml
```

**Regla de oro:** el código va en la raíz y los resultados en `datos/`. Para respaldar los datos, se copia `datos/`.

---

## 3. Checklist: dejarlo corriendo como servicio

Hazlo en este orden. Cada paso confirma una capa antes de pasar a la siguiente.

```bash
cd ~/Escritorio/files/davis_v2

# 1) El dongle está conectado y NO se está desconectando
lsusb | grep 0bda                       # debe aparecer: 0bda:2838 Realtek ... RTL2838
journalctl -k --since "-5min" | grep -c "USB disconnect"   # ideal: 0

# 2) Nadie más está usando el dongle (TUI abierta, pruebas, etc.)
pgrep -a rtldavis || echo "libre"

# 3) Las pruebas pasan (13 s)
python3 -m unittest discover -s tests -t .

# 4) Instalar y arrancar el servicio (detiene el viejo roto, respalda su unidad, arranca el nuevo)
sudo bash deploy/instalar_servicio.sh

# 5) Confirmar que sincroniza (entre 10 s y 2.5 min)
journalctl -u davis-monitor -f          # esperar: "rtldavis sincronizado: primer paquete a los N s"
#    Ctrl+C para dejar de mirar (el servicio sigue)

# 6) Ver los datos en vivo
python3 -m davis_monitor --visor
```

Después de eso, **ya no hace falta hacer nada más**: el servicio arranca solo si la Pi se reinicia, se recupera solo si rtldavis se cuelga o el dongle se desconecta y vuelve, y systemd lo reinicia si el programa se traba.

**Al volver, para revisar cómo fue la corrida:**

```bash
systemctl status davis-monitor                      # debe decir "active (running)"
python3 -m davis_monitor.basedatos calidad --dias 1 # eficiencia de recepción y huecos
journalctl -u davis-monitor --since today | grep -E "HEARTBEAT|Watchdog|USB|ALERTA"
ls datos/csv/2026/11/                               # un CSV por día
```

---

## 4. Guía de comandos

*Todos los comandos se corren desde `~/Escritorio/files/davis_v2`, salvo los de `systemctl`/`journalctl`, que funcionan desde cualquier lado.*

### 4.0 El lanzador `davis` (lo más cómodo)

Ícono **"Estación Davis"** en el Escritorio, o en una terminal:

| Comando | Qué hace |
|---|---|
| `davis` | Arranca los servicios que estén detenidos (pide contraseña si hace falta) y abre la **TUI conectada al servicio**. `q` sale y **el servicio sigue capturando** |
| `davis estado` | Estado de los 3 servicios, recepción actual y desconexiones USB de hoy |
| `davis iniciar` / `detener` / `reiniciar` | Los tres servicios a la vez (receptor, dashboard, túnel) |
| `davis logs` | Log del receptor en vivo |
| `davis visor` | Visor compacto |
| `davis calidad [días]` | Eficiencia de recepción |
| `davis web` | Abre el dashboard |
| `davis tui-directa` | TUI clásica usando el dongle: detiene el servicio y lo reanuda al salir |

> La TUI de `davis` **no abre el dongle**: lee lo que publica el servicio (`estado.json` cada 5 s y la tabla `resumen` para las tendencias), así que nunca pelea con él. En el título aparece `MODO SERVICIO`. La trama cruda y el contador de lluvia solo se ven en `tui-directa`.

### 4.1 Servicio (systemd)

| Comando | Qué hace |
|---|---|
| `sudo bash deploy/instalar_servicio.sh` | Instala o actualiza el servicio y lo arranca (también después de cambiar la unidad) |
| `systemctl status davis-monitor` | Estado: `active (running)` = bien. Muestra las últimas líneas del log |
| `sudo systemctl stop davis-monitor` | Detener (p. ej. para usar la TUI o mover el dongle) |
| `sudo systemctl start davis-monitor` | Arrancar |
| `sudo systemctl restart davis-monitor` | Reiniciar (necesario después de editar `config.json`) |
| `sudo systemctl disable davis-monitor` | Que **no** arranque solo al encender la Pi |
| `sudo systemctl enable davis-monitor` | Que arranque solo al encender (lo deja así el instalador) |
| `journalctl -u davis-monitor -f` | Log en vivo (Ctrl+C para salir) |
| `journalctl -u davis-monitor --since today` | Log de hoy |
| `journalctl -u davis-monitor --since "2026-11-03 18:00" --until "2026-11-04 08:00"` | Log de un rango |
| `journalctl -u davis-monitor -p warning --since today` | Solo advertencias y errores |
| `systemctl show davis-monitor -p NRestarts` | Cuántas veces lo reinició systemd (debería ser 0 o muy bajo) |

### 4.2 El programa

| Comando | Qué hace |
|---|---|
| `python3 -m davis_monitor` | Interfaz de texto. `q` para salir. Terminal de 90×24 como mínimo. **Detén el servicio antes** |
| `python3 -m davis_monitor --headless` | Igual que el servicio, pero en tu terminal (Ctrl+C para detener) |
| `python3 -m davis_monitor --visor` | Pantalla en vivo de solo lectura. Se puede usar **con el servicio corriendo** |
| `python3 -m davis_monitor --test-webhook` | Manda una notificación de prueba al celular |
| `python3 -m davis_monitor --replay archivo.txt` | Procesa una salida guardada de rtldavis, sin dongle |
| `python3 -m davis_monitor --version` | Versión |
| `python3 -m unittest discover -s tests -t .` | Corre las 55 pruebas |

### 4.3 Consultar y exportar datos

| Comando | Qué hace |
|---|---|
| `python3 -m davis_monitor.basedatos resumen` | Total de tramas, rango de fechas, conteo por tipo de paquete, lluvia total |
| `python3 -m davis_monitor.basedatos calidad --dias 1` | **Eficiencia de recepción** (% de tramas esperadas que llegaron), huecos > 30 s, minutos perdidos |
| `python3 -m davis_monitor.basedatos exportar --tabla resumen --desde 2026-11-03` | Resumen por minuto → `datos/exportaciones/resumen_2026-11-03_fin.csv` |
| `python3 -m davis_monitor.basedatos exportar --desde "2026-11-03 18:00" --hasta "2026-11-04 06:00" --salida noche.csv` | Tramas crudas de un rango → `datos/exportaciones/noche.csv` |
| `python3 -m davis_monitor.basedatos exportar --tipo TEMPERATURE --salida temp.csv` | Solo un tipo de paquete |
| `python3 -m davis_monitor.basedatos --db datos/db/historico_v23_migrado.db resumen` | Lo mismo, pero sobre el histórico de agosto/septiembre |
| `python3 -m davis_monitor.migrar` | Re-decodifica la base de la v2.3 (ya está hecho; solo si se borra el resultado) |

Consultas SQL directas (si `sqlite3` no está instalado: `sudo apt install sqlite3`):

```bash
# Promedios por hora de hoy
sqlite3 -header -column datos/db/eventos_davis.db "
  SELECT substr(timestamp,1,13) AS hora, ROUND(AVG(temp_c),1) temp, ROUND(AVG(humedad_pct),1) hr,
         ROUND(AVG(vpd_kpa),2) vpd, ROUND(MAX(rafaga_max_kmh),1) rafaga, ROUND(AVG(calidad_pct),1) calidad
  FROM resumen WHERE timestamp >= date('now','localtime') GROUP BY hora;"

# Mínima y máxima por día
sqlite3 -header -column datos/db/eventos_davis.db "
  SELECT date(timestamp) dia, MIN(temp_min_c) tmin, MAX(temp_max_c) tmax, MAX(radiacion_max_w_m2) rad
  FROM resumen GROUP BY dia;"

# Respaldo en caliente (con el servicio corriendo)
sqlite3 datos/db/eventos_davis.db ".backup datos/exportaciones/respaldo_$(date +%F).db"
```

En Python o pandas:

```python
import pandas as pd, sqlite3
df = pd.read_sql("SELECT * FROM resumen", sqlite3.connect("datos/db/eventos_davis.db"), parse_dates=["timestamp"])
# o directo los CSV:  pd.read_csv("datos/csv/2026/11/historial_davis_2026-11-03.csv")
```

### 4.4 Diagnóstico del hardware

| Comando | Qué revisa |
|---|---|
| `lsusb \| grep 0bda` | El dongle está conectado y el sistema lo reconoce |
| `journalctl -k -f \| grep -i usb` | Conexiones y desconexiones del USB en vivo (no debe aparecer `USB disconnect` si no lo tocas) |
| `journalctl -k --since today \| grep -c "USB disconnect"` | Cuántas desconexiones hubo hoy |
| `journalctl -k --since today \| grep "error -71"` | Fallas eléctricas o de señal en el conector o cable |
| `pgrep -a rtldavis` | Si hay un rtldavis corriendo (y quién lo lanzó) |
| `sudo systemctl stop davis-monitor && rtldavis -tf US` | rtldavis solo, sin el programa. Debe imprimir `Init channels...` y, en ≤ 135 s, `TRANSMITTER 0 SEEN` y líneas hexadecimales. Ctrl+C para salir |
| `rtldavis -tf US -v` | Lo mismo, con detalle de la demodulación |
| `lsmod \| grep dvb` | No debe mostrar nada (el driver de TV no debe adueñarse del dongle) |
| `cat /sys/devices/platform/soc*/soc*:firmware/get_throttled` | `0` = la Pi nunca tuvo bajo voltaje |

---

## 5. Configuración

**Precedencia:** variable de entorno `DAVIS_<CLAVE>` > `config.json` del proyecto > valor por defecto. Después de cambiar algo: `sudo systemctl restart davis-monitor`. Al arrancar, el log muestra la configuración efectiva (línea `Configuración: {...}`).

`config.json` **activo** hoy:

```json
{ "pluviometro_activo": false }
```

Todas las claves (copiables de `config.example.json`):

| Clave | Por defecto | Qué controla |
|---|---|---|
| `dir_datos` | `datos` | Carpeta de resultados (relativa = dentro del proyecto; absoluta = p. ej. un disco USB) |
| `log_level` | `INFO` | `DEBUG` muestra cada línea de rtldavis (útil para diagnóstico, llena el log) |
| `tx_id_objetivo` | `0` | ID del transmisor (0-7). En la consola Davis aparece como ID+1 |
| `pluviometro_activo` | `true` | `false` = lluvia se guarda **vacía** (no como 0 mm). **Hoy en `false`: pluviómetro dañado** |
| `tamano_cubeta_mm` | `0.2` | Cubeta del pluviómetro: 0.2 mm (métrico) o 0.254 (versión US 0.01") |
| `campos_requeridos` | `temp,humedad,viento_vel` | Qué debe haber llegado antes de escribir el primer minuto (máx. 5 min de espera) |
| `max_edad_dato_seg` | `600` | Un sensor que no reporta en 10 min deja de mostrarse como actual |
| `rtldavis_cmd` / `rtldavis_args` | `rtldavis` / `-tf US` | Comando del receptor. Prueba opcional: `-tf US -fc 900` |
| `watchdog_sync_seg` | `150` | Si rtldavis no sincroniza en este tiempo, se reinicia (ver 7.3) |
| `watchdog_sin_paquetes_seg` | `90` | Ya sincronizado: sin paquetes este tiempo = sincronía perdida → reinicio |
| `fallo_rapido_seg`, `backoff_inicial_seg`, `backoff_max_seg` | `10`, `2`, `60` | Espera creciente entre reintentos si rtldavis muere al instante (dongle ausente) |
| `intervalo_csv_seg` | `60` | Duración de cada fila del CSV / resumen |
| `intervalo_commit_seg` | `15` | Cada cuánto se escribe el lote a SQLite |
| `intervalo_estado_json_seg` | `5` | Frecuencia de `estado.json` |
| `intervalo_heartbeat_seg` | `900` | Línea `HEARTBEAT` en el log (cada 15 min) |
| `guardar_tramas_sqlite` | `true` | `false` = solo resúmenes (ahorra ~4.5 MB/día) |
| `dias_retener_sin_comprimir` / `comprimir_historial_resumen` | `7` / `true` | CSV de más de 7 días → `.csv.gz` |
| `webhook_url` / `webhook_formato` | `""` / `json` | Notificaciones. Para el celular: `https://ntfy.sh/<tema>` + `ntfy` |
| `umbral_helada_c` / `histeresis_helada_c` | `2.0` / `1.0` | Alerta de helada (≤ 2 °C) y fin de helada (> 3 °C) |
| `umbral_tormenta_mm_h` | `25` | Alerta de tormenta (sin efecto con pluviómetro inactivo) |
| `alerta_sin_datos_seg` | `900` | Avisar si pasan 15 min sin datos de la estación |
| `cooldown_alerta_bateria_seg` | `1800` | Mínimo entre avisos de batería |

Activar notificaciones al celular (app **ntfy**, gratis): suscríbete a un tema con un nombre difícil de adivinar y agrega en `config.json`:

```json
{ "pluviometro_activo": false, "webhook_url": "https://ntfy.sh/greenhouse-xyz123", "webhook_formato": "ntfy" }
```

Luego: `python3 -m davis_monitor --test-webhook` y `sudo systemctl restart davis-monitor`.

---

## 6. Los datos que produce

```
datos/
├── csv/AAAA/MM/historial_davis_AAAA-MM-DD.csv   un archivo por día; > 7 días → .csv.gz
├── db/eventos_davis.db                          base activa
├── db/historico_v23_migrado.db                  agosto-noviembre 2026 (v2.3, re-decodificado)
├── logs/davis_monitor.log (.1 … .5)             máx. 30 MB, rota solo
├── estado/estado.json                           estado en vivo
├── exportaciones/                               lo que exportes
└── legado_v23/                                  copia intacta de los datos de la v2.3
```

### 6.1 CSV por minuto (`historial_davis_AAAA-MM-DD.csv`)

Una fila al cierre de cada minuto de reloj (hh:mm:00) con lo ocurrido **durante** ese minuto (~23 tramas):

| Columna | Significado |
|---|---|
| `Fecha_Hora` | Fin del minuto (hora local) |
| `Temp_C`, `Temp_Min_C`, `Temp_Max_C` | Temperatura promedio, mínima y máxima del minuto |
| `Humedad_%` | Humedad relativa promedio |
| `Punto_Rocio_C` | Punto de rocío (Magnus). Si la hoja/superficie baja de este valor hay condensación (riesgo de hongos) |
| `VPD_kPa` | Déficit de presión de vapor. Guía de invernadero: ~0.4–0.8 propagación, ~0.8–1.2 crecimiento, > 1.5 estrés hídrico |
| `Viento_Prom_kmh`, `Viento_Max_kmh` | Velocidad promedio y máxima del minuto |
| `Direccion_Prom_Grados`, `Cardinal` | Dirección **vectorial** ponderada por velocidad (vacía si hubo calma) |
| `Rafaga_Max_kmh` | Mayor ráfaga reportada por la estación |
| `Lluvia_mm`, `Lluvia_Hoy_mm`, `Lluvia_Tasa_Max_mm_h` | Lluvia del minuto, acumulado del día y tasa máxima. **Vacías mientras `pluviometro_activo=false`** |
| `UV_Prom`, `UV_Max` | Índice UV |
| `Radiacion_Prom_W_m2`, `Radiacion_Max_W_m2` | Radiación solar |
| `Bateria_TX` | `OK` / `Baja` (batería del transmisor de la estación) |
| `Paquetes`, `Paquetes_Esperados`, `Calidad_%` | Tramas recibidas contra esperadas (≈ 23/min). Control de calidad de cada fila |

Una celda vacía significa "sin dato", nunca cero. Filtra por `Calidad_%` si necesitas solo minutos completos.

### 6.2 SQLite (`datos/db/eventos_davis.db`)

- **`eventos`**: una fila por trama recibida, con **solo** lo que esa trama midió (una trama de humedad no trae temperatura). Columnas: `timestamp`, `ts_unix`, `tipo_paquete`, `tx_id`, `bateria_baja`, `viento_kmh`, `viento_dir_grados`, `temp_c`, `humedad_pct`, `rafaga_kmh`, `lluvia_tasa_mm_h`, `lluvia_contador`, `lluvia_mm`, `indice_uv`, `radiacion_w_m2`, `trama_hex`. Se guarda la **trama cruda** (`trama_hex`), así que todo se puede volver a decodificar si algún día se corrige una fórmula.
- **`resumen`**: los mismos datos que el CSV, uno por minuto, más `segundos` y `ts_unix`.
- Volumen: ~34 000 tramas/día ≈ 4.5 MB/día. La base usa WAL, así que se puede leer con el servicio escribiendo.

### 6.3 `estado.json`

Se reescribe cada 5 s de forma atómica. Contiene los valores actuales, `edad_seg` (hace cuánto llegó cada dato) y un bloque `sdr` con `estado` (`Sincronizando` / `Recibiendo` / `Reiniciando` / `ERROR USB`), frecuencia, canal, paquetes, reinicios, `errores_usb`, `ultima_sync_seg` y `calidad_ultima_ventana_pct`. Sirve para dashboards externos; lo lee `--visor`.

### 6.4 Log

- Cada línea lleva hora, nivel e hilo (`MainThread`, `SDR`, `SQLiteWriter`, `Webhook`).
- **`HEARTBEAT`** cada 15 min resume la salud: `estado`, `válidas`, `crc_fail`, `reinicios`, `errores_usb`, `última_sync`, `calidad_última_ventana` y filas escritas en SQLite.
- Eventos a buscar: `sincronizado`, `Watchdog`, `USB`, `ALERTA`.

---

## 7. Cómo funciona por dentro

### 7.1 Hilos

| Hilo | Hace | Por qué separado |
|---|---|---|
| `SDR` | Lanza rtldavis y lee su salida línea por línea (bloqueante) | Nunca se pierde ni se retrasa una línea |
| `MainThread` | `tick()` una vez por segundo: watchdog, CSV, estado.json, heartbeat, alertas, compresión, aviso a systemd | Una sola rutina para la TUI y el servicio (en v2.3 estaba copiada) |
| `SQLiteWriter` | Dueño único de la base: recibe filas por cola y las escribe en lotes cada 15 s | El receptor nunca espera al disco; ~240 escrituras/h en la microSD en vez de ~1 400 |
| `Webhook` | Manda notificaciones con reintentos | La red lenta o caída no frena nada |

El estado compartido está protegido por un lock que **solo** cubre operaciones en memoria: nada de disco ni red dentro del lock.

### 7.2 Recorrido de una trama

1. rtldavis imprime `15:20:01.123 8005813339007F03 5 0 0 0 0 msg.ID=0`.
2. `protocolo.clasificar_linea` la reconoce como paquete (otras líneas: `Hop:` → canal/frecuencia, `Init channels` → sincronizando, `TRANSMITTER 0 SEEN` → sincronizado, errores USB → reinicio inmediato).
3. `validar_crc`: CRC-CCITT sobre los bytes 0-5 contra los bytes 6-7. Si falla, se descarta y se cuenta (`crc_fail`).
4. `decodificar` → tipo, ID, batería y valores (sección 8). Si el ID no es el configurado, se descarta (`id_fail`).
5. Lluvia: el contador rodante 0-127 se convierte en mm (con la vuelta 127→0 y descartando saltos imposibles).
6. Se actualiza el estado en memoria y el agregador del minuto, y la fila va a la cola de SQLite.
7. Al cerrar el minuto de reloj, `tick()` escribe la fila del CSV y de `resumen`, con rocío, VPD y calidad.

### 7.3 Sincronización de rtldavis y el watchdog en dos fases

Es lo que más afecta la cantidad de datos, así que conviene entenderlo:

- La estación transmite cada 2.5625 s y **cambia de canal en cada paquete** (51 canales entre 902 y 928 MHz). Para seguirla, rtldavis tiene que saber en qué punto de la secuencia va.
- Al arrancar, rtldavis escucha **solo el canal 0** (902.4 MHz) hasta 135 s (`Init channels: wait max 135 seconds`), sin imprimir nada. La estación pasa por ese canal una vez cada ~131 s: **hay una sola oportunidad por arranque**. Medido en esta Pi: sincroniza entre 7 y 129 s.
- Si esa trama se pierde, rtldavis **se queda esperando para siempre**.
- La v2.3 reiniciaba rtldavis tras 30 s de silencio, o sea, lo mataba antes de su única oportunidad. Así se formaban bucles de reinicio de 2–10 min (65 huecos y 143 min perdidos en 9 días).

v3 usa un watchdog **en dos fases**, basado en paquetes válidos:

| Fase | Límite | Razón |
|---|---|---|
| Sincronizando (aún sin el primer paquete) | `watchdog_sync_seg = 150` s | Una ventana completa de 135 s más margen. Esperar más es inútil |
| Sincronizado | `watchdog_sin_paquetes_seg = 90` s sin paquetes | Con ~2 % de pérdida normal, 35 pérdidas seguidas = sincronía perdida. rtldavis tardaría ~131 s en notarlo |
| Error USB (`cb transfer status`, `input/output error`) | Inmediato | rtldavis se cuelga al perder el USB |
| rtldavis muere al instante (dongle ausente) | Espera 2 → 4 → … → 60 s | No martillar el USB. Alerta tras 5 intentos |

Si el bucle principal se trabara, systemd lo reinicia (`WatchdogSec=120`; el programa avisa "sigo vivo" cada segundo).

### 7.4 Apagado

Ante SIGTERM (`systemctl stop`, apagado de la Pi) o Ctrl+C, el programa:

1. Detiene rtldavis (`terminate` y, si no responde en 5 s, `kill`).
2. Escribe el minuto parcial.
3. Vacía las colas de SQLite y de notificaciones.
4. Cierra la base.

Tarda menos de 1 s. Si Python muere de golpe, el kernel mata a rtldavis (`PR_SET_PDEATHSIG`), así que el dongle nunca queda tomado.

---

## 8. El protocolo Davis

Cada paquete son 8 bytes:

```
byte:   0          1            2              3     4     5     6   7
      [tipo|bat|id][viento mph][dirección viento][dato del tipo  ][x ][CRC   ]
       7-4  3  2-0
```

| Tipo (nibble alto del byte 0) | Nombre | Fórmula | "Sin dato" |
|---|---|---|---|
| `0x8` | Temperatura | `((b3<<8)|b4)` con signo, `/160` → °F → °C | `(b3<<4|b4>>4) = 0xFFC` |
| `0xA` | Humedad | `((b4>>4)<<8 | b3) / 10` % | 0 o > 100 |
| `0x9` | Ráfaga | `b3` mph × 1.609 | — |
| `0x5` | Tasa de lluvia | `b3=0xFF` → 0. Si no: tiempo entre vuelcos `t=((b4&0x30)<<4)|b3` (÷16 si `b4&0x40=0`), `3600/t × cubeta` | — |
| `0xE` | **Contador de lluvia** | `b3 & 0x7F`, rueda en 128; cada vuelco = 0.2 mm | — |
| `0x4` | UV | `((b3<<2)|(b4>>6)) / 50` | `0x3FF` |
| `0x6` | Radiación solar | `((b3<<2)|(b4>>6)) × 1.757936` W/m² | `≥ 0x3FE` |
| `0x2` / `0x7` | Supercap / celda solar | voltajes de la estación (informativos) | — |
| Todos | Viento | `b1` mph × 1.609; dirección `9 + b2×342/255` (zona muerta VP2 en el norte) | — |
| Byte 0 | ID / batería | ID = `b0 & 7`; batería baja = bit 3 (`b0 & 8`) | — |

Patrón real de esta estación: por cada ciclo llegan ~4 temperaturas, ~4 tasas de lluvia, ~4 contadores de lluvia, ~2 humedades y 1 de UV, solar y ráfaga cada uno. Por eso la humedad se actualiza cada ~25 s y UV/solar/ráfaga cada ~50 s.

**Correcciones respecto a la v2.3** (detalle en `docs/reporte_davis.md`):

- El ID y la batería se leían de bits equivocados: con batería baja se habría descartado todo.
- `0xE` es el contador de lluvia, no humedad de hoja. El "13" de hoja era falso.
- La tasa de lluvia es inversa al tiempo entre vuelcos.
- Se agregaron los sentinelas de "sin dato" y la dirección VP2.

---

## 9. Alertas

Salen en el log (`ALERTA: ...`) siempre, y por notificación si hay `webhook_url`:

| Alerta | Cuándo | Prioridad ntfy |
|---|---|---|
| Helada | Temperatura ≤ 2 °C. "Fin de helada" al pasar de 3 °C (histéresis: no repite por oscilaciones) | urgente |
| Tormenta severa | Tasa de lluvia ≥ 25 mm/h (requiere pluviómetro activo) | urgente |
| Sin datos de la estación | 15 min sin tramas. Después avisa "recepción restablecida" | alta |
| Reinicios repetidos | ≥ 3 reinicios del enlace en 30 min (máx. 1 aviso/h) | alta |
| Dongle no se puede abrir | 5 fallos seguidos al lanzar rtldavis | alta |
| Batería del transmisor | Cambio OK ↔ Baja (máx. 1 aviso cada 30 min) | normal |
| Inicio / apagado del sistema | Siempre | baja / normal |

---

## 10. Solución de problemas

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| `systemctl status` muestra `status=217/USER` | Unidad vieja con `User=sibajx` | `sudo bash deploy/instalar_servicio.sh` (instala la unidad correcta) |
| `status=226/NAMESPACE` | No existe `datos/` o el proyecto está en otra ruta | Correr el instalador (crea `datos/`). Si moviste el proyecto, ajusta `WorkingDirectory` y `ReadWritePaths` en la unidad |
| `journalctl` muestra `ERROR USB` / `input/output error` | Dongle desconectado, flojo o tomado por otro proceso | `lsusb \| grep 0bda`, `pgrep -a rtldavis`, reconectar firme. Se recupera solo cuando vuelve |
| Kernel: `device descriptor read/64, error -71`, `unable to enumerate USB device` | Falla eléctrica en el conector o cable del dongle | Quitar la extensión, usar otro puerto USB 2.0, cable corto. Si persiste, el dongle está dañado. **Desconectar y reconectar** (el puerto no reintenta solo) |
| Se queda en `Sincronizando` y se reinicia cada 150 s | La estación no se oye en el canal 0: lejos, sin batería, antena, o tx_id equivocado | `rtldavis -tf US -v`: si nunca aparece `TRANSMITTER 0 SEEN`, revisar antena, distancia y la estación. Probar `-tr` si el ID no es 0 |
| `Calidad_%` baja (< 90 %) con el enlace estable | Señal débil o interferencia | Antena vertical de 915 MHz (1/4 de onda ≈ 8.2 cm), lejos de la Pi y de puertos USB 3.0. Opcional: `-tf US -fc 900` |
| `No se encontró 'rtldavis' en el PATH` | rtldavis no instalado o fuera del PATH de systemd | Debe estar en `/usr/local/bin/rtldavis` (sección 11) |
| `flag provided but not defined: -tf` | Se compiló `bemasher/rtldavis` (archivado) | Compilar `lheijst/rtldavis` |
| La TUI dice "Terminal pequeña" | Menos de 90×24 | Agrandar la ventana o usar `--visor` |
| Dos programas pelean por el dongle | TUI abierta con el servicio corriendo | `sudo systemctl stop davis-monitor` antes de la TUI, o usar `--visor` |
| Notificación llega como JSON crudo | Falta `"webhook_formato": "ntfy"` | Agregarlo y reiniciar el servicio |
| `ImportError` / `No module named davis_monitor` | Corriste el comando fuera de la carpeta del proyecto | `cd ~/Escritorio/files/davis_v2` |
| Lluvia vacía en el CSV | `pluviometro_activo=false` (a propósito) | Ponerlo en `true` cuando se repare el sensor |

---

## 11. Instalación desde cero (otra máquina)

Esto ya está hecho en esta Pi; queda como referencia. Los pasos se adaptaron de la `INSTALACION.md` de la v2.3 (que era para Fedora) a Ubuntu/Raspberry Pi.

```bash
# 1. Dependencias
sudo apt install golang git build-essential librtlsdr-dev rtl-sdr sqlite3

# 2. Que el driver de TV no se adueñe del dongle
printf "blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n" | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo rmmod dvb_usb_rtl28xxu 2>/dev/null

# 3. Compilar rtldavis (fork lheijst: el original de bemasher no tiene -tf)
mkdir -p ~/go/src/github.com/{lheijst,jpoirier}
git clone https://github.com/jpoirier/gortlsdr.git ~/go/src/github.com/jpoirier/gortlsdr
git clone https://github.com/lheijst/rtldavis.git  ~/go/src/github.com/lheijst/rtldavis
cd ~/go/src/github.com/lheijst/rtldavis && GO111MODULE=off GOPATH=$HOME/go go install -v .
sudo cp ~/go/bin/rtldavis /usr/local/bin/      # para que systemd lo encuentre
rtldavis -h                                    # debe listar -tf, -tr, -fc, -gain...
#   Si falla con "undefined reference to rtlsdr_set_bias_tee": compilar librtlsdr de fuente
#   (git clone https://github.com/steve-m/librtlsdr; cmake ..; make; sudo make install; sudo ldconfig)

# 4. El proyecto (no necesita pip: solo biblioteca estándar)
git clone https://github.com/Sibajx/davis-sdr-greenhouse.git   # o copiar la carpeta davis_v2
cd davis_v2 && python3 -m unittest discover -s tests -t .

# 5. Ajustar usuario/rutas en deploy/davis-monitor.service si no es "alternas", y:
sudo bash deploy/instalar_servicio.sh            # instala también la regla udev del dongle
```

---

## 12. Estado actual del hardware y pendientes

*Actualizado: 2026-11-03.*

| Elemento | Estado | Acción |
|---|---|---|
| Temperatura, humedad, viento, ráfaga, UV, radiación | Funcionando. Validado contra 270 517 tramas reales y en vivo (98–100 % de recepción) | — |
| **Pluviómetro** | **Dañado** (contador fijo en `0x80` desde agosto, tasa siempre "seco") | `pluviometro_activo: false`. Al repararlo: ponerlo en `true`, reiniciar el servicio y probar con agua (cada vuelco = +0.2 mm en `--visor`) |
| Humedad de hoja/suelo | No existe en esta estación (requiere la estación Leaf & Soil de Davis) | El dato de la v2.3 era falso: no usarlo |
| **Dongle NooElec** | Conexión inestable: **60+ desconexiones USB el 03-nov**, `error -71` | Cable corto, puerto USB 2.0 fijo, que no se mueva. Revisar con `journalctl -k --since today \| grep -c "USB disconnect"` |
| Servicio | Unidad nueva lista en `deploy/` | Instalar con `sudo bash deploy/instalar_servicio.sh` |
| Notificaciones | Desactivadas | Opcional: configurar ntfy (sección 5) |
| Histórico v2.3 | Migrado a `datos/db/historico_v23_migrado.db` | Usar este en vez de la base vieja |

---

## 13. Dashboard web

Página web para ver la estación desde cualquier lado, publicada en un subdominio tuyo a través de un **túnel de Cloudflare** (no se abren puertos del router). Lo sirve `davis_web`, un servidor de la biblioteca estándar de Python que **solo lee** las bases (modo `ro`). No toca el dongle ni el receptor: si el dashboard falla, la captura sigue.

```
navegador ──HTTPS──▶ Cloudflare ──túnel──▶ cloudflared (Pi) ──▶ 127.0.0.1:8090 davis_web ──lee──▶ datos/db/*.db
                                                                                              └──lee──▶ datos/estado/estado.json
```

### 13.1 Secciones

| Sección | Acceso | Qué tiene |
|---|---|---|
| **En vivo** | público | Tarjetas con número animado, tendencia ▲▼ y mini gráfica; bandas de VPD y UV; brújula animada; tendencias de 1/6/24/48 h que se actualizan cada minuto |
| **Histórico** | público | Cualquier rango (atajos hoy, ayer, 7 d, 30 d, todo), variables elegibles, zoom, estadísticas mín/prom/máx |
| **Promediado Davis** | con sesión | Instantáneo vs. media móvil vs. registros de archivo tipo consola (5–120 min, media o integral), tabla y CSV |
| **Consultor de datos** | con sesión | Fuente (resumen, promediado, tramas crudas) + día y hora de inicio/fin + variables + resolución → vista previa y descarga **CSV / TXT / PNG** (varios formatos = ZIP) |
| **Usuarios** | solo admin | Crear, desactivar, cambiar rol, restablecer contraseña y eliminar cuentas, más la bitácora de actividad (logins, descargas) |

- **Animaciones:** cada vez que abres una sección (o vuelves a la pestaña del navegador), las gráficas se crean de nuevo y se dibujan desde cero hasta el último punto.
- **Fuera de línea:** si el receptor no actualiza `estado.json` por más de 60 s, aparece el aviso rojo. Las gráficas muestran entonces la ventana que termina en el último dato y el botón **Ver valores históricos** abre las últimas 24 h disponibles. Si el enlace está sincronizando, el aviso es amarillo.
- **Huecos:** donde faltan datos, la línea se corta; nunca se inventa una unión.
- Cada gráfica tiene botón **PNG** y **⤢** (ampliar con zoom). Hay tema oscuro/claro (◐) y diseño adaptado a celular.

### 13.1b Acceso público y Cloudflare Access

La cuenta de Cloudflare tiene una aplicación de Access, **"Subdominios s-dev (equipo)"**, que protege `*.s-dev.win`. Para que el dashboard sea público hay una aplicación específica, **"davis (publico)"** (`davis.s-dev.win`, política *publico → Bypass → Everyone*), igual que las de `ashli` y `lealtad`. La regla más específica gana, así que solo este subdominio queda abierto.

- **Volver a restringirlo:** en Zero Trust → Access → Applications, elimina "davis (publico)". El sitio vuelve a pedir el login del equipo.
- **Dentro del laboratorio:** el FortiGate bloquea `s-dev.win` (categoría "Newly Registered Domain"). Hay que pedir a TI que lo permita.

### 13.2 Promediado "método Davis"

Porta el método de `generar_prueba_promediado_davis.py` (validado contra el WiFiLogger):

- **Media móvil:** `rolling('30min', min_periods=15).mean()`. Es una ventana retrospectiva `(t−30 min, t]` que exige al menos la mitad de los minutos. Un test compara el resultado contra el cálculo directo punto por punto.
- **Registros de archivo:** un registro en cada marca de reloj múltiplo del intervalo (:00, :30…), con promedio, alta/baja, dirección dominante (vectorial), ráfaga máxima, rocío, VPD y cobertura %. Es el formato de exportación de la consola.
- **Método integral:** ∫x·dt / ∫dt por trapecios sobre las tramas crudas (≈ 1 dato cada 10–50 s). Da casi lo mismo que la media con muestreo regular y es más exacto con huecos. Máximo 31 días por consulta.
- El script original corregía además un **desfase de reloj de 61 min** del WiFiLogger. Ese ajuste es propio de la comparación con ese equipo y no se aplica aquí. Si se vuelve a comparar, hay que medirlo de nuevo.

### 13.3 Seguridad

- Contraseñas con **scrypt** y sal propia: mínimo 10 caracteres, letras y números.
- Cookie `HttpOnly; Secure; SameSite=Strict` con token aleatorio de 256 bits; en la base solo se guarda su SHA-256.
- **CSRF** obligatorio en toda acción que modifica algo y verificación de `Origin`.
- **Bloqueo** de 15 min tras 5 intentos fallidos, por usuario y por IP real (`CF-Connecting-IP`).
- Cabeceras CSP estrictas (sin scripts externos: ECharts va incluido), `X-Frame-Options: DENY`, `nosniff`.
- El servidor escucha **solo en 127.0.0.1**. Límites de rango por consulta (400 días; 31 días para tramas crudas y método integral).
- No se puede eliminar ni degradar al último administrador. Desactivar una cuenta o cambiarle la clave cierra sus sesiones.
- Las cuentas, sesiones y bitácora están en `datos/web/usuarios.db`.

### 13.4 Comandos

| Comando | Qué hace |
|---|---|
| `sudo bash deploy/instalar_web.sh` | Instala y arranca el servicio `davis-web` |
| `python3 -m davis_web admin crear <usuario>` | Crea un **administrador** (pide la contraseña). El primero se crea así |
| `python3 -m davis_web admin clave <usuario>` | Restablece la contraseña de cualquier cuenta (si olvidas la tuya) |
| `python3 -m davis_web admin listar` | Lista las cuentas |
| `bash deploy/configurar_cloudflare.sh davis.tudominio.com` | Crea el túnel, el DNS y el servicio `cloudflared` |
| `systemctl status davis-web cloudflared` | Estado de los dos servicios |
| `journalctl -u davis-web -f` / `datos/logs/davis_web.log` | Log del dashboard |
| `python3 -m davis_web` | Correrlo a mano (en `http://127.0.0.1:8090`) |

Ajustes en `config.json`: `web_puerto` (8090), `web_horas_sesion` (12), `web_titulo`, `web_cookie_segura` (true; **no** desactivar en producción).

### 13.5 Mudanza a un servidor (pendiente, para más adelante)

El diseño ya lo permite sin reescribir código:

1. **Opción simple:** copiar la carpeta `davis_v2` (con `datos/`) al servidor y apuntar el túnel al servidor. La Pi queda solo como receptor y envía los datos.
2. **Sincronizar los datos de la Pi al servidor:** cada pocos minutos con `rsync` de `datos/db` y `estado.json`, o mejor, que el receptor publique cada trama por **MQTT** (la Pi ya tiene Mosquitto en el puerto 1883) y el servidor la guarde.
3. A más escala: PostgreSQL/TimescaleDB en lugar de SQLite (el módulo `consultas.py` es el único punto que cambiaría).

---

## 14. Respaldos

Se respalda **solo, cada día**, sin detener nada. El respaldo usa la API de backup de SQLite, que saca una copia consistente aunque el receptor esté escribiendo.

| Qué | Dónde |
|---|---|
| Base activa, cuentas del dashboard, todos los CSV, `config.json` | `datos/respaldos/AAAA-MM-DD_HHMM/` (un respaldo por día) |
| Histórico migrado y legado v2.3 (no cambian) | `datos/respaldos/fijos/`, guardados **una sola vez** |
| Detalle de cada respaldo | `manifiesto.json`: SHA-256, filas por tabla, rango de fechas |
| Log | `datos/logs/respaldo.log` |

- **Programación:** tu crontab de usuario (`crontab -l`) revisa cada hora a los :17 y respalda si el último tiene más de 23 h. También revisa 10 min después de encender la Pi. Si estuvo apagada, se pone al día sola.
- **Verificación:** cada copia pasa `PRAGMA integrity_check` antes de guardarse. Un respaldo a medias nunca queda como válido, porque se escribe en `.parcial` y se renombra al final.
- **Retención:** los 7 más recientes, más 1 por semana (4 semanas) y 1 por mes (12 meses). Los demás se borran solos.
- **Protecciones:** no respalda con menos de 1 GB libre, no corre dos veces al mismo tiempo, y si falla avisa por ntfy (si está configurado) y lo anota en el log.

**Importante:** `datos/respaldos/` está **en la misma microSD**. Protege contra borrados, corrupción de la base o un error, pero **no contra que la tarjeta muera**. Para eso, conecta un disco USB y agrega en `config.json`:

```json
{ "pluviometro_activo": false, "dir_respaldos_extra": "/media/alternas/NOMBRE_DEL_DISCO" }
```

Cada respaldo se copiará también ahí (en `davis_respaldos/`), con la misma retención. Si el disco no está conectado, solo queda un aviso en el log.

| Comando | Qué hace |
|---|---|
| `davis respaldo` | Respaldar ahora |
| `davis respaldo listar` | Lista los respaldos y cuánto hace del último |
| `davis respaldo verificar` | Comprueba SHA-256 y que cada archivo se pueda descomprimir |

### Restaurar

```bash
davis detener                                         # detiene receptor, dashboard y túnel
cd ~/Escritorio/files/davis_v2/datos
R=respaldos/2026-11-03_1644                           # el respaldo elegido (davis respaldo listar)
mv db/eventos_davis.db db/eventos_davis.db.antes      # conservar la actual por si acaso
rm -f db/eventos_davis.db-wal db/eventos_davis.db-shm
gunzip -c $R/eventos_davis.db.gz > db/eventos_davis.db
gunzip -c $R/usuarios.db.gz      > web/usuarios.db    # solo si también quieres restaurar las cuentas
tar xzf $R/csv.tar.gz                                 # CSV (sobrescribe datos/csv/)
davis iniciar
```

Histórico migrado: `gunzip -c respaldos/fijos/historico_v23_migrado.db.gz > db/historico_v23_migrado.db`.
