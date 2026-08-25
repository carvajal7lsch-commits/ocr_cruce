import io
import os
import time
import threading
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
# Por que fallo la carga del motor, cuando falla. Sin esto el ImportError se perdia en
# silencio: la app arrancaba igual y el usuario recibia el mismo error cripto en CADA
# pagina ("OCR no configurado correctamente") sin ninguna pista de la causa real. Paso
# de verdad con un .exe empaquetado sin onnxruntime adentro.
ocr_error_carga = None

# --------------------------------------------------------------------------------------
# Que hardware ejecuta el OCR: se MIDE, no se adivina
# --------------------------------------------------------------------------------------
# onnxruntime puede correr los modelos en la CPU o en la GPU (DirectML, que funciona con
# cualquier GPU de Windows: Intel, AMD o NVIDIA, sin instalar nada aparte).
#
# La tentacion es "si hay GPU, usarla". No alcanza, porque la comparacion honesta no es
# GPU contra CPU: es GPU contra CPU CON TODOS SUS NUCLEOS. Medido sobre 8 paginas reales
# en una Intel Iris Xe (4 nucleos):
#
#     CPU, una pagina a la vez ....... 4.77 s/pagina
#     CPU, 8 en paralelo (lo normal) . 1.73 s/pagina   <- contra esto hay que competir
#     GPU, serializada ............... 1.46 s/pagina   <- gana, pero solo por ~16%
#
# Contra la CPU secuencial la GPU parece 3x mejor; contra la CPU real, apenas 16%. En una
# maquina con GPU dedicada la diferencia deberia ser mucho mayor, y por eso la decision no
# se cablea: se MIDE en cada equipo (ver calibrar_proveedor) y se guarda el ganador. Un
# portatil modesto se queda en CPU, uno con buena GPU la aprovecha, sin listas de modelos
# de GPU que mantener y sin arriesgarse a dejar a alguien con una app mas lenta.
#
# OJO CON LOS HILOS: DirectML NO admite inferencias concurrentes sobre el mismo motor. Con
# 2, 4 u 8 hilos el proceso se cae con segmentation fault, siempre, de forma reproducible.
# Como el servidor procesa varias paginas en paralelo, cuando el motor corre en GPU las
# llamadas se serializan con un candado (ver _ejecutar_rapidocr). No es una perdida: la
# GPU ya es un unico dispositivo y de todos modos atiende de a una.
PROVEEDOR_CPU = "cpu"
PROVEEDOR_GPU = "dml"

# Serializa las llamadas al motor cuando corre en GPU. Ver la nota sobre los hilos arriba:
# sin esto, con la GPU activa el proceso se cae con segmentation fault.
_candado_gpu = threading.Lock()

# Permite forzar uno a mano para pruebas: OCR_PROVEEDOR=cpu / OCR_PROVEEDOR=dml
_PROVEEDOR_FORZADO = (os.environ.get("OCR_PROVEEDOR") or "").strip().lower() or None

proveedor_actual = _PROVEEDOR_FORZADO or PROVEEDOR_CPU


def _params_motor(proveedor):
    params = dict(OCR_ENGINE_PARAMS_NEW_PKG)
    params["EngineConfig.onnxruntime.use_dml"] = (proveedor == PROVEEDOR_GPU)
    return params


def hay_gpu_disponible():
    """
    Si onnxruntime puede usar la GPU en esta maquina. Que PUEDA no significa que
    convenga: eso lo decide la medicion, no esta funcion.
    """
    try:
        import onnxruntime as ort
        return "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def soporta_gpu_esta_instalacion():
    """
    Si el onnxruntime instalado trae soporte de GPU, que es distinto de tener una GPU
    utilizable. Sirve para no confundir las dos cosas en la interfaz: el paquete normal
    ("onnxruntime") no habla con ninguna GPU, solo la variante "onnxruntime-directml" --
    y decirle a alguien "este equipo no tiene GPU" cuando en realidad tiene una perfecta
    pero el paquete no la soporta es una respuesta equivocada.
    """
    try:
        import importlib.metadata as md
        for dist in ("onnxruntime-directml", "onnxruntime-gpu"):
            try:
                md.version(dist)
                return True
            except md.PackageNotFoundError:
                continue
        return False
    except Exception:
        return False


try:
    from rapidocr import RapidOCR
    rapidocr_engine = RapidOCR(params=_params_motor(proveedor_actual))
    rapidocr_available = True
    _using_new_rapidocr_pkg = True
except Exception as err_nuevo:
    # Se captura Exception y no solo ImportError: al motor tambien lo puede tumbar un
    # modelo que no esta, un backend de inferencia ausente o un YAML de configuracion
    # que no se empaqueto -- y todos esos terminaban en el mismo silencio.
    try:
        from rapidocr_onnxruntime import RapidOCR
        rapidocr_engine = RapidOCR(**OCR_ENGINE_PARAMS_OLD_PKG)
        rapidocr_available = True
    except Exception as err_viejo:
        rapidocr_available = False
        rapidocr_engine = None
        ocr_error_carga = (
            f"rapidocr: {type(err_nuevo).__name__}: {err_nuevo} | "
            f"rapidocr_onnxruntime: {type(err_viejo).__name__}: {err_viejo}"
        )
        print(f"[ERROR] No se pudo iniciar el motor de OCR -> {ocr_error_carga}")


