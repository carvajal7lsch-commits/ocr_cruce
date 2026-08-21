import io
import cv2
import numpy as np
from PIL import Image

try:
    from pdf2image import convert_from_bytes
    pdf2image_available = True
except ImportError:
    pdf2image_available = False

# Parametros del motor RapidOCR, centralizados aqui para poder ajustarlos durante pruebas
# sin tener que rastrear donde se instancia el engine.
#
# intra_op_num_threads=2: el server corre varias paginas en paralelo
# (ThreadPoolExecutor). El default de RapidOCR (-1) hace que CADA inferencia use TODOS
# los cores, asi que con varios hilos simultaneos se pisan entre si. Pero el otro
# extremo -- fijarlo en 1, como estaba antes -- deja cada inferencia atada a un solo
# core y resulto ser el cuello de botella real del pipeline. Medido sobre 6 paginas
# reales (maquina de 8 cores, 3 repeticiones por config):
#     workers=4 x intra=1 (lo anterior) -> 2.77s por pagina
#     workers=8 x intra=2 (lo actual)   -> 1.95s por pagina
# Se comparo campo por campo (8 campos x 10 paginas) contra la config anterior: la
# salida del OCR es IDENTICA, solo cambia el reparto de CPU.
#
# (Se probo tambien bajar max_side_len a 1600, que era aun mas rapido (~2.8s -> 2.2s),
# pero degrada de verdad: partia nombres ("VALENCIA VILLEGAS ANTONIO" -> "VALENCIA
# VILLEGAS NOV"), perdia un lugar de nacimiento y un grupo sanguineo. Descartado.
# Desactivar use_cls solo daba ~2% y deja sin arreglar los escaneos rotados: tampoco.)
#
# (Se probo tambien bajar det_box_thresh/text_score y subir det_unclip_ratio para
# favorecer texto pequeño -- comparado linea por linea contra cedulas reales no dio
# mejora medible, solo mas ruido en zonas ya dificiles. Se dejan en valores de fabrica.)
OCR_INTRA_OP_THREADS = 2
OCR_ENGINE_PARAMS_NEW_PKG = {
    "EngineConfig.onnxruntime.intra_op_num_threads": OCR_INTRA_OP_THREADS,
    "EngineConfig.onnxruntime.inter_op_num_threads": 1,
}
OCR_ENGINE_PARAMS_OLD_PKG = dict(
    intra_op_num_threads=OCR_INTRA_OP_THREADS,
    inter_op_num_threads=1,
)

# El paquete "rapidocr" (PyPI, v3+) reemplazo a "rapidocr-onnxruntime" (v1.x, que
# es el que trae fijo el modelo chino/ingles PP-OCRv4). El paquete nuevo por defecto
# usa PP-OCRv6, un modelo de reconocimiento multi-idioma que SI declara soporte de
# español -- se probo contra cedulas colombianas reales y, comparado con el modelo
# viejo, resultó ~35-40% más rápido y (mas importante) preserva los espacios entre
# palabras ("FECHA DE NACIMIENTO" en vez de "FECHADENACIMIENTO"). Eso es relevante
# porque src/parser.py busca ese literal CON espacios para extraer la fecha de
# nacimiento -- con el modelo viejo fusionando palabras, esa extraccion fallaba
# silenciosamente en varios documentos. Por eso "rapidocr" es ahora el motor
# primario; "rapidocr_onnxruntime" queda solo como fallback si no esta instalado.
_using_new_rapidocr_pkg = False
try:
    from rapidocr import RapidOCR
    rapidocr_engine = RapidOCR(params=OCR_ENGINE_PARAMS_NEW_PKG)
    rapidocr_available = True
    _using_new_rapidocr_pkg = True
except ImportError:
    try:
        from rapidocr_onnxruntime import RapidOCR
        rapidocr_engine = RapidOCR(**OCR_ENGINE_PARAMS_OLD_PKG)
        rapidocr_available = True
    except ImportError:
        rapidocr_available = False
        rapidocr_engine = None


def _ejecutar_rapidocr(img_array):
    """
    Corre el motor RapidOCR que haya quedado disponible y normaliza el resultado
    a una lista de [caja, texto, score] -- igual sin importar si quedo activo el
    paquete nuevo "rapidocr" (devuelve un objeto RapidOCROutput) o el viejo
    "rapidocr_onnxruntime" (devuelve una tupla (lista, elapse)). Asi el resto del
    codigo (incluyendo el dibujo de cajas en server.py) no necesita saber cual es.
    """
    raw = rapidocr_engine(img_array)
    if raw is None:
        return []

    if _using_new_rapidocr_pkg:
        if not raw.txts:
            return []
        return [
            [box, txt, score]
            for box, txt, score in zip(raw.boxes, raw.txts, raw.scores)
        ]

    result, _elapse = raw
    return result or []

