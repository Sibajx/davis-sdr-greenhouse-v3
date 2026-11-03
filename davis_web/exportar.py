"""Consultor/generador de datos: arma la descarga que pidió el usuario.

Fuentes:  'minuto'      resumen por minuto (o agregado a la resolución pedida)
          'promediado'  registros tipo consola Davis (promediado.archivo)
          'tramas'      tramas crudas (máx. 31 días)
Formatos: csv · txt (tabla alineada con encabezado descriptivo) · png (gráficas)
          → un archivo si es un formato; ZIP si son varios.
"""
import csv
import io
import threading
import zipfile
from datetime import datetime
from typing import Dict, List, Sequence, Tuple

from . import consultas, promediado

MAX_DIAS = {"minuto": 400, "promediado": 400, "tramas": 31}
_LOCK_MPL = threading.Lock()   # matplotlib no es thread-safe


def _fecha(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def tabla(fuente: str, desde: float, hasta: float, claves: Sequence[str], resolucion: int = None,
          intervalo_min: int = 30, metodo: str = "media") -> Tuple[List[str], List[list], dict]:
    """→ (encabezados, filas, metadatos)"""
    dias = (hasta - desde) / 86400
    if hasta <= desde:
        raise ValueError("El fin debe ser posterior al inicio")
    if dias > MAX_DIAS[fuente]:
        raise ValueError(f"Rango máximo para '{fuente}': {MAX_DIAS[fuente]} días")
    meta = {"fuente": fuente, "desde": _fecha(desde), "hasta": _fecha(hasta),
            "generado": datetime.now().isoformat(timespec="seconds")}
    if fuente == "minuto":
        s = consultas.serie(desde, hasta, claves, resolucion)
        ks = list(s["series"])
        enc = ["Fecha_Hora"] + [f"{s['variables'][k]['etiqueta']} ({s['variables'][k]['unidad']})".strip()
                                .replace(" ()", "") for k in ks]
        filas = [[_fecha(t)] + [s["series"][k][i] for k in ks] for i, t in enumerate(s["t"])]
        meta.update(resolucion_seg=s["resolucion"], variables=ks,
                    nota="Cada fila resume el intervalo que TERMINA en Fecha_Hora")
        return enc, filas, meta
    if fuente == "promediado":
        a = promediado.archivo(desde, hasta, intervalo_min, metodo)
        campos = promediado.CAMPOS_ARCHIVO
        if not consultas.config.PLUVIOMETRO_ACTIVO:
            campos = [c for c in campos if c[0] != "lluvia"]
        enc = [c[1] for c in campos]
        filas = [[_fecha(r["fin"]) if k == "fin" else r.get(k) for k, _ in campos] for r in a["registros"]]
        meta.update(intervalo_min=intervalo_min, metodo=metodo,
                    nota=f"Método Davis: promedio retrospectivo de {intervalo_min} min, ventana (T-{intervalo_min}min, T], "
                         f"válido con >= {int(a['cobertura_min_pct'])}% de minutos")
        return enc, filas, meta
    if fuente == "tramas":
        filas_ev = consultas.filas_eventos(desde, hasta)
        if not filas_ev:
            return ["timestamp"], [], meta
        enc = [k for k in filas_ev[0].keys() if k != "id"]
        if not consultas.config.PLUVIOMETRO_ACTIVO:
            enc = [k for k in enc if not k.startswith("lluvia")]
        return enc, [[r[k] for k in enc] for r in filas_ev], meta
    raise ValueError("Fuente inválida")


def a_csv(enc, filas) -> bytes:
    b = io.StringIO()
    w = csv.writer(b)
    w.writerow(enc)
    w.writerows(["" if v is None else v for v in f] for f in filas)
    return b.getvalue().encode("utf-8-sig")   # BOM: Excel abre bien los acentos


def a_txt(enc, filas, meta) -> bytes:
    cuerpo = [["--" if v is None else (f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
               for v in f] for f in filas]
    anchos = [max([len(h)] + [len(f[i]) for f in cuerpo]) for i, h in enumerate(enc)]
    lineas = ["# Estación Davis Vantage Pro 2 — davis_monitor", *(f"# {k}: {v}" for k, v in meta.items()),
              f"# filas: {len(filas)}", "#",
              "  ".join(h.ljust(anchos[i]) for i, h in enumerate(enc)),
              "  ".join("-" * a for a in anchos)]
    lineas += ["  ".join(c.rjust(anchos[i]) if i else c.ljust(anchos[i]) for i, c in enumerate(f)) for f in cuerpo]
    return ("\n".join(lineas) + "\n").encode("utf-8")


def a_png(enc, filas, meta) -> Dict[str, bytes]:
    """Una figura con un panel por variable numérica (máx. 8)."""
    with _LOCK_MPL:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        if not filas:
            return {}
        x = [datetime.strptime(f[0][:19], "%Y-%m-%d %H:%M:%S") if isinstance(f[0], str) else
             datetime.fromtimestamp(f[0]) for f in filas]
        columnas = [i for i in range(1, len(enc))
                    if any(isinstance(f[i], (int, float)) and not isinstance(f[i], bool) for f in filas)
                    and not enc[i].lower().startswith(("minutos", "cobertura", "ts_unix", "tx_id", "bateria",
                                                       "lluvia_contador", "dir_dominante_grados"))][:8]
        if not columnas:
            return {}
        paleta = ["#e4572e", "#17bebb", "#ffc914", "#2e86ab", "#76b041", "#a23b72", "#f18f01", "#6c757d"]
        fig, ejes = plt.subplots(len(columnas), 1, figsize=(12, 2.4 * len(columnas) + 0.8), sharex=True,
                                 squeeze=False)
        for n, (ax, i) in enumerate(zip(ejes[:, 0], columnas)):
            y = [f[i] if isinstance(f[i], (int, float)) else float("nan") for f in filas]
            ax.plot(x, y, color=paleta[n % len(paleta)], linewidth=1.3)
            ax.set_ylabel(enc[i], fontsize=9)
            ax.grid(True, linestyle=":", alpha=0.5)
        ejes[-1, 0].xaxis.set_major_formatter(mdates.DateFormatter("%d/%m\n%H:%M"))
        fig.suptitle(f"Estación Davis — {meta['fuente']} — {meta['desde']} a {meta['hasta']}", fontsize=11)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=130)
        plt.close(fig)
        return {"graficas.png": buf.getvalue()}


def generar(fuente: str, desde: float, hasta: float, claves: Sequence[str], formatos: Sequence[str],
            resolucion: int = None, intervalo_min: int = 30, metodo: str = "media") -> Tuple[str, str, bytes]:
    """→ (nombre_archivo, content-type, contenido)"""
    formatos = [f for f in formatos if f in ("csv", "txt", "png")]
    if not formatos:
        raise ValueError("Elige al menos un formato")
    enc, filas, meta = tabla(fuente, desde, hasta, claves, resolucion, intervalo_min, metodo)
    base = f"davis_{fuente}_{datetime.fromtimestamp(desde):%Y%m%d-%H%M}_{datetime.fromtimestamp(hasta):%Y%m%d-%H%M}"
    archivos: Dict[str, bytes] = {}
    if "csv" in formatos:
        archivos[base + ".csv"] = a_csv(enc, filas)
    if "txt" in formatos:
        archivos[base + ".txt"] = a_txt(enc, filas, meta)
    if "png" in formatos:
        for nombre, datos in a_png(enc, filas, meta).items():
            archivos[f"{base}_{nombre}"] = datos
    if not archivos:
        raise ValueError("No hay datos para graficar en ese rango")
    if len(archivos) == 1:
        nombre, datos = next(iter(archivos.items()))
        tipo = {"csv": "text/csv; charset=utf-8", "txt": "text/plain; charset=utf-8",
                "png": "image/png"}[nombre.rsplit(".", 1)[1]]
        return nombre, tipo, datos
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, datos in archivos.items():
            z.writestr(nombre, datos)
    return base + ".zip", "application/zip", buf.getvalue()
