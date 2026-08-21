import re
import difflib
from datetime import date
from rapidfuzz import fuzz

# Regex compilados una sola vez a nivel de módulo
PATRON_DOC_BARCODE = re.compile(r'-(\d{6,10})-\d{8}\b')
PATRON_DOC_GENERICO = re.compile(r'\b\d{1,3}(?:\.?\d{3}){2,3}\b')
# Variante con los puntos OBLIGATORIOS: es el formato en que la cedula imprime el numero
# en el frente ("1.110.461.846"), en letra grande. Sirve para distinguirlo de la linea
# densa del reverso, donde los digitos van corridos y esta no matchea.
PATRON_DOC_FRENTE = re.compile(r'\b\d{1,3}(?:\.\d{3}){2,3}\b')
PATRON_FECHA = re.compile(r'\b\d{2}[-/\.]\d{2}[-/\.]\d{4}\b')
# Fecha con mes en letras: "26-JUN-1986", "26 NOV 1986" o "08 MAYO 1987". El mes admite
# de 3 a 10 letras porque no todas las cedulas lo abrevian -- algunas lo escriben
# completo ("MAYO", "SEPTIEMBRE"), y exigir exactamente 3 hacia que esas fechas no se
# detectaran en absoluto.
_MES_EN_LETRAS = r'[A-ZÁÉÍÓÚ]{3,10}'
PATRON_FECHA_NAC = re.compile(
    r'FECHA DE NACIMIENTO\D{0,15}(\d{2}[- /]?' + _MES_EN_LETRAS + r'[- /]?\d{4})',
    re.IGNORECASE
)
# La MISMA forma de fecha pero suelta, sin la etiqueta pegada -- se usa para buscarla en
# las lineas vecinas a la etiqueta cuando el OCR la dejo separada de esta (ver
# extraer_fecha_nacimiento).
PATRON_FECHA_MES_SUELTA = re.compile(r'\b(\d{2}[- /]?' + _MES_EN_LETRAS + r'[- /]?\d{4})\b')

# Tipo de documento: se busca la frase de encabezado completa, en orden de especificidad
# (tarjeta/extranjeria antes que ciudadania para no dar por sentado el caso mas comun).
PATRON_TIPO_TARJETA = re.compile(r'TARJETA\s+DE\s+IDENTIDAD', re.IGNORECASE)
PATRON_TIPO_EXTRANJERIA = re.compile(r'C[EÉ]DULA\s+DE\s+EXTRANJER[IÍ]A', re.IGNORECASE)
PATRON_TIPO_CIUDADANIA = re.compile(r'C[EÉ]DULA\s+DE\s+CIUDADAN[IÍ]A', re.IGNORECASE)
# Documento provisional que expide la Registraduría mientras se entrega la cédula física.
PATRON_TIPO_CONTRASENA = re.compile(r'CONTRASE[ÑN]A', re.IGNORECASE)

# Fecha y lugar de expedicion: "15-OCT-2004 FLORENCIA".
# El separador entre la fecha y la ciudad es OPCIONAL (y admite coma) a proposito: el OCR
# a veces las pega sin espacio ("28-JUL-2005IBAGUE") o las separa con coma
# ("17 JUL 2024, FLORENCIA"); exigir un espacio hacia que el campo se perdiera entero.
PATRON_FECHA_LUGAR_EXP = re.compile(
    r'(\d{2}[-\s/]?' + _MES_EN_LETRAS + r'[-\s/]?\d{4})[\s,]*([A-ZÁÉÍÓÚÑ ]{3,})'
)

# Estatura / Grupo sanguineo (RH) / Sexo: se identifican por FORMA de contenido, no por
# posicion, porque en la plantilla de la cedula estos tres valores aparecen en una fila
# seguidos de sus tres etiquetas en la fila siguiente (box-sorting del OCR), asi que un
# lookback de una sola linea no permite saber con certeza cual valor es cual.
PATRON_ESTATURA = re.compile(r'\b([12]\.\d{2})\b')             # "1.71" (acotado para no matchear "1.117...")
# Grupo sanguineo (RH). La forma canonica es "O+" (letra y despues signo), pero el OCR
# devuelve el mismo dato de otras tres maneras, todas vistas en cedulas reales: da vuelta
# el orden ("+O") y lee la O como un cero ("+0", "0+") -- la tipografia de la cedula las
# hace casi identicas. Se aceptan las cuatro y se normalizan a "O+" (ver _normalizar_rh):
# si no, el campo salia vacio aunque estuviera perfectamente legible.
# AB va antes que A/B para que no gane el prefijo; sin \b final porque un "+"/"-" al
# terminar la linea no genera boundary.
PATRON_RH = re.compile(r'\b(AB|A|B|O|0)([+-])(?!\w)')                  # "B+", "0+"
PATRON_RH_INVERTIDO = re.compile(r'(?<!\w)([+-])(AB|A|B|O|0)(?!\w)')   # "+B", "+0"
PATRON_SEXO = re.compile(r'^(M|F)$')                            # solo si la linea COMPLETA es "M" o "F"
# Sexo dentro de la zona MRZ (la linea legible por maquina del reverso), que lo trae en
# una posicion fija: 7 digitos (fecha de nacimiento + digito de control), la M o la F, y
# otros 7 (fecha de expiracion + control) -- p. ej. "8705080F3302244C0L...". Sirve de
# respaldo cuando el campo impreso "Sexo" no quedo legible en el OCR.
PATRON_MRZ_SEXO = re.compile(r'\d{7}([MF])\d{7}')

# Fecha de nacimiento -> edad calculada
MESES_ES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
# El mes se captura completo (3-10 letras) y luego se recorta a 3 para buscarlo en
# MESES_ES, asi sirve igual "MAY" que "MAYO" o "SEPTIEMBRE".
PATRON_FECHA_PARSEABLE = re.compile(r'(\d{2})[-\s/]?([A-ZÁÉÍÓÚ]{3,10})[-\s/]?(\d{4})')

KEYWORDS_EXCLUIR = (
    "REPÚBLICA", "REPUBLICA", "CEDULA", "CÉDULA", "CIUDADANÍA", "CIUDADANIA",
    "NACIMIENTO", "SEXO", "ESTATURA", "IDENTIFICACION", "IDENTIFICACIÓN",
    "REGISTRADOR", "EXPEDICION", "EXPEDICIÓN", "CAMSCANNER", "PERSONAL",
    "POWERED", "SCANNED", "SCANNER", "COLOMBIA", "FECHA", "LUGAR", "FIRMA", "HUELLA",
    "NACIONALIDAD", "EXPIRACIÓN", "EXPIRACION", "NUIP", "ICCOL",
    "DIC", "ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV",
    "ESTADO", "CIVIL", "NUMERO", "NÚMERO",
    # "COL" es el VALOR del campo Nacionalidad, no un nombre. Va aca (comparado por
    # palabra completa) y no como subcadena: buscarlo suelto descartaba nombres reales
    # que lo contienen -- NICOLAS, NICOLASA -- como si fueran texto de plantilla.
    "COL",
)
# Se compara por PALABRA COMPLETA, no por subcadena. Las abreviaturas de mes (MAR, JUL,
# SEP, ENE...) viven dentro de muchisimos nombres reales -- MARIA, MARTINEZ, MARQUEZ,
# JULIANA, SEPULVEDA, ENEIDA -- y con una comparacion de subcadena esos nombres se
# descartaban como si fueran texto de plantilla, dejando la tarjeta sin nombre aunque el
# OCR lo hubiera leido perfecto (caso real: "MARIA EUGENIA" se perdia por el "MAR").
_PATRON_KEYWORDS_EXCLUIR = re.compile(
    r'\b(?:' + '|'.join(re.escape(k) for k in KEYWORDS_EXCLUIR) + r')\b'
)

