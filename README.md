# davis-sdr-greenhouse · v3

**Receptor Davis Vantage Pro 2 vía RTL-SDR + dashboard web en tiempo real, para invernadero.**

> **Este repositorio es la continuación (v3) de [Sibajx/davis-sdr-greenhouse](https://github.com/Sibajx/davis-sdr-greenhouse).**
> El repositorio original contiene el **artículo**, los datos y el software **v2.3** con el que se obtuvieron los resultados publicados. Se conserva sin cambios como referencia del paper.
> Aquí está la versión **en operación**: el mismo sistema con errores corregidos, recepción más estable, promediado tipo consola Davis y un dashboard web con cuentas de usuario.

*English summary: continuation (v3) of [davis-sdr-greenhouse](https://github.com/Sibajx/davis-sdr-greenhouse), the repository of the article "A Low-Cost SDR-Based Edge IoT Architecture for Real-Time Decoding of Weather Telemetry in Cyber-Physical Greenhouse Drying Systems". That repository keeps the paper and the v2.3 code used for its results. This one contains the production system: protocol fixes, a two-phase SDR watchdog, Davis-console style averaging and a web dashboard (live and historical charts, user accounts, CSV/TXT/PNG export). Pure Python standard library; tested on a Raspberry Pi 5.*

---

## Relación con el repositorio original

| | [davis-sdr-greenhouse](https://github.com/Sibajx/davis-sdr-greenhouse) (original) | **davis-sdr-greenhouse-v3** (este) |
|---|---|---|
| Propósito | Artículo + reproducibilidad de sus resultados | Sistema en operación continua |
| Software | `davis_monitor` v2.3 | `davis_monitor` v3.0 + `davis_web` 1.0 |
| Datos | Campaña 29-ago → 4-sep-2026 | Se generan en `datos/` (no se suben a git) |
| Estado | Congelado (referencia del paper) | Activo |

**¿Cambian los resultados del artículo?** Las variables validadas en el paper (**temperatura, humedad, radiación solar e índice UV**) se decodifican **igual** en v3: se re-decodificaron las 270 517 tramas de la campaña con ambas versiones y coinciden (diferencias ≤ 0.05 por redondeo). Lo que v3 corrige está fuera de esas métricas o las mejora:

- **Watchdog SDR:** el de v2.3 cortaba la sincronización de rtldavis. Era la causa de la mayoría de las intervenciones del watchdog y de los huecos que bajaban el PDR.
- **Lectura del ID y de la batería:** se leían de bits equivocados. Con batería baja se habrían descartado todas las tramas.
- **Paquete `0xE`:** es el **contador de lluvia**, no humedad de hoja.
- **Tasa de lluvia:** usaba una fórmula incorrecta.
- **Tramas basura que pasaban el CRC:** ahora se descartan.

El detalle, con evidencia, está en [`docs/reporte_davis.md`](docs/reporte_davis.md).

---

## Qué incluye

```
davis_v2/
├── davis_monitor/     receptor: lanza/vigila rtldavis, decodifica, guarda CSV + SQLite, alertas
├── davis_web/         dashboard web (servidor de la biblioteca estándar + ECharts local)
├── davis              lanzador: servicios + TUI (davis ayuda)
├── tests/             55 pruebas (python3 -m unittest discover -s tests -t .)
├── deploy/            servicios systemd, instaladores, túnel Cloudflare, regla udev
├── docs/
│   ├── GUIA_COMPLETA.md          comandos y funcionamiento de todo el sistema  ← empezar aquí
│   ├── reporte_davis.md          análisis v2.3 → v3, errores y pruebas en hardware
│   └── MEJORAS_RECOMENDADAS.md   hoja de ruta
├── datos/             TODO lo generado (csv/AAAA/MM, db, logs, estado, web…); ver datos/LEEME.md
└── config.example.json
```

- **Receptor:**
  - Watchdog en dos fases.
  - Recuperación automática si el dongle se desconecta del USB.
  - Lluvia acumulada a partir del contador del pluviómetro.
  - CSV por minuto con promedios, extremos, punto de rocío, VPD, dirección vectorial y calidad de recepción.
  - SQLite con escritura por lotes.
  - Alertas push (ntfy).
- **Dashboard:**
  - Secciones públicas *En vivo* (animado, se actualiza cada minuto) e *Histórico*; avisa y cambia a históricos cuando el sistema está fuera de línea.
  - Con sesión: *Promediado Davis*, con el método validado del paper (media retrospectiva de 30 min, mínimo 15 muestras) y una variante por integral temporal.
  - Con sesión: *Consultor de datos*, con descarga en CSV, TXT y PNG por días u horas.
  - Administración de cuentas con bitácora de actividad.
- **Sin dependencias externas:** solo Python ≥ 3.9 y su biblioteca estándar. matplotlib es opcional, solo para exportar PNG.

## Inicio rápido

```bash
# requisitos: rtldavis (fork lheijst) en el PATH y un dongle RTL-SDR — ver docs/GUIA_COMPLETA.md §11
./davis                               # con los servicios instalados: los arranca y abre la TUI
python3 -m unittest discover -s tests -t .
python3 -m davis_monitor              # interfaz de texto
sudo bash deploy/instalar_servicio.sh # receptor como servicio

python3 -m davis_web admin crear <usuario>
sudo bash deploy/instalar_web.sh      # dashboard en 127.0.0.1:8090
bash deploy/configurar_cloudflare.sh davis.tudominio.com   # publicarlo con un túnel de Cloudflare
```

`config.json` (local, no versionado) guarda la configuración de cada estación; parte de `config.example.json`. En la estación actual: `{"pluviometro_activo": false}`, porque el pluviómetro está dañado. Así la lluvia queda vacía en lugar de "0 mm".

## Hardware probado

Raspberry Pi 5 · NooElec NESDR SMArt v5 (RTL2832U/R820T2) · Davis Vantage Pro 2 ISS, banda US 915 MHz · rtldavis 0.15 (lheijst).

## Cómo citar

Cita el artículo asociado (ver [`CITATION.cff`](CITATION.cff) y el [repositorio original](https://github.com/Sibajx/davis-sdr-greenhouse)). Si usas específicamente las mejoras de v3, cita también este repositorio.

## Licencia

MIT, los mismos autores que el repositorio original ([`LICENSE`](LICENSE)). Incluye Apache ECharts (Apache 2.0, ver [`NOTICE`](NOTICE)).
