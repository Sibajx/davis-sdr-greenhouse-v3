# Mejoras recomendadas para el sistema Davis-SDR

*Noviembre de 2026. Ordenadas por impacto sobre la calidad de los datos. Cada una dice si es hardware, configuración o código, y cuánto cuesta aproximadamente.*

## Prioridad alta: afectan los datos que ya se están capturando

| # | Mejora | Tipo | Por qué | Costo aprox. |
|---|---|---|---|---|
| 1 | **Fijar el dongle NooElec**: cable de extensión USB 2.0 corto y blindado (con ferrita), sujeto, en un puerto USB 2.0 de la Pi | Hardware | 60+ desconexiones el 03-nov y `error -71`. Es hoy la causa nº 1 de pérdida de datos | $100–200 MXN |
| 2 | **Reparar o reemplazar el pluviómetro** (Davis 6466M, o revisar su reed switch y cable) | Hardware | Sin lluvia no hay balance hídrico ni ET₀ confiable; hoy está desactivado (`pluviometro_activo: false`) | reparación ~$0–300; nuevo ~$2,500–4,000 MXN |
| 3 | **Antena de 915 MHz** (1/4 de onda vertical con plano de tierra, o colineal) en alto y lejos de la Pi y de USB 3.0 | Hardware | rtldavis sincroniza en el canal 0 (borde de banda, 902 MHz) y depende de recibir **una** trama. Más señal = sincronización al primer intento y menos del ~2 % de pérdida | $150–600 MXN |
| 4 | **Disco SSD por USB** en lugar de microSD para `datos/` (o al menos liberar espacio: `~/captura.iq` ocupa 1.8 GB; la SD está al 71 %) | Hardware | La microSD se desgasta con escrituras continuas y se corrompe con cortes de luz. Basta con mover `datos/` (`"dir_datos": "/media/ssd/davis"`) | SSD 128 GB ~$400 MXN |
| 5 | **UPS para la Pi** (HAT de batería o UPS pequeño) | Hardware | Un corte de luz a media escritura puede dañar la SD; con UPS, la Pi sigue capturando | $500–1,200 MXN |
| 6 | **Respaldo automático diario** de `datos/db/` a otro disco y a la nube (Google Drive con `rclone`, o Cloudflare R2: 10 GB gratis) | Config | Hoy hay una sola copia de los datos del paper | $0 |

## Prioridad media: más valor de los mismos datos

| # | Mejora | Tipo | Detalle |
|---|---|---|---|
| 7 | **Cloudflare Access (Zero Trust)** delante del dashboard o solo de `/api/login` | Config | Gratis hasta 50 usuarios: agrega verificación por correo o Google **antes** de llegar a tu login. Defensa en profundidad para algo expuesto a internet |
| 8 | **Notificaciones push con ntfy** (ya implementadas, solo falta activarlas) | Config | Helada, sin datos, dongle desconectado, batería. Ver la guía, sección 5 |
| 9 | **Monitoreo externo** con Healthchecks.io o UptimeRobot (gratis) sobre la URL pública | Config | Te avisa si se cae la Pi, el internet o el túnel, que es justo lo que el sistema no puede avisar por sí mismo |
| 10 | **Indicadores agronómicos** en el dashboard: ET₀ (Penman-Monteith FAO-56), grados-día de crecimiento, horas con VPD fuera de rango, horas de posible mojado foliar (T ≤ rocío + 1 °C) | Código | Todos se calculan con lo que ya se mide (T, HR, viento, radiación). Es lo que más usa un invernadero para riego y manejo de hongos |
| 11 | **Validación continua contra el WiFiLogger**: subir su CSV en el dashboard y obtener MAE/RMSE/r/R² por variable, como en el script de tu compañero | Código | Convierte la validación del paper en algo repetible cada mes |
| 12 | **Calibrar el ppm del dongle** (`rtl_test -p` unos minutos, o con una emisora conocida) y pasar `-ppm N` a rtldavis | Config | El AFC de rtldavis corrige ~+900 Hz, pero solo después de sincronizar; el canal 0 se escucha sin corrección |

## Para más adelante

| # | Mejora | Detalle |
|---|---|---|
| 13 | **Mudar el dashboard a un servidor** | Ver la guía, sección 13.5. La Pi queda como receptor y publica por MQTT (Mosquitto ya corre en la Pi, puerto 1883) |
| 14 | **Aprovechar Grafana + InfluxDB**, que ya están activos en la Pi | El receptor puede publicar a InfluxDB para paneles internos/técnicos; el dashboard propio queda para usuarios externos |
| 15 | **Segundo dongle como respaldo**, o un receptor en otra ubicación | Redundancia si uno falla; también permite comparar la calidad de recepción |
| 16 | **Sensores propios dentro del invernadero** (p. ej. SHT45/BME280 en una ESP32 → MQTT) | La Davis mide afuera; lo que le importa al cultivo es adentro. Con los dos se calcula el efecto del invernadero |
| 17 | **API de solo lectura con llaves** para que otros sistemas consuman los datos | Ya existe internamente (`/api/serie`); solo faltan llaves por usuario y límites de uso |

## Lo que NO recomiendo

- **Abrir puertos del router** para el dashboard: el túnel de Cloudflare es más seguro y no necesita IP fija.
- **Escuchar en `0.0.0.0`** (`web_host`): el servidor debe quedar en `127.0.0.1` detrás del túnel.
- **Usar `0 mm` cuando el pluviómetro falla**: es mejor un hueco honesto (vacío) que un dato falso en el paper.
