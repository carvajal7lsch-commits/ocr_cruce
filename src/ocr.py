import io
import cv2
import numpy as np
from PIL import Image

try:
    from pdf2image import convert_from_bytes
    pdf2image_available = True
except ImportError:
    pdf2image_available = False

try:
    import pytesseract
    pytesseract_available = True
except ImportError:
    pytesseract_available = False

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
    # Convert PIL RGB to OpenCV BGR
    if len(open_cv_image.shape) == 3:
        open_cv_image = open_cv_image[:, :, ::-1].copy()
        
    gray = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2GRAY)
    
    if metodo == "Solo Escala de Grises":
        # Convert gray back to PIL to pass to Tesseract
        return Image.fromarray(gray)
        
    elif metodo == "Binarización Blanco y Negro (Otsu)":
        thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return Image.fromarray(thresh)
        
    return imagen_pil

def extraer_texto_con_enrutamiento(pdf_bytes, page_num, pdf_dpi=150, poppler_path=None, img_filter="Solo Escala de Grises"):
    """
    Intenta extraer texto usando PyMuPDF (capa de texto digital).
    Si no es posible o el texto es muy corto/vacío/basura, recurre al flujo de OCR (Tesseract).
    Retorna (texto_extraido, origen_metodo)
    donde origen_metodo es 'Texto Embebido' o 'OCR'.
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
                        return text_extracted, "Texto Embebido"
        except Exception:
            # Fallback silencioso a OCR si algo falla con fitz
            pass

    # 2. Si falla o no está disponible, usar el pipeline de OCR original
    if pdf2image_available and pytesseract_available:
        try:
            # Convertimos solo la página necesaria para no consumir memoria/tiempo convirtiendo todo
            images = convert_from_bytes(pdf_bytes, dpi=pdf_dpi, first_page=page_num, last_page=page_num, poppler_path=poppler_path)
            if images:
                processed_img = preprocesar_imagen(images[0], metodo=img_filter)
                text_ocr = pytesseract.image_to_string(processed_img, lang='spa')
                return text_ocr, "OCR"
        except Exception as e:
            raise Exception(f"Fallo en la conversión/OCR de la página {page_num}: {e}")
            
    raise Exception("No se pudo extraer texto. PyMuPDF no disponible y/o OCR de Tesseract/pdf2image no configurados correctamente.")
