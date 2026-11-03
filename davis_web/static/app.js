/* Dashboard estación Davis — sin frameworks, ECharts local. */
"use strict";

// ====================================================================== utilidades
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, attrs = {}, ...hijos) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const h of hijos.flat()) if (h !== null && h !== undefined) e.append(h.nodeType ? h : document.createTextNode(h));
  return e;
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "--" : Number(v).toFixed(d);
const pad = (n) => String(n).padStart(2, "0");
const aLocal = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const fechaTxt = (ms) => new Date(ms).toLocaleString("es-MX", { dateStyle: "medium", timeStyle: "short" });
const haceTxt = (seg) => seg < 90 ? `${Math.round(seg)} s` : seg < 5400 ? `${Math.round(seg / 60)} min` : seg < 172800 ? `${(seg / 3600).toFixed(1)} h` : `${Math.round(seg / 86400)} días`;

function toast(msg, error = false) {
  const t = el("div", { class: "toast" + (error ? " error" : "") }, msg);
  $("#toasts").append(t);
  setTimeout(() => { t.style.transition = "opacity .4s"; t.style.opacity = 0; setTimeout(() => t.remove(), 400); }, error ? 6000 : 3500);
}

const S = { meta: null, usuario: null, estado: null, tab: null, vivoHoras: 6, vivoSerie: null, graficas: new Map(), prevValores: {} };

async function api(ruta, { metodo = "GET", cuerpo, blob = false } = {}) {
  const op = { method: metodo, credentials: "same-origin", headers: {} };
  if (cuerpo !== undefined) { op.headers["Content-Type"] = "application/json"; op.body = JSON.stringify(cuerpo); }
  if (metodo !== "GET" && S.usuario) op.headers["X-CSRF-Token"] = S.usuario.csrf;
  const r = await fetch(ruta, op);
  if (blob && r.ok) return r;
  let d = {};
  try { d = await r.json(); } catch { /* vacío */ }
  if (!r.ok) {
    if (r.status === 401 && S.usuario && ruta !== "/api/login") { S.usuario = null; pintarSesion(); }
    throw new Error(d.error || `Error ${r.status}`);
  }
  return d;
}

// ====================================================================== definiciones de gráficas
const COLORES = { temp: "--c1", rocio: "--c2", humedad: "--c4", vpd: "--c6", viento: "--c2", rafaga: "--c1", viento_max: "--c3",
  solar: "--c3", uv: "--c6", calidad: "--c5", lluvia: "--c4", lluvia_tasa: "--c2", temp_min: "--c4", temp_max: "--c7",
  solar_max: "--c1", uv_max: "--c7", direccion: "--c8", paquetes: "--c8" };
const color = (k) => css(COLORES[k] || "--c8");

const GRUPOS = [
  { id: "termo", titulo: "Temperatura y punto de rocío", series: [{ k: "temp", area: true }, { k: "rocio", punteada: true }, { k: "temp_max", fina: true }, { k: "temp_min", fina: true }] },
  { id: "humedad", titulo: "Humedad relativa y VPD", series: [{ k: "humedad", area: true }, { k: "vpd", eje: 1 }] },
  { id: "viento", titulo: "Viento", series: [{ k: "viento", area: true }, { k: "viento_max", fina: true }, { k: "rafaga", tipo: "scatter" }] },
  { id: "direccion", titulo: "Dirección del viento", series: [{ k: "direccion", tipo: "scatter" }], ymax: 360 },
  { id: "solar", titulo: "Radiación solar e índice UV", series: [{ k: "solar", area: true }, { k: "solar_max", fina: true }, { k: "uv", eje: 1 }, { k: "uv_max", eje: 1, fina: true }] },
  { id: "lluvia", titulo: "Lluvia", series: [{ k: "lluvia", tipo: "bar" }, { k: "lluvia_tasa", eje: 1 }] },
  { id: "calidad", titulo: "Calidad de recepción", series: [{ k: "calidad", tipo: "bar" }], ymax: 100 },
];
const VARS_VIVO = ["temp", "rocio", "humedad", "vpd", "viento", "rafaga", "solar", "uv", "calidad", "lluvia", "lluvia_tasa"];

function opcionBase({ ejes = [{}], zoom = false, leyenda = true } = {}) {
  const tx = css("--texto2"), linea = css("--borde");
  return {
    animationDuration: 1400, animationEasing: "cubicOut", animationDurationUpdate: 700,
    textStyle: { fontFamily: "system-ui, sans-serif" },
    grid: { left: 52, right: ejes.length > 1 ? 56 : 18, top: leyenda ? 34 : 14, bottom: zoom ? 64 : 28 },
    legend: leyenda ? { top: 0, textStyle: { color: tx }, icon: "roundRect", itemWidth: 14, itemHeight: 8 } : undefined,
    tooltip: { trigger: "axis", backgroundColor: css("--panel-solido"), borderColor: linea, textStyle: { color: css("--texto") },
      axisPointer: { type: "line", lineStyle: { color: css("--tenue") } } },
    xAxis: { type: "time", axisLine: { lineStyle: { color: linea } }, axisLabel: { color: tx, hideOverlap: true }, splitLine: { show: false } },
    yAxis: ejes.map((e, i) => ({ type: "value", name: e.nombre, nameTextStyle: { color: tx }, scale: e.max === undefined, max: e.max, min: e.min,
      axisLabel: { color: tx }, splitLine: { show: i === 0, lineStyle: { color: linea, type: "dashed" } } })),
    dataZoom: zoom ? [{ type: "inside" }, { type: "slider", height: 22, bottom: 8, borderColor: linea, textStyle: { color: tx } }] : [{ type: "inside" }],
  };
}