# Patrón MRZ: última línea tipo APELLIDO<APELLIDO<<NOMBRE<NOMBRE<
PATRON_MRZ_NOMBRE = re.compile(r'^([A-Z]+(?:<[A-Z]+)*)<<([A-Z]+(?:<[A-Z]+)*)<*$')

def normalizar_texto(texto):
    """
    Convierte a mayúsculas y remueve tildes y acentos para evitar fallos de coincidencia.
    """
    t = texto.upper()
    reemplazos = {"Á": "A", "É": "E", "Í": "I", "Ó": "O", "Ú": "U", "Ü": "U"}
    for original, reemplazo in reemplazos.items():
        t = t.replace(original, reemplazo)
    return t


def _normalizar_rh(letra, signo):
    """
    Deja el grupo sanguineo en su forma canonica ("O+"): la letra primero, y el cero que
    el OCR confunde con la O convertido de vuelta a O. Asi el reporte muestra siempre la
    misma forma sin importar como lo haya leido el OCR, y el cruce contra el Excel no
    falla por un "+0" que en realidad era un "O+".
    """
    return ("O" if letra == "0" else letra) + signo


def _es_candidato_mayusculas_plantilla(texto_original, minimo=0.8):
    """
    True si 'texto_original' (SIN normalizar/mayuscular todavia) esta predominantemente
    en mayuscula. Los campos OFICIALES de la cedula (apellidos, nombres, lugar de
    nacimiento, etc.) los imprime siempre la plantilla en mayuscula -- pero el titular
    tambien tiene su firma/rubrica impresa en un estilo cursivo aparte, que el OCR lee
    en minuscula o mayuscula/minuscula mezclada (casos reales: "Jeison Bastidus",
    "btRER a cHavaRRO"). Aunque esa firma contenga literalmente el mismo nombre, es una
    fuente MUCHO mas ruidosa para el OCR que el campo oficial -- se prefiere descartarla
    como candidato (y quedarse con el campo oficial, o con nada) antes que aceptar una
    version peor leida del nombre. Se probo contra casos reales: la firma da 14-50% de
    mayusculas, un campo oficial genuino da 100%.
    """
    letras = [c for c in texto_original if c.isalpha()]
    if not letras:
        return True
    return sum(1 for c in letras if c.isupper()) / len(letras) >= minimo

def similitud_etiqueta(texto_linea, target_label):
    """
    Mejor similitud (0-100) entre alguna palabra de la línea y la etiqueta objetivo,
    usando difflib. Devolver el numero -- y no solo un si/no contra un umbral -- permite
    desempatar cuando una misma linea se parece a DOS etiquetas distintas y hay que
    quedarse con la mas parecida (ver _clasificar_linea_de_fecha).
    """
    texto_norm = normalizar_texto(texto_linea)
    # Remover caracteres especiales y separar en palabras limpias
    palabras = [re.sub(r'[^A-ZÑ]', '', w) for w in texto_norm.split() if w.strip()]
    mejor = 0.0
    for p in palabras:
        if len(p) >= 4:
            # difflib calcula un ratio de coincidencia entre 0.0 y 1.0
            ratio = difflib.SequenceMatcher(None, p, target_label).ratio() * 100
            if ratio > mejor:
                mejor = ratio
    return mejor


def es_similar_etiqueta(texto_linea, target_label, threshold=70):
    """
    Compara matemáticamente cada palabra de la línea con una etiqueta objetivo
    usando la librería estándar difflib (Gestor de secuencias).
    Retorna True si hay una coincidencia de similitud >= threshold.
    """
    return similitud_etiqueta(texto_linea, target_label) >= threshold

# Frases COMPLETAS fijas de la plantilla de la cédula colombiana (no cambian de un
# documento a otro). A diferencia de labels_control/KEYWORDS_EXCLUIR (que buscan
# palabras sueltas), esto compara la línea completa -- sin espacios -- contra la frase
# completa -- también sin espacios -- por similitud de caracteres. Eso la hace tolerante
# tanto a que el OCR haya sustituido/perdido letras individuales COMO a que haya
# fusionado varias palabras en una sola sin espacio (p. ej. "REGISTRADURIA NACIONAL"
# leído como "PEGISTRADOINACIONAL"), que es el patrón de error más común y el que un
# chequeo palabra-por-palabra no detecta -- cada variante nueva de ruido que el OCR
# invente sobre una de estas frases ya fijas queda cubierta sin tener que agregarla
# a mano cada vez.
FRASES_PLANTILLA_CEDULA = [
    "REPUBLICA DE COLOMBIA", "IDENTIFICACION PERSONAL",
    "REGISTRADURIA NACIONAL DEL ESTADO CIVIL", "REGISTRADOR NACIONAL", "ESTADO CIVIL",
    "CEDULA DE CIUDADANIA", "CEDULA DE EXTRANJERIA", "TARJETA DE IDENTIDAD",
    "FECHA DE NACIMIENTO", "LUGAR DE NACIMIENTO", "FECHA Y LUGAR DE EXPEDICION",
    "FECHA Y LUGAR DE NACIMIENTO", "NACIONALIDAD", "NUMERO DE IDENTIFICACION",
    # Boilerplate propio de la Contraseña (el comprobante provisional que da la
    # Registraduria mientras se entrega la cedula fisica) -- sin esto, "CONTRASEÑA" y
    # "PRIMERA VEZ CC" se colaban como si fueran el nombre del titular. Se usa la frase
    # COMPLETA "PRIMERA VEZ CC" (no solo "PRIMERA VEZ") a proposito: se probo y "PRIMERA
    # VEZ" sola, al ser mas corta y generica, daba falsos positivos contra nombres reales
    # como "PRIMERO GARCIA" (75% de similitud) -- con la frase completa baja a 70%, ya
    # bajo el umbral. Por la misma razon no se agrega "SEGUNDA VEZ" sin haberla visto en
    # un documento real -- mejor esperar un caso real (como se hizo con esta) que adivinar.
    "CONTRASEÑA", "PRIMERA VEZ CC", "LUGAR DE PREPARACION",
    "OFICINA DE ENTREGA", "ESTE COMPROBANTE ES VALIDO",
]
_FRASES_PLANTILLA_COMPACTAS = [
    re.sub(r'[^A-ZÑ]', '', normalizar_texto(f)) for f in FRASES_PLANTILLA_CEDULA
]

