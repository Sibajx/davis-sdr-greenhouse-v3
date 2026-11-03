# datos/ — todo lo que genera davis_monitor

Esta carpeta la crea y la llena el programa (no se sube a git). Rutas definidas en `davis_monitor/rutas.py`.

| Carpeta | Qué hay | Cada cuánto | Quién lo usa |
|---|---|---|---|
| `csv/AAAA/MM/historial_davis_AAAA-MM-DD.csv` | Resumen por minuto (23 columnas: promedios, mín/máx, rocío, VPD, lluvia, calidad). Un archivo por día; los de más de 7 días se comprimen a `.csv.gz` en su misma carpeta | 1 fila/min | Excel, análisis del paper |
| `db/eventos_davis.db` | SQLite **activa**: tabla `eventos` (cada trama) y `resumen` (igual que el CSV) | lotes cada 15 s | consultas SQL, `basedatos` |
| `db/historico_v23_migrado.db` | Histórico 2026-08-26 → 2026-11-03 de la v2.3, re-decodificado con el protocolo corregido | fijo | comparación / paper |
| `logs/davis_monitor.log` | Log rotativo (5 × 5 MB → `.log.1` … `.log.5`) | continuo | diagnóstico |
| `estado/estado.json` | Último estado en vivo (lo lee `--visor`, dashboards) | cada 5 s | visor |
| `exportaciones/` | CSV que saques con `python3 -m davis_monitor.basedatos exportar` | a pedido | tú |
| `legado_v23/` | Copia intacta de `~/capturas_davis` (datos crudos de la v2.3: base, log, CSV) | fijo | respaldo |

Comandos útiles (desde la raíz del proyecto):

```bash
python3 -m davis_monitor.basedatos resumen
python3 -m davis_monitor.basedatos calidad --dias 1
python3 -m davis_monitor.basedatos exportar --tabla resumen --desde 2026-11-01   # → datos/exportaciones/
python3 -m davis_monitor.basedatos --db datos/db/historico_v23_migrado.db resumen
```

**Respaldo:** basta con copiar esta carpeta entera (con el servicio detenido, o usando
`sqlite3 datos/db/eventos_davis.db ".backup respaldo.db"` para la base en caliente).