function serieEcharts(def, nombre, datos, unidad) {
  const c = color(def.k);
  const s = { name: nombre, type: def.tipo || "line", data: datos, yAxisIndex: def.eje || 0, showSymbol: false,
    connectNulls: false, smooth: def.tipo ? false : 0.25, color: c, emphasis: { focus: "series" },
    tooltip: { valueFormatter: (v) => v === null || v === undefined ? "--" : `${v} ${unidad || ""}` } };
  if (!def.tipo || def.tipo === "line") {
    s.lineStyle = { width: def.fina ? 1 : 2.2, type: def.punteada ? "dashed" : "solid", opacity: def.fina ? .55 : 1 };
    s.sampling = "lttb";
    if (def.area) s.areaStyle = { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: c + "55" }, { offset: 1, color: c + "00" }]) };
  }
  if (def.tipo === "scatter") s.symbolSize = 4;
  if (def.tipo === "bar") { s.barMaxWidth = 10; s.itemStyle = { borderRadius: [3, 3, 0, 0] }; }
  return s;
}

// Inserta un punto nulo en cada hueco (> 2.5 × resolución) para que la línea se CORTE en vez de
// unir en diagonal dos extremos separados por horas sin datos (eso aparentaba datos inexistentes).
function conCortes(t, ys, resSeg) {
  const lim = resSeg * 2500, out = [];
  for (let i = 0; i < t.length; i++) {
    if (i && t[i] - t[i - 1] > lim) out.push([t[i - 1] + resSeg * 1000, null]);
    out.push([t[i], ys[i]]);
  }
  return out;
}

function crearGrafica(cont, titulo, idGrafica, opcion, alAmpliar) {
  const caja = el("div", { class: "grafica" },
    el("header", {}, el("h3", {}, titulo), el("div", { class: "herr" },
      alAmpliar ? el("button", { onclick: alAmpliar, title: "Ampliar" }, "⤢") : null,
      el("button", { title: "Descargar PNG", onclick: () => descargarPNG(idGrafica, titulo) }, "PNG"))),
    el("div", { class: "lienzo" }));
  caja.style.animationDelay = `${cont.children.length * 70}ms`;
  cont.append(caja);
  const inst = echarts.init($(".lienzo", caja), null, { renderer: "canvas" });
  inst.setOption(opcion);
  S.graficas.set(idGrafica, inst);
  return inst;
}

function descargarPNG(id, nombre) {
  const g = S.graficas.get(id);
  if (!g) return;
  const a = el("a", { href: g.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: css("--panel-solido") }),
    download: `${(nombre || id).replace(/[^\wáéíóúñ-]+/gi, "_")}.png` });
  a.click();
}

function limpiarGraficas(prefijo) {
  for (const [id, g] of S.graficas) if (!prefijo || id.startsWith(prefijo)) { g.dispose(); S.graficas.delete(id); }
}

function graficasDeSerie(cont, prefijo, serie, { zoom = false, grupos = GRUPOS, ampliar = true } = {}) {
  limpiarGraficas(prefijo);
  cont.replaceChildren();
  const vars = serie.variables, t = serie.t.map((x) => x * 1000);
  let n = 0;
  for (const g of grupos) {
    const defs = g.series.filter((d) => vars[d.k]);
    if (!defs.length) continue;
    const ejes = [{ nombre: vars[defs[0].k].unidad, max: g.ymax, min: g.ymax ? 0 : undefined }];
    const d1 = defs.find((d) => d.eje === 1);
    if (d1) ejes.push({ nombre: vars[d1.k].unidad });
    const op = opcionBase({ ejes, zoom });
    op.series = defs.map((d) => serieEcharts(d, vars[d.k].etiqueta, conCortes(t, serie.series[d.k], serie.resolucion), vars[d.k].unidad));
    if (!t.length) op.graphic = { type: "text", left: "center", top: "middle", style: { text: "Sin datos en este rango", fill: css("--tenue"), fontSize: 14 } };
    const id = `${prefijo}-${g.id}`;
    crearGrafica(cont, g.titulo, id, op, ampliar ? () => ampliarGrafica(g.titulo, op) : null);
    n++;
  }
  if (!n) cont.append(el("div", { class: "panel vacio" }, "Elige al menos una variable"));
}

function ampliarGrafica(titulo, op) {
  $("#mg-titulo").textContent = titulo;
  abrirModal("#modal-grafica");
  limpiarGraficas("modal");
  requestAnimationFrame(() => {
    const g = echarts.init($("#mg-grafica"));
    const o = { ...op, ...opcionBase({ ejes: op.yAxis.map((y) => ({ nombre: y.name, max: y.max, min: y.min })), zoom: true }), series: op.series };
    g.setOption(o);
    S.graficas.set("modal", g);
  });
  $("#mg-png").onclick = () => descargarPNG("modal", titulo);
}

// ====================================================================== estado / conexión
const UV_BANDAS = [[3, "Bajo", "--c5"], [6, "Moderado", "--c3"], [8, "Alto", "--c1"], [11, "Muy alto", "--error"], [99, "Extremo", "--c6"]];
const VPD_BANDAS = [[0.4, "Muy húmedo", "--c4"], [0.8, "Propagación", "--c2"], [1.2, "Óptimo", "--c5"], [1.6, "Alto", "--c3"], [99, "Estrés", "--error"]];
const banda = (v, b) => v === null || v === undefined ? null : b.find(([lim]) => v < lim);