# Subconjunto de FRASES_PLANTILLA_CEDULA usado especificamente para el "Filtro de Línea
# Anterior" (ver mas abajo): ahi el objetivo es detectar que la línea de ANTES sea la
# etiqueta del registrador/firmante, para no confundir su nombre IMPRESO con el del
# titular. Usar la lista COMPLETA de frases ahi (como se hizo en un intento anterior)
# fue un error -- encabezados genericos como "CEDULA DE CIUDADANIA" aparecen 1-2 líneas
# antes de MUCHO contenido legitimo (apellidos, nombres) por pura cercania en el
# documento, sin que eso signifique que lo que sigue sea ruido. Solo las frases
# realmente asociadas al registrador/firma deben excluir la línea siguiente.
FRASES_FIRMA_REGISTRADOR = [
    "REGISTRADURIA NACIONAL DEL ESTADO CIVIL", "REGISTRADOR NACIONAL", "FIRMA",
]
_FRASES_FIRMA_REGISTRADOR_COMPACTAS = [
    re.sub(r'[^A-ZÑ]', '', normalizar_texto(f)) for f in FRASES_FIRMA_REGISTRADOR
]


def es_ruido_de_plantilla(texto_linea, threshold=75, frases_compactas=None):
    """
    True si 'texto_linea' probablemente sea una lectura ruidosa del OCR sobre una de
    las frases dadas en 'frases_compactas' (por defecto, cualquiera de
    FRASES_PLANTILLA_CEDULA), y no un dato real (nombre, apellido).

    Se calcula la similitud global (ratio) contra cada frase completa. Ademas, SOLO si
    el candidato ya tiene al menos 9 caracteres, tambien se calcula partial_ratio (la
    mejor VENTANA dentro de la frase larga que se parece al candidato) -- eso es lo que
    detecta que el candidato es un fragmento de varias palabras fusionadas sin espacio
    (p. ej. "REGISTRADURIA NACIONAL" leido como "PEGISTRADOINACIONAL"). partial_ratio
    NO se usa con candidatos mas cortos que eso: al ignorar la longitud, encuentra por
    puro azar alguna ventana parecida dentro de una frase larga incluso para una
    palabra corta que no tiene nada que ver -- se probo contra nombres/apellidos
    colombianos reales y un nombre de pila corto como "MARIA" (5 letras) llegaba a
    marcarse como ruido (75% contra "CEDULA DE EXTRANJERIA") sin serlo. Calibrado
    para que el peor falso positivo quede en ~67% y el ruido real siga detectandose
    en ~78-100%, dejando margen de sobra a cada lado del umbral de 75%.
    """
    candidato = re.sub(r'[^A-ZÑ]', '', normalizar_texto(texto_linea))
    if len(candidato) < 4:
        return False
    objetivo = frases_compactas if frases_compactas is not None else _FRASES_PLANTILLA_COMPACTAS
    for frase in objetivo:
        score = fuzz.ratio(candidato, frase)
        if len(candidato) >= 9:
            score = max(score, fuzz.partial_ratio(candidato, frase))
        if score >= threshold:
            return True
    return False


def es_similar_a_labels_tarjeta(texto_linea, threshold=70):
    """
    Verifica si la línea es ruido de plantilla (ver es_ruido_de_plantilla) o si alguna
    de sus palabras sueltas es matemáticamente similar a una palabra clave de la
    cédula o un departamento de Colombia, para descartarla como candidato a nombre.
    """
    if es_ruido_de_plantilla(texto_linea):
        return True

    texto_norm = normalizar_texto(texto_linea)
    palabras = [re.sub(r'[^A-ZÑ]', '', w) for w in texto_norm.split() if w.strip()]
    labels_control = [
        "REPUBLICA", "COLOMBIA", "IDENTIFICACION", "PERSONAL",
        "CEDULA", "CIUDADANIA", "APELLIDOS", "NOMBRES",
        "NACIMIENTO", "REGISTRADOR", "EXPEDICION", "FIRMA",
        # Departamentos de Colombia más comunes para excluir líneas de municipios
        "HUILA", "TOLIMA", "CAQUETA", "VALLE", "META", "CUNDINAMARCA",
        "BOYACA", "SANTANDER", "ANTIOQUIA", "NARIÑO", "CAUCA",
        "PUTUMAYO", "AMAZONAS", "GUAJIRA", "ATLANTICO", "BOLIVAR",
        "CESAR", "CORDOBA", "SUCRE", "CALDAS", "QUINDIO", "RISARALDA"
    ]
    for p in palabras:
        if len(p) >= 4:
            for label in labels_control:
                ratio = difflib.SequenceMatcher(None, p, label).ratio() * 100
                if ratio >= threshold:
                    return True
    return False

def detectar_cara_cedula(texto):
    """
    Detecta si la página corresponde al Anverso (Frente) o Reverso (Atrás) de la cédula.
    Utiliza un sistema de pesos calibrado para soportar tanto cédulas nuevas como antiguas.
    """
    texto_upper = texto.upper()

    # Indicadores del reverso con sus respectivos pesos (incluye estatura, sexo y expedición)
    keywords_reverso = {
        "REGISTRADOR": 5, "HUELLA": 5, "INDICE": 5, "ÍNDICE": 5, "DERECHO": 5,
        "ICCOL": 5, "<<<<<": 5, "COL108": 5, "ESTATURA": 5, "SEXO": 5,
        "EXPEDICION": 3, "EXPEDICIÓ": 3
    }

    # Indicadores del anverso con sus respectivos pesos (específicos del frente)
    keywords_anverso = {
        "NUIP": 5, "NÚMERO": 5, "NUMERO": 5,
        "REPÚBLICA": 2, "REPUBLICA": 2, "CEDULA": 2, "CÉDULA": 2,
        "APELLIDOS": 3, "NOMBRES": 3
    }

    score_reverso = sum(peso for kw, peso in keywords_reverso.items() if kw in texto_upper)
    score_anverso = sum(peso for kw, peso in keywords_anverso.items() if kw in texto_upper)

    if score_reverso >= 6 and score_anverso >= 6:
        return "Ambas Caras (Completo)"
    elif score_reverso > score_anverso:
        return "Reverso (Atrás)"
    elif score_anverso > score_reverso:
        return "Anverso (Frente)"
    else:
        # Fallback rápido si los pesos empatan o no se detectan
        if "<<<<<" in texto_upper or "REGISTRADOR" in texto_upper:
            return "Reverso (Atrás)"
        return "Anverso (Frente)"

