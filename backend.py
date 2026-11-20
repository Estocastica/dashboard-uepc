import asyncio
import csv
import json
import os
import re
import sys
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from playwright.async_api import async_playwright, Browser, Page, Playwright

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

URL_SIMULADOR = "https://portal2.uepc.org.ar/nuevosimulador2017/Default.aspx"
IPC_CACHE_FILE = os.path.join(BASE_DIR, "ipc_pampeana_cache.csv")
PRECIOS_CACHE_FILE = os.path.join(BASE_DIR, "precios_indec_pampeana_cache.json")
IPC_SERIES_IDS = ["145.3_INGPAMANA_DICI_M_15", "148.3_INIVELANA_DICI_M_26"]
URL_PRECIOS_INDEC_XLS = "https://www.indec.gob.ar/ftp/cuadros/economia/sh_ipc_precios_promedio.xls"

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
    "--single-process",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-setuid-sandbox",
    "--js-flags=--max-old-space-size=192",
]

MESES_ES = {
    "enero": 1, "ene": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6,
    "julio": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sep": 9, "set": 9,
    "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12
}

NOMBRES_MESES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

CATALOGO_BIENES_INDEC = [
    {
        "id": "asado",
        "emoji": "🥩",
        "nombre": "Asado",
        "unidad": "kg",
        "patrones": [r"^asado\b"],
        "precio_fallback": 14850.0
    },
    {
        "id": "leche",
        "emoji": "🥛",
        "nombre": "Leche fresca entera en sachet",
        "unidad": "litros",
        "patrones": [r"leche\s+fresca\s+entera"],
        "precio_fallback": 1720.0
    },
    {
        "id": "pan",
        "emoji": "🥖",
        "nombre": "Pan francés tipo flauta",
        "unidad": "kg",
        "patrones": [r"pan\s+franc[eé]s"],
        "precio_fallback": 3450.0
    },
    {
        "id": "yerba",
        "emoji": "🧉",
        "nombre": "Yerba mate (paquete 500 g)",
        "unidad": "paquetes (500 g)",
        "patrones": [r"yerba\s+mate"],
        "precio_fallback": 2480.0
    },
    {
        "id": "fideos",
        "emoji": "🍝",
        "nombre": "Fideos secos tipo guisero (500 g)",
        "unidad": "paquetes (500 g)",
        "patrones": [r"fideos\s+secos"],
        "precio_fallback": 1650.0
    },
    {
        "id": "huevos",
        "emoji": "🥚",
        "nombre": "Huevos de gallina (docena)",
        "unidad": "docenas",
        "patrones": [r"huevos\s+de\s+gallina"],
        "precio_fallback": 3650.0
    },
    {
        "id": "aceite",
        "emoji": "🌻",
        "nombre": "Aceite de girasol (1,5 litros)",
        "unidad": "botellas (1,5 L)",
        "patrones": [r"aceite\s+de\s+girasol"],
        "precio_fallback": 4350.0
    },
    {
        "id": "pollo",
        "emoji": "🍗",
        "nombre": "Pollo entero",
        "unidad": "kg",
        "patrones": [r"pollo\s+entero"],
        "precio_fallback": 4290.0
    },
    {
        "id": "papa",
        "emoji": "🥔",
        "nombre": "Papa",
        "unidad": "kg",
        "patrones": [r"^papa\b"],
        "precio_fallback": 1380.0
    },
    {
        "id": "papel_higienico",
        "emoji": "🧻",
        "nombre": "Papel higiénico (4 rollos)",
        "unidad": "paquetes (4 u.)",
        "patrones": [r"papel\s+higi[eé]nico"],
        "precio_fallback": 2890.0
    }
]


def parsear_monto_ars(texto: str) -> float:
    limpio = texto.replace("$", "").replace("\xa0", "").strip()
    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")
    return float(limpio)


def formatear_monto_ars(valor: Optional[float]) -> str:
    if valor is None:
        return "N/D"
    formateado = f"{valor:,.2f}"
    formateado = formateado.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"${formateado}"