function animarNumero(nodo, desde, hasta, dec) {
  if (desde === null || desde === undefined || Number.isNaN(desde) || hasta === null) { nodo.textContent = fmt(hasta, dec); return; }
  const t0 = performance.now(), dur = 650;
  const paso = (t) => {
    const p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
    nodo.textContent = fmt(desde + (hasta - desde) * e, dec);
    if (p < 1) requestAnimationFrame(paso);
  };
  requestAnimationFrame(paso);
}

function definicionTarjetas(e, sdr) {
  const lluvia = S.estado?.pluviometro_activo;
  const uvB = banda(e.uv, UV_BANDAS), vpdB = banda(e.vpd_kpa, VPD_BANDAS);
  const t = [
    { k: "temp", et: "Temperatura", v: e.temp_c, u: "°C", d: 1, det: e.punto_rocio_c !== null ? `Rocío ${fmt(e.punto_rocio_c)} °C` : "" },
    { k: "humedad", et: "Humedad relativa", v: e.humedad_pct, u: "%", d: 0 },
    { k: "vpd", et: "VPD", v: e.vpd_kpa, u: "kPa", d: 2, banda: vpdB },
    { k: "viento", et: "Viento", v: e.viento_kmh, u: "km/h", d: 1, brujula: true, det: `${e.viento_cardinal || "--"} · ${fmt(e.viento_dir_grados, 0)}°` },
    { k: "rafaga", et: "Ráfaga", v: e.rafaga_kmh, u: "km/h", d: 1 },
    { k: "solar", et: "Radiación solar", v: e.solar_w_m2, u: "W/m²", d: 0 },
    { k: "uv", et: "Índice UV", v: e.uv, u: "", d: 1, banda: uvB },
  ];
  if (lluvia) t.push({ k: "lluvia", et: "Lluvia hoy", v: e.lluvia_hoy_mm, u: "mm", d: 1, det: `Tasa ${fmt(e.lluvia_tasa_mm_h)} mm/h` });
  t.push({ k: "calidad", et: "Enlace SDR", v: sdr.calidad_ultima_ventana_pct, u: "%", d: 0,
    det: `${sdr.estado || "--"} · ${sdr.paquetes_validos ?? 0} tramas · batería TX ${e.bateria_tx || "--"}` });
  return t;
}

function pintarTarjetas() {
  const cont = $("#tarjetas");
  const d = S.estado?.datos;
  if (!d) { cont.replaceChildren(el("div", { class: "panel vacio" }, S.estado?.motivo || "Sin datos del receptor")); return; }
  const e = d.estacion, sdr = d.sdr, edades = e.edad_seg || {};
  const defs = definicionTarjetas(e, sdr);
  const nuevo = cont.dataset.n !== String(defs.length);
  if (nuevo) { cont.replaceChildren(); cont.dataset.n = defs.length; limpiarGraficas("brujula"); limpiarGraficas("mini"); }
  defs.forEach((t, i) => {
    let c = cont.children[i];
    const colorVar = t.banda ? t.banda[2] : COLORES[t.k] || "--c8";
    if (!c) {
      c = el("div", { class: "tarjeta" + (t.brujula ? " brujula" : ""), style: `animation-delay:${i * 45}ms`, "data-k": t.k },
        el("div", { class: "et" }, el("span", {}, t.et), el("span", { class: "tend" })),
        el("div", { class: "valor" }, el("span", { class: "num" }, "--"), el("small", {}, t.u)),
        el("div", { class: "det" }), t.brujula ? el("div", { class: "g" }) : el("div", { class: "mini" }));
      c.addEventListener("click", () => abrirVariable(t.k));
      cont.append(c);
    }
    c.style.setProperty("--color", css(colorVar));
    const num = $(".num", c), prev = S.prevValores[t.k];
    if (prev !== t.v) {
      animarNumero(num, prev, t.v, t.d);
      if (prev !== undefined) { num.classList.remove("destello"); void num.offsetWidth; num.classList.add("destello"); }
      const tend = $(".tend", c);
      tend.textContent = (prev === undefined || prev === null || t.v === null) ? "" : t.v > prev ? "▲" : t.v < prev ? "▼" : "";
      S.prevValores[t.k] = t.v;
    }
    const det = $(".det", c);
    det.replaceChildren(t.banda ? el("span", { class: "etiqueta-banda" }, t.banda[1]) : "", t.banda && t.det ? " " : "", t.det || "");
    const clave = { temp: "temp", humedad: "humedad", viento: "viento_vel", rafaga: "rafaga", solar: "solar", uv: "uv" }[t.k];
    c.classList.toggle("vieja", S.estado.conexion === "fuera_de_linea" || (clave && edades[clave] > 600));
    if (t.brujula) pintarBrujula($(".g", c), e.viento_dir_grados, t.v);
  });
  pintarMinis();
}

function pintarBrujula(nodo, dir, vel) {
  let g = S.graficas.get("brujula");
  const tx = css("--texto2");
  if (!g) {
    g = echarts.init(nodo);
    S.graficas.set("brujula", g);
    g.setOption({ animationDurationUpdate: 1200, animationEasingUpdate: "elasticOut",
      series: [{ type: "gauge", startAngle: 90, endAngle: -270, min: 0, max: 360, splitNumber: 8, radius: "92%",
        axisLine: { lineStyle: { width: 6, color: [[1, css("--borde")]] } },
        axisTick: { distance: -6, length: 4, lineStyle: { color: tx } }, splitLine: { distance: -8, length: 9, lineStyle: { color: tx } },
        axisLabel: { distance: 10, color: tx, fontSize: 11, formatter: (v) => ({ 0: "N", 45: "NE", 90: "E", 135: "SE", 180: "S", 225: "SO", 270: "O", 315: "NO" }[v] || "") },
        pointer: { icon: "path://M2 -60 L0 -70 L-2 -60 L-6 0 L6 0 Z", length: "70%", width: 8, itemStyle: { color: css("--c2") } },
        anchor: { show: true, size: 10, itemStyle: { color: css("--c2") } },
        detail: { show: false }, title: { show: false }, data: [{ value: 0 }] }] });
  }
  g.setOption({ series: [{ data: [{ value: dir ?? 0 }] }] });
}