def detectar_tipo_documento(texto):
    """
    Clasifica el tipo de documento a partir de la frase de encabezado. Se revisan
    los tipos mas especificos primero para no asumir "CC" por defecto (eso
    etiquetaria mal una Tarjeta de Identidad, por ejemplo). Los tres documentos de
    identidad se devuelven abreviados (CC/TI/CE, la nomenclatura oficial colombiana)
    en vez del nombre completo -- la Contraseña se deja sin abreviar por no tener una
    sigla de uso tan estandarizado.
    Retorna None si no se encontro ninguna frase reconocible (p. ej. una pagina
    de puro reverso, donde el encabezado no aparece).
    """
    if PATRON_TIPO_TARJETA.search(texto):
        return "TI"
    if PATRON_TIPO_EXTRANJERIA.search(texto):
        return "CE"
    if PATRON_TIPO_CIUDADANIA.search(texto):
        return "CC"
    if PATRON_TIPO_CONTRASENA.search(texto):
        return "CONTRASEÑA"

    # Respaldo difuso: los patrones de arriba exigen la frase EXACTA y seguida, y eso
    # falla en dos situaciones muy comunes:
    #   - El OCR se come una letra ("CEDULA DE CIUDADAMIA").
    #   - El box-sorting parte la frase en lineas separadas y mete otra en medio
    #     ("CEDULA DE" / "REPUBLICA DE COLOMBIA" / "CIUDADANIA").
    # Por eso se busca ademas la PALABRA distintiva de cada tipo por similitud. Las
    # cuatro palabras son bien distintas entre si (se midieron: la mas parecida a otra
    # queda en 38%), y "IDENTIDAD" no se confunde con el "IDENTIFICACION PERSONAL" que
    # trae toda cedula (61%), asi que el umbral de 80 deja margen de sobra a ambos lados.
    for linea in texto.split('\n'):
        linea_norm = normalizar_texto(linea.strip())
        if not linea_norm:
            continue
        if es_similar_etiqueta(linea_norm, "TARJETA", 80) or es_similar_etiqueta(linea_norm, "IDENTIDAD", 80):
            return "TI"
        if es_similar_etiqueta(linea_norm, "EXTRANJERIA", 80):
            return "CE"
        if es_similar_etiqueta(linea_norm, "CIUDADANIA", 80):
            return "CC"
        if es_similar_etiqueta(linea_norm, "CONTRASEÑA", 80):
            return "CONTRASEÑA"
    return None

def _valor_junto_a_etiqueta(lineas, idx_etiqueta, ya_tomado=""):
    """
    Devuelve el valor impreso que le corresponde a una etiqueta de la cedula
    ("Apellidos" / "Nombres"): prueba las 2 lineas ANTERIORES y luego las 2 siguientes
    -- en ese orden porque el box-sorting del OCR suele emitir el valor antes que su
    etiqueta -- y se queda con el primer candidato que no sea texto de la plantilla,
    otra etiqueta, ni el valor que ya se le asigno al otro campo.

    Apellidos y Nombres comparten esta funcion a proposito. Antes cada uno traia su
    propia lista de palabras a excluir y se fueron desincronizando: la de Nombres no
    tenia "NUIP", asi que en una cedula donde el OCR dejo el NUIP entre la etiqueta
    "Nombres" y su valor, el nombre detectado terminaba siendo "NUIP" (caso real:
    "MURCIA ARTUNDUAGA NUIP" en vez de "MURCIA ARTUNDUAGA VANESSA ALEXANDRA").
    """
    candidatos_indices = [
        idx for idx in (idx_etiqueta - 1, idx_etiqueta - 2, idx_etiqueta + 1, idx_etiqueta + 2)
        if 0 <= idx < len(lineas)
    ]
    for idx in candidatos_indices:
        candidato = re.sub(r'[^A-Za-zÁÉÍÓÚñÑ ]', '', lineas[idx]).strip()
        candidato_upper = normalizar_texto(candidato)
        if len(candidato) <= 2 or candidato_upper == ya_tomado:
            continue
        # Palabras de la plantilla o de OTRO campo de la cedula (NUIP, REPUBLICA,
        # NACIONALIDAD, FECHA, COL...). Es el mismo filtro por palabra completa que ya
        # usa la Prioridad 3, en vez de una lista propia por rama.
        if _PATRON_KEYWORDS_EXCLUIR.search(candidato_upper):
            continue
        if (es_similar_etiqueta(candidato_upper, "APELLIDOS", threshold=70)
                or es_similar_etiqueta(candidato_upper, "NOMBRES", threshold=70)):
            continue
        if es_similar_a_labels_tarjeta(candidato_upper, threshold=70):
            continue
        if not _es_candidato_mayusculas_plantilla(candidato):
            continue
        return candidato_upper
    return ""


def _es_version_extendida(candidato, base):
    """
    True si 'candidato' es el MISMO texto que 'base' pero mas largo -- es decir, base
    parece una version truncada de candidato. Se comparan solo las letras (sin espacios
    ni signos) para que no importe como quedaron separadas las palabras.

    Se usa para detectar nombres cortados por el ancho fijo del MRZ: si lo que se leyo
    del campo impreso empieza igual que lo del MRZ pero continua, el MRZ venia recortado.
    Al exigir que uno sea prefijo EXACTO del otro, dos nombres distintos nunca lo
    activan -- solo el mismo nombre al que le falta el final.
    """
    solo_letras = lambda t: re.sub(r'[^A-ZÑ]', '', normalizar_texto(t or ""))
    c, b = solo_letras(candidato), solo_letras(base)
    return len(c) > len(b) > 0 and c.startswith(b)


# Que tan parecidos tienen que ser el nombre impreso y el del MRZ para considerarlos el
# MISMO nombre leido dos veces (y no dos nombres distintos). Medido sobre cedulas reales:
# los desacuerdos que son el mismo nombre dan 90-99 -- "TOVAR CASTIELO"/"TOVAR CASTILLO"
# da 95, "MUNOZ"/"MUÑOZ" da 96 -- mientras que una lectura posicional que se fue a
# cualquier lado queda MUY por debajo.
_UMBRAL_MISMO_NOMBRE = 90


def _impreso_le_gana_al_mrz(impreso, mrz):
    """
    El MRZ es la fuente por defecto para el nombre, pero hay dos formas conocidas en que
    pierde contra el campo impreso -- y en las dos se trata del MISMO nombre leido dos
    veces, no de dos nombres distintos:

    1. Viene TRUNCADO: los renglones del MRZ son de ancho fijo (30 caracteres) y cortan
       los nombres largos ("...ANDREA<PATRICI" mientras el campo impreso dice "ANDREA
       PATRICIA" completo).
    2. Viene PEOR LEIDO o EMPOBRECIDO: el MRZ es la letra mas chica y densa de la cedula,
       la que peor sobrevive a un escaneo o una fotocopia (caso real: "TOVAR<CASTIELO"
       por "TOVAR CASTILLO"). Y ademas, por norma, el MRZ no lleva enies ni tildes
       ("TORRES<MUNOZ" por "TORRES MUÑOZ"), asi que cuando ambos dicen lo mismo el
       impreso siempre trae MAS informacion.

    Para que esto no le de la victoria a una lectura posicional que agarro basura, se
    exige que los dos textos sean casi el mismo y que el impreso NO tenga menos letras
    que el MRZ: si al impreso le faltan, es el impreso el que perdio algo por el camino
    y entonces se queda el MRZ.
    """
    if not impreso or not mrz:
        return False
    # Truncado: prefijo exacto, la senial mas fuerte y la que no necesita umbral.
    if _es_version_extendida(impreso, mrz):
        return True
    solo_letras = lambda t: re.sub(r'[^A-ZÑ]', '', normalizar_texto(t or ""))
    if len(solo_letras(impreso)) < len(solo_letras(mrz)):
        return False
    return fuzz.ratio(normalizar_texto(impreso), normalizar_texto(mrz)) >= _UMBRAL_MISMO_NOMBRE


