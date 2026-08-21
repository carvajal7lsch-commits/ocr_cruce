import io
import re
import os
import sys
import time
import uuid
import hashlib
import threading
import webbrowser
import datetime
import pandas as pd
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional

# Import standard library checks and helper modules from src
from src.ocr import (
    preprocesar_imagen,
    extraer_texto_con_enrutamiento,
    pdf2image_available,
    rapidocr_available,
    pymupdf_available
)
from src.parser import (
    extraer_datos_texto,
    detectar_cara_cedula,
    calcular_edad
)
from src.reporter import generate_excel_report
from src import db
from pdf2image import pdfinfo_from_bytes

try:
    from rapidfuzz import fuzz, process as fuzz_process
except ImportError:
    fuzz = None
    fuzz_process = None

# Carpeta base real de la app -- NO simplemente el directorio actual, porque si esto
# corre empaquetado como ejecutable (PyInstaller) y se abre con doble clic desde una
# carpeta distinta, el directorio de trabajo puede no ser el de la app.
# OJO: NO es os.path.dirname(sys.executable) -- en un build --onedir de PyInstaller
# moderno (6.x) los datos empaquetados (--add-data, como static/ y poppler/) quedan
# adentro de una subcarpeta "_internal/", no junto al .exe. sys._MEIPASS es lo que
# PyInstaller expone especificamente para encontrar esa carpeta (para --onefile
# apunta a un directorio temporal; para --onedir, que es lo que usa esta app, apunta
# a "_internal/", que es permanente y con permisos de escritura -- por eso tambien
# sirve para guardar ahi los datos que la app genera en tiempo de ejecucion, como el
# historial SQLite y las imagenes recortadas). En modo script normal, se usa la
# carpeta de este archivo.
if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolver_poppler_path():
    """
    Encuentra el poppler que va a usar pdf2image, en orden de prioridad:
    1. Variable de entorno POPPLER_PATH (override explicito).
    2. Un archivo local "poppler_path.txt" junto a este script, con la ruta en texto
       plano -- pensado para desarrollo local: a diferencia de una variable de entorno
       de Windows (que solo la ven procesos NUEVOS despues de configurarla -- una
       terminal ya abierta, o hasta una pestaña nueva dentro de la misma app de
       terminal, puede seguir viendo el valor viejo hasta reiniciar sesion), este
       archivo se lee directo en cada arranque, sin ese problema. No se versiona en
       git (cada quien tiene poppler en un lugar distinto).
    3. Una carpeta "poppler/bin" empaquetada junto al ejecutable/script -- asi es como
       se distribuye en el .exe para que la app funcione en una maquina que no tiene
       poppler instalado ni en el PATH.
    4. None -- pdf2image usa el poppler que encuentre en el PATH del sistema (typical
       en Linux/Mac con apt/brew, o si alguien lo agrego a mano en Windows).
    """
    desde_env = os.environ.get("POPPLER_PATH")
    if desde_env and os.path.exists(desde_env):
        return desde_env

    archivo_config = os.path.join(BASE_DIR, "poppler_path.txt")
    if os.path.exists(archivo_config):
        with open(archivo_config, "r", encoding="utf-8") as f:
            desde_archivo = f.read().strip()
        if desde_archivo and os.path.exists(desde_archivo):
            return desde_archivo

    empaquetado = os.path.join(BASE_DIR, "poppler", "bin")
    if os.path.exists(empaquetado):
        return empaquetado

    return None


app = FastAPI(title="Auditoría de Cédulas OCR & Excel")

# Enable CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global in-memory storage for background tasks
tasks_db = {}
# Global variable to store last generated report bytes
generated_reports = {}
# Contexto necesario para re-conciliar tras una edicion (df_excel_clean, etc).
# Va SEPARADO de tasks_db a proposito: df_excel_clean es un DataFrame de pandas, y
# /api/status/{task_id} devuelve tasks_db[task_id] tal cual -- si el DataFrame
# quedara ahi, cada poll intentaria serializarlo a JSON y reventaria con un 500.
task_reconcile_context = {}

@app.on_event("startup")
def _startup():
    db.init_db()
    _backfill_claves_cache()

# Patterns for auto-detection
PATTERN_DOC_COL = re.compile(r'\b(doc|documento|cédula|cedula|cc|id|identificación|identificacion|número|numero|nro)\b', re.IGNORECASE)
PATTERN_NAME_COL = re.compile(r'\b(nom|nombre|nombres|empleado|persona|cliente|usuario|apellidos?)\b', re.IGNORECASE)

class AuditConfig(BaseModel):
    selected_row_idx: int = 0
    key_col: str
    compare_cols: List[str]
    similarity_threshold: int = 90
    start_page: int = 1
    end_page: int = 1
    pdf_dpi: int = 300
    img_filter: str = "Solo Escala de Grises"