function pintarMinis() {
  const s = S.vivoSerie;
  if (!s) return;
  for (const c of $$("#tarjetas .tarjeta")) {
    const k = c.dataset.k, m = $(".mini", c);
    if (!m || !s.series[k]) continue;
    const id = "mini-" + k;
    let g = S.graficas.get(id);
    if (!g) { g = echarts.init(m); S.graficas.set(id, g); }
    const datos = conCortes(s.t.map((t) => t * 1000), s.series[k], s.resolucion).slice(-180);
    const col = color(k);
    g.setOption({ animationDuration: 1000, grid: { left: 0, right: 0, top: 4, bottom: 0 },
      xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
      series: [{ type: "line", data: datos, showSymbol: false, smooth: .3, lineStyle: { width: 1.5, color: col },
        areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: col + "40" }, { offset: 1, color: col + "00" }]) } }] });
  }
}

function pintarConexion() {
  const e = S.estado, nodo = $("#conexion");
  nodo.dataset.estado = e.conexion;
  $(".texto", nodo).textContent = { en_linea: "En vivo", sincronizando: "Sincronizando", fuera_de_linea: "Fuera de línea" }[e.conexion];
  nodo.title = e.motivo;
  const av = $("#aviso-offline");
  av.classList.toggle("oculto", e.conexion === "en_linea");
  av.classList.toggle("sync", e.conexion === "sincronizando");
  $("#aviso-titulo").textContent = e.conexion === "sincronizando" ? "Receptor sincronizando con la estación." : "Sistema fuera de línea.";
  $("#aviso-motivo").textContent = e.motivo + ".";
}

async function actualizarEstado() {
  try {
    const anterior = S.estado?.conexion;
    S.estado = await api("/api/estado");
    pintarConexion();
    if (S.tab === "vivo") pintarTarjetas();
    if (anterior && anterior !== S.estado.conexion) {
      toast(S.estado.conexion === "en_linea" ? "Recepción restablecida" : S.estado.motivo, S.estado.conexion !== "en_linea");
      if (S.tab === "vivo") cargarVivo(true);
    }
  } catch (err) {
    S.estado = { conexion: "fuera_de_linea", motivo: "No se puede contactar al servidor", datos: S.estado?.datos };
    pintarConexion();
  }
}

// ====================================================================== EN VIVO
function finVivo() {
  // fuera de línea: la ventana termina en el último dato disponible, no "ahora"
  const r = S.meta?.rango?.hasta;
  return S.estado?.conexion === "fuera_de_linea" && r ? r : Date.now() / 1000;
}

async function cargarVivo(redibujar = true) {
  const hasta = finVivo(), desde = hasta - S.vivoHoras * 3600;
  const vars = VARS_VIVO.filter((k) => S.meta.variables[k]);
  $("#vivo-rango-txt").textContent = S.estado?.conexion === "fuera_de_linea" ? `· últimos datos hasta ${fechaTxt(hasta * 1000)}` : "· actualización cada minuto";
  try {
    const s = await api(`/api/serie?desde=${desde}&hasta=${hasta + 60}&vars=${vars.join(",")}&res=${S.vivoHoras > 24 ? 300 : 60}`);
    S.vivoSerie = s;
    if (redibujar) graficasDeSerie($("#graficas-vivo"), "vivo", s, { grupos: GRUPOS.filter((g) => g.id !== "direccion") });
    else actualizarSeriesVivo(s);
    pintarMinis();
  } catch (err) { toast("No se pudieron cargar las tendencias: " + err.message, true); }
}

function actualizarSeriesVivo(s) {
  const t = s.t.map((x) => x * 1000);
  for (const g of GRUPOS) {
    const inst = S.graficas.get(`vivo-${g.id}`);
    if (!inst) continue;
    const defs = g.series.filter((d) => s.variables[d.k]);
    inst.setOption({ series: defs.map((d) => ({ data: conCortes(t, s.series[d.k], s.resolucion) })) });
  }
}

function abrirVariable(k) {
  const s = S.vivoSerie;
  if (!s || !s.series[k]) return;
  const g = GRUPOS.find((gr) => gr.series.some((d) => d.k === k));
  const defs = g.series.filter((d) => s.variables[d.k]);
  const ejes = [{ nombre: s.variables[defs[0].k].unidad, max: g.ymax, min: g.ymax ? 0 : undefined }];
  const d1 = defs.find((d) => d.eje === 1);
  if (d1) ejes.push({ nombre: s.variables[d1.k].unidad });
  const op = opcionBase({ ejes, zoom: true });
  op.series = defs.map((d) => serieEcharts(d, s.variables[d.k].etiqueta, conCortes(s.t.map((x) => x * 1000), s.series[d.k], s.resolucion), s.variables[d.k].unidad));
  ampliarGrafica(`${g.titulo} · últimas ${S.vivoHoras} h`, op);
}