def extraer_documento(texto):
    """
    Extrae el numero de documento cruzando las DOS fuentes que trae la cedula, en vez de
    confiar ciegamente en una sola:

    - El frente lo imprime en letra grande y con puntos ("1.110.461.846").
    - El reverso lo repite dentro de una linea densa de codigo
      ("P.2900100.63141102-F-1110451046-20051212").

    Antes se prefería SIEMPRE el del reverso, y cuando el OCR lo leia mal (facil: son
    digitos chiquitos y corridos) el documento quedaba equivocado aunque el del frente
    estuviera perfecto -- caso real: 1110451046 en vez de 1110461846, que generaba una
    falsa alerta de anomalia contra el Excel.

    Reglas, en orden:
    1. El numero impreso CON PUNTOS gana si tiene largo de cedula (8-10 digitos). Ese
       patron es muy especifico -- es literalmente el campo "NUMERO"/"NUIP" de la
       cedula -- y va en letra grande, la mas facil de leer para el OCR.
    2. Si no, el del reverso, siempre que tambien tenga largo de cedula.
    3. Si ninguno es plausible, se devuelve lo que haya, priorizando lo impreso.

    El chequeo de largo es lo que evita dos fallos vistos en cedulas reales:
    - El OCR parte la linea del reverso en dos y el patron captura un pedazo suelto:
      de "P-4400900-00796692 / 115946894-..." salia "4400900", que es el codigo de la
      oficina y no la cedula. Con 7 digitos no pasa el filtro de plausibilidad.
    - El OCR lee mal un par de digitos del reverso (1110451046 en vez de 1110461846) y
      generaba una falsa alerta contra el Excel.
    """
    def _plausible(numero):
        # Las cedulas y NUIP colombianos estan en ese rango de digitos; un numero mas
        # corto o mas largo es otra cosa (codigo de oficina, consecutivo, sello).
        return numero is not None and 8 <= len(numero) <= 10

    doc_reverso = None
    matches_barcode = list(PATRON_DOC_BARCODE.finditer(texto))
    if matches_barcode:
        crudo = matches_barcode[-1].group(1)
        try:
            doc_reverso = str(int(crudo))  # normaliza y quita ceros a la izquierda
        except ValueError:
            doc_reverso = crudo

    def _limpiar(match):
        if not match:
            return None
        candidato = match.group(0).replace('.', '')
        # Una cedula/NUIP colombiana nunca empieza en cero. Si el candidato trae un cero
        # a la izquierda, casi seguro es otra cosa (un sello, un codigo de registro) que
        # por casualidad tiene forma de grupos de 3 digitos.
        return None if candidato.startswith('0') else candidato

    # Con puntos = el campo impreso, inconfundible. Sin puntos = mucho menos confiable
    # (puede caer sobre cualquier numero largo de la pagina), asi que solo se usa al final.
    doc_puntos = _limpiar(PATRON_DOC_FRENTE.search(texto))
    doc_generico = _limpiar(PATRON_DOC_GENERICO.search(texto))

    if _plausible(doc_puntos) and _plausible(doc_reverso):
        if doc_puntos == doc_reverso:
            return doc_puntos          # doble confirmacion
        if fuzz and fuzz.ratio(doc_puntos, doc_reverso) >= 70:
            # Se parecen: es el mismo numero con algun digito mal leido. Gana el
            # impreso, que va en letra grande y es mucho mas facil de leer que la
            # linea densa del reverso.
            return doc_puntos
        # Muy distintos: lo del frente no parece ser el documento sino otro numero de
        # la pagina (un sello, un consecutivo) que por casualidad quedo con formato de
        # grupos de 3 digitos. El del reverso tiene un formato inconfundible.
        return doc_reverso

    if _plausible(doc_puntos):
        return doc_puntos
    if _plausible(doc_reverso):
        return doc_reverso
    if _plausible(doc_generico):
        return doc_generico

    return doc_puntos or doc_reverso or doc_generico


# Las otras fechas que trae una cedula y que NO son la de nacimiento. Sirven para
# descartar candidatas cuando su etiqueta vecina habla de una de estas.
_ETIQUETAS_OTRAS_FECHAS = ("EXPEDICION", "VENCIMIENTO", "EXPIRACION")


def _clasificar_linea_de_fecha(linea_norm):
    """
    Dice si la linea es la etiqueta de la fecha de NACIMIENTO, la de OTRA fecha
    (expedicion/vencimiento/expiracion), o ninguna. Retorna "nacimiento", "otra" o None.

    No basta con umbrales independientes: "NACIMIENTO" y "VENCIMIENTO" comparten el
    sufijo "CIMIENTO" y se parecen entre si un 86%, asi que la linea de nacimiento
    tambien pasaba el umbral de vencimiento y las dos quedaban empatadas. Por eso se
    compara cual de las dos se parece MAS y gana esa.

    Ademas se descarta la etiqueta del LUGAR de nacimiento, que comparte la palabra pero
    rotula otro campo: si la linea dice "lugar" y no dice "fecha", es el lugar. La forma
    combinada "FECHA Y LUGAR DE NACIMIENTO" (que trae la Contraseña) si cuenta, porque
    nombra ambos campos a la vez.
    """
    sim_nacimiento = similitud_etiqueta(linea_norm, "NACIMIENTO")
    sim_otra = max(similitud_etiqueta(linea_norm, etq) for etq in _ETIQUETAS_OTRAS_FECHAS)

    if sim_nacimiento >= 70 and sim_nacimiento > sim_otra:
        es_solo_lugar = (es_similar_etiqueta(linea_norm, "LUGAR", 70)
                         and not es_similar_etiqueta(linea_norm, "FECHA", 70))
        return None if es_solo_lugar else "nacimiento"

    if sim_otra >= 70:
        return "otra"

    return None