@app.post("/api/analyze-excel")
async def analyze_excel(excel_file: UploadFile = File(...)):
    try:
        content = await excel_file.read()
        df_raw = pd.read_excel(io.BytesIO(content), header=None)
        
        # 1. AUTO-DETECTION OF HEADER ROW
        detected_row_idx = 0
        found_header = False
        
        for i in range(min(15, len(df_raw))):
            row_vals = [str(val).strip() for val in df_raw.iloc[i].dropna().tolist()]
            has_doc = any(PATTERN_DOC_COL.search(val) for val in row_vals)
            has_name = any(PATTERN_NAME_COL.search(val) for val in row_vals)
            if has_doc and has_name:
                detected_row_idx = i
                found_header = True
                break
                
        if not found_header:
            for i in range(min(15, len(df_raw))):
                row_vals = [str(val).strip() for val in df_raw.iloc[i].dropna().tolist()]
                if any(PATTERN_DOC_COL.search(val) or PATTERN_NAME_COL.search(val) for val in row_vals):
                    detected_row_idx = i
                    break
        
        # Generate row preview options for UI
        row_previews = []
        max_rows_to_preview = min(15, len(df_raw))
        for i in range(max_rows_to_preview):
            row_preview = [str(x) for x in df_raw.iloc[i].dropna().tolist()[:4]]
            preview_str = f"Fila {i+1}: " + " | ".join(row_preview)
            if len(df_raw.iloc[i].dropna()) > 4:
                preview_str += " ..."
            row_previews.append({"index": i, "preview": preview_str})

        # Columns for detected row
        header_vals = df_raw.iloc[detected_row_idx].tolist()
        clean_headers = []
        for i, val in enumerate(header_vals):
            if pd.isna(val) or str(val).strip() == "":
                clean_headers.append(f"Columna_{i+1}")
            else:
                clean_headers.append(str(val).strip())
                
        df_excel = df_raw.iloc[detected_row_idx + 1:].copy()
        df_excel.columns = clean_headers
        df_excel = df_excel.reset_index(drop=True)
        excel_cols = [col for col in df_excel.columns if not str(col).startswith("Columna_") and not str(col).startswith("Unnamed:")]
        
        # Auto-detect default columns
        dyn_key_col = None
        dyn_compare_cols = []
        for col in excel_cols:
            if PATTERN_DOC_COL.search(str(col)):
                dyn_key_col = col
                break
        if not dyn_key_col and excel_cols:
            dyn_key_col = excel_cols[0]
            
        for col in excel_cols:
            if col == dyn_key_col:
                continue
            if PATTERN_NAME_COL.search(str(col)):
                dyn_compare_cols.append(col)
        if not dyn_compare_cols and len(excel_cols) > 1:
            for col in excel_cols:
                if col != dyn_key_col:
                    dyn_compare_cols.append(col)
                    break
        if not dyn_compare_cols and excel_cols:
            dyn_compare_cols = [excel_cols[0]]

        # Convert preview to dict format
        preview_data = df_excel.head(5).fillna("").to_dict(orient="records")

        return {
            "row_previews": row_previews,
            "detected_row_idx": detected_row_idx,
            "headers": excel_cols,
            "key_col_default": dyn_key_col,
            "compare_cols_default": dyn_compare_cols,
            "preview": preview_data
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al analizar el Excel: {str(e)}")

@app.post("/api/pdf-info")
async def get_pdf_info(pdf_file: UploadFile = File(...)):
    try:
        content = await pdf_file.read()
        total_pages = 1
        try:
            import fitz
            with fitz.open(stream=content, filetype="pdf") as doc:
                total_pages = len(doc)
        except Exception:
            try:
                pdf_info = pdfinfo_from_bytes(content)
                total_pages = pdf_info.get("Pages", 1)
            except Exception:
                total_pages = 1
        return {
            "total_pages": total_pages,
            # Ritmo con el que el frontend calcula el tiempo estimado en cuanto se
            # elige el rango de paginas. Viene del historial real de esta maquina
            # (ver segundos_por_pagina_historico); si aun no hay historial se manda
            # None y el frontend avisa que la estimacion se afinara al usarla.
            "segundos_por_pagina": db.segundos_por_pagina_historico(),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener info del PDF: {str(e)}")

def reconciliar_y_generar_reporte(df_ocr, df_excel_clean, key_col, compare_cols,
                                   similarity_threshold, run_meta):
    """
    Compara los datos ya extraidos del PDF (df_ocr, una fila por documento) contra las
    filas del Excel (df_excel_clean) y genera el reporte formateado. Se usa tanto en la
    corrida inicial de una auditoria como al editar un dato y volver a conciliar --
    misma logica, sin duplicar codigo.

    De paso calcula sugerencias de correccion (ancladas al Excel cargado, via rapidfuzz):
    - Alertas por "cedula diferente" (el nombre coincide pero el documento no): el dato
      correcto YA se conoce (id_ex), se expone como sugerencia_documento.
    - Alertas por "nombre diferente" (documento coincide, nombre no): se sugiere el
      nombre tal como esta en Excel.
    - Huerfanos (paginas del PDF sin ningun match): se busca por similitud de digitos
      contra los documentos que quedaron "faltantes en PDF"; si hay uno muy parecido
      (>=75%), probablemente sea un digito mal leido por el OCR.
    """
    lista_coinciden = []
    lista_anomalias = []
    lista_solo_excel = []
    paginas_pdf_emparejadas = set()

    def _campos_pdf_extra(pdf_row):
        """Campos nuevos extraídos de la cédula, con el sufijo _PDF usado en el reporte."""
        return {
            "Tipo_Documento_PDF": pdf_row.get('Tipo_Documento_OCR', ''),
            "Fecha_Nacimiento_PDF": pdf_row.get('Fecha_Nacimiento_OCR', ''),
            "Edad_PDF": pdf_row.get('Edad_OCR', ''),
            "Lugar_Nacimiento_PDF": pdf_row.get('Lugar_Nacimiento_OCR', ''),
            "Sexo_PDF": pdf_row.get('Sexo_OCR', ''),
            "Estatura_PDF": pdf_row.get('Estatura_OCR', ''),
            "Grupo_Sanguineo_PDF": pdf_row.get('Grupo_Sanguineo_OCR', ''),
            "Fecha_Lugar_Expedicion_PDF": pdf_row.get('Fecha_Lugar_Expedicion_OCR', ''),
        }

    for idx_ex, row_excel in df_excel_clean.iterrows():
        id_ex = str(row_excel['Identificación_Limpia']).strip()
        nombre_ex = str(row_excel['Nombre_Base']).strip()

        match_doc = None
        if id_ex:
            match_doc = df_ocr[df_ocr['Documento_OCR'] == id_ex]

        if match_doc is not None and not match_doc.empty:
            pdf_row = match_doc.iloc[0]
            nombre_ocr = str(pdf_row['Nombre_OCR']).strip()
            similitud = _similitud_nombre(nombre_ex, nombre_ocr)

            registro = {
                "Identificación_Excel": id_ex,
                "Nombre_Excel": nombre_ex,
                "Identificación_PDF": pdf_row['Documento_OCR'],
                "Nombre_PDF": nombre_ocr,
                "Similitud_Nombre_%": similitud,
                "Página_PDF": int(pdf_row['Página']),
                **_campos_pdf_extra(pdf_row),
                "Texto_Completo_PDF": pdf_row['Texto_Completo']
            }
            for col in compare_cols:
                registro[f"{col}_excel"] = row_excel[col]
                registro[f"{col}_score"] = similitud

            paginas_pdf_emparejadas.add(pdf_row['Página'])

            if similitud >= similarity_threshold:
                lista_coinciden.append(registro)
            else:
                registro["Alerta_Detalle"] = f"La cédula coincide, pero el nombre del PDF ('{nombre_ocr}') tiene baja coincidencia con el de Excel ('{nombre_ex}')."
                registro["sugerencia_nombre"] = nombre_ex
                lista_anomalias.append(registro)
        else:
            best_match_ocr = None
            best_score = 0

            for idx_ocr, pdf_row in df_ocr.iterrows():
                page_num = pdf_row['Página']
                if page_num in paginas_pdf_emparejadas:
                    continue

                nombre_ocr = str(pdf_row['Nombre_OCR']).strip()
                similitud = _similitud_nombre(nombre_ex, nombre_ocr) if fuzz else 0

                if similitud >= 80 and similitud > best_score:
                    best_score = similitud
                    best_match_ocr = pdf_row

            if best_match_ocr is not None:
                pdf_row = best_match_ocr
                id_ocr = pdf_row['Documento_OCR']

                registro = {
                    "Identificación_Excel": id_ex,
                    "Nombre_Excel": nombre_ex,
                    "Identificación_PDF": id_ocr,
                    "Nombre_PDF": pdf_row['Nombre_OCR'],
                    "Similitud_Nombre_%": best_score,
                    "Página_PDF": int(pdf_row['Página']),
                    **_campos_pdf_extra(pdf_row),
                    "Texto_Completo_PDF": pdf_row['Texto_Completo'],
                    "Alerta_Detalle": f"El nombre coincide ({_fmt_pct(best_score)}%), pero la cédula del PDF ('{id_ocr}') difiere de la de Excel ('{id_ex}').",
                    "sugerencia_documento": id_ex,
                    "sugerencia_confianza": round(float(best_score), 1),
                }
                for col in compare_cols:
                    registro[f"{col}_excel"] = row_excel[col]
                    registro[f"{col}_score"] = best_score

                paginas_pdf_emparejadas.add(pdf_row['Página'])
                lista_anomalias.append(registro)
            else:
                # Tercer intento: ni el documento ni el nombre hicieron match limpio,
                # pero puede que el documento leido se parezca MUCHO al de Excel letra
                # por letra (tipico de un 5/6 u 0/8 mal leido). Si esto tambien fallara,
                # antes se dejaba como "Solo en PDF" (huerfano) con una sugerencia
                # meramente informativa -- pero eso es enganoso: si ya sabemos que
                # probablemente SI esta en el Excel, no deberia aparecer como si no
                # tuviera ninguna relacion. Por eso ahora se promueve a Alertas, igual
                # que el resto de casos "posible pero no confirmado".
                best_doc_match = None
                best_doc_score = 0
                if fuzz_process and id_ex:
                    documentos_candidatos = []
                    filas_candidatas = []
                    for idx_ocr, pdf_row in df_ocr.iterrows():
                        if pdf_row['Página'] in paginas_pdf_emparejadas:
                            continue
                        doc_ocr = str(pdf_row['Documento_OCR']).strip()
                        if doc_ocr:
                            documentos_candidatos.append(doc_ocr)
                            filas_candidatas.append(pdf_row)
                    if documentos_candidatos:
                        resultado = fuzz_process.extractOne(
                            id_ex, documentos_candidatos, scorer=fuzz.ratio, score_cutoff=75
                        )
                        if resultado:
                            _, best_doc_score, idx_match = resultado
                            best_doc_match = filas_candidatas[idx_match]

                if best_doc_match is not None:
                    pdf_row = best_doc_match
                    nombre_ocr = str(pdf_row['Nombre_OCR']).strip()
                    similitud_nombre = _similitud_nombre(nombre_ex, nombre_ocr) if fuzz else 0

                    registro = {
                        "Identificación_Excel": id_ex,
                        "Nombre_Excel": nombre_ex,
                        "Identificación_PDF": pdf_row['Documento_OCR'],
                        "Nombre_PDF": nombre_ocr,
                        "Similitud_Nombre_%": similitud_nombre,
                        "Página_PDF": int(pdf_row['Página']),
                        **_campos_pdf_extra(pdf_row),
                        "Texto_Completo_PDF": pdf_row['Texto_Completo'],
                        "Alerta_Detalle": f"El documento del PDF ('{pdf_row['Documento_OCR']}') se parece mucho al de Excel ('{id_ex}', {_fmt_pct(best_doc_score)}% de similitud) pero no coincide exacto -- probablemente un dígito mal leído.",
                        "sugerencia_documento": id_ex,
                        "sugerencia_nombre": nombre_ex,
                        "sugerencia_confianza": round(float(best_doc_score), 1),
                    }
                    for col in compare_cols:
                        registro[f"{col}_excel"] = row_excel[col]
                        registro[f"{col}_score"] = similitud_nombre

                    paginas_pdf_emparejadas.add(pdf_row['Página'])
                    lista_anomalias.append(registro)
                else:
                    solo_excel_entry = {
                        "Identificación_Excel": id_ex,
                        "Nombre_Excel": nombre_ex
                    }
                    for col in compare_cols:
                        solo_excel_entry[f"{col}_excel"] = row_excel[col]
                    lista_solo_excel.append(solo_excel_entry)

    # Lo que queda sin emparejar en este punto de verdad no se parece a NADA del
    # Excel (ni documento exacto, ni nombre >=80%, ni documento >=75% de similitud) --
    # por eso ya no se calcula una sugerencia aqui: cualquier candidato razonable ya
    # quedo promovido a Alertas y Anomalías arriba.
    lista_solo_pdf = []
    for idx_ocr, pdf_row in df_ocr.iterrows():
        if pdf_row['Página'] not in paginas_pdf_emparejadas:
            lista_solo_pdf.append({
                "Página_PDF": int(pdf_row['Página']),
                "Identificación_PDF": pdf_row['Documento_OCR'],
                "Nombre_PDF": pdf_row['Nombre_OCR'],
                **_campos_pdf_extra(pdf_row),
                "Texto_Completo_PDF": pdf_row['Texto_Completo']
            })

    metrics = {
        "correct": len(lista_coinciden),
        "alerts": len(lista_anomalias),
        "missing": len(lista_solo_excel),
        "huerfanos": len(lista_solo_pdf)
    }

    formatted_report_bytes = generate_excel_report(
        key_col, compare_cols,
        lista_coinciden, lista_anomalias, lista_solo_pdf, lista_solo_excel,
        metrics, run_meta
    )

    return lista_coinciden, lista_anomalias, lista_solo_pdf, lista_solo_excel, metrics, formatted_report_bytes


def _construir_df_ocr_desde_live_results(live_results):
    """
    Reconstruye el DataFrame que espera reconciliar_y_generar_reporte a partir de
    live_results (una fila por documento, ya deduplicada) -- usado despues de una
    edicion, cuando ya no tenemos las paginas crudas del OCR a mano.
    """
    columnas = ["Página", "Documento_OCR", "Nombre_OCR", "Fecha_Nacimiento_OCR", "Cara_OCR",
                "Tipo_Documento_OCR", "Lugar_Nacimiento_OCR", "Sexo_OCR", "Estatura_OCR",
                "Grupo_Sanguineo_OCR", "Fecha_Lugar_Expedicion_OCR", "Edad_OCR",
                "Metodo_Extraccion", "Texto_Completo"]

    filas = []
    for lr in live_results:
        filas.append({
            "Página": lr["pages"][0] if lr.get("pages") else 0,
            "Documento_OCR": str(lr.get("document") or "").strip(),
            "Nombre_OCR": str(lr.get("name") or "").strip(),
            "Fecha_Nacimiento_OCR": str(lr.get("date") or "").strip(),
            "Cara_OCR": lr.get("side") or "No detectado",
            "Tipo_Documento_OCR": str(lr.get("tipo_documento") or "").strip(),
            "Lugar_Nacimiento_OCR": str(lr.get("lugar_nacimiento") or "").strip(),
            "Sexo_OCR": str(lr.get("sexo") or "").strip(),
            "Estatura_OCR": str(lr.get("estatura") or "").strip(),
            "Grupo_Sanguineo_OCR": str(lr.get("grupo_sanguineo") or "").strip(),
            "Fecha_Lugar_Expedicion_OCR": str(lr.get("fecha_lugar_expedicion") or "").strip(),
            "Edad_OCR": lr.get("edad") or "",
            "Metodo_Extraccion": lr.get("method", ""),
            "Texto_Completo": lr.get("raw_text", ""),
        })

    df = pd.DataFrame(filas, columns=columnas)
    if not df.empty:
        # Consistente con la corrida inicial: ya no se descartan los placeholders
        # "Sujeto_Pag_N" -- quedan visibles como huerfanos en vez de desaparecer.
        df = df[df['Documento_OCR'].str.strip() != ''].copy()
    return df


def _propagar_sugerencias(live_results, lista_anomalias, lista_solo_pdf):
    """
    Copia las sugerencias calculadas en la reconciliacion (que viven en los registros
    del reporte) hacia las tarjetas de live_results, que es lo que se ve en la Consola.
    Se limpian las sugerencias viejas primero para que una correccion ya aplicada no
    deje una sugerencia obsoleta colgada en la tarjeta.
    """
    for lr in live_results:
        lr["sugerencia_documento"] = None
        lr["sugerencia_nombre"] = None
        lr["sugerencia_confianza"] = None

    por_documento = {lr["document"]: lr for lr in live_results}
    for entry in lista_anomalias + lista_solo_pdf:
        lr = por_documento.get(entry.get("Identificación_PDF"))
        if not lr:
            continue
        if entry.get("sugerencia_documento"):
            lr["sugerencia_documento"] = entry["sugerencia_documento"]
        if entry.get("sugerencia_nombre"):
            lr["sugerencia_nombre"] = entry["sugerencia_nombre"]
        if entry.get("sugerencia_confianza") is not None:
            lr["sugerencia_confianza"] = entry["sugerencia_confianza"]


def _es_subconjunto_de_palabras(nombre_a, nombre_b):
    """
    True si TODAS las palabras del nombre mas corto aparecen (tal cual, palabra
    completa) en el nombre mas largo -- p. ej. "LIDA YASMIN" dentro de "LIDA YASMIN
    ALDANA BOHORQUEZ". Es una señal fuerte de que el OCR solo capturo una PARTE del
    nombre completo (le falto una linea, un apellido) en vez de haber leido a otra
    persona. Se exige un minimo de 2 palabras compartidas (no solo 1) para no dar por
    buena una coincidencia por un unico nombre de pila comun entre dos personas
    distintas -- igual asi queda un riesgo residual (chico, pero no cero) si dos
    personas DISTINTAS del mismo lote comparten un nombre+apellido completo de 2
    palabras; se acepta ese riesgo a cambio de resolver el caso mucho mas frecuente
    de una pagina con el nombre incompleto.
    """
    palabras_a, palabras_b = set(nombre_a.split()), set(nombre_b.split())
    corto, largo = (palabras_a, palabras_b) if len(palabras_a) <= len(palabras_b) else (palabras_b, palabras_a)
    return len(corto) >= 2 and corto.issubset(largo)


def _fmt_pct(valor):
    """Porcentaje para mostrar dentro de un texto: sin el '.0' cuando es redondo."""
    numero = round(float(valor), 1)
    return str(int(numero)) if numero == int(numero) else str(numero)


def _similitud_nombre(nombre_a, nombre_b):
    """
    token_sort_ratio ordena las PALABRAS pero no perdona que el OCR haya fusionado dos
    de ellas en una sola sin espacio (p. ej. "CALA LEON" leido como "CALALEON") -- eso
    baja el puntaje aunque sea la misma persona (caso real: 83% en vez de ~100%). Como
    respaldo, se compara cada nombre con sus letras ordenadas alfabeticamente y sin
    espacios -- asi ni el orden de las palabras ni donde cae el espacio importan. OJO:
    esta comparacion por letras es un discriminador debil (nombres en español comparten
    mucho alfabeto -- se probo con pares de personas distintas y ya dan 60-70% solo por
    azar), asi que unicamente se usa como "rescate" cuando es practicamente un anagrama
    exacto (>=95%); por debajo de eso se descarta en vez de arriesgarse a inflar el
    puntaje de dos personas que no son la misma. Tambien se rescata el caso en que un
    nombre es subconjunto limpio del otro (ver _es_subconjunto_de_palabras) -- ahi si
    se pone en 100 directo, porque no hay ninguna palabra que NO calce, solo faltan.

    Se usa tanto para comparar Excel-vs-PDF (con un documento ya (casi) coincidiendo de
    respaldo) como para decidir si dos paginas de OCR sin documento en comun son la
    MISMA persona (ver _reasignar_huerfanos_por_nombre / _auto_fusionar_huerfanos_en_
    live_results) -- en ese segundo caso no hay un documento que respalde la decision,
    asi que el mismo umbral alto (90) que se usa en esas funciones es lo que mantiene el
    riesgo de fusionar a dos personas distintas razonablemente bajo.
    """
    if not fuzz:
        return 100
    if _es_subconjunto_de_palabras(nombre_a, nombre_b):
        return 100.0
    por_palabras = fuzz.token_sort_ratio(nombre_a, nombre_b)
    letras_a = ''.join(sorted(nombre_a.replace(' ', '')))
    letras_b = ''.join(sorted(nombre_b.replace(' ', '')))
    por_letras = fuzz.ratio(letras_a, letras_b)
    if por_letras >= 95:
        return round(max(por_palabras, por_letras), 1)
    # Se redondea aca (y no al mostrar) para que el mismo valor limpio viaje al reporte
    # de Excel, a las tablas de la web y a los textos de alerta -- rapidfuzz devuelve
    # cosas como 90.32424323432 y un decimal alcanza de sobra para decidir un umbral.
    return round(por_palabras, 1)


def _fusionar_entradas_live_results(existente, otra):
    """
    Combina 'otra' dentro de 'existente' (dos tarjetas de live_results del mismo
    task). Se usa cuando el usuario corrige el documento de una tarjeta y ese valor
    ya pertenece a otra -- tipico cuando el anverso y el reverso de la MISMA cedula
    quedaron separados porque uno de los dos se leyo mal (ver PATRON_DOC_GENERICO
    en parser.py). Sin esto, "corregir" el documento dejaria dos tarjetas duplicadas
    con el mismo numero en vez de una sola completa.
    """
    if len(otra.get("name") or "") > len(existente.get("name") or ""):
        existente["name"] = otra["name"]
    if otra.get("date") and not existente.get("date"):
        existente["date"] = otra["date"]

    for pagina, imagen in zip(otra.get("pages", []), otra.get("images", [])):
        if pagina not in existente["pages"]:
            existente["pages"].append(pagina)
            existente["images"].append(imagen)

    for lado in otra.get("sides", []):
        if lado not in existente["sides"]:
            existente["sides"].append(lado)
    if "Anverso (Frente)" in existente["sides"] and "Reverso (Atrás)" in existente["sides"]:
        existente["side"] = "Ambas Caras (Completo)"

    if otra.get("raw_text"):
        existente["raw_text"] = (existente.get("raw_text") or "") + "\n\n" + otra["raw_text"]

    for campo in ("tipo_documento", "lugar_nacimiento", "sexo", "estatura",
                  "grupo_sanguineo", "fecha_lugar_expedicion", "edad"):
        if otra.get(campo) and not existente.get(campo):
            existente[campo] = otra[campo]

    return existente


PATRON_DOC_PLACEHOLDER = re.compile(r'^Sujeto_Pag_\d+$')


def _auto_fusionar_huerfanos_en_live_results(live_results, threshold=90, max_distancia_paginas=2):
    """
    Fusiona automaticamente las tarjetas "huerfanas" (paginas donde el OCR no logro
    leer el numero de documento y se quedaron con el placeholder "Sujeto_Pag_N") con
    la tarjeta de la MISMA persona cuando el nombre coincide con muy alta confianza --
    tipico del reverso de una cedula cuyo anverso ya se leyo bien en otra pagina. Es
    el mismo caso que _fusionar_entradas_live_results ya resuelve a mano cuando el
    usuario corrige el documento; esto lo hace solo, sin esperar esa correccion, y
    unicamente cuando la confianza es alta para no mezclar dos personas distintas.
    Se corre una sola vez cuando todas las paginas ya terminaron (no durante el
    procesamiento en paralelo) para comparar contra el universo completo de tarjetas.

    Usa _similitud_nombre (no fuzz.token_sort_ratio puro) para que un nombre huerfano
    INCOMPLETO (p. ej. "LIDA YASMIN" cuando la version completa es "ALDANA BOHORQUEZ
    LIDA YASMIN" en la otra pagina) tambien fusione: token_sort_ratio solo, al ser un
    subconjunto y no una simple reordenacion, daba 56% -- muy por debajo del umbral --
    y la fusion nunca se disparaba.

    SEGURIDAD: ademas del nombre, se exige que la pagina huerfana este a lo sumo
    max_distancia_paginas de alguna pagina ya asociada a la tarjeta candidata. El
    anverso y el reverso de UNA cedula casi siempre quedan en paginas consecutivas del
    mismo PDF (se escanean juntos) -- esto reduce muchisimo el riesgo de fusionar por
    error a dos personas DISTINTAS que compartan nombre y apellido en otra parte del
    lote: aunque el nombre matchee al 100%, si esta lejos no se fusiona.
    """
    if not fuzz:
        return live_results

    reales = [lr for lr in live_results if not PATRON_DOC_PLACEHOLDER.match(lr["document"])]
    huerfanos = [lr for lr in live_results if PATRON_DOC_PLACEHOLDER.match(lr["document"])]
    if not reales:
        return live_results

    fusionados_ids = set()
    for h in huerfanos:
        nombre = (h.get("name") or "").strip()
        if len(nombre) <= 3:
            continue
        pagina_h = h["pages"][0] if h.get("pages") else None
        mejor_score, mejor_real = 0, None
        for r in reales:
            if pagina_h is not None and r.get("pages"):
                distancia = min(abs(pagina_h - p) for p in r["pages"])
                if distancia > max_distancia_paginas:
                    continue
            score = _similitud_nombre(nombre, (r.get("name") or "").strip())
            if score > mejor_score:
                mejor_score, mejor_real = score, r
        if mejor_real is not None and mejor_score >= threshold:
            _fusionar_entradas_live_results(mejor_real, h)
            fusionados_ids.add(id(h))

    return [lr for lr in live_results if id(lr) not in fusionados_ids]


def _reasignar_huerfanos_por_nombre(df_ocr, threshold=90, max_distancia_paginas=2):
    """
    Version de la fusion automatica de arriba para el DataFrame plano que usa la
    conciliacion (df_ocr): si una fila quedo con el placeholder "Sujeto_Pag_N" pero
    su Nombre_OCR coincide con muchisima confianza con el de OTRA fila que si tiene
    un documento real, se le reasigna ese mismo Documento_OCR -- asi el dedup por
    Documento_OCR que ya existe mas abajo las trata como una sola persona en vez de
    dejar la pagina sin numero como un huerfano aparte en "Solo en PDF".

    Usa _similitud_nombre (no fuzz.token_sort_ratio puro) por la misma razon que
    _auto_fusionar_huerfanos_en_live_results: un nombre huerfano incompleto (subconjunto
    del nombre completo en la otra pagina) da un puntaje bajo con token_sort_ratio solo.

    SEGURIDAD: ademas del nombre, se exige que la pagina huerfana este a lo sumo
    max_distancia_paginas de la pagina candidata (ver el comentario en
    _auto_fusionar_huerfanos_en_live_results para el porque).
    """
    if df_ocr.empty or not fuzz:
        return df_ocr

    es_placeholder = df_ocr['Documento_OCR'].str.match(r'^Sujeto_Pag_\d+$')
    reales = df_ocr[~es_placeholder]
    if reales.empty:
        return df_ocr

    for idx in df_ocr[es_placeholder].index:
        nombre = str(df_ocr.at[idx, 'Nombre_OCR']).strip()
        if len(nombre) <= 3:
            continue
        pagina_h = df_ocr.at[idx, 'Página']
        cercanas = reales[(reales['Página'] - pagina_h).abs() <= max_distancia_paginas]
        if cercanas.empty:
            continue
        mejor_score, mejor_doc = 0, None
        for _, fila in cercanas.iterrows():
            score = _similitud_nombre(nombre, str(fila['Nombre_OCR']).strip())
            if score > mejor_score:
                mejor_score, mejor_doc = score, fila['Documento_OCR']
        if mejor_doc is not None and mejor_score >= threshold:
            df_ocr.at[idx, 'Documento_OCR'] = mejor_doc

    return df_ocr


def _integrar_resultado_en_live_results(task_id, res):
    """
    Mete el resultado de UNA pagina en las tarjetas de la Consola (live_results),
    agrupando por documento. Antes vivia suelto dentro del loop de OCR; ahora tambien lo
    usan las paginas que salen del cache, que tienen que pasar por exactamente la misma
    fusion (mismo criterio de nombre mas largo, fecha que rellena, caras, etc.).
    """
    # Update live results database (grouping by Document ID) in real-time
    if "live_results" not in tasks_db[task_id]:
        tasks_db[task_id]["live_results"] = []

    # Mismo identificador (real o "Sujeto_Pag_N") que ya quedo calculado
    # en page_data, para que la tarjeta de la Consola y la fila de
    # conciliacion de esta pagina usen exactamente el mismo valor.
    doc_str = res["page_data"]["Documento_OCR"]

    existing_idx = None
    for j, live_res in enumerate(tasks_db[task_id]["live_results"]):
        if live_res["document"] == doc_str:
            existing_idx = j
            break

    datos_res = res.get("datos") or {}
    campos_nuevos = ("tipo_documento", "lugar_nacimiento", "sexo",
                      "estatura", "grupo_sanguineo", "fecha_lugar_expedicion")

    if existing_idx is not None:
        existing = tasks_db[task_id]["live_results"][existing_idx]
        name_curr = str(res["name_detected"]).strip() if res["name_detected"] else ""
        if len(name_curr) > len(existing["name"]):
            existing["name"] = name_curr
        date_curr = str(res["date_detected"]).strip()
        date_changed = False
        if date_curr and date_curr != "None" and (not existing["date"] or existing["date"] == "None"):
            existing["date"] = date_curr
            date_changed = True
        if res["page_num"] not in existing["pages"]:
            existing["pages"].append(res["page_num"])
            existing["images"].append({"page": res["page_num"], "url": res["image_url"]})
        if res["cara_detected"] not in existing["sides"]:
            existing["sides"].append(res["cara_detected"])
        if "Anverso (Frente)" in existing["sides"] and "Reverso (Atrás)" in existing["sides"]:
            existing["side"] = "Ambas Caras (Completo)"
        else:
            existing["side"] = res["cara_detected"] if res["cara_detected"] != "No detectado" else existing["side"]
        existing["raw_text"] += f"\n\n--- [PÁGINA {res['page_num']}] ---\n\n{res['full_text']}"

        # Los campos nuevos suelen aparecer en una sola cara (p. ej. reverso);
        # se rellenan solo si aun estan vacios, igual que la fecha.
        for campo in campos_nuevos:
            valor_nuevo = datos_res.get(campo)
            if valor_nuevo and not existing.get(campo):
                existing[campo] = valor_nuevo
        if date_changed or not existing.get("edad"):
            existing["edad"] = calcular_edad(existing["date"])
    else:
        nueva_entrada = {
            "document": doc_str,
            "name": str(res["name_detected"]).strip() if res["name_detected"] else "",
            "date": str(res["date_detected"]).strip() if res["date_detected"] else "",
            "side": res["cara_detected"],
            "sides": [res["cara_detected"]],
            "method": res["metodo_origen"],
            "pages": [res["page_num"]],
            "images": [{"page": res["page_num"], "url": res["image_url"]}],
            "raw_text": res["full_text"],
            "edad": datos_res.get("edad"),
        }
        for campo in campos_nuevos:
            nueva_entrada[campo] = datos_res.get(campo)
        tasks_db[task_id]["live_results"].append(nueva_entrada)

    # Save current page text for live log
    tasks_db[task_id]["current_page_text"] = res["full_text"]


# --------------------------------------------------------------------------------------
# Cache de OCR: reusar el escaneo de un PDF que ya se proceso antes
# --------------------------------------------------------------------------------------
# Escanear es lo caro de toda la corrida (~2 seg por pagina entre convertir a imagen y
# pasar el OCR). Como el resultado depende unicamente del PDF y de dos ajustes (dpi y
# filtro), volver a subir el MISMO archivo no tiene por que costar lo mismo la segunda
# vez. Se guarda el resultado por pagina en la tabla ocr_cache y la imagen anotada en
# static/ocr_cache/, ambas con el hash del contenido en la clave.

CACHE_IMG_DIRNAME = "ocr_cache"


def _hash_pdf(pdf_bytes):
    """Identidad del PDF por CONTENIDO, no por nombre de archivo."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def _slug_filtro(img_filter):
    """El filtro va en el nombre del archivo, asi que se deja apto para disco."""
    return re.sub(r'[^a-z0-9]+', '-', str(img_filter).lower()).strip('-') or "sin-filtro"


def _nombre_imagen_cache(pdf_hash, dpi, img_filter, page_num):
    return f"{pdf_hash[:16]}_{dpi}_{_slug_filtro(img_filter)}_p{page_num}.jpg"


# <hash16>_<dpi>_<filtro-slug>_pN.jpg -- ver _nombre_imagen_cache
_RE_NOMBRE_IMAGEN_CACHE = re.compile(r'^([0-9a-f]{16})_(\d+)_(.+)_p\d+\.jpg$', re.IGNORECASE)


def _clave_cache_desde_imagenes(live_results, claves_conocidas):
    """
    Deduce la clave del cache de una auditoria vieja a partir del nombre de sus imagenes
    anotadas. El nombre solo trae los primeros 16 caracteres del hash y el filtro
    "sluggeado", asi que no alcanza por si solo: se usa para ubicar la clave COMPLETA
    entre las que ya existen en el cache. Retorna None si no se puede identificar.
    """
    for entrada in live_results or []:
        for imagen in (entrada or {}).get("images") or []:
            nombre = str((imagen or {}).get("url") or "").rsplit("/", 1)[-1]
            match = _RE_NOMBRE_IMAGEN_CACHE.match(nombre)
            if not match:
                continue
            hash16, dpi, slug = match.group(1).lower(), int(match.group(2)), match.group(3).lower()
            for clave in claves_conocidas:
                if (clave["pdf_hash"][:16].lower() == hash16
                        and clave["dpi"] == dpi
                        and _slug_filtro(clave["img_filter"]) == slug):
                    return clave
    return None


def _backfill_claves_cache():
    """
    Completa la clave del cache en las auditorias guardadas ANTES de que se empezara a
    persistir (las que ya estaban en el historial al actualizar la app). Sin esto,
    borrarlas del historial no borraria su cache y el mismo PDF seguiria saliendo
    cacheado para siempre.

    Corre al arrancar y es idempotente: una vez completada, la auditoria ya no aparece
    en la consulta. Las que no se puedan resolver (su cache ya no existe) se dejan como
    estan -- si no hay cache, tampoco hay nada que borrar despues.
    """
    try:
        pendientes = db.auditorias_sin_clave_cache()
        if not pendientes:
            return
        claves_conocidas = db.claves_cache_ocr()
        if not claves_conocidas:
            return
        for task_id, live_results in pendientes:
            clave = _clave_cache_desde_imagenes(live_results, claves_conocidas)
            if clave:
                db.set_audit_cache_key(task_id, clave["pdf_hash"], clave["dpi"], clave["img_filter"])
    except Exception as err:
        print(f"Advertencia: no se pudieron completar las claves de cache del historial: {err}")


def _borrar_cache_ocr_de_auditoria(clave, task_id):
    """
    Borra el escaneo cacheado (filas de ocr_cache + imagenes anotadas en disco) de una
    auditoria que se acaba de eliminar del historial, para que volver a subir ese PDF lo
    procese desde cero en vez de reusar el escaneo viejo.

    No se borra nada si quedo OTRA auditoria en el historial escaneada con la misma clave:
    el cache es por contenido de PDF, no por auditoria, asi que llevarselo dejaria a esa
    otra con la Vista del Documento en blanco.
    """
    resultado = {"cache_ocr_borrado": False, "paginas_cache_borradas": 0}
    if not clave:
        return resultado
    try:
        if db.otra_auditoria_usa_cache(clave["pdf_hash"], clave["dpi"], clave["img_filter"], task_id):
            return resultado

        filas, huerfanas = db.delete_cache_ocr(clave["pdf_hash"], clave["dpi"], clave["img_filter"])

        # A las imagenes que nombraba el cache se les suman las que sigan en disco con el
        # prefijo de esta clave: si alguna fila quedo sin image_url, el .jpg igual tiene
        # que irse -- si no, nadie mas lo referencia y ocupa disco para siempre.
        cache_dir = os.path.join(BASE_DIR, "static", CACHE_IMG_DIRNAME)
        prefijo = f"{clave['pdf_hash'][:16]}_{clave['dpi']}_{_slug_filtro(clave['img_filter'])}_p"
        nombres = set(huerfanas)
        try:
            nombres.update(n for n in os.listdir(cache_dir) if n.startswith(prefijo))
        except OSError:
            pass
        for nombre in nombres:
            ruta = os.path.join(cache_dir, nombre)
            if os.path.isfile(ruta):
                os.remove(ruta)

        resultado.update({"cache_ocr_borrado": True, "paginas_cache_borradas": filas})
    except Exception as err:
        # Que falle la limpieza del cache no puede tumbar el borrado del historial, que
        # para el usuario es lo que pidio y ya se hizo.
        print(f"Advertencia: no se pudo borrar el cache de OCR de {task_id}: {err}")
    return resultado


def _rangos_contiguos(paginas):
    """
    [3,4,5,9,10] -> [(3,5), (9,10)]. Sirve para pedirle a poppler solo los tramos que
    faltan escanear: si el cache ya tiene el medio del PDF, no se convierte al pedo.
    """
    rangos = []
    for pagina in sorted(paginas):
        if rangos and pagina == rangos[-1][1] + 1:
            rangos[-1][1] = pagina
        else:
            rangos.append([pagina, pagina])
    return [(ini, fin) for ini, fin in rangos]


def _cache_utilizable(cache_dir, data):
    """
    Una entrada solo sirve si su imagen anotada sigue en disco -- si no, la Vista del
    Documento saldria rota y es preferible volver a escanear esa pagina.
    """
    if not isinstance(data, dict) or not data.get("page_data"):
        return False
    url = data.get("image_url") or ""
    if not url:
        return False
    return os.path.isfile(os.path.join(cache_dir, url.rsplit("/", 1)[-1]))


def run_audit_background(
    task_id: str,
    excel_bytes: bytes,
    pdf_bytes: bytes,
    selected_row_idx: int,
    key_col: str,
    compare_cols: List[str],
    similarity_threshold: int,
    start_page: int,
    end_page: int,
    pdf_dpi: int,
    img_filter: str,
    excel_filename: str = None,
    pdf_filename: str = None
):
    started_at = time.time()
    # Ritmo de corridas anteriores en esta maquina: sirve para dar una estimacion desde
    # el primer segundo, antes de tener datos de ESTA corrida. Puede ser None la primera
    # vez que se usa la app.
    try:
        ritmo_historico = db.segundos_por_pagina_historico()
    except Exception:
        ritmo_historico = None
    try:
        tasks_db[task_id]["status"] = "processing"
        tasks_db[task_id]["_started_at"] = started_at

        # 1. Parse Excel
        df_raw = pd.read_excel(io.BytesIO(excel_bytes), header=None)
        header_vals = df_raw.iloc[selected_row_idx].tolist()
        clean_headers = []
        for i, val in enumerate(header_vals):
            if pd.isna(val) or str(val).strip() == "":
                clean_headers.append(f"Columna_{i+1}")
            else:
                clean_headers.append(str(val).strip())
                
        df_excel = df_raw.iloc[selected_row_idx + 1:].copy()
        df_excel.columns = clean_headers
        df_excel = df_excel.reset_index(drop=True)
        
        # 2. Loop pages in PDF
        audit_results = []
        pages_range = list(range(start_page, end_page + 1))
        total_to_process = len(pages_range)
        
        poppler_path = _resolver_poppler_path()

        # Antes de convertir o escanear nada: ver que paginas de este mismo PDF (mismo
        # contenido, mismo dpi, mismo filtro) ya se escanearon en alguna corrida previa.
        # Las que esten cacheadas no se convierten NI se pasan por OCR.
        pdf_hash = _hash_pdf(pdf_bytes)
        cache_dir = os.path.join(BASE_DIR, "static", CACHE_IMG_DIRNAME)
        os.makedirs(cache_dir, exist_ok=True)
        try:
            paginas_cacheadas = db.get_cached_pages(pdf_hash, pdf_dpi, img_filter, start_page, end_page)
            paginas_cacheadas = {
                pagina: data for pagina, data in paginas_cacheadas.items()
                if _cache_utilizable(cache_dir, data)
            }
        except Exception as cache_err:
            # El cache es una optimizacion: si falla, se escanea todo como siempre.
            print(f"Advertencia: no se pudo leer el cache de OCR: {cache_err}")
            paginas_cacheadas = {}

        if paginas_cacheadas:
            try:
                db.touch_cache_ocr(pdf_hash, pdf_dpi, img_filter, start_page, end_page)
            except Exception:
                pass

        paginas_a_procesar = [p for p in pages_range if p not in paginas_cacheadas]
        # Progreso y ETA se miden contra lo que HAY QUE escanear, no contra el total del
        # rango: si 40 de 42 paginas salen del cache, la barra tiene que reflejar eso.
        total_a_escanear = len(paginas_a_procesar)
        tasks_db[task_id]["cached_pages"] = len(paginas_cacheadas)
        tasks_db[task_id]["scanned_pages"] = total_a_escanear


        # Convertir el PDF a imagenes en bloques (no todo de una sola vez): con DPI alto
        # o PDFs largos, convertir TODO antes de reportar cualquier progreso podia tardar
        # minutos mostrando 0% -- se veia trabado aunque estuviera funcionando bien.
        # Reservamos 0-40% de la barra para esta conversion y 40-90% para el OCR.
        #
        # Dos ajustes de velocidad sobre la conversion en si (pdf2image/poppler):
        # - thread_count: por defecto pdf2image renderiza 1 pagina a la vez aunque la
        #   maquina tenga varios nucleos. Pasarle thread_count reparte cada bloque entre
        #   varios procesos de poppler en paralelo.
        # - grayscale=True: le pedimos a poppler que renderice directo en blanco y negro
        #   en vez de a color -- hay menos datos que decodificar. El OCR igual convierte
        #   todo a escala de grises internamente, asi que no se pierde nada para el
        #   reconocimiento; el unico efecto visible es que la imagen en "Vista del
        #   Documento Analizado" deja de verse a color (decision tomada con el usuario).
        # Las imagenes van indexadas POR NUMERO DE PAGINA (antes era una lista corrida
        # desde start_page): con el cache los tramos a convertir pueden tener huecos.
        imagenes_por_pagina = {}
        CONVERSION_CHUNK_SIZE = 20
        poppler_threads = min(4, os.cpu_count() or 4)
        if not paginas_a_procesar:
            tasks_db[task_id]["progress"] = 40
            tasks_db[task_id]["status_detail"] = f"Las {total_to_process} página(s) ya estaban escaneadas: recuperándolas..."
        else:
            if paginas_cacheadas:
                tasks_db[task_id]["status_detail"] = f"{len(paginas_cacheadas)} página(s) ya escaneadas; convirtiendo las {total_a_escanear} restantes (DPI {pdf_dpi})..."
            else:
                tasks_db[task_id]["status_detail"] = f"Convirtiendo {total_a_escanear} página(s) del PDF a imágenes (DPI {pdf_dpi})..."
            try:
                from pdf2image import convert_from_bytes
                for tramo_ini, tramo_fin in _rangos_contiguos(paginas_a_procesar):
                    pagina_actual = tramo_ini
                    while pagina_actual <= tramo_fin:
                        fin_bloque = min(pagina_actual + CONVERSION_CHUNK_SIZE - 1, tramo_fin)
                        bloque = convert_from_bytes(
                            pdf_bytes,
                            dpi=pdf_dpi,
                            first_page=pagina_actual,
                            last_page=fin_bloque,
                            poppler_path=poppler_path,
                            thread_count=poppler_threads,
                            grayscale=True,
                        )
                        for offset, imagen in enumerate(bloque):
                            imagenes_por_pagina[pagina_actual + offset] = imagen
                        convertidas = len(imagenes_por_pagina)
                        tasks_db[task_id]["progress"] = int((convertidas / total_a_escanear) * 40)
                        tasks_db[task_id]["status_detail"] = f"Convirtiendo PDF a imágenes... ({convertidas}/{total_a_escanear} páginas)"
                        # Mientras se convierte todavia no hay ritmo de OCR medido, asi que la
                        # estimacion se apoya en el historial de la maquina (ver
                        # segundos_por_pagina_historico). Se afina sola apenas empiece el OCR.
                        if ritmo_historico:
                            tasks_db[task_id]["_eta_total_estimado"] = ritmo_historico * total_a_escanear
                        pagina_actual = fin_bloque + 1
            except Exception as e:
                # Fallback si la conversion en bloque falla: cada pagina se convierte
                # individualmente mas adelante (mas lento, pero sigue funcionando).
                imagenes_por_pagina = {}

        if total_a_escanear:
            tasks_db[task_id]["status_detail"] = "Leyendo cédulas con OCR..."
        # A partir de aqui empieza la fase pesada. Se marca el inicio para poder medir
        # el ritmo REAL de esta corrida y con eso estimar lo que falta.
        ocr_started_at = time.time()

        # Define a helper function to process a single page (OCR + drawing)
        def process_page_task(page_num, pre_rendered):
            try:
                full_text, metodo_origen, ocr_result = extraer_texto_con_enrutamiento(
                    pdf_bytes,
                    page_num,
                    pdf_dpi=pdf_dpi,
                    poppler_path=poppler_path,
                    img_filter=img_filter,
                    pre_rendered_image=pre_rendered
                )
                datos = extraer_datos_texto(full_text)
            except Exception as e:
                # Antes esto se tragaba el error en silencio -- "Fallo" sin mas detalle,
                # imposible de diagnosticar despues (ni en consola ni en el reporte). Se
                # imprime a consola para quien tenga acceso a la terminal, y ademas queda
                # en el texto crudo de la tarjeta/reporte para que tambien sea visible
                # para quien solo use la app (sin terminal a mano).
                print(f"[ERROR] Página {page_num}: {type(e).__name__}: {e}")
                datos = {}
                metodo_origen = "Fallo"
                full_text = f"[ERROR] {type(e).__name__}: {e}"
                ocr_result = None

            doc_detected = datos.get("documento")
            name_detected = datos.get("nombre_completo")
            date_detected = datos.get("fecha_nacimiento")
            cara_detected = datos.get("cara") or "No detectado"

            # Si el OCR no encontro NINGUN numero de documento en esta pagina, se le
            # asigna un identificador placeholder unico por pagina en vez de dejarlo
            # vacio -- asi la pagina sigue siendo visible en la conciliacion (aparece
            # como huerfano en vez de desaparecer en silencio) y se puede fusionar
            # despues a mano editando el documento si en realidad es el reverso de
            # una cedula ya identificada en otra pagina.
            doc_str = str(doc_detected).strip().replace('.', '') if doc_detected else f"Sujeto_Pag_{page_num}"

            page_data = {
                "Página": page_num,
                "Documento_OCR": doc_str,
                "Nombre_OCR": str(name_detected).strip() if name_detected else "",
                "Fecha_Nacimiento_OCR": str(date_detected).strip() if date_detected else "",
                "Cara_OCR": cara_detected,
                "Tipo_Documento_OCR": str(datos.get("tipo_documento") or "").strip(),
                "Lugar_Nacimiento_OCR": str(datos.get("lugar_nacimiento") or "").strip(),
                "Sexo_OCR": str(datos.get("sexo") or "").strip(),
                "Estatura_OCR": str(datos.get("estatura") or "").strip(),
                "Grupo_Sanguineo_OCR": str(datos.get("grupo_sanguineo") or "").strip(),
                "Fecha_Lugar_Expedicion_OCR": str(datos.get("fecha_lugar_expedicion") or "").strip(),
                "Metodo_Extraccion": metodo_origen,
                "Texto_Completo": full_text
            }
            
            image_url = None
            try:
                import cv2
                import numpy as np
                from PIL import Image

                # La imagen anotada se guarda en static/ocr_cache/ con el hash del PDF en
                # el nombre (antes iba a static/uploads/ con el task_id). Dos motivos: la
                # comparte cualquier corrida futura del mismo PDF, y la limpieza horaria
                # de uploads/ no se la lleva puesta.
                os.makedirs(cache_dir, exist_ok=True)
                
                # Use pre-rendered or convert page
                if pre_rendered is not None:
                    img_pil = pre_rendered
                else:
                    from pdf2image import convert_from_bytes
                    images = convert_from_bytes(pdf_bytes, dpi=pdf_dpi, first_page=page_num, last_page=page_num, poppler_path=poppler_path, grayscale=True)
                    img_pil = images[0] if images else None
                    
                if img_pil:
                    image_filename = _nombre_imagen_cache(pdf_hash, pdf_dpi, img_filter, page_num)
                    image_path = os.path.join(cache_dir, image_filename)
                    
                    if metodo_origen == "OCR" and ocr_result:
                        # Preprocess image to get scale factor
                        processed_img = preprocesar_imagen(img_pil, metodo=img_filter)
                        proc_w, proc_h = processed_img.size
                        orig_w, orig_h = img_pil.size
                        
                        scale_x = orig_w / proc_w
                        scale_y = orig_h / proc_h
                        
                        # Draw bounding boxes
                        img_np = np.array(img_pil).copy()
                        # Ensure BGR format for cv2 saving/drawing
                        if len(img_np.shape) == 2:
                            img_np = cv2.cvtColor(img_np, cv2.COLOR_GRAY2BGR)
                        elif img_np.shape[2] == 4:
                            img_np = cv2.cvtColor(img_np, cv2.COLOR_RGBA2BGR)
                        elif len(img_np.shape) == 3:
                            img_np = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
                        
                        for item in ocr_result:
                            dt_box = item[0]
                            # Scale the points back to original image coordinates
                            scaled_box = []
                            for pt in dt_box:
                                scaled_box.append([pt[0] * scale_x, pt[1] * scale_y])
                            pts = np.array(scaled_box, dtype=np.int32)
                            # Draw polygon (thickness 2, color green BGR: (0, 255, 0))
                            cv2.polylines(img_np, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
                        
                        cv2.imwrite(image_path, img_np)
                    else:
                        # Just save clean image
                        img_pil.save(image_path, "JPEG")
                        
                    image_url = f"/{CACHE_IMG_DIRNAME}/{image_filename}"
            except Exception as e:
                pass
                
            return {
                "page_data": page_data,
                "page_num": page_num,
                "doc_detected": doc_detected,
                "name_detected": name_detected,
                "date_detected": date_detected,
                "cara_detected": cara_detected,
                "datos": datos,
                "metodo_origen": metodo_origen,
                "full_text": full_text,
                "image_url": image_url
            }
            
        # Execute the page tasks in parallel!
        # Una pagina por core: el trabajo pesado es el OCR, que ademas usa 2 hilos por
        # inferencia (ver OCR_INTRA_OP_THREADS en src/ocr.py). Esa sobresuscripcion a
        # proposito midio mejor que ajustarse justo al numero de cores, porque la
        # inferencia no mantiene la CPU ocupada el 100% del tiempo. El tope evita que en
        # un servidor con muchisimos cores se lancen cientos de hilos peleandose.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        max_threads = min(os.cpu_count() or 4, 12)
        
        # We need to preserve the order or sort the final audit_results list by page number
        task_results = []

        # Las paginas que salieron del cache se integran primero y en orden de pagina: no
        # cuestan nada y asi las tarjetas de la Consola aparecen en el mismo orden que
        # tendrian en una corrida normal.
        for page_num in sorted(paginas_cacheadas):
            res_cacheado = paginas_cacheadas[page_num]
            task_results.append(res_cacheado)
            try:
                _integrar_resultado_en_live_results(task_id, res_cacheado)
            except Exception as ex:
                print(f"Error integrando la página {page_num} desde el cache: {ex}")
        if paginas_cacheadas:
            tasks_db[task_id]["current_page"] = max(paginas_cacheadas)

        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            futures = {}
            for page_num in paginas_a_procesar:
                pre_rendered = imagenes_por_pagina.get(page_num)

                future = executor.submit(process_page_task, page_num, pre_rendered)
                futures[future] = page_num
                
            # Cuantas paginas terminadas hacen falta antes de fiarse del ritmo medido
            # (ver el detalle mas abajo). En lotes mas chicos que el numero de hilos se
            # baja el listón, para que igual llegue a estimar algo.
            umbral_ritmo = max(2, min(max_threads, total_a_escanear or 1))
            completed_count = 0
            for future in as_completed(futures):
                p_num = futures[future]
                completed_count += 1
                
                # Update progress (40-90%: 0-40% ya se uso para convertir el PDF a imagenes)
                tasks_db[task_id]["progress"] = 40 + int((completed_count / max(total_a_escanear, 1)) * 50)

                # Se guarda el TOTAL estimado de la corrida, no el restante: asi el
                # endpoint de estado puede restarle el tiempo ya transcurrido en cada
                # consulta y el contador baja segundo a segundo. Guardar directamente el
                # restante lo dejaba congelado entre pagina y pagina -- con varios hilos
                # las paginas terminan en oleadas, y el numero se quedaba quieto ~15s y
                # luego pegaba saltos.
                #
                # El ritmo sale del OCR REAL de esta corrida, que es lo que hace fiable
                # la estimacion: no supone nada del equipo, ni del DPI, ni de si las
                # paginas traen texto embebido.
                #
                # Se espera a tener una OLEADA completa de paginas terminadas (tantas
                # como hilos) antes de hacerle caso al ritmo medido. Con N hilos las
                # paginas no terminan de a una sino en oleadas, y medir a mitad de la
                # primera oleada reparte el tiempo de N paginas entre las 2 o 3 que ya
                # acabaron: el ritmo sale inflado y la estimacion pegaba un salto (se
                # midio un pico de 62s restantes cuando faltaban 26s reales).
                if completed_count >= umbral_ritmo:
                    ritmo_real = (time.time() - ocr_started_at) / completed_count
                    tiempo_conversion = ocr_started_at - started_at
                    nuevo_total = tiempo_conversion + ritmo_real * total_a_escanear
                    # Promedio suavizado con lo que ya se creia: amortigua los saltos
                    # cuando una oleada entra de golpe, sin dejar de converger al valor
                    # real a medida que avanza la corrida.
                    anterior = tasks_db[task_id].get("_eta_total_estimado")
                    if anterior:
                        nuevo_total = 0.7 * anterior + 0.3 * nuevo_total
                    tasks_db[task_id]["_eta_total_estimado"] = nuevo_total
                # Las paginas se procesan en paralelo (varios hilos), asi que terminan en
                # el orden en que cada una acaba y NO en orden de pagina -- se ve saltado
                # en el indicador si mostramos la ultima que termino sin mas. Usamos el
                # maximo alcanzado hasta ahora para que el numero mostrado nunca retroceda,
                # aunque por dentro sigan llegando fuera de orden.
                tasks_db[task_id]["current_page"] = max(tasks_db[task_id].get("current_page", 0), p_num)
                
                try:
                    res = future.result()
                    task_results.append(res)
                    
                    _integrar_resultado_en_live_results(task_id, res)

                    # Recien escaneada: al cache, para que la proxima vez que suban este
                    # mismo PDF esta pagina no se vuelva a procesar. Si el guardado falla
                    # no pasa nada grave -- se pierde el ahorro, no el resultado.
                    try:
                        db.save_cached_page(pdf_hash, pdf_dpi, img_filter, p_num, res)
                    except Exception as cache_err:
                        print(f"Advertencia: no se pudo cachear la página {p_num}: {cache_err}")
                    
                except Exception as ex:
                    print(f"Error procesando resultado de página {p_num}: {ex}")

        # Ahora que TODAS las paginas ya terminaron (sin importar el orden en que lo
        # hicieron), se intenta fusionar automaticamente cualquier tarjeta huerfana
        # (documento sin leer) con la tarjeta de la misma persona por similitud de
        # nombre -- ver _auto_fusionar_huerfanos_en_live_results.
        tasks_db[task_id]["live_results"] = _auto_fusionar_huerfanos_en_live_results(
            tasks_db[task_id].get("live_results", [])
        )

        # Sort results by Page number to preserve order in report
        task_results.sort(key=lambda x: x["page_num"])
        audit_results = [r["page_data"] for r in task_results]

        # 3. Deduplicate
        # Cada pagina sin documento legible trae su propio placeholder unico
        # "Sujeto_Pag_N" (ver process_page_task). Antes de deduplicar por
        # Documento_OCR, _reasignar_huerfanos_por_nombre intenta reasignarle a esas
        # filas el documento real de otra pagina con un nombre casi identico (misma
        # logica que arriba, pero sobre este DataFrame plano) -- asi el dedup de abajo
        # las une en vez de dejarlas como huerfanas en la conciliacion. Solo quedan
        # visibles como huerfanas las que de verdad no se parecen a nada mas.
        df_ocr = pd.DataFrame(audit_results)
        df_ocr = _reasignar_huerfanos_por_nombre(df_ocr)

        if not df_ocr.empty:
            df_ocr['_completitud'] = (
                (df_ocr['Nombre_OCR'].str.len() > 3).astype(int) * 3 +
                (df_ocr['Fecha_Nacimiento_OCR'].str.strip() != 'None').astype(int) * 2 +
                (df_ocr['Fecha_Nacimiento_OCR'].str.strip() != '').astype(int) * 2
            )
            # Antes de deduplicar (que se queda con UNA sola pagina por documento, casi
            # siempre el anverso), guardamos todas las paginas para rellenar los campos
            # nuevos -- viven en el reverso, asi que si solo mirasemos la pagina ganadora
            # se perderian siempre.
            df_ocr_todas_paginas = df_ocr.copy()
            df_ocr = df_ocr.sort_values('_completitud', ascending=False).drop_duplicates(subset='Documento_OCR', keep='first')
            df_ocr = df_ocr.drop(columns=['_completitud'])

            campos_a_rellenar = [
                'Tipo_Documento_OCR', 'Lugar_Nacimiento_OCR', 'Sexo_OCR',
                'Estatura_OCR', 'Grupo_Sanguineo_OCR', 'Fecha_Lugar_Expedicion_OCR'
            ]

            def _primer_no_vacio(serie):
                for v in serie:
                    if v and str(v).strip() and str(v).strip().lower() != 'none':
                        return v
                return ''

            fill_map = df_ocr_todas_paginas.groupby('Documento_OCR')[campos_a_rellenar].agg(_primer_no_vacio)
            df_ocr = df_ocr.set_index('Documento_OCR')
            for campo in campos_a_rellenar:
                df_ocr[campo] = fill_map[campo]

            # El nombre tambien puede quedar mas completo en OTRA pagina del mismo
            # documento -- caso real: una pagina que solo alcanzo a leer "LIDA YASMIN" y
            # su pagina hermana (unida a este mismo documento por
            # _reasignar_huerfanos_por_nombre) que si tenia "ALDANA BOHORQUEZ LIDA
            # YASMIN" completo por MRZ. _completitud decide que fila "gana" por otros
            # criterios (fecha de nacimiento, sobre todo) y esa fila ganadora no es
            # necesariamente la del nombre mas largo -- por eso el nombre se toma aparte,
            # como el mas largo visto en CUALQUIER pagina de ese documento, no solo el de
            # la fila ganadora.
            nombre_mas_completo = df_ocr_todas_paginas.groupby('Documento_OCR')['Nombre_OCR'].agg(
                lambda s: max(s, key=lambda v: len(str(v).strip()))
            )
            df_ocr['Nombre_OCR'] = nombre_mas_completo
            df_ocr = df_ocr.reset_index()

            # La edad se calcula sobre la fecha de nacimiento ya definitiva de la fila ganadora
            df_ocr['Edad_OCR'] = df_ocr['Fecha_Nacimiento_OCR'].apply(calcular_edad).fillna('')

        df_excel_clean = df_excel.copy()
        df_excel_clean['Identificación_Limpia'] = df_excel_clean[key_col].astype(str).str.extract(r'(\d+)')
        df_excel_clean['Identificación_Limpia'] = df_excel_clean['Identificación_Limpia'].fillna('')
        
        if compare_cols:
            df_temp = df_excel_clean[compare_cols].fillna('').astype(str)
            for col in compare_cols:
                df_temp[col] = df_temp[col].apply(lambda x: '' if str(x).lower().strip() == 'nan' else str(x).strip())
            df_excel_clean['Nombre_Base'] = df_temp.agg(lambda x: ' '.join([s for s in x if s.strip()]), axis=1).str.upper().str.replace(r'\s+', ' ', regex=True).str.strip()
        else:
            col_fallback = df_excel_clean.columns[0]
            df_excel_clean['Nombre_Base'] = df_excel_clean[col_fallback].fillna('').astype(str).str.upper().str.strip()
            
        tasks_db[task_id]["progress"] = 90
        tasks_db[task_id]["eta_seconds"] = 0  # ya no queda OCR pendiente

        # 4. Reconciliación + reporte (función reusable, la vuelve a llamar el endpoint de edición)
        # El cronometro se corta aqui, justo antes de conciliar/escribir el reporte --
        # esos dos pasos son rapidos (pandas + openpyxl en memoria) comparado con el OCR,
        # asi que la diferencia es despreciable y evita tener que regenerar el reporte
        # dos veces solo para poder incluir el tiempo adentro.
        elapsed_seconds = round(time.time() - started_at, 1)

        run_meta = {
            "excel_filename": excel_filename,
            "pdf_filename": pdf_filename,
            "start_page": start_page,
            "end_page": end_page,
            "similarity_threshold": similarity_threshold,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "elapsed_seconds": elapsed_seconds,
        }

        (lista_coinciden, lista_anomalias, lista_solo_pdf, lista_solo_excel,
         metrics, formatted_report_bytes) = reconciliar_y_generar_reporte(
            df_ocr, df_excel_clean, key_col, compare_cols, similarity_threshold, run_meta
        )

        # Save generated report
        generated_reports[task_id] = formatted_report_bytes

        results_payload = {
            "coinciden": lista_coinciden,
            "anomalias": lista_anomalias,
            "solo_excel": lista_solo_excel,
            "solo_pdf": lista_solo_pdf
        }

        _propagar_sugerencias(tasks_db[task_id].get("live_results", []), lista_anomalias, lista_solo_pdf)

        tasks_db[task_id].update({
            "status": "completed",
            "progress": 100,
            "metrics": metrics,
            "results": results_payload,
            "elapsed_seconds": elapsed_seconds,
        })

        # Se cachea el contexto necesario para poder editar un dato y volver a conciliar
        # despues sin re-correr el OCR. Deliberadamente NO va en tasks_db (ver el
        # comentario junto a la declaración de task_reconcile_context mas arriba).
        task_reconcile_context[task_id] = {
            "df_excel_clean": df_excel_clean,
            "key_col": key_col,
            "compare_cols": compare_cols,
            "similarity_threshold": similarity_threshold,
            # Clave del cache de OCR de esta corrida: la necesita el endpoint de edicion
            # para dejar las correcciones a mano guardadas junto al escaneo (ver
            # _guardar_correccion_en_cache).
            "cache_ocr": {"pdf_hash": pdf_hash, "dpi": pdf_dpi, "img_filter": img_filter},
            "run_meta_base": {
                "excel_filename": excel_filename,
                "pdf_filename": pdf_filename,
                "start_page": start_page,
                "end_page": end_page,
            },
        }

        # Guardar en el historial persistente. Un fallo aqui NUNCA debe tumbar el
        # flujo en memoria que ya funciona -- por eso va envuelto aparte.
        try:
            db.save_completed_audit(
                task_id=task_id,
                excel_filename=excel_filename,
                pdf_filename=pdf_filename,
                key_col=key_col,
                compare_cols=compare_cols,
                similarity_threshold=similarity_threshold,
                start_page=start_page,
                end_page=end_page,
                metrics=metrics,
                results=results_payload,
                live_results=tasks_db[task_id].get("live_results", []),
                report_bytes=formatted_report_bytes,
                pdf_url=tasks_db[task_id].get("pdf_url"),
                elapsed_seconds=elapsed_seconds,
                scanned_pages=total_a_escanear,
                # Con que clave quedo escaneado este PDF: es lo que permite borrar su
                # cache si despues se elimina la auditoria del historial.
                pdf_hash=pdf_hash,
                pdf_dpi=pdf_dpi,
                img_filter=img_filter,
            )
        except Exception as db_err:
            print(f"Advertencia: no se pudo guardar la auditoría {task_id} en el historial: {db_err}")

    except Exception as e:
        tasks_db[task_id].update({
            "status": "error",
            "error": str(e)
        })

@app.post("/api/start-audit")
async def start_audit(
    background_tasks: BackgroundTasks,
    excel_file: UploadFile = File(...),
    pdf_file: UploadFile = File(...),
    selected_row_idx: int = Form(0),
    key_col: str = Form(...),
    compare_cols: str = Form(...), # Comma separated list
    similarity_threshold: int = Form(90),
    start_page: int = Form(1),
    end_page: int = Form(1),
    pdf_dpi: int = Form(300),
    img_filter: str = Form("Solo Escala de Grises")
):
    try:
        task_id = str(uuid.uuid4())
        
        # Split compare columns
        compare_cols_list = [c.strip() for c in compare_cols.split(",") if c.strip()]
        
        # Read bytes
        excel_bytes = await excel_file.read()
        pdf_bytes = await pdf_file.read()
        
        # Create uploads folder and clean up older files (pero sin tocar los archivos
        # de auditorias ya guardadas en el historial -- si no, una auditoria reabierta
        # despues de una hora mostraria imagenes rotas)
        #
        # OJO con los dos nombres distintos que conviven en esta carpeta:
        #   ocr_<task_id>_<pagina>.jpg  -> imagenes anotadas (las de auditorias viejas;
        #                                  las nuevas ya viven en static/ocr_cache/)
        #   <task_id>.pdf              -> el PDF que subio el usuario, que alimenta el visor
        # Antes la excepcion solo contemplaba el .jpg, asi que el .pdf de una auditoria
        # guardada se borraba igual al cumplir la hora y el visor quedaba en 404 al
        # reabrirla desde el historial.
        uploads_dir = os.path.join(BASE_DIR, "static", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        try:
            import time
            now = time.time()
            persisted_ids = db.get_all_persisted_task_ids()
            for filename in os.listdir(uploads_dir):
                filepath = os.path.join(uploads_dir, filename)
                if not os.path.isfile(filepath) or os.stat(filepath).st_mtime >= now - 3600:
                    continue
                match = re.match(r'(?:ocr_)?([0-9a-fA-F\-]{36})(?:_\d+\.jpg|\.pdf)$', filename)
                if match and match.group(1) in persisted_ids:
                    continue
                os.remove(filepath)
        except Exception:
            pass
            
        # Purga del cache de OCR: lo que no se usa hace mas de 30 dias se borra (fila +
        # imagen), asi static/ocr_cache/ no crece indefinidamente. Un PDF de 500 paginas
        # deja 500 jpg; sin esto el disco se llena solo.
        try:
            cache_dir = os.path.join(BASE_DIR, "static", CACHE_IMG_DIRNAME)
            for nombre_img in db.purgar_cache_ocr(dias=30):
                ruta_img = os.path.join(cache_dir, nombre_img)
                if os.path.isfile(ruta_img):
                    os.remove(ruta_img)
        except Exception:
            pass

        pdf_path = os.path.join(uploads_dir, f"{task_id}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        
        tasks_db[task_id] = {
            "status": "queued",
            "progress": 0,
            "current_page": 0,
            "total_pages": end_page - start_page + 1,
            "eta_seconds": None,  # se llena en cuanto hay con que estimar (ver run_audit_background)
            "pdf_url": f"/uploads/{task_id}.pdf"
        }
        
        background_tasks.add_task(
            run_audit_background,
            task_id=task_id,
            excel_bytes=excel_bytes,
            pdf_bytes=pdf_bytes,
            selected_row_idx=selected_row_idx,
            key_col=key_col,
            compare_cols=compare_cols_list,
            similarity_threshold=similarity_threshold,
            start_page=start_page,
            end_page=end_page,
            pdf_dpi=pdf_dpi,
            img_filter=img_filter,
            excel_filename=excel_file.filename,
            pdf_filename=pdf_file.filename
        )
        
        return {"task_id": task_id}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al iniciar validación: {str(e)}")

@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks_db:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")

    task = tasks_db[task_id]
    # El restante se calcula AQUI, en cada consulta, restandole al total estimado el
    # tiempo ya transcurrido -- asi baja de forma continua aunque las paginas terminen
    # en oleadas (ver el comentario en run_audit_background).
    if task.get("status") in ("processing", "queued"):
        total_estimado = task.get("_eta_total_estimado")
        inicio = task.get("_started_at")
        if total_estimado and inicio:
            task["eta_seconds"] = max(0, round(total_estimado - (time.time() - inicio)))
    return task

@app.get("/api/download/{task_id}")
async def download_report(task_id: str):
    report_bytes = generated_reports.get(task_id)
    if report_bytes is None:
        # No esta en la cache en memoria (p. ej. el server se reinicio) -- buscar en el historial persistente
        report_bytes = db.get_report_bytes(task_id)
        if report_bytes is not None:
            generated_reports[task_id] = report_bytes

    if report_bytes is None:
        raise HTTPException(status_code=404, detail="Reporte no disponible o tarea incompleta")

    return StreamingResponse(
        io.BytesIO(report_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=Reporte_Auditoria_OCR_{task_id[:8]}.xlsx"}
    )

@app.get("/api/history")
async def get_history(limit: int = 50, offset: int = 0):
    return {"audits": db.list_audits(limit=limit, offset=offset)}

@app.get("/api/history/{task_id}")
async def get_history_detail(task_id: str):
    detail = db.get_audit_detail(task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Auditoría no encontrada en el historial")
    return detail

@app.delete("/api/history/{task_id}")
async def delete_history_entry(task_id: str):
    # La clave del cache hay que leerla ANTES de borrar la fila: despues ya no queda de
    # donde deducir que paginas de ocr_cache eran de esta auditoria.
    clave_cache = db.get_audit_cache_key(task_id)

    if not db.delete_audit(task_id):
        raise HTTPException(status_code=404, detail="Auditoría no encontrada en el historial")
    generated_reports.pop(task_id, None)

    # Borrar la auditoria tiene que borrar tambien su escaneo cacheado; si no, volver a
    # subir el mismo PDF seguiria saliendo del cache y no habria forma de reprocesarlo.
    cache = _borrar_cache_ocr_de_auditoria(clave_cache, task_id)

    return {"deleted": True, **cache}

# Como se llama cada campo editable en cada una de las tres capas: la tarjeta de la
# Consola (live_results), la fila que va al reporte (page_data) y el dict crudo del
# parser (datos). Se usa para que una correccion hecha a mano quede tambien guardada en
# el cache de OCR.
_CAMPOS_LIVE_A_PAGE_DATA = {
    "document": "Documento_OCR",
    "name": "Nombre_OCR",
    "date": "Fecha_Nacimiento_OCR",
    "tipo_documento": "Tipo_Documento_OCR",
    "lugar_nacimiento": "Lugar_Nacimiento_OCR",
    "sexo": "Sexo_OCR",
    "estatura": "Estatura_OCR",
    "grupo_sanguineo": "Grupo_Sanguineo_OCR",
    "fecha_lugar_expedicion": "Fecha_Lugar_Expedicion_OCR",
    "edad": "Edad_OCR",
}
_CAMPOS_LIVE_A_DATOS = {
    "document": "documento",
    "name": "nombre_completo",
    "date": "fecha_nacimiento",
    "tipo_documento": "tipo_documento",
    "lugar_nacimiento": "lugar_nacimiento",
    "sexo": "sexo",
    "estatura": "estatura",
    "grupo_sanguineo": "grupo_sanguineo",
    "fecha_lugar_expedicion": "fecha_lugar_expedicion",
    "edad": "edad",
}
_CAMPOS_LIVE_A_DETECTADO = {
    "document": "doc_detected",
    "name": "name_detected",
    "date": "date_detected",
}


def _guardar_correccion_en_cache(task_id, entry):
    """
    Deja la correccion hecha a mano guardada en el cache de OCR, en las paginas de esa
    cedula. Asi, si mas adelante se vuelve a subir el mismo PDF, el dato ya sale
    corregido y no hay que repetir el trabajo manual.

    Contrapartida asumida a proposito: el cache deja de ser un espejo exacto de lo que
    dice el PDF. Si la correccion estuvo mal, el error se arrastra a las corridas
    siguientes hasta que se vuelva a corregir (o se cambie el dpi/filtro, que es otra
    clave de cache y fuerza un escaneo limpio).

    No se sincroniza el texto crudo (Texto_Completo): ese sigue siendo lo que leyo el
    OCR, que es justamente lo que sirve para auditar de donde salio el dato.
    """
    ctx = task_reconcile_context.get(task_id) or {}
    clave = ctx.get("cache_ocr")
    if not clave:
        return

    for pagina in entry.get("pages") or []:
        try:
            data = db.get_cached_pages(
                clave["pdf_hash"], clave["dpi"], clave["img_filter"], pagina, pagina
            ).get(pagina)
            if not data:
                continue

            page_data = data.setdefault("page_data", {})
            datos = data.setdefault("datos", {})
            for campo, columna in _CAMPOS_LIVE_A_PAGE_DATA.items():
                valor = entry.get(campo)
                page_data[columna] = "" if valor is None else str(valor).strip()
                datos[_CAMPOS_LIVE_A_DATOS[campo]] = valor
            for campo, clave_detectado in _CAMPOS_LIVE_A_DETECTADO.items():
                data[clave_detectado] = entry.get(campo)

            db.save_cached_page(clave["pdf_hash"], clave["dpi"], clave["img_filter"], pagina, data)
        except Exception as err:
            print(f"Advertencia: no se pudo guardar la corrección de la página {pagina} en el cache: {err}")


@app.put("/api/task/{task_id}/records/{document}")
async def edit_record(task_id: str, document: str, edits: dict = Body(...)):
    """
    Corrige uno o mas campos de una cedula ya procesada y vuelve a conciliar contra el
    Excel de una vez -- puede mover el registro entre categorias y el reporte
    descargable ya sale con el dato corregido. Solo funciona mientras la auditoria
    sigue "caliente" en este proceso del servidor (ver nota junto a task_reconcile_context).
    """
    if task_id not in tasks_db or task_id not in task_reconcile_context:
        raise HTTPException(
            status_code=404,
            detail="Esta auditoría no está disponible para edición en esta sesión (probablemente el servidor se reinició desde que se corrió)."
        )

    live_results = tasks_db[task_id].get("live_results", [])
    entry = next((lr for lr in live_results if lr["document"] == document), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="No se encontró ese documento en los resultados de esta auditoría.")

    campos_editables = {
        "document", "name", "date", "tipo_documento", "lugar_nacimiento", "sexo",
        "estatura", "grupo_sanguineo", "fecha_lugar_expedicion"
    }
    nuevo_documento = edits.get("document")
    for campo, valor in edits.items():
        if campo in campos_editables:
            entry[campo] = valor

    if "date" in edits:
        entry["edad"] = calcular_edad(entry.get("date"))

    # Si la correccion del documento hace que ahora coincida con OTRA tarjeta ya
    # existente (tipico cuando el anverso y el reverso de la misma cedula quedaron
    # separados por un digito mal leido), se fusionan en una sola en vez de dejar
    # un duplicado con el mismo numero de documento.
    if nuevo_documento and nuevo_documento != document:
        duplicado = next((lr for lr in live_results if lr is not entry and lr["document"] == nuevo_documento), None)
        if duplicado is not None:
            _fusionar_entradas_live_results(duplicado, entry)
            live_results.remove(entry)
            entry = duplicado

    # La correccion tambien va al cache de OCR de este PDF (decision del usuario: que el
    # cache devuelva el dato ya corregido y no haya que volver a arreglarlo a mano).
    _guardar_correccion_en_cache(task_id, entry)

    ctx = task_reconcile_context[task_id]
    df_ocr = _construir_df_ocr_desde_live_results(live_results)

    run_meta = {
        **ctx["run_meta_base"],
        "similarity_threshold": ctx["similarity_threshold"],
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "elapsed_seconds": tasks_db[task_id].get("elapsed_seconds"),
    }

    try:
        (lista_coinciden, lista_anomalias, lista_solo_pdf, lista_solo_excel,
         metrics, formatted_report_bytes) = reconciliar_y_generar_reporte(
            df_ocr, ctx["df_excel_clean"], ctx["key_col"], ctx["compare_cols"],
            ctx["similarity_threshold"], run_meta
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al reconciliar con los datos corregidos: {str(e)}")

    generated_reports[task_id] = formatted_report_bytes
    results_payload = {
        "coinciden": lista_coinciden,
        "anomalias": lista_anomalias,
        "solo_excel": lista_solo_excel,
        "solo_pdf": lista_solo_pdf
    }
    _propagar_sugerencias(live_results, lista_anomalias, lista_solo_pdf)

    tasks_db[task_id].update({"metrics": metrics, "results": results_payload})

    try:
        db.save_completed_audit(
            task_id=task_id,
            excel_filename=ctx["run_meta_base"]["excel_filename"],
            pdf_filename=ctx["run_meta_base"]["pdf_filename"],
            key_col=ctx["key_col"],
            compare_cols=ctx["compare_cols"],
            similarity_threshold=ctx["similarity_threshold"],
            start_page=ctx["run_meta_base"]["start_page"],
            end_page=ctx["run_meta_base"]["end_page"],
            metrics=metrics,
            results=results_payload,
            live_results=live_results,
            report_bytes=formatted_report_bytes,
            pdf_url=tasks_db[task_id].get("pdf_url"),
            elapsed_seconds=tasks_db[task_id].get("elapsed_seconds"),
            scanned_pages=tasks_db[task_id].get("scanned_pages"),
            pdf_hash=ctx["cache_ocr"]["pdf_hash"],
            pdf_dpi=ctx["cache_ocr"]["dpi"],
            img_filter=ctx["cache_ocr"]["img_filter"],
        )
    except Exception as db_err:
        print(f"Advertencia: no se pudo actualizar la auditoría {task_id} en el historial: {db_err}")

    return {
        "status": "completed",
        "progress": 100,
        "metrics": metrics,
        "results": results_payload,
        "live_results": live_results,
        "elapsed_seconds": tasks_db[task_id].get("elapsed_seconds"),
        "pdf_url": tasks_db[task_id].get("pdf_url"),
    }

@app.get("/api/task/{task_id}/excel-records")
async def get_excel_records(task_id: str):
    """Lista las personas del Excel cargado (nombre + documento + fila completa)."""
    if task_id not in task_reconcile_context:
        raise HTTPException(
            status_code=404,
            detail="El Excel de esta auditoría no está disponible en esta sesión (probablemente el servidor se reinició desde que se corrió)."
        )
    df = task_reconcile_context[task_id]["df_excel_clean"]
    registros = []
    for _, row in df.iterrows():
        registros.append({
            "documento": str(row["Identificación_Limpia"]).strip(),
            "nombre": str(row["Nombre_Base"]).strip(),
            "columnas": {
                col: (None if pd.isna(row[col]) else row[col])
                for col in df.columns
                if col not in ("Identificación_Limpia", "Nombre_Base")
            },
        })
    return {"records": registros}

# Serve Frontend static files
_static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(_static_dir):
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


def _encontrar_puerto_libre(preferido=8000, intentos=15):
    """
    Prueba el puerto preferido y, si esta ocupado (otra app en la maquina del usuario,
    o incluso otra instancia de esta misma app que quedo colgada), prueba los
    siguientes hasta encontrar uno libre. Sin esto, un puerto ocupado le impediria a
    alguien sin conocimientos tecnicos abrir la app con un simple doble clic -- tendria
    que enterarse de que existe un problema de puerto y saber corregirlo a mano.
    """
    import socket
    for offset in range(intentos):
        puerto = preferido + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.bind(("127.0.0.1", puerto))
                return puerto
            except OSError:
                continue
    return preferido  # ninguno quedo libre; se intenta con el preferido de todas formas


def _abrir_navegador_cuando_este_listo(url, intentos=40, espera_seg=0.5):
    """
    Espera a que el servidor responda antes de abrir el navegador -- para el .exe
    empaquetado (pensado para alguien sin conocimientos tecnicos), asi la app se
    siente como una aplicacion de escritorio normal: doble clic y se abre solo, en
    vez de tener que copiar una URL a mano. Reintenta en vez de esperar un tiempo fijo
    porque la primera vez que arranca puede tardar mas (descarga de modelos de OCR).
    """
    import urllib.request
    for _ in range(intentos):
        try:
            urllib.request.urlopen(url, timeout=1)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(espera_seg)
    # Si nunca respondio, igual se intenta abrir -- el usuario vera el error de
    # conexion en el navegador en vez de que la app se quede sin hacer nada visible.
    webbrowser.open(url)


if __name__ == "__main__":
    import uvicorn

    # sys.frozen = True cuando esto corre como el .exe empaquetado (PyInstaller) --
    # ahi no tiene sentido --reload (no hay archivos fuente que vigilar dentro del
    # ejecutable, y ademas falla al no poder re-importar "server:app" por nombre) y
    # se abre el navegador solo, como una app de escritorio. En modo desarrollo
    # (python server.py directo) se mantiene el flujo de siempre.
    empaquetado = getattr(sys, "frozen", False)
    puerto_deseado = int(os.environ.get("PORT", 8000))

    if empaquetado:
        # Si PORT vino explicito, se respeta tal cual (alguien lo puso a proposito).
        # Si no, se busca automaticamente uno libre a partir de 8000 -- asi un puerto
        # ocupado en la maquina del usuario nunca le rompe el doble-clic-y-listo.
        puerto = puerto_deseado if "PORT" in os.environ else _encontrar_puerto_libre(puerto_deseado)
        url = f"http://127.0.0.1:{puerto}"
        threading.Thread(target=_abrir_navegador_cuando_este_listo, args=(url,), daemon=True).start()
        uvicorn.run(app, host="127.0.0.1", port=puerto, reload=False)
    else:
        puerto = puerto_deseado
        url = f"http://127.0.0.1:{puerto}"
        uvicorn.run("server:app", host="127.0.0.1", port=puerto, reload=True)