// ====================================================================== HISTÓRICO
const H = { vars: new Set(["temp", "rocio", "humedad", "vpd", "viento", "rafaga", "solar", "uv"]) };

function chipsVariables(cont, conjunto, alCambiar) {
  cont.replaceChildren();
  for (const [k, v] of Object.entries(S.meta.variables)) {
    const c = el("span", { class: "chip" + (conjunto.has(k) ? " on" : ""), style: `--color:${color(k)}` }, v.etiqueta);
    c.addEventListener("click", () => { conjunto.has(k) ? conjunto.delete(k) : conjunto.add(k); c.classList.toggle("on"); alCambiar && alCambiar(); });
    cont.append(c);
  }
}

function presetRango(p, ini, fin) {
  const ahora = new Date(), hoy = new Date(ahora.getFullYear(), ahora.getMonth(), ahora.getDate());
  let a, b = ahora;
  const r = S.meta.rango;
  if (p === "hoy") a = hoy;
  else if (p === "ayer") { a = new Date(hoy - 86400000); b = hoy; }
  else if (p === "todo") { a = new Date(r.desde * 1000); b = new Date(r.hasta * 1000 + 60000); }
  else a = new Date(ahora - Number(p) * 86400000);
  ini.value = aLocal(a); fin.value = aLocal(b);
}

async function cargarHistorico() {
  const desde = $("#h-desde").value, hasta = $("#h-hasta").value, res = $("#h-res").value;
  if (!desde || !hasta) return;
  const btn = $("#h-consultar"); btn.disabled = true;
  try {
    const s = await api(`/api/serie?desde=${desde}&hasta=${hasta}&vars=${[...H.vars].join(",")}${res ? "&res=" + res : ""}`);
    graficasDeSerie($("#graficas-historico"), "hist", s, { zoom: true });
    pintarEstadisticas($("#h-stats"), s);
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; }
}

function pintarEstadisticas(cont, s) {
  cont.replaceChildren();
  const res = s.resolucion >= 3600 ? `${s.resolucion / 3600} h` : `${s.resolucion / 60} min`;
  cont.append(el("div", { class: "estadistica" }, el("b", {}, "Puntos"), el("span", {}, `${s.t.length} · resolución ${res}`)));
  for (const [k, v] of Object.entries(s.variables)) {
    const xs = s.series[k].filter((x) => x !== null);
    if (!xs.length) continue;
    const prom = xs.reduce((a, b) => a + b, 0) / xs.length;
    const txt = v.agregacion === "sum" ? `total ${fmt(xs.reduce((a, b) => a + b, 0), v.decimales)}`
      : `mín ${fmt(Math.min(...xs), v.decimales)} · prom ${fmt(prom, v.decimales)} · máx ${fmt(Math.max(...xs), v.decimales)}`;
    cont.append(el("div", { class: "estadistica", style: `border-left:3px solid ${color(k)}` }, el("b", {}, `${v.etiqueta} (${v.unidad})`), el("span", {}, txt)));
  }
}

// ====================================================================== PROMEDIADO DAVIS
async function cargarPromediado() {
  const desde = $("#p-desde").value, hasta = $("#p-hasta").value, iv = $("#p-intervalo").value, met = $("#p-metodo").value;
  const btn = $("#p-consultar"); btn.disabled = true;
  try {
    const [mov, arc] = await Promise.all([
      api(`/api/promediado/movil?desde=${desde}&hasta=${hasta}&ventana=${iv}`),
      api(`/api/promediado/archivo?desde=${desde}&hasta=${hasta}&intervalo=${iv}&metodo=${met}`)]);
    limpiarGraficas("prom");
    const cont = $("#graficas-promediado"); cont.replaceChildren();
    const t = mov.t.map((x) => x * 1000);
    for (const [k, v] of Object.entries(mov.variables)) {
      const op = opcionBase({ ejes: [{ nombre: v.unidad }], zoom: true });
      const c = color(k);
      op.series = [
        { name: "Instantáneo (1 min)", type: "line", showSymbol: false, data: conCortes(t, mov.instantaneo[k], 60), lineStyle: { width: 1, opacity: .45 }, color: c, sampling: "lttb" },
        { name: `Promedio móvil ${mov.ventana_min} min`, type: "line", showSymbol: false, smooth: .2, data: conCortes(t, mov.promediado[k], 60), lineStyle: { width: 2.6 }, color: css("--texto") },
        { name: `Registro Davis (${met})`, type: "scatter", symbolSize: 6, color: css("--acento"), data: arc.registros.map((r) => [r.fin * 1000, r[k]]) },
      ];
      crearGrafica(cont, `${v.etiqueta} — método Davis`, `prom-${k}`, op, () => ampliarGrafica(v.etiqueta, op));
    }
    $("#p-total").textContent = `· ${arc.registros.length} registros de ${iv} min (${met})`;
    tablaRegistros($("#p-tabla"), arc.registros);
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; }
}

const COLS_ARCHIVO = [["fin", "Fecha/hora"], ["temp", "Temp °C"], ["temp_alta", "Alta"], ["temp_baja", "Baja"], ["humedad", "HR %"],
  ["rocio", "Rocío"], ["vpd", "VPD"], ["viento", "Viento"], ["cardinal", "Dir."], ["rafaga", "Ráfaga"], ["solar", "Rad. W/m²"],
  ["solar_alta", "Rad. alta"], ["uv", "UV"], ["uv_alto", "UV alto"], ["lluvia", "Lluvia"], ["cobertura_pct", "Cobertura %"]];