def extraer_fecha_nacimiento(texto, lineas):
    """
    Extrae la fecha de nacimiento ("21-NOV-1967").

    Primero se prueba el camino rapido: la etiqueta exacta seguida de la fecha
    ("FECHA DE NACIMIENTO 21-NOV-1967").

    Si eso falla, se invierte la busqueda: en vez de ubicar la etiqueta y mirar donde
    cayo la fecha, se recorren las FECHAS y para cada una se mira su vecindario para
    decidir a que campo pertenece. Ese cambio es el que aguanta lo que hace el OCR en la
    practica, donde la etiqueta aparece de cualquier forma:
      - pegada a la fecha en la misma linea ("FECHA.DE NACIMIENTO 08-JUL-1994"),
      - despues de la fecha ("21-NOV-1967" / "FECHA DE NACIMIENTO"),
      - antes ("Fecha de nacimiento" / "G.S." / "08 JUL 1989"),
      - o partida y revuelta entre dos lineas ("NAOMIENTG 28-JUL-1999" / "FECHADE").

    Para no confundirla con las OTRAS fechas de la cedula (expedicion, vencimiento), se
    exige que la etiqueta de nacimiento este mas CERCA de la fecha que cualquiera de
    esas otras, y se descartan las fechas que traen una ciudad pegada en la misma linea
    ("09-DIC-1985 CARTAGO"), que es la forma tipica de la de expedicion.

    Como ultimo recurso queda una fecha totalmente numerica (DD-MM-YYYY).
    """
    match_directo = PATRON_FECHA_NAC.search(texto)
    if match_directo:
        return match_directo.group(1)

    lineas_norm = [normalizar_texto(l.strip()) for l in lineas]

    for i, linea_norm in enumerate(lineas_norm):
        # Fecha + ciudad en la misma linea es la de expedicion, no la de nacimiento.
        if PATRON_FECHA_LUGAR_EXP.search(linea_norm):
            continue
        m = PATRON_FECHA_MES_SUELTA.search(linea_norm)
        if not m:
            continue

        # Se mira la propia linea y hasta 2 a cada lado, anotando a que distancia queda
        # la etiqueta de nacimiento MAS CERCANA y la de otra fecha mas cercana.
        dist_nacimiento = dist_otra = None
        for j in range(max(0, i - 2), min(len(lineas_norm), i + 3)):
            distancia = abs(j - i)
            clase = _clasificar_linea_de_fecha(lineas_norm[j])
            if clase == "nacimiento" and (dist_nacimiento is None or distancia < dist_nacimiento):
                dist_nacimiento = distancia
            elif clase == "otra" and (dist_otra is None or distancia < dist_otra):
                dist_otra = distancia

        if dist_nacimiento is not None and (dist_otra is None or dist_nacimiento < dist_otra):
            return m.group(1)

    match_fecha = PATRON_FECHA.search(texto)
    if match_fecha:
        return match_fecha.group(0)

    return None


def extraer_lugar_nacimiento(lineas, nombre_completo=None):
    """
    Busca la etiqueta "LUGAR DE NACIMIENTO" y toma como valor las 1-2 líneas
    anteriores (ciudad + departamento) -- en esta plantilla de cédula el valor
    se imprime ANTES que su etiqueta, igual que Apellidos/Nombres.

    Salvaguarda: nunca se acepta un candidato que ya forma parte del nombre ya
    extraido (nombre_completo). Caso real que motiva esto: en la Contraseña (el
    comprobante provisional antes de la cedula fisica), la etiqueta combinada
    "FECHA Y LUGAR DE NACIMIENTO" queda justo despues del nombre del titular en vez
    de despues de un lugar -- sin este chequeo, el nombre se duplicaba tal cual como
    si fuera su propio lugar de nacimiento.
    """
    nombre_norm = normalizar_texto(nombre_completo) if nombre_completo else ""

    def _candidato_valido(bruto):
        candidato = bruto.strip()
        candidato_norm = normalizar_texto(candidato)
        if len(candidato) < 2:
            return None
        if any(kw in candidato_norm for kw in ("FECHA", "NUMERO", "APELLIDOS", "NOMBRES", "NACIMIENTO", "ESTATURA", "SEXO")):
            return None
        # Un lugar tiene NOMBRE, no es un dato de otro campo vecino. Sin este minimo de
        # letras se colaban valores de campos contiguos -- caso real: el grupo sanguineo
        # "O+" (y su variante mal leida "+0") terminaba guardado como lugar de
        # nacimiento por ser la linea justo anterior a la etiqueta.
        if sum(c.isalpha() for c in candidato) < 3:
            return None
        # Un lugar tampoco es una FECHA. El filtro de "mayoria digitos" de abajo no
        # alcanza cuando el mes viene escrito en letras: "08 MAYO 1987 O+" tiene mas
        # letras que numeros y se colaba entero como lugar de nacimiento.
        if PATRON_FECHA_MES_SUELTA.search(candidato_norm) or PATRON_FECHA.search(candidato_norm):
            return None
        digitos = sum(c.isdigit() for c in candidato)
        if digitos > len(candidato) * 0.5:
            return None
        if nombre_norm and candidato_norm in nombre_norm:
            return None
        return candidato.upper()

    for i, linea in enumerate(lineas):
        linea_upper = linea.upper().strip()
        if not (es_similar_etiqueta(linea_upper, "LUGAR", 70) and es_similar_etiqueta(linea_upper, "NACIMIENTO", 70)):
            continue

        # Hay cedulas que imprimen el valor ANTES de su etiqueta ("MORELIA" /
        # "(CAQUETA)" / "LUGAR DE NACIMIENTO") y otras que lo imprimen DESPUES
        # ("Lugar de nacimiento" / "BOGOTA D.C. (CUNDINAMARCA)"), asi que se prueban
        # las dos direcciones: primero hacia atras y, si ahi no hay nada usable,
        # hacia adelante.
        candidatos = [c for c in (_candidato_valido(lineas[j])
                                  for j in (i - 2, i - 1) if j >= 0) if c]
        if not candidatos:
            candidatos = [c for c in (_candidato_valido(lineas[j])
                                      for j in (i + 1, i + 2) if j < len(lineas)) if c]
        if candidatos:
            return " ".join(candidatos)
    return None

def extraer_fecha_lugar_expedicion(lineas):
    """
    Busca la etiqueta "FECHA Y LUGAR DE EXPEDICION" y valida que la línea anterior
    tenga forma de "DD-MES-YYYY CIUDAD" antes de aceptarla como valor.
    """
    for i, linea in enumerate(lineas):
        linea_upper = linea.upper().strip()
        if not es_similar_etiqueta(linea_upper, "EXPEDICION", 70):
            continue
        for idx_cand in (i - 1, i - 2):
            if idx_cand < 0:
                continue
            candidato_norm = normalizar_texto(lineas[idx_cand].strip())
            m = PATRON_FECHA_LUGAR_EXP.search(candidato_norm)
            if m:
                return f"{m.group(1)} {m.group(2).strip()}"
    return None