def cargar_ipc_pampeana() -> Tuple[Dict[str, float], str]:
    serie: Dict[str, float] = {}
    origen = ""

    for sid in IPC_SERIES_IDS:
        url = f"https://apis.datos.gob.ar/series/api/series/?ids={sid}&start_date=2022-01-01&limit=500&format=json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                for fila in payload.get("data", []):
                    if len(fila) >= 2 and fila[0] and fila[1] is not None:
                        ym = str(fila[0])[:7]
                        serie[ym] = float(fila[1])
            if "2026-10" in serie:
                origen = f"INDEC API oficial ({sid})"
                try:
                    with open(IPC_CACHE_FILE, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(["periodo", "ipc"])
                        for k in sorted(serie.keys()):
                            writer.writerow([k, serie[k]])
                except Exception:
                    pass
                break
        except Exception:
            continue

    if "2026-10" not in serie and os.path.exists(IPC_CACHE_FILE):
        with open(IPC_CACHE_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("periodo") and row.get("ipc"):
                    serie[row["periodo"][:7]] = float(row["ipc"])
        origen = "Caché local INDEC (ipc_pampeana_cache.csv)"

    if "2026-10" in serie:
        if "2026-11" not in serie:
            serie["2026-11"] = round(serie["2026-10"] * 1.03, 6)
        if "2026-12" not in serie:
            serie["2026-12"] = round(serie["2026-11"] * 1.03, 6)

    return serie, origen


def cargar_precios_medios_pampeana() -> Dict[str, Any]:
    precios_detectados: Dict[str, float] = {}
    periodo_detectado = "Octubre 2026"
    origen = "Referencia canasta INDEC Pampeana (oct. 2026)"

    try:
        import xlrd  # type: ignore
        req = urllib.request.Request(URL_PRECIOS_INDEC_XLS, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=18) as resp:
            xls_bytes = resp.read()

        wb = xlrd.open_workbook(file_contents=xls_bytes)
        sheet = wb.sheet_by_index(0)

        en_pampeana = False
        col_oct26: Optional[int] = None

        for r in range(sheet.nrows):
            fila_str = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            texto_fila = " ".join(fila_str).lower()

            if "pampeana" in texto_fila and len(texto_fila) < 120:
                en_pampeana = True
                col_oct26 = None
                continue

            if en_pampeana and any(reg in texto_fila for reg in ("noreste", "noroeste", "cuyo", "patagonia", "gba")) and "pampeana" not in texto_fila:
                if precios_detectados:
                    break

            if en_pampeana:
                for c in range(sheet.ncols):
                    cell = sheet.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        dt = xlrd.xldate_as_tuple(cell.value, wb.datemode)
                        if dt[0] == 2026 and dt[1] == 10:
                            col_oct26 = c
                    elif isinstance(cell.value, str) and re.search(r"oct.*26|2026-10", cell.value.lower()):
                        col_oct26 = c

                nombre_item = fila_str[0].lower() if fila_str else ""
                if nombre_item:
                    for bien in CATALOGO_BIENES_INDEC:
                        if bien["id"] in precios_detectados:
                            continue
                        if any(re.search(pat, nombre_item) for pat in bien["patrones"]):
                            val_encontrado = None
                            if col_oct26 is not None and col_oct26 < sheet.ncols:
                                v = sheet.cell_value(r, col_oct26)
                                if isinstance(v, (int, float)) and v > 0:
                                    val_encontrado = float(v)
                            if val_encontrado is None:
                                for c in range(sheet.ncols - 1, 0, -1):
                                    v = sheet.cell_value(r, c)
                                    if isinstance(v, (int, float)) and v > 50:
                                        val_encontrado = float(v)
                                        break
                            if val_encontrado is not None:
                                precios_detectados[bien["id"]] = round(val_encontrado, 2)

        if precios_detectados:
            origen = "INDEC oficial (sh_ipc_precios_promedio.xls — Región Pampeana)"
            try:
                with open(PRECIOS_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(
                        {"origen": origen, "periodo": periodo_detectado, "precios": precios_detectados},
                        f,
                        ensure_ascii=False,
                        indent=2
                    )
            except Exception:
                pass
    except Exception:
        if os.path.exists(PRECIOS_CACHE_FILE):
            try:
                with open(PRECIOS_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                    precios_detectados = cached.get("precios", {})
                    origen = cached.get("origen", origen)
                    periodo_detectado = cached.get("periodo", periodo_detectado)
            except Exception:
                pass

    bienes_finales = []
    for bien in CATALOGO_BIENES_INDEC:
        precio = precios_detectados.get(bien["id"], bien["precio_fallback"])
        es_oficial_xls = bien["id"] in precios_detectados
        bienes_finales.append({
            "id": bien["id"],
            "emoji": bien["emoji"],
            "nombre": bien["nombre"],
            "unidad": bien["unidad"],
            "precio_oct_2026": round(precio, 2),
            "precio_formateado": formatear_monto_ars(precio),
            "fuente_directa_xls": es_oficial_xls
        })

    return {
        "origen": origen,
        "periodo": periodo_detectado,
        "bienes": bienes_finales
    }


IPC_PAMPEANA, IPC_ORIGEN = cargar_ipc_pampeana()
CANASTA_BIENES_PAMPEANA = cargar_precios_medios_pampeana()


def calcular_analisis_real(resultados_raw: List[Dict[str, Any]]) -> Dict[str, Any]:
    ipc_base_oct26 = IPC_PAMPEANA.get("2026-10")

    registros_todos = []
    for r in resultados_raw:
        nom_val = parsear_monto_ars(r["salario"])
        ym = r["sort_key"]
        real_val: Optional[float] = None
        if ipc_base_oct26 and ym in IPC_PAMPEANA and IPC_PAMPEANA[ym] > 0:
            real_val = nom_val * (ipc_base_oct26 / IPC_PAMPEANA[ym])

        registros_todos.append({
            **r,
            "salario_nominal_num": round(nom_val, 2),
            "salario_real_num": round(real_val, 2) if real_val is not None else None,
            "salario_real": formatear_monto_ars(real_val)
        })

    salarios_por_mes: Dict[str, Dict[str, Any]] = {}
    for r in registros_todos:
        if r.get("tiene_fecha") and re.match(r"^\d{4}-\d{2}$", r["sort_key"]):
            salarios_por_mes[r["sort_key"]] = {
                "nominal": r["salario_nominal_num"],
                "propuesta": r["propuesta"]
            }

    registros_arrastre_inicial_2023: List[Dict[str, Any]] = []
    previos_2023 = sorted([k for k in salarios_por_mes.keys() if k < "2023-01"])
    claves_2023_en_adelante = sorted([k for k in salarios_por_mes.keys() if k >= "2023-01"])

    if previos_2023:
        ultimo_nominal: Optional[float] = salarios_por_mes[previos_2023[-1]]["nominal"]
        ultima_propuesta: str = salarios_por_mes[previos_2023[-1]]["propuesta"]
    elif claves_2023_en_adelante:
        primera_clave = claves_2023_en_adelante[0]
        ultimo_nominal = salarios_por_mes[primera_clave]["nominal"]
        ultima_propuesta = f"Arrastre inicial desde {salarios_por_mes[primera_clave]['propuesta']}"

        primer_mes_2023 = int(primera_clave.split("-")[1]) if primera_clave.startswith("2023-") else 1
        for m_prev in range(1, primer_mes_2023):
            ym_prev = f"2023-{m_prev:02d}"
            ipc_prev = IPC_PAMPEANA.get(ym_prev)
            real_prev: Optional[float] = None
            if ipc_base_oct26 and ipc_prev and ipc_prev > 0:
                real_prev = ultimo_nominal * (ipc_base_oct26 / ipc_prev)
            registros_arrastre_inicial_2023.append({
                "orden_dom": m_prev,
                "propuesta": f"{ultima_propuesta} ({NOMBRES_MESES[m_prev]})",
                "fecha": f"{NOMBRES_MESES[m_prev]} 2023",
                "sort_key": ym_prev,
                "tiene_fecha": True,
                "salario": formatear_monto_ars(ultimo_nominal),
                "salario_nominal_num": round(ultimo_nominal, 2),
                "salario_real_num": round(real_prev, 2) if real_prev is not None else None,
                "salario_real": formatear_monto_ars(real_prev)
            })
    else:
        ultimo_nominal = None
        ultima_propuesta = ""

    serie_mensual: List[Dict[str, Any]] = []
    for anio in (2023, 2024, 2025, 2026):
        for mes in range(1, 13):
            ym = f"{anio:04d}-{mes:02d}"
            es_arrastre = True
            if ym in salarios_por_mes:
                ultimo_nominal = salarios_por_mes[ym]["nominal"]
                ultima_propuesta = salarios_por_mes[ym]["propuesta"]
                es_arrastre = False
            elif anio == 2023 and not previos_2023 and claves_2023_en_adelante:
                es_arrastre = True

            ipc_val = IPC_PAMPEANA.get(ym)
            real_val: Optional[float] = None
            if ultimo_nominal is not None and ipc_base_oct26 and ipc_val and ipc_val > 0:
                real_val = ultimo_nominal * (ipc_base_oct26 / ipc_val)

            serie_mensual.append({
                "anio": anio,
                "mes": mes,
                "periodo": ym,
                "fecha": f"{NOMBRES_MESES[mes]} {anio}",
                "propuesta_vigente": ultima_propuesta if ultimo_nominal is not None else "Sin dato",
                "es_arrastre": es_arrastre,
                "es_proyeccion_ipc": ym in ("2026-11", "2026-12"),
                "ipc": round(ipc_val, 4) if ipc_val is not None else None,
                "nominal_num": round(ultimo_nominal, 2) if ultimo_nominal is not None else None,
                "nominal": formatear_monto_ars(ultimo_nominal),
                "real_num": round(real_val, 2) if real_val is not None else None,
                "real": formatear_monto_ars(real_val)
            })

    promedios_por_anio: Dict[int, Optional[float]] = {}
    meses_con_dato_por_anio: Dict[int, int] = {}

    for anio in (2023, 2024, 2025, 2026):
        vals = [m["real_num"] for m in serie_mensual if m["anio"] == anio and m["real_num"] is not None]
        meses_con_dato_por_anio[anio] = len(vals)
        promedios_por_anio[anio] = (sum(vals) / len(vals)) if vals else None

    base_2023 = promedios_por_anio.get(2023)
    resumen_anual: List[Dict[str, Any]] = []
    perdida_total_2024_2026 = 0.0

    for anio in (2023, 2024, 2025, 2026):
        prom = promedios_por_anio.get(anio)
        if prom is not None and base_2023 is not None and base_2023 > 0:
            indice_base_2023 = (prom / base_2023) * 100.0
            var_pct = ((prom / base_2023) - 1.0) * 100.0
            perdida_mensual = 0.0 if anio == 2023 else (base_2023 - prom)
            perdida_anual_12m = 0.0 if anio == 2023 else (perdida_mensual * 12.0)
        else:
            indice_base_2023 = None
            var_pct = None
            perdida_mensual = None
            perdida_anual_12m = None

        if anio in (2024, 2025, 2026) and perdida_anual_12m is not None:
            perdida_total_2024_2026 += perdida_anual_12m

        resumen_anual.append({
            "anio": anio,
            "meses_computados": meses_con_dato_por_anio[anio],
            "promedio_real_num": round(prom, 2) if prom is not None else None,
            "promedio_real": formatear_monto_ars(prom),
            "indice_2023_100": round(indice_base_2023, 2) if indice_base_2023 is not None else None,
            "variacion_pct_vs_2023": round(var_pct, 2) if var_pct is not None else None,
            "perdida_mensual_num": round(perdida_mensual, 2) if perdida_mensual is not None else None,
            "perdida_mensual": formatear_monto_ars(perdida_mensual) if anio != 2023 else "$0,00 (Año base)",
            "perdida_anual_12m_num": round(perdida_anual_12m, 2) if perdida_anual_12m is not None else None,
            "perdida_anual_12m": formatear_monto_ars(perdida_anual_12m) if anio != 2023 else "$0,00 (Año base)"
        })

    registros_desde_2023 = sorted(
        registros_arrastre_inicial_2023 + [r for r in registros_todos if r["sort_key"] >= "2023-01"],
        key=lambda x: (x["sort_key"], x.get("orden_dom", 0))
    )

    return {
        "resultados": registros_desde_2023,
        "serie_mensual": serie_mensual,
        "resumen_anual": resumen_anual,
        "perdida_total_2024_2026_num": round(perdida_total_2024_2026, 2),
        "perdida_total_2024_2026": formatear_monto_ars(perdida_total_2024_2026),
        "canasta_bienes": CANASTA_BIENES_PAMPEANA,
        "ipc_info": {
            "origen": IPC_ORIGEN or "No disponible",
            "ipc_2026_10": IPC_PAMPEANA.get("2026-10"),
            "ipc_2026_11_est": IPC_PAMPEANA.get("2026-11"),
            "ipc_2026_12_est": IPC_PAMPEANA.get("2026-12")
        }
    }


def parsear_inicio_propuesta(texto_propuesta: str) -> Optional[Tuple[int, int]]:
    fuente = texto_propuesta.lower()
    patron_mes_anio = r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)\b.*?\b(20\d{2})\b"
    match = re.search(patron_mes_anio, fuente)
    if match:
        return (int(match.group(2)), MESES_ES[match.group(1)])

    patron_mes_yy = r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[/\-\s]+(2[2-6])\b"
    match_yy = re.search(patron_mes_yy, fuente)
    if match_yy:
        return (2000 + int(match_yy.group(2)), MESES_ES[match_yy.group(1)])

    match_num = re.search(r"\b(0?[1-9]|1[0-2])[/\-](20\d{2})\b", fuente)
    if match_num:
        return (int(match_num.group(2)), int(match_num.group(1)))

    match_anio = re.search(r"\b(202[2-6])\b", fuente)
    if match_anio:
        return (int(match_anio.group(1)), 1)

    return None


def siguiente_mes(anio: int, mes: int) -> Tuple[int, int]:
    return (anio + 1, 1) if mes == 12 else (anio, mes + 1)


async def optimizar_carga_pagina(page: Page):
    async def interceptar(route):
        if route.request.resource_type in ("image", "font", "media"):
            await route.abort()
            return
        await route.continue_()

    await page.route("**/*", interceptar)


async def cerrar_modal_bienvenida(page: Page):
    try:
        btn_continuar = page.locator("button:has-text('Continuar'), input[value*='Continuar' i], a:has-text('Continuar')")
        if await btn_continuar.count() > 0 and await btn_continuar.first.is_visible():
            await btn_continuar.first.click()
            await page.wait_for_timeout(120)
    except Exception:
        pass


async def ejecutar_cambio_aspnet(page: Page, control_id: str, valor: str, forzar: bool = False):
    loc = page.locator(f"#{control_id}")
    if await loc.count() == 0:
        return

    info_el = await loc.evaluate(
        """(el, val) => {
            el.style.display = 'inline-block';
            el.style.visibility = 'visible';
            el.removeAttribute('disabled');
            const onchange = (el.getAttribute('onchange') || '').toLowerCase();
            return {
                yaSeleccionado: el.value === String(val),
                disparaPostback: onchange.includes('__dopostback')
            };
        }""",
        str(valor)
    )

    if info_el["yaSeleccionado"] and not forzar:
        return

    if info_el["disparaPostback"]:
        try:
            async with page.expect_response(
                lambda r: "default.aspx" in r.url.lower() and r.request.method == "POST",
                timeout=12000
            ):
                await loc.select_option(value=str(valor), force=True)
        except Exception:
            await page.wait_for_timeout(200)
    else:
        await loc.select_option(value=str(valor), force=True)
        await page.wait_for_timeout(120)

    try:
        await page.wait_for_function(
            """() => {
                if (window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager) {
                    const prm = Sys.WebForms.PageRequestManager.getInstance();
                    return !prm.get_isInAsyncPostBack();
                }
                return document.readyState === 'complete';
            }""",
            timeout=8000
        )
    except Exception:
        pass


async def obtener_info_propuesta_y_mes(page: Page) -> Dict[str, Any]:
    return await page.evaluate("""() => {
        const limpiar = (t) => (t || '').replace(/\\s+/g, ' ').replace(/:$/, '').trim();
        const mesesRegex = /^\\s*(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\\b/i;
        const placeholderRegex = /seleccione|especificar|^\\s*-+\\s*$/i;

        const getLabel = (el) => {
            if (el.id) {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                if (lbl && limpiar(lbl.innerText)) return limpiar(lbl.innerText);
            }
            const row = el.closest('.row, tr, .form-group');
            if (row) {
                const nodos = Array.from(row.querySelectorAll('label, span, strong, b, div, td'))
                    .filter(n => !n.contains(el) && n.children.length === 0 && !n.classList.contains('select2-selection__rendered'));
                for (const p of nodos) {
                    const txt = limpiar(p.innerText);
                    if (txt && txt.length > 1 && txt.length < 70 && !/^\\d+$/.test(txt)) return txt;
                }
            }
            return el.id || '';
        };

        let prop = null;
        let mes = null;

        const selects = Array.from(document.querySelectorAll('select'));
        for (const s of selects) {
            const lbl = getLabel(s).toLowerCase();
            const idName = (s.id + ' ' + s.name).toLowerCase();
            const opts = Array.from(s.options).map(o => ({
                value: o.value,
                text: o.text.trim(),
                selected: o.selected
            }));

            if (lbl.includes('propuesta') || idName.includes('propuesta')) {
                const validas = opts.filter(o => o.value && !placeholderRegex.test(o.text));
                const selOpt = opts.find(o => o.selected);
                const esValidoActual = selOpt && selOpt.value && !placeholderRegex.test(selOpt.text);
                prop = {
                    id: s.id,
                    esValidoActual: Boolean(esValidoActual),
                    opciones: validas
                };
            } else {
                const opcionesMeses = opts.filter(o => o.value && !placeholderRegex.test(o.text) && mesesRegex.test(o.text));
                if (opcionesMeses.length > 0) {
                    const selOpt = opts.find(o => o.selected);
                    const esValidoActual = selOpt && selOpt.value && mesesRegex.test(selOpt.text);
                    mes = {
                        id: s.id,
                        esValidoActual: Boolean(esValidoActual),
                        opciones: opcionesMeses
                    };
                }
            }
        }

        return { prop, mes };
    }""")


async def asegurar_propuesta_y_mes_activos(page: Page):
    info = await obtener_info_propuesta_y_mes(page)
    if info.get("prop") and not info["prop"]["esValidoActual"] and info["prop"]["opciones"]:
        await ejecutar_cambio_aspnet(page, info["prop"]["id"], info["prop"]["opciones"][0]["value"])
        info = await obtener_info_propuesta_y_mes(page)

    if info.get("mes") and not info["mes"]["esValidoActual"] and info["mes"]["opciones"]:
        await ejecutar_cambio_aspnet(page, info["mes"]["id"], info["mes"]["opciones"][0]["value"])


class SimuladorSession:
    def __init__(self):
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def limpiar(self):
        for obj, fn in ((self.page, "close"), (self.browser, "close"), (self.pw, "stop")):
            if obj is not None:
                try:
                    await getattr(obj, fn)()
                except Exception:
                    pass
        self.page = None
        self.browser = None
        self.pw = None

    async def asegurar_sesion(self, forzar_recarga: bool = False):
        if forzar_recarga or self.pw is None or self.browser is None:
            await self.limpiar()
            self.pw = await async_playwright().start()
            try:
                self.browser = await self.pw.chromium.launch(
                    headless=True,
                    args=CHROMIUM_ARGS
                )
            except Exception:
                await self.limpiar()
                raise

        if self.page is None or self.page.is_closed():
            self.page = await self.browser.new_page(viewport={"width": 1366, "height": 900})
            await optimizar_carga_pagina(self.page)
            await self.page.goto(URL_SIMULADOR, wait_until="domcontentloaded", timeout=60000)
            await cerrar_modal_bienvenida(self.page)
            await asegurar_propuesta_y_mes_activos(self.page)

    async def cerrar(self):
        await self.limpiar()


sesion = SimuladorSession()


@app.on_event("shutdown")
async def shutdown_event():
    await sesion.cerrar()


async def inspeccionar_dom_completo(page: Page) -> Dict[str, Any]:
    return await page.evaluate("""() => {
        const limpiar = (t) => (t || '').replace(/\\s+/g, ' ').replace(/:$/, '').trim();
        const mesesRegex = /^\\s*(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\\b/i;

        const etiquetaLegiblePorId = (idStr) => {
            const s = (idStr || '').toLowerCase();
            if (s.includes('propuesta')) return 'Propuesta salarial';
            if (s.includes('cargouno')) return 'Cargo';
            if (s.includes('horasuno') || /nrohoras|nhoras|numhoras|canthoras|horas.*uno/.test(s)) return 'Nº Horas';
            if (s.includes('zonauno')) return 'Zona';
            if (s.includes('extension')) return 'Extensión horaria';
            if (s.includes('antiguedad')) return 'Antigüedad';
            if (s.includes('hijos')) return 'Nº Hijos';
            if (s.includes('afiliado')) return 'Afiliado UEPC';
            if (s.includes('incentivouno')) return 'Incentivo Docente';
            if (s.includes('jornadaextendidauno')) return 'Jornada extendida';
            if (s.includes('horasprolongacion')) return 'Horas prolongación';
            if (s.includes('bonminuno')) return 'Bonificación por Minoridad';
            if (s.includes('tardifuno')) return 'Tarea diferenciada';
            if (s.includes('comespuno')) return 'Complemento especial';
            if (s.includes('anexouno')) return 'Anexo';
            return idStr || 'Campo';
        };

        const obtenerEtiqueta = (el) => {
            if (el.id) {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                if (lbl && limpiar(lbl.innerText)) return limpiar(lbl.innerText);
            }
            const row = el.closest('.row, tr, .form-group');
            if (row) {
                const posibles = Array.from(row.querySelectorAll('label, span, strong, b, td'))
                    .filter(n => !n.contains(el) && n.children.length === 0 && !n.classList.contains('select2-selection__rendered'));
                for (const p of posibles) {
                    const txt = limpiar(p.innerText);
                    if (txt && txt.length > 0 && txt.length < 55 && !/^\\d+$/.test(txt) && !txt.startsWith('ctl00_')) {
                        return txt;
                    }
                }
            }
            return etiquetaLegiblePorId(el.id || el.name || '');
        };

        const esDeOtroCargo = (idName, label) => {
            const combined = (idName + ' ' + label).toLowerCase();
            return /(dos|tres|cuatro|cargo[234]|_c[234]|c[234]_|_2$|_3$|_4$)/i.test(combined);
        };

        const controles = [];
        const etiquetasVistas = new Set();
        const elementos = Array.from(document.querySelectorAll('select, input[type="text"], input[type="number"], input[type="checkbox"], input:not([type])'));

        for (const el of elementos) {
            if (el.classList.contains('select2-search__field')) continue;
            const tipoInput = (el.getAttribute('type') || 'text').toLowerCase();
            if (el.tagName === 'INPUT' && ['hidden', 'submit', 'button', 'radio', 'password', 'file'].includes(tipoInput)) continue;

            const idName = ((el.id || '') + ' ' + (el.name || '')).toLowerCase();
            const label = obtenerEtiqueta(el);

            if (esDeOtroCargo(idName, label)) continue;

            let ocultoPorTab = false;
            let parent = el.parentElement;
            while (parent && parent !== document.body) {
                if (parent.classList && parent.classList.contains('tab-pane') && !parent.classList.contains('active')) {
                    ocultoPorTab = true;
                    break;
                }
                parent = parent.parentElement;
            }
            if (ocultoPorTab) continue;

            const esCampoHora = /n[°ºo]?\\s*horas|^horas$/i.test(label) || /horasuno|nrohoras|nhoras|canthoras/i.test(idName);
            if (el.tagName !== 'SELECT' && !esCampoHora) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || el.offsetParent === null) continue;
            }

            if (/incentivo\\s*docente/i.test(label) && el.disabled) continue;

            const isPropuesta = /propuesta/i.test((el.id || '') + ' ' + label);

            if (el.tagName === 'SELECT' && !isPropuesta) {
                const esSelectMes = Array.from(el.options).some(o => mesesRegex.test(o.text));
                if (esSelectMes) continue;
            }

            const labelFinal = esCampoHora ? 'Nº Horas' : label;
            const claveUnica = labelFinal.toLowerCase();
            if (etiquetasVistas.has(claveUnica)) continue;
            etiquetasVistas.add(claveUnica);

            if (el.tagName === 'SELECT') {
                const opts = Array.from(el.options).map(o => ({
                    value: o.value,
                    text: o.text.trim(),
                    selected: o.selected
                }));
                if (opts.length === 0) {
                    opts.push({ value: '', text: '(Seleccione Cargo primero)', selected: true });
                }
                controles.push({
                    tag: 'select',
                    id: el.id,
                    name: el.name,
                    label: labelFinal,
                    is_propuesta: isPropuesta,
                    value: el.value,
                    options: opts
                });
            } else if (tipoInput === 'checkbox') {
                controles.push({
                    tag: 'checkbox',
                    id: el.id,
                    name: el.name,
                    label: labelFinal,
                    is_propuesta: false,
                    checked: el.checked
                });
            } else {
                controles.push({
                    tag: 'input',
                    input_type: tipoInput,
                    id: el.id,
                    name: el.name,
                    label: labelFinal,
                    is_propuesta: false,
                    value: el.value
                });
            }
        }

        return {
            url: window.location.href,
            controles: controles
        };
    }""")


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open(os.path.join(BASE_DIR, "index.html"), "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/estado-dom")
async def get_estado_dom(reset: bool = False):
    async with sesion._get_lock():
        try:
            await sesion.asegurar_sesion(forzar_recarga=reset)
            await cerrar_modal_bienvenida(sesion.page)
            await asegurar_propuesta_y_mes_activos(sesion.page)
            estado = await inspeccionar_dom_completo(sesion.page)
            return {
                "status": "ok",
                "estado": estado,
                "canasta_bienes": CANASTA_BIENES_PAMPEANA,
                "ipc_info": {
                    "origen": IPC_ORIGEN or "No disponible",
                    "ipc_2026_10": IPC_PAMPEANA.get("2026-10")
                }
            }
        except Exception as exc:
            await sesion.limpiar()
            return JSONResponse(
                status_code=500,
                content={"status": "error", "detail": str(exc)}
            )


@app.post("/api/interactuar")
async def interactuar_control(payload: Dict[str, Any]):
    control_id = payload.get("id")
    tag = payload.get("tag")
    valor = payload.get("value")

    async with sesion._get_lock():
        try:
            await sesion.asegurar_sesion(forzar_recarga=False)
            page = sesion.page
            await cerrar_modal_bienvenida(page)
            await asegurar_propuesta_y_mes_activos(page)

            if tag == "select":
                await ejecutar_cambio_aspnet(page, control_id, str(valor), forzar=True)
                await asegurar_propuesta_y_mes_activos(page)
            elif tag == "checkbox":
                loc = page.locator(f"#{control_id}")
                if await loc.count() > 0:
                    await loc.set_checked(bool(valor))
                    await page.wait_for_timeout(120)
            else:
                loc = page.locator(f"#{control_id}")
                if await loc.count() > 0:
                    await rellenar_input_aspnet(page, loc, str(valor))

            estado = await inspeccionar_dom_completo(page)
            return {"status": "ok", "estado": estado}
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={"status": "error", "detail": str(exc)}
            )


async def rellenar_input_aspnet(page: Page, loc, val_str: str):
    actual_val = await loc.evaluate("""(el, v) => {
        const cur = (el.value || '').trim();
        const style = window.getComputedStyle(el);
        const visible = style.display !== 'none' && style.visibility !== 'hidden' && !el.disabled && !el.readOnly && el.offsetParent !== null;
        if (cur === v && !visible) return { skip: true };
        el.style.display = 'inline-block';
        el.style.visibility = 'visible';
        el.removeAttribute('disabled');
        el.removeAttribute('readonly');
        el.value = v;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return { skip: false, visible };
    }""", val_str)

    if actual_val.get("skip"):
        return

    if actual_val.get("visible"):
        try:
            await loc.fill(val_str, timeout=2500)
        except Exception:
            pass

    try:
        async with page.expect_response(
            lambda r: "default.aspx" in r.url.lower() and r.request.method == "POST",
            timeout=5000
        ):
            if actual_val.get("visible"):
                try:
                    await loc.press("Tab", timeout=1500)
                except Exception:
                    pass
    except Exception:
        await page.wait_for_timeout(120)


async def buscar_control_horas_cargo1(page: Page):
    candidatos = [
        "input[id*='HorasUno' i]",
        "input[id*='NroHoras' i]",
        "input[id*='NHoras' i]",
        "input[id*='CantHoras' i]",
        "select[id*='HorasUno' i]",
        "select[id*='NroHoras' i]"
    ]
    for sel in candidatos:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            return loc

    found_id = await page.evaluate("""() => {
        const limpiar = (t) => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        const nodos = Array.from(document.querySelectorAll('label, span, td, div, th'));
        for (const n of nodos) {
            if (n.children.length > 2) continue;
            const txt = limpiar(n.innerText);
            if (/^(n[°ºo]?\\s*horas|horas\\s*c[aá]tedra|horas)$/i.test(txt)) {
                const row = n.closest('.row, tr, .form-group') || n.parentElement;
                if (!row) continue;
                const inp = row.querySelector('input:not([type="hidden"]), select');
                if (inp && inp.id && !/dos|tres|cuatro|jornada|prolong/i.test(inp.id)) {
                    return inp.id;
                }
            }
        }
        return null;
    }""")
    if found_id:
        loc = page.locator(f"#{found_id}").first
        if await loc.count() > 0:
            return loc
    return None


async def aplicar_parametros_lista(page: Page, parametros: List[Dict[str, Any]]):
    horas_uno_objetivo: Optional[str] = None
    for p in parametros:
        ident = (p.get("label", "") + " " + p.get("id", "")).lower()
        if ("horas" in ident or "cátedra" in ident or "catedra" in ident) and "jornada" not in ident and "prolong" not in ident and "dos" not in ident and "tres" not in ident and "cuatro" not in ident:
            v = str(p.get("value", "")).strip()
            if v != "" and v != "0":
                horas_uno_objetivo = v

    for param in parametros:
        p_id = param["id"]
        p_tag = param["tag"]
        p_val = param["value"]

        loc = page.locator(f"#{p_id}")
        if await loc.count() == 0 and ("horas" in (param.get("label", "") + " " + p_id).lower()):
            loc_alt = await buscar_control_horas_cargo1(page)
            if loc_alt is not None:
                loc = loc_alt

        if await loc.count() == 0:
            continue

        if p_tag == "select":
            if not p_val:
                continue
            estado_opcion = await loc.evaluate("""(el, target) => {
                const byVal = Array.from(el.options).find(o => o.value === target.value);
                if (byVal) return { found: true, val: byVal.value, current: el.value };
                const byText = Array.from(el.options).find(o => o.text.trim() === target.text);
                if (byText) return { found: true, val: byText.value, current: el.value };
                return { found: false };
            }""", {"value": str(p_val), "text": param.get("text", "")})

            if not estado_opcion["found"]:
                raise ValueError(
                    f"La opción '{param.get('text', p_val)}' de '{param.get('label', p_id)}' no existe en esta propuesta."
                )

            if estado_opcion["current"] != estado_opcion["val"]:
                await ejecutar_cambio_aspnet(page, p_id, estado_opcion["val"])

        elif p_tag == "checkbox":
            actual_chk = await loc.is_checked()
            if actual_chk != bool(p_val):
                await loc.set_checked(bool(p_val))
                await page.wait_for_timeout(100)

        else:
            val_str = str(p_val).strip()
            if val_str != "":
                actual_txt = await loc.input_value()
                if actual_txt.strip() != val_str:
                    await rellenar_input_aspnet(page, loc, val_str)

    if horas_uno_objetivo:
        loc_horas = await buscar_control_horas_cargo1(page)
        if loc_horas is not None:
            tag_h = await loc_horas.evaluate("el => el.tagName.toLowerCase()")
            if tag_h == "select":
                await loc_horas.select_option(value=horas_uno_objetivo, force=True)
            else:
                actual_h = (await loc_horas.input_value()).strip()
                if actual_h != horas_uno_objetivo:
                    await rellenar_input_aspnet(page, loc_horas, horas_uno_objetivo)


async def leer_salario_total_bolsillo(page: Page) -> str:
    lectura = await page.evaluate("""() => {
        let salarioTotalBolsillo = null;
        const candidatos = Array.from(document.querySelectorAll('div, tr, li, td, p'));

        for (const bloque of candidatos) {
            const txt = (bloque.innerText || '').trim();
            if (/^Salario\\s+Total\\s+de\\s+Bolsillo/i.test(txt)) {
                const match = txt.match(/\\$\\s*[\\d.,]+/);
                if (match) {
                    salarioTotalBolsillo = match[0].replace(/\\s+/g, '');
                    break;
                }
            }
        }

        if (!salarioTotalBolsillo) {
            const todos = Array.from(document.querySelectorAll('*'));
            for (let i = 0; i < todos.length; i++) {
                if (todos[i].children.length === 0 && /^Salario\\s+Total\\s+de\\s+Bolsillo$/i.test(todos[i].innerText.trim())) {
                    const padre = todos[i].parentElement;
                    if (padre) {
                        const matchPadre = padre.innerText.match(/\\$\\s*[\\d.,]+/);
                        if (matchPadre) {
                            salarioTotalBolsillo = matchPadre[0].replace(/\\s+/g, '');
                            break;
                        }
                    }
                }
            }
        }

        return { salario: salarioTotalBolsillo };
    }""")

    if not lectura["salario"] or lectura["salario"] in ("$0.00", "$0,00", "$0"):
        raise ValueError("Salario Total de Bolsillo resultó $0.00 (verifique Nº Horas / cargo).")

    return lectura["salario"]


def buscar_opcion_mes_en_selector(opciones_mes: List[Dict[str, Any]], mes_num: int) -> Optional[Dict[str, Any]]:
    nombre_objetivo = NOMBRES_MESES[mes_num].lower()
    for opt in opciones_mes:
        txt = opt["text"].strip().lower()
        if txt.startswith(nombre_objetivo) or (mes_num == 9 and txt.startswith("setiembre")):
            return opt
    return None


@app.get("/api/ejecutar-barrido")
async def ejecutar_barrido(request: Request, params: str):
    config = json.loads(params)
    propuesta_id: str = config["propuesta_id"]
    parametros_cargo1: List[Dict[str, Any]] = config["parametros"]

    param_cargo = [
        p for p in parametros_cargo1
        if re.search(r"cargo", (p.get("label", "") + " " + p.get("id", "")), re.I)
        and not re.search(r"dos|tres|cuatro", (p.get("label", "") + " " + p.get("id", "")), re.I)
    ]
    otros_params = [p for p in parametros_cargo1 if p not in param_cargo]

    async def event_generator():
        async with sesion._get_lock():
            yield f"data: {json.dumps({'type': 'status', 'message': 'Preparando sesión secuencial...'})}\n\n"
            await sesion.asegurar_sesion(forzar_recarga=False)
            base_page = sesion.page

            propuestas_raw = await base_page.evaluate(f"""() => {{
                const sel = document.getElementById('{propuesta_id}');
                if (!sel) return [];
                return Array.from(sel.options)
                    .filter(o => o.value && o.value !== '0' && o.value !== '-1' && !/seleccione/i.test(o.text))
                    .map((o, i) => ({{ value: o.value, text: o.text.trim(), dom_idx: i }}));
            }}""")

            await sesion.limpiar()

            propuestas = [{**p, "inicio": parsear_inicio_propuesta(p["text"])} for p in propuestas_raw]
            propuestas_ordenadas = sorted(
                propuestas,
                key=lambda p: (
                    p["inicio"][0] if p["inicio"] else 9999,
                    p["inicio"][1] if p["inicio"] else 99,
                    -p["dom_idx"]
                )
            )

            desde_2023 = [p for p in propuestas_ordenadas if p["inicio"] is not None and p["inicio"] >= (2023, 1)]
            previas_2023 = [p for p in propuestas_ordenadas if p["inicio"] is not None and p["inicio"] < (2023, 1)]
            if previas_2023 and not any(p["inicio"] == (2023, 1) for p in desde_2023):
                propuestas_filtradas = [previas_2023[-1]] + desde_2023
            else:
                propuestas_filtradas = desde_2023

            total = len(propuestas_filtradas)
            yield f"data: {json.dumps({'type': 'total', 'total': total})}\n\n"

            resultados: List[Dict[str, Any]] = []
            ipc_base_oct26 = IPC_PAMPEANA.get("2026-10")

            def calcular_real_puntual(salario_str: str, ym: str) -> str:
                try:
                    nom = parsear_monto_ars(salario_str)
                    if ipc_base_oct26 and ym in IPC_PAMPEANA and IPC_PAMPEANA[ym] > 0:
                        return formatear_monto_ars(nom * (ipc_base_oct26 / IPC_PAMPEANA[ym]))
                except Exception:
                    pass
                return "N/D"

            pw_b = await async_playwright().start()
            browser_b: Optional[Browser] = None
            page_b: Optional[Page] = None

            try:
                yield f"data: {json.dumps({'type': 'status', 'message': 'Abriendo simulador UEPC en modo secuencial...'})}\n\n"
                browser_b = await pw_b.chromium.launch(headless=True, args=CHROMIUM_ARGS)
                page_b = await browser_b.new_page(viewport={"width": 1366, "height": 900})
                await optimizar_carga_pagina(page_b)
                await page_b.goto(URL_SIMULADOR, wait_until="domcontentloaded", timeout=60000)
                await cerrar_modal_bienvenida(page_b)

                for idx, prop in enumerate(propuestas_filtradas, start=1):
                    if await request.is_disconnected():
                        break

                    prop_val = prop["value"]
                    prop_text = prop["text"]
                    inicio = prop["inicio"]

                    limite_siguiente: Optional[Tuple[int, int]] = None
                    if inicio is not None:
                        for sig in propuestas_filtradas[idx:]:
                            if sig["inicio"] is not None and sig["inicio"] > inicio:
                                limite_siguiente = sig["inicio"]
                                break

                    yield f"data: {json.dumps({'type': 'status', 'message': f'Propuesta {idx}/{total}: {prop_text}'})}\n\n"

                    try:
                        await ejecutar_cambio_aspnet(page_b, propuesta_id, prop_val)
                        await aplicar_parametros_lista(page_b, param_cargo)

                        info = await obtener_info_propuesta_y_mes(page_b)
                        sub_meses = info.get("mes")

                        if inicio is not None and sub_meses and sub_meses["opciones"]:
                            cur_y, cur_m = inicio
                            salario_inicial_prop = None

                            for paso in range(12):
                                if paso > 0 and limite_siguiente is not None and (cur_y, cur_m) >= limite_siguiente:
                                    break
                                if paso > 0 and limite_siguiente is None and cur_y > inicio[0] and cur_m > 3:
                                    break

                                opt_mes = buscar_opcion_mes_en_selector(sub_meses["opciones"], cur_m)
                                if not opt_mes:
                                    break

                                await ejecutar_cambio_aspnet(page_b, sub_meses["id"], opt_mes["value"])
                                await aplicar_parametros_lista(page_b, otros_params)
                                salario = await leer_salario_total_bolsillo(page_b)

                                if paso == 0:
                                    salario_inicial_prop = salario
                                elif limite_siguiente is None and cur_y > inicio[0] and salario == salario_inicial_prop:
                                    break

                                nombre_mes = NOMBRES_MESES[cur_m]
                                sort_key = f"{cur_y:04d}-{cur_m:02d}"
                                registro = {
                                    "orden_dom": idx * 100 + paso,
                                    "propuesta": f"{prop_text} ({nombre_mes})",
                                    "fecha": f"{nombre_mes} {cur_y}",
                                    "sort_key": sort_key,
                                    "tiene_fecha": True,
                                    "salario": salario,
                                    "salario_real": calcular_real_puntual(salario, sort_key)
                                }
                                resultados.append(registro)
                                if sort_key >= "2023-01":
                                    ev = {
                                        "type": "progress",
                                        "idx": idx,
                                        "total": total,
                                        "registro": registro
                                    }
                                    yield f"data: {json.dumps(ev)}\n\n"

                                cur_y, cur_m = siguiente_mes(cur_y, cur_m)

                        elif inicio is not None:
                            await aplicar_parametros_lista(page_b, otros_params)
                            salario = await leer_salario_total_bolsillo(page_b)
                            cur_y, cur_m = inicio
                            nombre_mes = NOMBRES_MESES[cur_m]
                            sort_key = f"{cur_y:04d}-{cur_m:02d}"
                            registro = {
                                "orden_dom": idx * 100,
                                "propuesta": prop_text,
                                "fecha": f"{nombre_mes} {cur_y}",
                                "sort_key": sort_key,
                                "tiene_fecha": True,
                                "salario": salario,
                                "salario_real": calcular_real_puntual(salario, sort_key)
                            }
                            resultados.append(registro)
                            if sort_key >= "2023-01":
                                ev = {
                                    "type": "progress",
                                    "idx": idx,
                                    "total": total,
                                    "registro": registro
                                }
                                yield f"data: {json.dumps(ev)}\n\n"

                    except Exception as exc:
                        ev_err = {
                            "type": "error",
                            "idx": idx,
                            "total": total,
                            "propuesta": prop_text,
                            "error": str(exc)
                        }
                        yield f"data: {json.dumps(ev_err)}\n\n"

            finally:
                for obj, fn in ((page_b, "close"), (browser_b, "close"), (pw_b, "stop")):
                    if obj is not None:
                        try:
                            await getattr(obj, fn)()
                        except Exception:
                            pass

            resultados_ordenados = sorted(
                resultados,
                key=lambda r: (r["sort_key"], r["orden_dom"])
            )
            analisis = calcular_analisis_real(resultados_ordenados)
            yield f"data: {json.dumps({'type': 'done', **analisis})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")