function tablaRegistros(tabla, regs) {
  const cols = COLS_ARCHIVO.filter(([k]) => k !== "lluvia" || S.meta.pluviometro_activo);
  tabla.replaceChildren(el("thead", {}, el("tr", {}, cols.map(([, n]) => el("th", {}, n)))),
    el("tbody", {}, regs.slice(-300).reverse().map((r) => el("tr", {}, cols.map(([k]) =>
      el("td", {}, k === "fin" ? fechaTxt(r.fin * 1000) : r[k] === null || r[k] === undefined ? "--" : String(r[k])))))));
}

// ====================================================================== CONSULTOR
const C = { vars: new Set(["temp", "humedad", "rocio", "vpd", "viento", "rafaga", "solar", "uv"]) };

function parametrosConsultor() {
  const p = new URLSearchParams({ fuente: $("#c-fuente").value, desde: $("#c-desde").value, hasta: $("#c-hasta").value });
  if (p.get("fuente") === "minuto") { p.set("vars", [...C.vars].join(",")); if ($("#c-res").value) p.set("res", $("#c-res").value); }
  if (p.get("fuente") === "promediado") { p.set("intervalo", $("#c-intervalo").value); p.set("metodo", $("#c-metodo").value); }
  return p;
}

async function previaConsultor() {
  const p = parametrosConsultor(), btn = $("#c-previa"); btn.disabled = true;
  try {
    const d = await api(`/api/vista-previa?${p}`);
    $("#c-panel-tabla").classList.remove("oculto");
    $("#c-total").textContent = `· ${d.total} filas (se muestran ${Math.min(200, d.total)})`;
    $("#c-tabla").replaceChildren(el("thead", {}, el("tr", {}, d.encabezados.map((h) => el("th", {}, h)))),
      el("tbody", {}, d.filas.map((f) => el("tr", {}, f.map((v) => el("td", {}, v === null ? "--" : String(v)))))));
    const cont = $("#graficas-consultor");
    if (p.get("fuente") === "minuto") {
      const s = await api(`/api/serie?${p}`);
      graficasDeSerie(cont, "cons", s, { zoom: true });
    } else if (p.get("fuente") === "promediado") {
      const a = await api(`/api/promediado/archivo?${p}`);
      limpiarGraficas("cons"); cont.replaceChildren();
      const op = opcionBase({ ejes: [{ nombre: "°C / %" }, { nombre: "W/m²" }], zoom: true });
      const t = a.registros.map((r) => r.fin * 1000);
      op.series = [["temp", 0], ["humedad", 0], ["solar", 1]].map(([k, eje]) =>
        serieEcharts({ k, eje, area: k === "solar" }, S.meta.variables[k].etiqueta, t.map((x, i) => [x, a.registros[i][k]]), S.meta.variables[k].unidad));
      crearGrafica(cont, `Promediado Davis · ${a.intervalo_min} min`, "cons-prom", op);
    } else { limpiarGraficas("cons"); cont.replaceChildren(el("div", { class: "panel nota" }, "Las tramas crudas no se grafican en la vista previa; elige el formato PNG para obtener sus gráficas.")); }
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; }
}

async function descargarConsultor() {
  const p = parametrosConsultor();
  const formatos = $$("#tab-consultor .checks input:checked").map((c) => c.value);
  if (!formatos.length) return toast("Elige al menos un formato", true);
  p.set("formatos", formatos.join(","));
  const btn = $("#c-descargar"); btn.disabled = true; btn.textContent = "Generando…";
  try {
    const r = await api(`/api/exportar?${p}`, { blob: true });
    const nombre = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || "davis.dat";
    const url = URL.createObjectURL(await r.blob());
    el("a", { href: url, download: nombre }).click();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    toast(`Descargado: ${nombre}`);
  } catch (err) { toast(err.message, true); } finally { btn.disabled = false; btn.textContent = "⬇ Descargar"; }
}