def extraer_estatura_sexo_rh(lineas):
    """
    Estatura, grupo sanguineo (RH) y sexo aparecen como una fila de 3 valores
    seguida de una fila de 3 etiquetas (box-sorting del OCR), por lo que un
    lookback posicional no basta para saber cual valor pertenece a cual etiqueta.
    En vez de eso: se ubica una ventana alrededor de las lineas de etiqueta
    (ESTATURA/SEXO/RH) y dentro de esa ventana cada valor se identifica por su
    FORMA (estatura ~ "1.71", RH ~ "B+", sexo ~ "M"/"F" en una linea sola).
    Retorna (estatura, grupo_sanguineo, sexo), cualquiera puede ser None.
    """
    label_idxs = []
    for i, linea in enumerate(lineas):
        linea_upper = linea.upper().strip()
        # "G.S." (Grupo Sanguineo) es como algunas cedulas rotulan el RH. Al quitarle
        # los puntos quedan 2 letras, y es_similar_etiqueta ignora palabras de menos de
        # 4 -- por eso se compara aparte contra las formas cortas conocidas. Sin esto la
        # etiqueta no contaba y la ventana no llegaba hasta el valor "O+".
        letras = re.sub(r'[^A-ZÑ]', '', normalizar_texto(linea_upper))
        if (es_similar_etiqueta(linea_upper, "ESTATURA", 70)
                or es_similar_etiqueta(linea_upper, "SEXO", 70)
                or "RH" in normalizar_texto(linea_upper)
                or letras in ("GS", "GSRH", "RHGS")):
            label_idxs.append(i)

    if not label_idxs:
        return None, None, None

    # La ventana se abre 6 lineas a cada lado porque, segun la cedula, los valores caen
    # ARRIBA o ABAJO de sus etiquetas, y ademas van en bloques de tres
    # (Nacionalidad/Estatura/Sexo -> COL/1.62/F): hacia adelante hacen falta tantas
    # lineas como hacia atras. Con el margen de 3 que habia antes, el sexo quedaba
    # justo por fuera y se perdia.
    inicio = max(0, min(label_idxs) - 6)
    fin = min(len(lineas), max(label_idxs) + 6)

    estatura = grupo_sanguineo = sexo = None
    for linea in lineas[inicio:fin]:
        linea_norm = normalizar_texto(linea.strip())
        if estatura is None:
            m = PATRON_ESTATURA.search(linea_norm)
            if m:
                estatura = m.group(1)
        if grupo_sanguineo is None:
            m = PATRON_RH.search(linea_norm)
            if m:
                grupo_sanguineo = _normalizar_rh(m.group(1), m.group(2))
            else:
                # El mismo dato con el signo adelante ("+0"). Se prueba DESPUES de la
                # forma canonica para que esa siempre tenga prioridad.
                m = PATRON_RH_INVERTIDO.search(linea_norm)
                if m:
                    grupo_sanguineo = _normalizar_rh(m.group(2), m.group(1))
        if sexo is None:
            m = PATRON_SEXO.match(linea_norm)
            if m:
                sexo = m.group(1)

    return estatura, grupo_sanguineo, sexo

def _safe_replace_year(d, year):
    """Cumpleaños 29-feb: en un año no bisiesto cae al 28-feb."""
    try:
        return d.replace(year=year)
    except ValueError:
        return d.replace(year=year, day=28)

def calcular_edad(fecha_nacimiento_str):
    """
    Calcula la edad como "X años y Y días" a partir de una fecha en formato
    DD-MES-YYYY (el mismo que captura PATRON_FECHA_NAC). Retorna None si la
    fecha esta vacia, no se puede interpretar, o resulta invalida/futura.
    """
    if not fecha_nacimiento_str or str(fecha_nacimiento_str).strip().lower() == "none":
        return None

    m = PATRON_FECHA_PARSEABLE.search(normalizar_texto(str(fecha_nacimiento_str)))
    if not m:
        return None

    dia_s, mes_abbr, anio_s = m.groups()
    mes = MESES_ES.get(mes_abbr[:3])
    if not mes:
        return None

    try:
        nacimiento = date(int(anio_s), mes, int(dia_s))
    except ValueError:
        return None

    hoy = date.today()
    if nacimiento > hoy:
        return None

    ultimo_cumple = _safe_replace_year(nacimiento, hoy.year)
    years = hoy.year - nacimiento.year
    if ultimo_cumple > hoy:
        years -= 1
        ultimo_cumple = _safe_replace_year(nacimiento, hoy.year - 1)

    dias = (hoy - ultimo_cumple).days
    if dias == 0:
        return f"{years} años (cumple hoy)"
    return f"{years} años y {dias} días"

def extraer_nombre_mrz(texto):
    """
    Intenta extraer el nombre desde la zona MRZ (Machine Readable Zone).
    Ejemplo: CARVAJAL<HOME<<JUAN<SEBASTIAN< → CARVAJAL HOME / JUAN SEBASTIAN
    """
    for linea in texto.split('\n'):
        linea = linea.strip()
        # La línea MRZ de nombre tiene << como separador apellidos/nombres
        match = PATRON_MRZ_NOMBRE.match(linea)
        if match:
            apellidos = match.group(1).replace('<', ' ').strip()
            nombres = match.group(2).replace('<', ' ').strip()
            nombre_completo = f"{apellidos} {nombres}".strip()
            if len(nombre_completo) > 4:
                return nombre_completo
    return None