def _ejecutar_rapidocr(img_array):
    """
    Corre el motor RapidOCR que haya quedado disponible y normaliza el resultado
    a una lista de [caja, texto, score] -- igual sin importar si quedo activo el
    paquete nuevo "rapidocr" (devuelve un objeto RapidOCROutput) o el viejo
    "rapidocr_onnxruntime" (devuelve una tupla (lista, elapse)). Asi el resto del
    codigo (incluyendo el dibujo de cajas en server.py) no necesita saber cual es.
    """
    if proveedor_actual == PROVEEDOR_GPU:
        # De a una: DirectML no soporta inferencias concurrentes (ver la nota de hilos).
        with _candado_gpu:
            raw = rapidocr_engine(img_array)
    else:
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

def aplicar_proveedor(proveedor):
    """
    Cambia el motor a CPU o GPU en caliente. Devuelve True si quedo aplicado.

    Si crear el motor nuevo falla (driver raro, GPU ocupada, lo que sea), se conserva el
    que estaba funcionando: quedarse sin OCR por intentar acelerarlo seria un mal
    negocio.
    """
    global rapidocr_engine, proveedor_actual
    if not _using_new_rapidocr_pkg or proveedor == proveedor_actual:
        return False
    if proveedor == PROVEEDOR_GPU and not hay_gpu_disponible():
        return False
    try:
        rapidocr_engine = RapidOCR(params=_params_motor(proveedor))
        proveedor_actual = proveedor
        print(f"[INFO] Motor de OCR usando: {proveedor}")
        return True
    except Exception as err:
        print(f"[ADVERTENCIA] No se pudo cambiar el motor a {proveedor} "
              f"({type(err).__name__}: {err}); sigue en {proveedor_actual}.")
        return False


def calibrar_proveedor(muestras):
    """
    Corre las mismas paginas por CPU y por GPU y devuelve cual conviene en ESTA maquina,
    junto con los dos tiempos: (ganador, seg_cpu, seg_gpu). Devuelve (None, ...) si no
    hay nada que comparar.

    Cada camino se mide COMO SE VA A USAR de verdad, que es lo unico que hace comparable
    el resultado:
      - CPU: varias paginas a la vez, igual que el servidor.
      - GPU: de a una, porque DirectML se cae con inferencias concurrentes.
    Compararlas de otra forma da respuestas que suenan bien y son falsas: midiendo la CPU
    de a una pagina, la GPU parecia 3x mejor cuando en realidad gana ~16%.

    Se usan paginas REALES y de tamaños distintos, no una imagen repetida: DirectML
    recompila su grafo cada vez que cambia la forma de la entrada, asi que una sola
    muestra repetida mediria un caso que no ocurre nunca.

    No se toca el motor activo: se crean motores aparte para medir, y quien llama decide
    que hacer con el resultado.
    """
    if not _using_new_rapidocr_pkg or not muestras or not hay_gpu_disponible():
        return None, None, None

    from concurrent.futures import ThreadPoolExecutor

    def medir(proveedor):
        motor = RapidOCR(params=_params_motor(proveedor))
        motor(muestras[0])                 # calentamiento: la 1a inferencia carga pesos
        inicio = time.time()
        if proveedor == PROVEEDOR_GPU:
            for m in muestras:
                motor(m)
        else:
            hilos = min(len(muestras), (os.cpu_count() or 4))
            with ThreadPoolExecutor(max_workers=hilos) as ex:
                list(ex.map(motor, muestras))
        return (time.time() - inicio) / len(muestras)

    try:
        seg_cpu = medir(PROVEEDOR_CPU)
        seg_gpu = medir(PROVEEDOR_GPU)
    except Exception as err:
        print(f"[ADVERTENCIA] No se pudo calibrar el motor de OCR "
              f"({type(err).__name__}: {err}); se sigue en CPU.")
        return None, None, None

    # Se exige un margen del 10% para cambiar a GPU: si van parejos gana la CPU, que es el
    # camino probado, el que no depende del driver de video y el que permite procesar
    # varias paginas a la vez.
    ganador = PROVEEDOR_GPU if seg_gpu < seg_cpu * 0.9 else PROVEEDOR_CPU
    print(f"[INFO] Calibracion de OCR -> CPU {seg_cpu:.2f}s/pag | "
          f"GPU {seg_gpu:.2f}s/pag | gana: {ganador}")
    return ganador, seg_cpu, seg_gpu


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
            
    # Se dice QUE falto y POR QUE, en vez del mensaje generico de antes: este error se
    # repite una vez por pagina, asi que si no trae la causa, no trae nada.
    faltantes = []
    if not pdf2image_available:
        faltantes.append("pdf2image (poppler)")
    if not rapidocr_available or rapidocr_engine is None:
        faltantes.append(f"motor de OCR ({ocr_error_carga or 'no disponible'})")
    if not pymupdf_available:
        faltantes.append("PyMuPDF")
    raise Exception("No se pudo extraer texto. Falta: " + "; ".join(faltantes))
