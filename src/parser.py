import re
import difflib
from datetime import date
from rapidfuzz import fuzz

# Regex compilados una sola vez a nivel de módulo
PATRON_DOC_BARCODE = re.compile(r'-(\d{6,10})-\d{8}\b')
PATRON_DOC_GENERICO = re.compile(r'\b\d{1,3}(?:\.?\d{3}){2,3}\b')
PATRON_FECHA = re.compile(r'\b\d{2}[-/\.]\d{2}[-/\.]\d{4}\b')
# Fecha de nacimiento viene como "26-JUN-1986" o "26 NOV 1986" (mes abreviado, permite espacio/guión/barra opcionales)
PATRON_FECHA_NAC = re.compile(
    r'FECHA DE NACIMIENTO\D{0,15}(\d{2}[- /]?[A-ZÁÉÍÓÚ]{3}[- /]?\d{4})',
    re.IGNORECASE
)

# Tipo de documento: se busca la frase de encabezado completa, en orden de especificidad
# (tarjeta/extranjeria antes que ciudadania para no dar por sentado el caso mas comun).
PATRON_TIPO_TARJETA = re.compile(r'TARJETA\s+DE\s+IDENTIDAD', re.IGNORECASE)
PATRON_TIPO_EXTRANJERIA = re.compile(r'C[EÉ]DULA\s+DE\s+EXTRANJER[IÍ]A', re.IGNORECASE)
PATRON_TIPO_CIUDADANIA = re.compile(r'C[EÉ]DULA\s+DE\s+CIUDADAN[IÍ]A', re.IGNORECASE)
# Documento provisional que expide la Registraduría mientras se entrega la cédula física.
PATRON_TIPO_CONTRASENA = re.compile(r'CONTRASE[ÑN]A', re.IGNORECASE)

# Fecha y lugar de expedicion: "15-OCT-2004 FLORENCIA"
PATRON_FECHA_LUGAR_EXP = re.compile(
    r'(\d{2}[-\s/]?[A-ZÁÉÍÓÚ]{3}[-\s/]?\d{4})\s+([A-ZÁÉÍÓÚÑ ]{3,})'
)

# Estatura / Grupo sanguineo (RH) / Sexo: se identifican por FORMA de contenido, no por
# posicion, porque en la plantilla de la cedula estos tres valores aparecen en una fila
# seguidos de sus tres etiquetas en la fila siguiente (box-sorting del OCR), asi que un
# lookback de una sola linea no permite saber con certeza cual valor es cual.
PATRON_ESTATURA = re.compile(r'\b([12]\.\d{2})\b')             # "1.71" (acotado para no matchear "1.117...")
PATRON_RH = re.compile(r'\b(AB|A|B|O)[+-](?!\w)')              # "B+" (AB antes que A/B; sin \b final porque
                                                                 # "+"/"-" al final de línea no genera boundary)
PATRON_SEXO = re.compile(r'^(M|F)$')                            # solo si la linea COMPLETA es "M" o "F"

# Fecha de nacimiento -> edad calculada
MESES_ES = {
    "ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DIC": 12,
}
PATRON_FECHA_PARSEABLE = re.compile(r'(\d{2})[-\s/]?([A-Z]{3})[-\s/]?(\d{4})')

