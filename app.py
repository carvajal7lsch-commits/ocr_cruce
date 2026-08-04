import streamlit as st
import pandas as pd
import io
import re
import os
import cv2
import numpy as np
from PIL import Image

# Import standard library checks and helper modules from src
from src.ocr import (
    preprocesar_imagen, 
    extraer_texto_con_enrutamiento, 
    pdf2image_available, 
    pytesseract_available, 
    pymupdf_available
)
from src.parser import (
    extraer_datos_texto,
    detectar_cara_cedula,
    PATRON_DOC_BARCODE,
    PATRON_DOC_GENERICO,
    PATRON_FECHA_NAC
)
from src.reporter import generate_excel_report

try:
    from rapidfuzz import fuzz
    rapidfuzz_available = True
except ImportError:
    rapidfuzz_available = False

try:
    from pdf2image import convert_from_bytes
except ImportError:
    pass

try:
    import fitz
except ImportError:
    fitz = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

# Configure Streamlit page
st.set_page_config(
    page_title="Auditoría de Cédulas OCR & Excel",
    page_icon="🆔",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom premium styling
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&display=swap');
    html, body, .stApp, p, span, label, li, ul, div {
        font-family: 'Inter', sans-serif;
    }
    
    h1, h2, h3, .main-header {
        font-family: 'Outfit', sans-serif;
    }
    
    /* Modern Glassmorphic Metric Cards */
    .metric-card {
        background: rgba(255, 255, 255, 0.85);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        border-radius: 16px;
        padding: 22px 24px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.02);
        border: 1px solid rgba(229, 231, 235, 0.6);
        border-left: 6px solid #4f46e5;
        margin-bottom: 15px;
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .metric-card:hover {
        transform: translateY(-4px);
        box-shadow: 0 12px 25px rgba(0, 0, 0, 0.06);
        border-color: rgba(79, 70, 229, 0.2);
    }
    .metric-title {
        font-size: 13px;
        color: #4b5563;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 6px;
    }
    .metric-value {
        font-size: 32px;
        color: #111827;
        font-weight: 800;
        letter-spacing: -0.02em;
    }
    .main-header {
        font-size: 2.6rem;
        font-weight: 800;
        background: linear-gradient(135deg, #4f46e5 0%, #06b6d4 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        letter-spacing: -0.03em;
        display: inline-block;
    }
    
    /* Premium Streamlit Button */
    div.stButton > button:first-child {
        background: linear-gradient(135deg, #4f46e5 0%, #3b82f6 100%);
        color: white;
        font-weight: 600;
        font-size: 16px;
        letter-spacing: -0.01em;
        padding: 14px 28px;
        border-radius: 12px;
        border: none;
        box-shadow: 0 4px 14px rgba(79, 70, 229, 0.3);
        transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        width: 100%;
        margin-top: 10px;
    }
    div.stButton > button:first-child:hover {
        transform: translateY(-2px);
        box-shadow: 0 8px 22px rgba(79, 70, 229, 0.45);
        background: linear-gradient(135deg, #4338ca 0%, #2563eb 100%);
    }
    div.stButton > button:first-child:active {
        transform: translateY(0);
    }
</style>
""", unsafe_allow_html=True)

# Main Title
st.markdown("""
<div style='display: flex; align-items: center; gap: 12px; margin-bottom: 10px;'>
    <span style='font-size: 2.6rem;'>🆔</span>
    <span class='main-header'>Auditoría de Cédulas por OCR & Excel</span>
</div>
""", unsafe_allow_html=True)




# Definición de rutas por defecto de forma interna
tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
poppler_dir = r"C:\poppler-26.02.0\Library\bin"

# Sidebar Navigation
with st.sidebar:
    st.image("https://cdn-icons-png.flaticon.com/512/732/732220.png", width=70)
    st.markdown("### 🎛️ Modo de Trabajo")
    app_mode = st.radio(
        "Selecciona el módulo:",
        options=["📊 Cruce Masivo OCR vs Excel", "🔬 Laboratorio de Prueba OCR (Entrenamiento)"],
        index=0
    )
    
    if not pymupdf_available:
        st.warning("⚠️ PyMuPDF (fitz) no está detectado en el proceso actual de Python. Por favor, cancela la terminal de Streamlit (Ctrl+C) y vuelve a iniciarla (`streamlit run app.py`) para cargar la librería recién instalada.")

    st.markdown("---")
    st.markdown("### 📸 Calidad de Imagen (DPI)")
    pdf_dpi = st.slider(
        "Resolución DPI (Más alto = Mejor OCR, Más lento):",
        min_value=100,
        max_value=400,
        value=150,
        step=50,
        help="150 DPI es el estándar recomendado para leer cédulas sin errores tipográficos."
    )
    
    st.markdown("---")
    st.markdown("### 🖼️ Preprocesamiento de Imagen")
    img_filter = st.selectbox(
        "Filtro de imagen a aplicar:",
        options=["Ninguno (Imagen Original)", "Solo Escala de Grises", "Binarización Blanco y Negro (Otsu)"],
        index=1,
        help="Elige 'Ninguno' para enviar la imagen original a color al OCR. A veces la binarización rompe letras delgadas."
    )

# ==========================================
# MODULE 1: CRUCE MASIVO
# ==========================================
if app_mode == "📊 Cruce Masivo OCR vs Excel":
    st.markdown("<p style='color: #4b5563; font-size: 1.1rem;'>Digitaliza y audita imágenes de documentos de identidad en lote comparándolas con tu base de datos de Excel.</p>", unsafe_allow_html=True)
    
    # Carga de archivos en la pantalla principal (2 columnas) con diseño premium
    st.markdown("<div style='background-color: rgba(255,255,255,0.6); padding: 20px; border-radius: 12px; border: 1px dashed rgba(79, 70, 229, 0.25); margin-bottom: 20px;'>", unsafe_allow_html=True)
    col_up1, col_up2 = st.columns(2)
    with col_up1:
        excel_file = st.file_uploader("📊 Base de Datos Excel (.xlsx, .xls)", type=["xlsx", "xls"], key="excel_main", help="Sube tu archivo de Excel con los datos de control.")
    with col_up2:
        pdf_file = st.file_uploader("📄 Archivo PDF con Cédulas (.pdf)", type=["pdf"], key="pdf_main", help="Sube el archivo PDF que contiene los documentos escaneados.")
    st.markdown("</div>", unsafe_allow_html=True)

    if not excel_file or not pdf_file:
        st.info("👋 Por favor, carga el archivo Excel y el PDF con las cédulas arriba para comenzar el análisis.")
    else:
        # Read Excel Columns
        try:
            df_raw = pd.read_excel(excel_file, header=None)
            
            # --- AUTO-DETECCIÓN INTELIGENTE DE FILA DE ENCABEZADOS (Búsqueda por palabras completas) ---
            import re
            pattern_doc_col = re.compile(r'\b(doc|documento|cédula|cedula|cc|id|identificación|identificacion|número|numero|nro)\b', re.IGNORECASE)
            pattern_name_col = re.compile(r'\b(nom|nombre|nombres|empleado|persona|cliente|usuario|apellidos?)\b', re.IGNORECASE)
            
            detected_row_idx = 0
            found_header = False
            
            # Buscar una fila que contenga tanto un indicio de Documento como de Nombre (Coincidencia Perfecta de fila)
            for i in range(min(15, len(df_raw))):
                row_vals = [str(val).strip() for val in df_raw.iloc[i].dropna().tolist()]
                has_doc = any(pattern_doc_col.search(val) for val in row_vals)
                has_name = any(pattern_name_col.search(val) for val in row_vals)
                if has_doc and has_name:
                    detected_row_idx = i
                    found_header = True
                    break
                    
            # Si no se encuentra, buscar una fila que tenga al menos uno de los dos
            if not found_header:
                for i in range(min(15, len(df_raw))):
                    row_vals = [str(val).strip() for val in df_raw.iloc[i].dropna().tolist()]
                    if any(pattern_doc_col.search(val) or pattern_name_col.search(val) for val in row_vals):
                        detected_row_idx = i
                        break
            
            # Generar opciones para mostrar las primeras filas
            row_options = []
            max_rows_to_preview = min(15, len(df_raw))
            for i in range(max_rows_to_preview):
                row_preview = [str(x) for x in df_raw.iloc[i].dropna().tolist()[:4]]
                preview_str = f"Fila {i+1}: " + " | ".join(row_preview)
                if len(df_raw.iloc[i].dropna()) > 4:
                    preview_str += " ..."
                row_options.append(preview_str)
                
            # Mostramos un banner de la fila detectada automáticamente
            st.info(f"📍 **Fila de encabezado detectada automáticamente:** `Fila {detected_row_idx + 1}`")
            
            # Expander para los ajustes avanzados de fila y columnas
            with st.expander("⚙️ Ajustes Avanzados de Fila y Columnas (Opcional)"):
                selected_row_idx = st.selectbox(
                    "¿En qué fila están tus columnas (encabezado)?",
                    options=range(max_rows_to_preview),
                    index=detected_row_idx,
                    format_func=lambda x: row_options[x]
                )
                
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
                excel_cols = [col for col in df_excel.columns if not str(col).startswith("Columna_") and not str(col).startswith("Unnamed:")]
                
                # Auto-detección dinámica para pre-seleccionar en la UI
                dyn_key_col = None
                dyn_compare_cols = []
                for col in excel_cols:
                    if pattern_doc_col.search(str(col)):
                        dyn_key_col = col
                        break
                if not dyn_key_col and excel_cols:
                    dyn_key_col = excel_cols[0]
                    
                for col in excel_cols:
                    if col == dyn_key_col:
                        continue
                    if pattern_name_col.search(str(col)):
                        dyn_compare_cols.append(col)
                if not dyn_compare_cols and len(excel_cols) > 1:
                    for col in excel_cols:
                        if col != dyn_key_col:
                            dyn_compare_cols.append(col)
                            break
                if not dyn_compare_cols and excel_cols:
                    dyn_compare_cols = [excel_cols[0]]
                
                st.markdown("---")
                col_setup1, col_setup2 = st.columns(2)
                with col_setup1:
                    key_col = st.selectbox(
                        "Columna de Documento (Excel):",
                        options=excel_cols,
                        index=excel_cols.index(dyn_key_col) if dyn_key_col in excel_cols else 0
                    )
                with col_setup2:
                    sugeridas_validas = [c for c in dyn_compare_cols if c in excel_cols and c != key_col]
                    compare_cols = st.multiselect(
                        "Columnas a verificar (Fuzzy Match / Se concatenarán en orden):",
                        options=[c for c in excel_cols if c != key_col],
                        default=sugeridas_validas if sugeridas_validas else None
                    )
            
            # Mostrar banner informativo de columnas activas afuera del expander
            st.success(f"🔍 **Columnas activas:** Fila de encabezado: `{selected_row_idx + 1}` | **Documento** ➔ `{key_col}` | **Nombre(s)/Apellido(s)** ➔ `{', '.join(compare_cols)}`")
            
            st.markdown("### 👁️ Vista Previa del Excel Cargado")
            st.dataframe(df_excel.head(5), use_container_width=True)
            
        except Exception as e:
            st.error(f"Error al leer el archivo de Excel: {e}")
            st.stop()
            
        st.markdown("### ⚙️ Configuración del Cruce y Validación")
        similarity_threshold = st.slider(
            "Umbral de Similitud Mínimo (%):",
            min_value=40,
            max_value=100,
            value=85
        )
        
        try:
            from pdf2image import pdfinfo_from_bytes
            poppler_path = poppler_dir.strip().strip('"').strip("'") if poppler_dir.strip() else None
            pdf_info = pdfinfo_from_bytes(pdf_file.getvalue(), poppler_path=poppler_path)
            total_pages = pdf_info.get("Pages", 1)
        except Exception:
            total_pages = 100
        
        st.markdown(f"### 📄 Rango de Páginas a Procesar (Total: {total_pages} pág.)")
        col_range1, col_range2 = st.columns(2)
        with col_range1:
            start_page = st.number_input("Página de Inicio:", min_value=1, max_value=total_pages, value=1)
        with col_range2:
            end_page = st.number_input("Página de Fin:", min_value=1, max_value=total_pages, value=total_pages)

        # Session state initialization
        if 'audit_completed' not in st.session_state:
            st.session_state['audit_completed'] = False

        if st.button("🚀 Iniciar Validación por OCR"):
            clean_tess_path = tesseract_path.strip().strip('"').strip("'")
            if os.path.exists(clean_tess_path):
                pytesseract.pytesseract.tesseract_cmd = clean_tess_path
            else:
                st.error(f"❌ Ruta a Tesseract no válida: {clean_tess_path}")
                st.stop()
            poppler_path = poppler_dir.strip().strip('"').strip("'") if poppler_dir.strip() else None
            
            audit_results = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            pages_range = list(range(int(start_page), int(end_page) + 1))
            total_to_process = len(pages_range)
            
            for idx, page_num in enumerate(pages_range):
                status_text.text(f"Procesando página {page_num}...")
                
                try:
                    full_text, metodo_origen = extraer_texto_con_enrutamiento(
                        pdf_file.getvalue(),
                        page_num,
                        pdf_dpi=pdf_dpi,
                        poppler_path=poppler_path,
                        img_filter=img_filter
                    )
                    doc_detected, name_detected, date_detected, cara_detected = extraer_datos_texto(full_text)
                except Exception as e:
                    st.error(f"Error en página {page_num}: {e}")
                    doc_detected, name_detected, date_detected, cara_detected = None, None, None, "No detectado"
                    metodo_origen = "Fallo"
                    full_text = ""
                
                result_entry = {
                    "Página": page_num,
                    "Documento_OCR": str(doc_detected).strip().replace('.', '') if doc_detected else "",
                    "Nombre_OCR": str(name_detected).strip(),
                    "Fecha_Nacimiento_OCR": str(date_detected).strip(),
                    "Cara_OCR": cara_detected,
                    "Metodo_Extraccion": metodo_origen,
                    "Texto_Completo": full_text
                }
                audit_results.append(result_entry)
                progress_bar.progress((idx + 1) / total_to_process)
                
            status_text.text("Deduplicando y filtrando páginas...")
            
            df_ocr = pd.DataFrame(audit_results)
            
            # === DEDUPLICACIÓN INTELIGENTE ===
            total_paginas_raw = len(df_ocr)
            
            # 1. Eliminar páginas sin documento detectado (reversos sin MRZ, páginas ilegibles, etc.)
            df_ocr = df_ocr[df_ocr['Documento_OCR'].str.strip() != ''].copy()
            paginas_sin_doc = total_paginas_raw - len(df_ocr)
            
            # 2. Deduplicar: si varias páginas leen el mismo documento, quedarse con la que tiene más datos
            if not df_ocr.empty:
                # Calcular un "score de completitud" para cada fila
                df_ocr['_completitud'] = (
                    (df_ocr['Nombre_OCR'].str.len() > 3).astype(int) * 3 +
                    (df_ocr['Fecha_Nacimiento_OCR'].str.strip() != 'None').astype(int) * 2 +
                    (df_ocr['Fecha_Nacimiento_OCR'].str.strip() != '').astype(int) * 2
                )
                # Ordenar por documento y completitud descendente, quedarse con el primero
                df_ocr = df_ocr.sort_values('_completitud', ascending=False).drop_duplicates(subset='Documento_OCR', keep='first')
                df_ocr = df_ocr.drop(columns=['_completitud'])
            
            paginas_dedup = total_paginas_raw - paginas_sin_doc - len(df_ocr)
            
            st.info(
                f"📊 **Deduplicación:** {total_paginas_raw} páginas procesadas → "
                f"{paginas_sin_doc} sin documento descartadas, "
                f"{max(0, paginas_dedup)} duplicadas fusionadas → "
                f"**{len(df_ocr)} registros únicos** para cruzar."
            )
            
            status_text.text("Realizando conciliación de datos...")
            
            df_excel_clean = df_excel.copy()
            df_excel_clean['Identificación_Limpia'] = df_excel_clean[key_col].astype(str).str.extract(r'(\d+)')
            df_excel_clean['Identificación_Limpia'] = df_excel_clean['Identificación_Limpia'].fillna('')
            
            if compare_cols:
                # Reemplazar NaN o nulos por vacío y concatenar con espacio
                df_temp = df_excel_clean[compare_cols].fillna('').astype(str)
                for col in compare_cols:
                    df_temp[col] = df_temp[col].apply(lambda x: '' if str(x).lower().strip() == 'nan' else str(x).strip())
                
                df_excel_clean['Nombre_Base'] = df_temp.agg(lambda x: ' '.join([s for s in x if s.strip()]), axis=1).str.upper().str.replace(r'\s+', ' ', regex=True).str.strip()
            else:
                # Fallback por defecto a la primera columna disponible si no hay ninguna de comparación
                col_fallback = excel_cols[0] if excel_cols else df_excel_clean.columns[0]
                df_excel_clean['Nombre_Base'] = df_excel_clean[col_fallback].fillna('').astype(str).str.upper().str.strip()
            
            # --- ALGORITMO DE CONCILIACIÓN INTELIGENTE (El Excel es la fuente de verdad) ---
            lista_coinciden = []
            lista_anomalias = []
            lista_solo_excel = []
            
            # Rastrear qué páginas del PDF fueron asociadas
            paginas_pdf_emparejadas = set()
            
            # Iteramos por cada persona en el Excel
            for idx_ex, row_excel in df_excel_clean.iterrows():
                id_ex = str(row_excel['Identificación_Limpia']).strip()
                nombre_ex = str(row_excel['Nombre_Base']).strip()
                
                # 1. Buscar coincidencia exacta por Cédula/Identificación
                match_doc = None
                if id_ex:
                    match_doc = df_ocr[df_ocr['Documento_OCR'] == id_ex]
                
                if match_doc is not None and not match_doc.empty:
                    # Encontrado por identificación exacta
                    pdf_row = match_doc.iloc[0]
                    nombre_ocr = str(pdf_row['Nombre_OCR']).strip()
                    similitud = fuzz.token_sort_ratio(nombre_ex, nombre_ocr)
                    
                    registro = {
                        "Identificación_Excel": id_ex,
                        "Nombre_Excel": nombre_ex,
                        "Identificación_PDF": pdf_row['Documento_OCR'],
                        "Nombre_PDF": nombre_ocr,
                        "Similitud_Nombre_%": similitud,
                        "Página_PDF": pdf_row['Página'],
                        "Texto_Completo_PDF": pdf_row['Texto_Completo']
                    }
                    
                    paginas_pdf_emparejadas.add(pdf_row['Página'])
                    
                    if similitud >= similarity_threshold:
                        lista_coinciden.append(registro)
                    else:
                        registro["Alerta_Detalle"] = f"La cédula coincide, pero el nombre del PDF ('{nombre_ocr}') tiene baja coincidencia con el de Excel ('{nombre_ex}')."
                        lista_anomalias.append(registro)
                else:
                    # 2. Si no coincide el ID, buscar por coincidencia de Nombre (Fuzzy Match >= 80%)
                    best_match_ocr = None
                    best_score = 0
                    
                    for idx_ocr, pdf_row in df_ocr.iterrows():
                        page_num = pdf_row['Página']
                        if page_num in paginas_pdf_emparejadas:
                            continue
                        
                        nombre_ocr = str(pdf_row['Nombre_OCR']).strip()
                        similitud = fuzz.token_sort_ratio(nombre_ex, nombre_ocr)
                        
                        # Umbral del 80% para asegurar que es la misma persona con un error en el ID
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
                            "Página_PDF": pdf_row['Página'],
                            "Texto_Completo_PDF": pdf_row['Texto_Completo'],
                            "Alerta_Detalle": f"El nombre coincide ({best_score}%), pero la cédula del PDF ('{id_ocr}') difiere de la de Excel ('{id_ex}'). Posible error en número o lectura."
                        }
                        
                        paginas_pdf_emparejadas.add(pdf_row['Página'])
                        lista_anomalias.append(registro)
                    else:
                        # 3. No se encontró en el PDF
                        lista_solo_excel.append({
                            "Identificación_Excel": id_ex,
                            "Nombre_Excel": nombre_ex
                        })
                        
            # 4. Obtener páginas PDF que no pudieron ser asociadas a nadie en Excel (Huérfanas)
            lista_solo_pdf = []
            for idx_ocr, pdf_row in df_ocr.iterrows():
                if pdf_row['Página'] not in paginas_pdf_emparejadas:
                    lista_solo_pdf.append({
                        "Página_PDF": pdf_row['Página'],
                        "Identificación_PDF": pdf_row['Documento_OCR'],
                        "Nombre_PDF": pdf_row['Nombre_OCR'],
                        "Texto_Completo_PDF": pdf_row['Texto_Completo']
                    })
                    
            # Convertir a DataFrames con columnas aseguradas
            df_coinciden = pd.DataFrame(lista_coinciden)
            if df_coinciden.empty:
                df_coinciden = pd.DataFrame(columns=["Identificación_Excel", "Nombre_Excel", "Identificación_PDF", "Nombre_PDF", "Similitud_Nombre_%", "Página_PDF", "Texto_Completo_PDF"])
                
            df_anomalias = pd.DataFrame(lista_anomalias)
            if df_anomalias.empty:
                df_anomalias = pd.DataFrame(columns=["Identificación_Excel", "Nombre_Excel", "Identificación_PDF", "Nombre_PDF", "Similitud_Nombre_%", "Página_PDF", "Alerta_Detalle", "Texto_Completo_PDF"])
                
            df_solo_pdf = pd.DataFrame(lista_solo_pdf)
            if df_solo_pdf.empty:
                df_solo_pdf = pd.DataFrame(columns=["Página_PDF", "Identificación_PDF", "Nombre_PDF", "Texto_Completo_PDF"])
                
            df_solo_excel = pd.DataFrame(lista_solo_excel)
            if df_solo_excel.empty:
                df_solo_excel = pd.DataFrame(columns=["Identificación_Excel", "Nombre_Excel"])
            
            st.session_state['audit_completed'] = True
            st.session_state['coinciden'] = df_coinciden
            st.session_state['revisar_manual'] = df_anomalias
            st.session_state['solo_en_pdf'] = df_solo_pdf
            st.session_state['solo_en_excel'] = df_solo_excel
            
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df_coinciden.to_excel(writer, sheet_name='Verificados Perfectos', index=False)
                df_anomalias.to_excel(writer, sheet_name='Alertas y Anomalías', index=False)
                df_solo_pdf.to_excel(writer, sheet_name='Solo en PDF (Huérfanos)', index=False)
                df_solo_excel.to_excel(writer, sheet_name='Solo en Excel (Faltantes)', index=False)
                
            st.session_state['report_bytes'] = output.getvalue()
            status_text.text("¡Auditoría y conciliación finalizada!")

        if st.session_state['audit_completed']:
            df_coinciden = st.session_state['coinciden']
            df_revisar = st.session_state['revisar_manual']
            df_solo_pdf = st.session_state['solo_en_pdf']
            df_solo_excel = st.session_state['solo_en_excel']
            report_bytes = st.session_state['report_bytes']
            
            st.markdown("### 📊 Tablero de Control de Auditoría Conciliada")
            col_m1, col_m2, col_m3 = st.columns(3)
            with col_m1:
                st.markdown(f"<div class='metric-card'><div class='metric-title'>Coinciden Perfectamente</div><div class='metric-value'>{len(df_coinciden)}</div></div>", unsafe_allow_html=True)
            with col_m2:
                st.markdown(f"<div class='metric-card' style='border-left-color:#ef4444;'><div class='metric-title'>Alertas / Anomalías</div><div class='metric-value'>{len(df_revisar)}</div></div>", unsafe_allow_html=True)
            with col_m3:
                st.markdown(f"<div class='metric-card' style='border-left-color:#f59e0b;'><div class='metric-title'>No Encontrados en PDF</div><div class='metric-value'>{len(df_solo_excel)}</div></div>", unsafe_allow_html=True)
                
            tab1, tab2, tab3 = st.tabs([
                "✅ Verificados Perfectos", 
                "⚠️ Alertas y Anomalías",
                "🔍 No Encontrados en PDF (Faltantes)"
            ])
            with tab1: 
                st.dataframe(df_coinciden, use_container_width=True)
            with tab2: 
                if not df_revisar.empty:
                    st.warning("⚠️ Se detectaron discrepancias entre los datos del PDF y el Excel. Por favor, revísalas a continuación:")
                st.dataframe(df_revisar, use_container_width=True)
            with tab3:
                if not df_solo_excel.empty:
                    st.info("🔍 Los siguientes registros existen en el archivo de Excel de control, pero no se leyó o encontró su documento de identidad en el archivo PDF:")
                st.dataframe(df_solo_excel, use_container_width=True)
            
            st.download_button(
                label="📥 Descargar Reporte de Auditoría Conciliada (Excel)",
                data=report_bytes,
                file_name="Reporte_Auditoria_Conciliada.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

# ==========================================
# MODULE 2: LABORATORIO DE ENTRENAMIENTO OCR
# ==========================================
elif app_mode == "🔬 Laboratorio de Prueba OCR (Entrenamiento)":
    st.markdown("<p style='color: #4b5563; font-size: 1.1rem;'>Sube un documento individual para ver el procesamiento de imagen en tiempo real, analizar el texto extraído y entrenar el comportamiento de las expresiones regulares.</p>", unsafe_allow_html=True)
    
    test_file = st.file_uploader("Sube una Cédula (PDF o Imagen)", type=["pdf", "png", "jpg", "jpeg", "webp"])

    if test_file:
        # Check type
        is_pdf = test_file.name.lower().endswith(".pdf")
        
        # Load Page/Image
        imagen_a_procesar = None
        extracted_text_from_pdf = None
        
        if is_pdf:
            try:
                poppler_path = poppler_dir.strip().strip('"').strip("'") if poppler_dir.strip() else None
                # Determinar número de páginas
                if pymupdf_available:
                    with fitz.open(stream=test_file.getvalue(), filetype="pdf") as doc:
                        num_pages = len(doc)
                else:
                    num_pages = 1
                
                page_sel = 1
                if num_pages > 1:
                    page_sel = st.number_input("Selecciona la página a analizar:", min_value=1, max_value=num_pages, value=1)
                
                # Check for text layer if not forcing OCR
                forzar_ocr = st.checkbox("Forzar OCR (Ignorar capa de texto del PDF)", value=False)
                
                if not forzar_ocr and pymupdf_available:
                    try:
                        with fitz.open(stream=test_file.getvalue(), filetype="pdf") as doc:
                            if 0 <= page_sel - 1 < len(doc):
                                page = doc[page_sel - 1]
                                text_extracted = page.get_text().strip()
                                
                                # Debug expander for the user to see what PyMuPDF extracts
                                with st.expander("🛠️ Diagnóstico de Capa de Texto del PDF (PyMuPDF)"):
                                    st.write(f"**Total caracteres extraídos ( get_text() ):** {len(text_extracted)}")
                                    
                                    # Probar extracción de anotaciones
                                    annots_text = []
                                    try:
                                        for annot in page.annots():
                                            content = annot.info.get("content", "").strip()
                                            if content:
                                                annots_text.append(content)
                                    except Exception:
                                        pass
                                    st.write(f"**Texto en Anotaciones:** {annots_text if annots_text else 'Ninguno'}")
                                    
                                    # Probar extracción de formularios/widgets
                                    widgets_text = []
                                    try:
                                        for widget in page.widgets():
                                            val = widget.field_value.strip() if widget.field_value else ""
                                            if val:
                                                widgets_text.append(val)
                                    except Exception:
                                        pass
                                    st.write(f"**Texto en Formularios/Campos:** {widgets_text if widgets_text else 'Ninguno'}")
                                    
                                    # Probar bloques
                                    blocks = page.get_text("blocks")
                                    st.write(f"**Bloques de texto detectados:** {len(blocks)}")
                                    
                                    # Mostrar XML (primeros 500 carac.)
                                    xml_text = page.get_text("xml")
                                    st.write(f"**Estructura XML (vista previa):**")
                                    st.code(xml_text[:500] if xml_text else "[Sin XML]")
                                    
                                    st.write(f"**Fuentes embebidas en esta página:**", page.get_fonts())
                                    st.write("**Texto crudo extraído:**")
                                    st.code(text_extracted if text_extracted else "[Capa de texto vacía]")
                                
                                if len(text_extracted) > 15 and any(c.isalnum() for c in text_extracted):
                                    extracted_text_from_pdf = text_extracted
                                else:
                                    st.info(f"ℹ️ La capa de texto del PDF en esta página solo tiene {len(text_extracted)} caracteres (insuficiente), por lo que se usará OCR.")
                    except Exception as e:
                        st.warning(f"⚠️ Error al leer capa de texto embebido: {e}")
                
                # We always need the image to display it in the UI, so render it anyway
                with st.spinner("Renderizando página del PDF..."):
                    pdf_imgs = convert_from_bytes(test_file.getvalue(), dpi=pdf_dpi, first_page=page_sel, last_page=page_sel, poppler_path=poppler_path)
                    imagen_a_procesar = pdf_imgs[0]
            except Exception as e:
                st.error(f"Error al procesar el PDF: {e}")
                st.stop()
        else:
            try:
                imagen_a_procesar = Image.open(test_file)
            except Exception as e:
                st.error(f"Error al abrir la imagen: {e}")
                st.stop()

        if imagen_a_procesar:
            # 1. Processing OpenCV dynamically using sidebar filter
            try:
                img_procesada = preprocesar_imagen(imagen_a_procesar, metodo=img_filter)
            except Exception as e:
                st.warning(f"No se pudo aplicar preprocesamiento OpenCV: {e}")
                img_procesada = imagen_a_procesar

            # 2. Columns layout
            col_img, col_ocr = st.columns([1, 1])
            
            # Execute OCR and bounding boxes detection or display embedded text
            clean_tess_path = tesseract_path.strip().strip('"').strip("'")
            raw_ocr_text = ""
            annotated_image = None
            
            if extracted_text_from_pdf is not None:
                # We extracted text directly from the PDF
                raw_ocr_text = extracted_text_from_pdf
                cara_detected = detectar_cara_cedula(raw_ocr_text)
                raw_ocr_text = f"--- DETECTADO: {cara_detected.upper()} (Capa de Texto Embebida) ---\n\n" + raw_ocr_text
                st.success("ℹ️ Texto extraído directamente de la capa digital del PDF (sin ejecutar OCR).")
            else:
                if os.path.exists(clean_tess_path):
                    pytesseract.pytesseract.tesseract_cmd = clean_tess_path
                    
                    try:
                        from pytesseract import Output
                        with st.spinner("Ejecutando OCR y calculando coordenadas de palabras..."):
                            # Get raw text
                            raw_ocr_text = pytesseract.image_to_string(img_procesada, lang='spa')
                            
                            # Detect side and prepend it to the text area
                            cara_detected = detectar_cara_cedula(raw_ocr_text)
                            raw_ocr_text = f"--- DETECTADO: {cara_detected.upper()} (OCR) ---\n\n" + raw_ocr_text
                            
                            # Get word coordinates
                            d = pytesseract.image_to_data(img_procesada, output_type=Output.DICT, lang='spa')
                            
                            # Draw bounding boxes on a copy
                            img_np = np.array(img_procesada)
                            # Ensure RGB format for colorful drawings
                            if len(img_np.shape) == 2:
                                img_np = cv2.cvtColor(img_np, cv2.COLOR_GRAY2RGB)
                            elif img_np.shape[2] == 4:
                                img_np = cv2.cvtColor(img_np, cv2.COLOR_RGBA2RGB)
                            else:
                                img_np = img_np.copy()
                                
                            n_boxes = len(d['level'])
                            for i in range(n_boxes):
                                # Filter high-confidence words (conf > 15) and non-empty texts
                                if int(d['conf'][i]) > 15 and d['text'][i].strip():
                                    (x, y, w, h) = (d['left'][i], d['top'][i], d['width'][i], d['height'][i])
                                    # Green bounding box
                                    cv2.rectangle(img_np, (x, y), (x + w, y + h), (0, 255, 0), 2)
                                    # Red text label
                                    text_clean = re.sub(r'[^A-Za-z0-9ÁÉÍÓÚñÑ]', '', d['text'][i])
                                    cv2.putText(img_np, text_clean, (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (239, 68, 68), 1, cv2.LINE_AA)
                                    
                            annotated_image = Image.fromarray(img_np)
                    except Exception as e:
                        st.error(f"Error al ejecutar Tesseract: {e}")
                        raw_ocr_text = ""
                else:
                    st.error("❌ Indica la ruta correcta de Tesseract en la barra lateral para ejecutar el OCR.")
            
            with col_img:
                st.markdown("### 👁️ Vista de Imagen Analizada")
                show_boxes = st.checkbox("🔍 Mostrar Cajas de Texto Detectadas (OCR Debug)", value=True)
                
                if show_boxes and annotated_image is not None:
                    st.image(annotated_image, caption=f"Localización de Palabras en la Cédula (Cajas: Verdes | Lecturas: Rojas)", use_container_width=True)
                else:
                    st.image(img_procesada, caption=f"Imagen Procesada ({img_filter})", use_container_width=True)
            
            with col_ocr:
                st.markdown("### 📝 Texto Completo Extraído por OCR (Editable)")
                st.info("💡 Puedes seleccionar, copiar o modificar este texto para ver cómo reaccionan las expresiones regulares de extracción de abajo.")

                # Interactive Text Area so user can select/edit
                edited_text = st.text_area(
                    "Texto OCR:",
                    value=raw_ocr_text,
                    height=300,
                    help="Este es el texto devuelto por Tesseract. Resalta y selecciona lo que necesites."
                )
                
            # 3. Dynamic Parser results
            st.markdown("---")
            st.markdown("### 🔍 Resultados del Extractor de Datos (Regex)")
            
            if edited_text.strip():
                doc_t, name_t, date_t, cara_t = extraer_datos_texto(edited_text)
                
                col_res1, col_res2, col_res3, col_res4 = st.columns(4)
                with col_res1:
                    st.text_input("💳 Documento Extraído (Barcode/Fallback):", value=doc_t if doc_t else "No detectado", disabled=True)
                with col_res2:
                    st.text_input("👤 Nombres Extraídos (Heurística):", value=name_t if name_t else "No detectado", disabled=True)
                with col_res3:
                    st.text_input("📅 Fecha de Nacimiento:", value=date_t if date_t else "No detectado", disabled=True)
                with col_res4:
                    st.text_input("🔍 Cara Detectada (Anverso/Reverso):", value=cara_t if cara_t else "No detectado", disabled=True)
                
                # Expose matches debug
                with st.expander("🔬 Ver coincidencias técnicas detalladas (Regex Debug)"):
                    st.markdown("**1. Código de Barras (Patrón):**")
                    barcode_matches = list(PATRON_DOC_BARCODE.finditer(edited_text))
                    if barcode_matches:
                        for m in barcode_matches:
                            st.code(f"Match: {m.group(0)} -> Grupo 1 (Cédula): {m.group(1)}")
                    else:
                        st.write("No se encontraron coincidencias de formato código de barras.")
                        
                    st.markdown("**2. Patrón Numérico Genérico:**")
                    gen_match = PATRON_DOC_GENERICO.search(edited_text)
                    if gen_match:
                        st.code(f"Match: {gen_match.group(0)}")
                    else:
                        st.write("No se encontraron números con formato de cédula genérico.")
                        
                    st.markdown("**3. Patrón de Fecha de Nacimiento:**")
                    f_nac = PATRON_FECHA_NAC.search(edited_text)
                    if f_nac:
                        st.code(f"Match: {f_nac.group(0)} -> Grupo 1: {f_nac.group(1)}")
                    else:
                        st.write("No se encontró fecha de nacimiento explícita.")
            else:
                st.warning("No hay texto para analizar. Asegúrate de cargar un archivo y configurar correctamente Tesseract.")