try:
    import fitz
    pymupdf_available = True
except ImportError:
    pymupdf_available = False

def preprocesar_imagen(imagen_pil, metodo="Ninguno"):
    """Aplica el método de preprocesamiento seleccionado usando OpenCV."""
    if metodo == "Ninguno (Imagen Original)":
        return imagen_pil
        
    open_cv_image = np.array(imagen_pil)
    # La imagen puede venir ya en escala de grises (poppler la renderiza asi por
    # velocidad, ver server.py) -- en ese caso el array es 2D y no hay nada que
    # convertir. Si viene a color, se convierte PIL RGB -> OpenCV BGR -> gris.
    if len(open_cv_image.shape) == 2:
        gray = open_cv_image
    else:
        bgr_image = open_cv_image[:, :, ::-1].copy()
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)

    if metodo == "Solo Escala de Grises":
        # 1. Ecualización de contraste local muy suave (CLAHE) para definir las letras sin quemar los bordes
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        enhanced_gray = clahe.apply(gray)
        return Image.fromarray(enhanced_gray)
        
    elif metodo == "Binarización Blanco y Negro (Otsu)":
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return Image.fromarray(thresh)
        
    elif metodo == "Binarización Adaptativa (Sombras/Reflejos)":
        # Aplica desenfoque gaussiano para reducir ruido y umbral adaptativo local (ideal para fotos con flash o sombras)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        thresh = cv2.adaptiveThreshold(
            blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
            cv2.THRESH_BINARY, 11, 2
        )
        return Image.fromarray(thresh)
        
    elif metodo == "Mejora de Contraste y Enfoque (Nítido)":
        # Utiliza CLAHE para ecualizar contraste local y aplica un kernel de enfoque lineal para trazos borrosos
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        cl_img = clahe.apply(gray)
        kernel_enfocar = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        enfoque = cv2.filter2D(cl_img, -1, kernel_enfocar)
        return Image.fromarray(enfoque)
        
    return imagen_pil

def extraer_texto_con_enrutamiento(pdf_bytes, page_num, pdf_dpi=150, poppler_path=None, img_filter="Solo Escala de Grises", pre_rendered_image=None):
    """
    Intenta extraer texto usando PyMuPDF (capa de texto digital).
    Si no es posible o el texto es muy corto/vacío/basura, recurre al flujo de OCR (Tesseract).
    Retorna (texto_extraido, origen_metodo, ocr_result)
    donde origen_metodo es 'Texto Embebido' o 'OCR', e ocr_result son las coordenadas brutas del OCR.
    """
    # 1. Intentar PyMuPDF si está disponible
    if pymupdf_available:
        try:
            # Abrimos el PDF directamente de los bytes (sin guardarlo en disco)
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                if 0 <= page_num - 1 < len(doc):
                    page = doc[page_num - 1]
                    text_extracted = page.get_text().strip()
                    # Criterio: Al menos 15 caracteres y algún alfanumérico para considerarse válido
                    if len(text_extracted) > 15 and any(c.isalnum() for c in text_extracted):
                        return text_extracted, "Texto Embebido", None
        except Exception:
            # Fallback silencioso a OCR si algo falla con fitz
            pass

    # 2. Si falla o no está disponible, usar el pipeline de OCR original
    if pdf2image_available and rapidocr_available and rapidocr_engine is not None:
        try:
            if pre_rendered_image is not None:
                img_pil = pre_rendered_image
            else:
                # Convertimos solo la página necesaria para no consumir memoria/tiempo convirtiendo todo
                images = convert_from_bytes(pdf_bytes, dpi=pdf_dpi, first_page=page_num, last_page=page_num, poppler_path=poppler_path, grayscale=True)
                img_pil = images[0] if images else None
                
            if img_pil:
                processed_img = preprocesar_imagen(img_pil, metodo=img_filter)
                # RapidOCR espera un numpy array o una ruta/PIL Image
                result = _ejecutar_rapidocr(np.array(processed_img))
                if result:
                    # Unir todo el texto detectado
                    text_ocr = "\n".join([line[1] for line in result if line[1]])
                else:
                    text_ocr = ""
                return text_ocr, "OCR", result
        except Exception as e:
            raise Exception(f"Fallo en la conversión/OCR de la página {page_num}: {e}")
            
    raise Exception("No se pudo extraer texto. PyMuPDF no disponible y/o OCR de RapidOCR/pdf2image no configurados correctamente.")