KEYWORDS_EXCLUIR = (
    "REPÚBLICA", "REPUBLICA", "CEDULA", "CÉDULA", "CIUDADANÍA", "CIUDADANIA",
    "NACIMIENTO", "SEXO", "ESTATURA", "IDENTIFICACION", "IDENTIFICACIÓN",
    "REGISTRADOR", "EXPEDICION", "EXPEDICIÓN", "CAMSCANNER", "PERSONAL",
    "POWERED", "SCANNED", "SCANNER", "COLOMBIA", "FECHA", "LUGAR", "FIRMA", "HUELLA",
    "NACIONALIDAD", "EXPIRACIÓN", "EXPIRACION", "NUIP", "ICCOL",
    "DIC", "ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV",
    "ESTADO", "CIVIL", "NUMERO", "NÚMERO",
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

def es_similar_etiqueta(texto_linea, target_label, threshold=70):
    """
    Compara matemáticamente cada palabra de la línea con una etiqueta objetivo
    usando la librería estándar difflib (Gestor de secuencias).
    Retorna True si hay una coincidencia de similitud >= threshold.
    """
    texto_norm = normalizar_texto(texto_linea)
    # Remover caracteres especiales y separar en palabras limpias
    palabras = [re.sub(r'[^A-ZÑ]', '', w) for w in texto_norm.split() if w.strip()]
    for p in palabras:
        if len(p) >= 4:
            # diflib calcula un ratio de coincidencia entre 0.0 y 1.0
            ratio = difflib.SequenceMatcher(None, p, target_label).ratio() * 100
            if ratio >= threshold:
                return True
    return False

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

    for i, linea in enumerate(lineas):
        linea_upper = linea.upper().strip()
        if not (es_similar_etiqueta(linea_upper, "LUGAR", 70) and es_similar_etiqueta(linea_upper, "NACIMIENTO", 70)):
            continue
        candidatos = []
        for idx_cand in (i - 2, i - 1):
            if idx_cand < 0:
                continue
            candidato = lineas[idx_cand].strip()
            candidato_norm = normalizar_texto(candidato)
            if len(candidato) < 2:
                continue
            if any(kw in candidato_norm for kw in ("FECHA", "NUMERO", "APELLIDOS", "NOMBRES", "NACIMIENTO", "ESTATURA", "SEXO")):
                continue
            digitos = sum(c.isdigit() for c in candidato)
            if digitos > len(candidato) * 0.5:
                continue
            if nombre_norm and candidato_norm in nombre_norm:
                continue
            candidatos.append(candidato.upper())
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
        if (es_similar_etiqueta(linea_upper, "ESTATURA", 70)
                or es_similar_etiqueta(linea_upper, "SEXO", 70)
                or "RH" in normalizar_texto(linea_upper)):
            label_idxs.append(i)

    if not label_idxs:
        return None, None, None

    inicio = max(0, min(label_idxs) - 6)
    fin = min(len(lineas), max(label_idxs) + 3)

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
                grupo_sanguineo = m.group(0)
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

    documento = None
    fecha_nacimiento = None

    # Detectar la cara y el tipo de documento
    cara = detectar_cara_cedula(texto)
    tipo_documento = detectar_tipo_documento(texto)

    # 1. Documento
    matches_barcode = list(PATRON_DOC_BARCODE.finditer(texto))
    if matches_barcode:
        try:
            documento = str(int(matches_barcode[-1].group(1)))  # normaliza y quita ceros a la izquierda
        except ValueError:
            documento = matches_barcode[-1].group(1)
    else:
        match_generico = PATRON_DOC_GENERICO.search(texto)
        if match_generico:
            candidato = match_generico.group(0).replace('.', '')
            # Una cedula/NUIP colombiana nunca empieza en cero. Si el candidato trae
            # un cero a la izquierda, casi seguro es otra cosa (un sello, un codigo de
            # registro) que por casualidad tiene forma de grupos de 3 digitos -- mejor
            # dejar el documento sin detectar que asignarle un numero equivocado.
            if not candidato.startswith('0'):
                documento = candidato

    # 2. Fecha de nacimiento
    match_fecha_nac = PATRON_FECHA_NAC.search(texto)
    if match_fecha_nac:
        fecha_nacimiento = match_fecha_nac.group(1)
    else:
        match_fecha = PATRON_FECHA.search(texto)
        if match_fecha:
            fecha_nacimiento = match_fecha.group(0)

    # 3. Nombre — Sistema de 3 prioridades
    nombre_completo = ""

    # PRIORIDAD 1: MRZ (Machine Readable Zone) — la fuente más confiable
    nombre_mrz = extraer_nombre_mrz(texto)
    if nombre_mrz:
        nombre_completo = nombre_mrz.upper()

    # PRIORIDAD 2: Contexto posicional (como las apps de escáner)
    if not nombre_completo:
        apellidos_encontrado = ""
        nombres_encontrado = ""

        for i, linea in enumerate(lineas):
            linea_upper = linea.upper().strip()

            # Detectar etiqueta "Apellidos" (dinámico por similitud >= 70%)
            is_apellidos_label = es_similar_etiqueta(linea_upper, "APELLIDOS", threshold=70)
            if is_apellidos_label:
                # Buscar candidato a apellido: primero intentamos las líneas anteriores y luego las siguientes
                candidatos_indices = []
                if i - 1 >= 0: candidatos_indices.append(i - 1)
                if i - 2 >= 0: candidatos_indices.append(i - 2)
                if i + 1 < len(lineas): candidatos_indices.append(i + 1)
                if i + 2 < len(lineas): candidatos_indices.append(i + 2)

                for idx_cand in candidatos_indices:
                    candidato = re.sub(r'[^A-Za-zÁÉÍÓÚñÑ ]', '', lineas[idx_cand]).strip()
                    candidato_upper = normalizar_texto(candidato)
                    # Evitar asociar etiquetas o palabras clave
                    if len(candidato) > 2 and not any(kw in candidato_upper for kw in ["NOMBRE", "NUIP", "NUMERO", "REPUBLICA", "FECHA"]):
                        if not es_similar_etiqueta(candidato_upper, "APELLIDOS", threshold=70) and not es_similar_etiqueta(candidato_upper, "NOMBRES", threshold=70):
                            if not es_similar_a_labels_tarjeta(candidato_upper, threshold=70):
                                if _es_candidato_mayusculas_plantilla(candidato):
                                    apellidos_encontrado = candidato_upper
                                    break

            # Detectar etiqueta "Nombres" (dinámico por similitud >= 70%)
            is_nombres_label = es_similar_etiqueta(linea_upper, "NOMBRES", threshold=70)
            if is_nombres_label and not is_apellidos_label and "FECHA" not in linea_upper:
                candidatos_indices = []
                if i - 1 >= 0: candidatos_indices.append(i - 1)
                if i - 2 >= 0: candidatos_indices.append(i - 2)
                if i + 1 < len(lineas): candidatos_indices.append(i + 1)
                if i + 2 < len(lineas): candidatos_indices.append(i + 2)

                for idx_cand in candidatos_indices:
                    candidato = re.sub(r'[^A-Za-zÁÉÍÓÚñÑ ]', '', lineas[idx_cand]).strip()
                    candidato_upper = normalizar_texto(candidato)
                    # Evitar asociar etiquetas o palabras clave
                    if len(candidato) > 2 and not any(kw in candidato_upper for kw in ["NACIONALIDAD", "ESTATURA", "SEXO", "FECHA", "COL", "NACIMIENTO"]):
                        if not es_similar_etiqueta(candidato_upper, "APELLIDOS", threshold=70) and not es_similar_etiqueta(candidato_upper, "NOMBRES", threshold=70):
                            if not es_similar_a_labels_tarjeta(candidato_upper, threshold=70):
                                if candidato_upper != apellidos_encontrado and _es_candidato_mayusculas_plantilla(candidato):
                                    nombres_encontrado = candidato_upper
                                    break

        # Exigimos la presencia de AMBOS campos para dar por válida la Prioridad 2 (posicional).
        # Si falta alguno de los dos, es seguro que el OCR omitió una de las etiquetas;
        # en ese caso es mucho más robusto usar el fallback (Prioridad 3) para juntar ambas partes.
        if apellidos_encontrado and nombres_encontrado:
            nombre_completo = f"{apellidos_encontrado} {nombres_encontrado}".strip()

    # PRIORIDAD 3: Heurística por eliminación (último recurso)
    if not nombre_completo:
        posibles_nombres = []
        for idx, linea in enumerate(lineas):
            linea_upper = linea.upper()
            if "DETECTADO" in linea_upper or linea.startswith("---") or "<<" in linea:
                continue
            if any(keyword in linea_upper for keyword in KEYWORDS_EXCLUIR):
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
                posibles_nombres.append(linea_limpia)
        nombre_completo = " ".join(posibles_nombres[:2]).strip() if posibles_nombres else ""

    # 4. Campos adicionales (lugar de nacimiento, expedición, estatura/RH/sexo, edad calculada)
    lugar_nacimiento = extraer_lugar_nacimiento(lineas, nombre_completo)
    fecha_lugar_expedicion = extraer_fecha_lugar_expedicion(lineas)
    estatura, grupo_sanguineo, sexo = extraer_estatura_sexo_rh(lineas)
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