// ====================================================================== USUARIOS (admin)
async function cargarUsuarios() {
  try {
    const [{ usuarios }, { eventos }] = await Promise.all([api("/api/usuarios"), api("/api/auditoria?limite=150")]);
    const t = $("#tabla-usuarios");
    t.replaceChildren(el("thead", {}, el("tr", {}, ["Usuario", "Nombre", "Rol", "Estado", "Último acceso", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, usuarios.map((u) => el("tr", {},
        el("td", {}, u.usuario), el("td", {}, u.nombre || ""),
        el("td", {}, el("span", { class: "pill " + (u.rol === "admin" ? "admin" : "") }, u.rol)),
        el("td", {}, el("span", { class: "pill " + (u.activo ? "" : "inactivo") }, u.activo ? "activo" : "inactivo")),
        el("td", {}, u.ultimo_acceso ? fechaTxt(u.ultimo_acceso * 1000) : "nunca"),
        el("td", {}, el("div", { class: "acc" },
          el("button", { class: "btn btn-sec", onclick: () => editarUsuario(u.id, { activo: !u.activo }) }, u.activo ? "Desactivar" : "Activar"),
          el("button", { class: "btn btn-sec", onclick: () => editarUsuario(u.id, { rol: u.rol === "admin" ? "usuario" : "admin" }) }, u.rol === "admin" ? "Hacer usuario" : "Hacer admin"),
          el("button", { class: "btn btn-sec", onclick: () => restablecerClave(u) }, "Restablecer clave"),
          u.id !== S.usuario.id ? el("button", { class: "btn btn-peligro", onclick: () => borrarUsuario(u) }, "Eliminar") : null))))));
    $("#tabla-auditoria").replaceChildren(el("thead", {}, el("tr", {}, ["Fecha", "Usuario", "Acción", "Detalle", "IP"].map((h) => el("th", {}, h)))),
      el("tbody", {}, eventos.map((e) => el("tr", {}, el("td", {}, fechaTxt(e.ts * 1000)), el("td", {}, e.usuario || ""),
        el("td", {}, e.accion), el("td", { style: "text-align:left" }, e.detalle || ""), el("td", {}, e.ip || "")))));
  } catch (err) { toast(err.message, true); }
}
async function editarUsuario(id, cambios) {
  try { await api(`/api/usuarios/${id}`, { metodo: "POST", cuerpo: cambios }); toast("Cuenta actualizada"); cargarUsuarios(); } catch (e) { toast(e.message, true); }
}
async function restablecerClave(u) {
  const c = prompt(`Nueva contraseña temporal para ${u.usuario} (mín. 10 caracteres, letras y números). Se le pedirá cambiarla al entrar:`);
  if (!c) return;
  try { await api(`/api/usuarios/${u.id}/clave`, { metodo: "POST", cuerpo: { clave: c } }); toast("Contraseña restablecida"); } catch (e) { toast(e.message, true); }
}
async function borrarUsuario(u) {
  if (!confirm(`¿Eliminar la cuenta "${u.usuario}"? No se puede deshacer.`)) return;
  try { await api(`/api/usuarios/${u.id}`, { metodo: "DELETE" }); toast("Cuenta eliminada"); cargarUsuarios(); } catch (e) { toast(e.message, true); }
}

// ====================================================================== sesión
function pintarSesion() {
  const u = S.usuario;
  document.body.classList.toggle("con-sesion", !!u);
  $("#btn-login").classList.toggle("oculto", !!u);
  $("#menu-usuario").classList.toggle("oculto", !u);
  $$(".solo-admin").forEach((x) => x.classList.toggle("oculto", !(u && u.rol === "admin")));
  if (u) $("#btn-usuario").textContent = `${u.nombre || u.usuario} ▾`;
  if (u && u.cambiar_clave) { $("#clave-nota").textContent = "El administrador pidió que cambies tu contraseña."; abrirModal("#modal-clave"); }
  if (!u && S.tab === "usuarios") location.hash = "#vivo";
}

function abrirModal(s) { $(s).classList.remove("oculto"); const i = $("input", $(s)); if (i) setTimeout(() => i.focus(), 50); }
function cerrarModales() { $$(".modal").forEach((m) => m.classList.add("oculto")); limpiarGraficas("modal"); }

// ====================================================================== navegación
function irA(tab) {
  if (!["vivo", "historico", "promediado", "consultor", "usuarios"].includes(tab)) tab = "vivo";
  if (tab === "usuarios" && S.usuario?.rol !== "admin") tab = "vivo";
  S.tab = tab;
  $$(".pestanas a").forEach((a) => a.classList.toggle("activa", a.dataset.tab === tab));
  $$(".tab").forEach((s) => s.classList.toggle("oculto", s.id !== `tab-${tab}`));
  const sec = $(`#tab-${tab}`); sec.style.animation = "none"; void sec.offsetWidth; sec.style.animation = "";
  // al abrir cada sección las gráficas se crean de nuevo: la animación arranca de cero hasta el punto actual
  if (tab === "vivo") { pintarTarjetas(); cargarVivo(true); }
  if (tab === "historico") cargarHistorico();
  if (tab === "promediado" && S.usuario) cargarPromediado();
  if (tab === "usuarios") cargarUsuarios();
  if (tab === "consultor") { limpiarGraficas("cons"); $("#graficas-consultor").replaceChildren(); }
}

// ====================================================================== arranque
function poblarSelect(sel, valores, etiqueta, defecto) {
  for (const v of valores) sel.append(el("option", { value: v, selected: v === defecto }, etiqueta(v)));
}
const txtRes = (r) => r >= 86400 ? "1 día" : r >= 3600 ? `${r / 3600} h` : `${r / 60} min`;

async function iniciar() {
  const tema = localStorageSeguro("tema");
  if (tema) document.documentElement.dataset.tema = tema;
  setInterval(() => { $("#reloj").textContent = new Date().toLocaleTimeString("es-MX"); }, 1000);

  S.meta = await api("/api/meta");
  document.title = S.meta.titulo; $("#titulo").textContent = S.meta.titulo;
  $("#pie-version").textContent = `davis_web ${S.meta.version}`;
  if (S.meta.rango.desde) $("#pie-rango").textContent = `datos del ${fechaTxt(S.meta.rango.desde * 1000)} al ${fechaTxt(S.meta.rango.hasta * 1000)}`;
  try { S.usuario = (await api("/api/sesion")).usuario; } catch { S.usuario = null; }

  for (const s of ["#h-res", "#c-res"]) poblarSelect($(s), S.meta.resoluciones, txtRes);
  for (const s of ["#p-intervalo", "#c-intervalo"]) poblarSelect($(s), S.meta.intervalos_promediado, (v) => `${v} min`, 30);
  presetRango("1", $("#h-desde"), $("#h-hasta"));
  presetRango("2", $("#p-desde"), $("#p-hasta"));
  presetRango("hoy", $("#c-desde"), $("#c-hasta"));
  chipsVariables($("#h-vars"), H.vars, cargarHistorico);
  chipsVariables($("#c-vars"), C.vars);

  // eventos
  window.addEventListener("hashchange", () => irA(location.hash.slice(1)));
  window.addEventListener("resize", () => S.graficas.forEach((g) => g.resize()));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { actualizarEstado(); irA(S.tab); } });
  $("#vivo-rango").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("#vivo-rango button").forEach((x) => x.classList.toggle("activo", x === b));
    S.vivoHoras = Number(b.dataset.h); cargarVivo(true);
  });
  $("#h-atajos").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { presetRango(b.dataset.p, $("#h-desde"), $("#h-hasta")); cargarHistorico(); } });
  $("#h-consultar").onclick = cargarHistorico;
  $("#p-consultar").onclick = cargarPromediado;
  $("#p-descargar").onclick = () => {
    const p = new URLSearchParams({ fuente: "promediado", desde: $("#p-desde").value, hasta: $("#p-hasta").value,
      intervalo: $("#p-intervalo").value, metodo: $("#p-metodo").value, formatos: "csv" });
    api(`/api/exportar?${p}`, { blob: true }).then(async (r) => {
      const url = URL.createObjectURL(await r.blob());
      el("a", { href: url, download: r.headers.get("Content-Disposition").match(/filename="([^"]+)"/)[1] }).click();
    }).catch((e) => toast(e.message, true));
  };
  $("#c-fuente").onchange = () => {
    const f = $("#c-fuente").value;
    $("#c-op-minuto").classList.toggle("oculto", f !== "minuto");
    $("#c-op-prom").classList.toggle("oculto", f !== "promediado");
    $("#c-vars").classList.toggle("oculto", f !== "minuto");
  };
  $("#c-previa").onclick = previaConsultor;
  $("#c-descargar").onclick = descargarConsultor;
  $("#c-png-pantalla").onclick = () => {
    const ids = [...S.graficas.keys()].filter((k) => k.startsWith("cons"));
    if (!ids.length) return toast("Primero genera la vista previa", true);
    ids.forEach((id) => descargarPNG(id, id));
  };
  $("#btn-ver-historico").onclick = () => {
    const fin = (S.meta.rango.hasta || Date.now() / 1000) * 1000;
    $("#h-desde").value = aLocal(fin - 86400000); $("#h-hasta").value = aLocal(fin + 60000);
    location.hash = "#historico"; if (S.tab === "historico") cargarHistorico();
  };
  $("#btn-tema").onclick = () => {
    const t = document.documentElement.dataset.tema === "oscuro" ? "claro" : "oscuro";
    document.documentElement.dataset.tema = t; localStorageSeguro("tema", t);
    limpiarGraficas(); $("#tarjetas").dataset.n = ""; irA(S.tab);
  };
  $("#btn-login").onclick = () => abrirModal("#modal-login");
  $$(".abrir-login").forEach((b) => b.onclick = () => abrirModal("#modal-login"));
  $$(".cerrar-modal").forEach((b) => b.onclick = cerrarModales);
  $$(".modal").forEach((m) => m.addEventListener("click", (e) => { if (e.target === m) cerrarModales(); }));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") cerrarModales(); });
  $("#btn-usuario").onclick = () => $("#menu-usuario").classList.toggle("abierto");
  document.addEventListener("click", (e) => { if (!e.target.closest("#menu-usuario")) $("#menu-usuario").classList.remove("abierto"); });
  $("#btn-logout").onclick = async () => { try { await api("/api/logout", { metodo: "POST" }); } catch { /* */ } S.usuario = null; pintarSesion(); toast("Sesión cerrada"); irA(S.tab); };
  $("#btn-mi-clave").onclick = () => { $("#clave-nota").textContent = ""; abrirModal("#modal-clave"); };

  $("#form-login").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    $("#login-error").textContent = "";
    try {
      S.usuario = (await api("/api/login", { metodo: "POST", cuerpo: { usuario: f.get("usuario"), clave: f.get("clave") } })).usuario;
      e.target.reset(); cerrarModales(); pintarSesion(); toast(`Bienvenido, ${S.usuario.nombre || S.usuario.usuario}`); irA(S.tab);
    } catch (err) { $("#login-error").textContent = err.message; }
  });
  $("#form-clave").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    if (f.get("nueva") !== f.get("nueva2")) { $("#clave-error").textContent = "Las contraseñas nuevas no coinciden"; return; }
    try {
      const r = await api("/api/cuenta/clave", { metodo: "POST", cuerpo: { actual: f.get("actual"), nueva: f.get("nueva") } });
      e.target.reset(); cerrarModales(); toast(r.mensaje); S.usuario = null; pintarSesion(); abrirModal("#modal-login");
    } catch (err) { $("#clave-error").textContent = err.message; }
  });
  $("#form-usuario").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/usuarios", { metodo: "POST", cuerpo: { usuario: f.get("usuario"), nombre: f.get("nombre"), clave: f.get("clave"),
        rol: f.get("rol"), cambiar_clave: f.get("cambiar_clave") === "on" } });
      e.target.reset(); toast("Cuenta creada"); cargarUsuarios();
    } catch (err) { toast(err.message, true); }
  });

  pintarSesion();
  await actualizarEstado();
  irA(location.hash.slice(1) || "vivo");
  setInterval(actualizarEstado, 5000);
  setInterval(() => { if (S.tab === "vivo" && !document.hidden && S.estado?.conexion !== "fuera_de_linea") cargarVivo(false); }, 60000);
  setInterval(async () => { try { S.meta.rango = (await api("/api/meta")).rango; } catch { /* */ } }, 300000);
}

function localStorageSeguro(k, v) {
  try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; }
}

document.addEventListener("DOMContentLoaded", () => iniciar().catch((e) => {
  document.body.prepend(el("div", { class: "aviso" }, el("span", {}, "⚠"), el("div", {}, "No se pudo iniciar el dashboard: " + e.message)));
}));