def extraer_datos_texto(texto):
    """
    Analiza el texto mediante expresiones regulares para extraer campos clave.
    Prioriza la extracción por MRZ (Machine Readable Zone) y usa heurística como fallback.
    Retorna un dict con todos los campos detectados (None cuando no se pudo detectar).
    """
    lineas = [linea.strip() for linea in texto.split('\n') if linea.strip()]

    # Detectar la cara y el tipo de documento
    cara = detectar_cara_cedula(texto)
    tipo_documento = detectar_tipo_documento(texto)

    # 1. Documento
    documento = extraer_documento(texto)

    # 2. Fecha de nacimiento
    fecha_nacimiento = extraer_fecha_nacimiento(texto, lineas)

    # 3. Nombre — Sistema de 3 prioridades
    nombre_completo = ""

    # PRIORIDAD 1: MRZ (Machine Readable Zone) — la fuente más confiable
    nombre_mrz = extraer_nombre_mrz(texto)
    if nombre_mrz:
        nombre_completo = nombre_mrz.upper()

    # PRIORIDAD 2: Contexto posicional (como las apps de escáner)
    # Se calcula SIEMPRE, incluso si el MRZ ya dio un nombre, porque sirve para detectar
    # un MRZ truncado (ver mas abajo). Es barato: solo recorre las lineas ya cargadas.
    if True:
        apellidos_encontrado = ""
        nombres_encontrado = ""

        for i, linea in enumerate(lineas):
            linea_upper = linea.upper().strip()

            # Detectar etiqueta "Apellidos" (dinámico por similitud >= 70%)
            is_apellidos_label = es_similar_etiqueta(linea_upper, "APELLIDOS", threshold=70)
            if is_apellidos_label:
                apellidos_encontrado = _valor_junto_a_etiqueta(lineas, i) or apellidos_encontrado

            # Detectar etiqueta "Nombres" (dinámico por similitud >= 70%)
            is_nombres_label = es_similar_etiqueta(linea_upper, "NOMBRES", threshold=70)
            if is_nombres_label and not is_apellidos_label and "FECHA" not in linea_upper:
                # El apellido ya asignado no puede volver a salir como nombre: la etiqueta
                # "Nombres" tiene el bloque de apellidos justo encima y seria su primer
                # candidato.
                nombres_encontrado = _valor_junto_a_etiqueta(
                    lineas, i, ya_tomado=apellidos_encontrado) or nombres_encontrado

        # Exigimos la presencia de AMBOS campos para dar por válida la Prioridad 2 (posicional).
        # Si falta alguno de los dos, es seguro que el OCR omitió una de las etiquetas;
        # en ese caso es mucho más robusto usar el fallback (Prioridad 3) para juntar ambas partes.
        nombre_posicional = ""
        if apellidos_encontrado and nombres_encontrado:
            nombre_posicional = f"{apellidos_encontrado} {nombres_encontrado}".strip()

        if not nombre_completo:
            nombre_completo = nombre_posicional
        elif _impreso_le_gana_al_mrz(nombre_posicional, nombre_completo):
            # El MRZ gana casi siempre, pero cuando el campo impreso dice EL MISMO nombre
            # y lo dice mejor (completo, o con la enie y las tildes que el MRZ no lleva),
            # gana el impreso. Ver _impreso_le_gana_al_mrz.
            nombre_completo = nombre_posicional

    # PRIORIDAD 3: Heurística por eliminación (último recurso)
    if not nombre_completo:
        posibles_nombres = []
        # Indices de las lineas pegadas a una etiqueta de Apellidos/Nombres. Cuando el
        # OCR destroza UNA de las dos etiquetas, la Prioridad 2 se cae entera aunque la
        # otra si se haya leido -- pero esa que sobrevivio sigue marcando donde estan el
        # apellido y el nombre (uno arriba y otro abajo). Dandoles preferencia se evita
        # quedarse con basura que aparezca antes en la pagina: en un caso real el nombre
        # salia "DUMLRO AYALA CASTRO" ("DUMLRO" era un "NUMERO" mal leido) y se perdia
        # el "JOSE WALTER" que estaba justo debajo de la etiqueta de apellidos.
        vecinas_a_etiqueta = set()
        for i, linea in enumerate(lineas):
            linea_upper = linea.upper().strip()
            if (es_similar_etiqueta(linea_upper, "APELLIDOS", threshold=70)
                    or es_similar_etiqueta(linea_upper, "NOMBRES", threshold=70)):
                for vecino in (i - 1, i + 1):
                    if 0 <= vecino < len(lineas):
                        vecinas_a_etiqueta.add(vecino)

        for idx, linea in enumerate(lineas):
            linea_upper = linea.upper()
            if "DETECTADO" in linea_upper or linea.startswith("---") or "<<" in linea:
                continue
            if _PATRON_KEYWORDS_EXCLUIR.search(linea_upper):
                continue
            # Filtrar dinámicamente cualquier etiqueta o palabra clave de la plantilla de la cédula
            if es_similar_a_labels_tarjeta(linea_upper, threshold=70):
                continue

            # Filtro de Línea Anterior (Contexto): si alguna de las 1-2 líneas anteriores es
            # una etiqueta de metadato, esta línea es el valor de ese metadato (como
            # estatura, sexo, o el nombre IMPRESO del registrador/firmante -- que no es el
            # nombre del titular de la cédula) y no un nombre. Se mira hasta 2 líneas atrás
            # porque el nombre del registrador suele venir en 2 líneas seguidas (nombres +
            # apellidos) despues de su etiqueta. Ademas de la lista de palabras clave exactas,
            # tambien se prueba es_ruido_de_plantilla en cada línea anterior -- si el OCR
            # tambien garabateo la ETIQUETA (p. ej. "REGISTRADOR" leido como "PEGISTRADOR"),
            # la comparacion exacta de abajo no la reconoceria como etiqueta y dejaria pasar
            # el nombre del registrador como si fuera el del titular.
            if idx - 1 >= 0:
                lineas_prev = [lineas[idx - 1]] + ([lineas[idx - 2]] if idx - 2 >= 0 else [])
                if any(es_ruido_de_plantilla(lp, frases_compactas=_FRASES_FIRMA_REGISTRADOR_COMPACTAS) for lp in lineas_prev):
                    continue
                prev_linea_norm = normalizar_texto(lineas[idx-1])
                if any(k in prev_linea_norm for k in ["LUGAR", "NACIMIENTO", "FECHA", "EXPEDICION", "EXPEDICIÓ", "ESTATURA", "SEXO", "REGISTRADOR", "FIRMA"]):
                    continue

            digits = sum(c.isdigit() for c in linea)
            if digits > len(linea) * 0.3:
                continue
            if not _es_candidato_mayusculas_plantilla(linea):
                continue
            linea_limpia = re.sub(r'[^A-Za-zÁÉÍÓÚñÑ ]', '', linea).strip().upper()
            linea_limpia = re.sub(r'\s+', ' ', linea_limpia).strip()
            if len(linea_limpia) > 4 and linea_limpia not in ("DE", "EL", "LA", "LOS", "DEL", "GM", "COL", "GS"):
                posibles_nombres.append((idx, linea_limpia))

        # Si alguna etiqueta de Apellidos/Nombres sobrevivio al OCR, se usan solo sus
        # vecinas; si ninguna quedo legible, se cae al comportamiento de siempre (las
        # dos primeras candidatas de la pagina).
        junto_a_etiqueta = [t for i, t in posibles_nombres if i in vecinas_a_etiqueta]
        elegidas = junto_a_etiqueta if len(junto_a_etiqueta) >= 2 else [t for _, t in posibles_nombres]
        nombre_completo = " ".join(elegidas[:2]).strip() if elegidas else ""

    # 4. Campos adicionales (lugar de nacimiento, expedición, estatura/RH/sexo, edad calculada)
    lugar_nacimiento = extraer_lugar_nacimiento(lineas, nombre_completo)
    fecha_lugar_expedicion = extraer_fecha_lugar_expedicion(lineas)
    estatura, grupo_sanguineo, sexo = extraer_estatura_sexo_rh(lineas)
    if not sexo:
        # El campo impreso no quedo legible, pero la zona MRZ del reverso trae el sexo
        # en una posicion fija y es mucho mas resistente al ruido del OCR.
        match_mrz_sexo = PATRON_MRZ_SEXO.search(texto)
        if match_mrz_sexo:
            sexo = match_mrz_sexo.group(1)
    edad = calcular_edad(fecha_nacimiento)

    return {
        "documento": documento,
        "nombre_completo": nombre_completo,
        "fecha_nacimiento": fecha_nacimiento,
        "cara": cara,
        "tipo_documento": tipo_documento,
        "lugar_nacimiento": lugar_nacimiento,
        "sexo": sexo,
        "estatura": estatura,
        "grupo_sanguineo": grupo_sanguineo,
        "fecha_lugar_expedicion": fecha_lugar_expedicion,
        "edad": edad,
    }
