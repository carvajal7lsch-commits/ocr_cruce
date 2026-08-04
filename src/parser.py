import re
import difflib

# Regex compilados una sola vez a nivel de módulo
PATRON_DOC_BARCODE = re.compile(r'-(\d{6,10})-\d{8}\b')
PATRON_DOC_GENERICO = re.compile(r'\b\d{1,3}(?:\.?\d{3}){2,3}\b')
PATRON_FECHA = re.compile(r'\b\d{2}[-/\.]\d{2}[-/\.]\d{4}\b')
# Fecha de nacimiento viene como "26-JUN-1986" o "26 NOV 1986" (mes abreviado, permite espacio/guión/barra opcionales)
PATRON_FECHA_NAC = re.compile(
    r'FECHA DE NACIMIENTO\D{0,15}(\d{2}[- /]?[A-ZÁÉÍÓÚ]{3}[- /]?\d{4})',
    re.IGNORECASE
)

KEYWORDS_EXCLUIR = (
    "REPÚBLICA", "REPUBLICA", "CEDULA", "CÉDULA", "CIUDADANÍA", "CIUDADANIA",
    "NACIMIENTO", "SEXO", "ESTATURA", "IDENTIFICACION", "IDENTIFICACIÓN",
    "REGISTRADOR", "EXPEDICION", "EXPEDICIÓN", "CAMSCANNER", "PERSONAL",
    "POWERED", "COLOMBIA", "FECHA", "LUGAR", "FIRMA", "HUELLA",
    "NACIONALIDAD", "EXPIRACIÓN", "EXPIRACION", "NUIP", "ICCOL",
    "DIC", "ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV",
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

def es_similar_a_labels_tarjeta(texto_linea, threshold=70):
    """
    Verifica si alguna palabra de la línea es matemáticamente similar a las palabras
    clave de la plantilla de la cédula o departamentos de Colombia para descartar ruido.
    """
    texto_norm = normalizar_texto(texto_linea)
    palabras = [re.sub(r'[^A-ZÑ]', '', w) for w in texto_norm.split() if w.strip()]
    labels_control = [
        "REPUBLICA", "COLOMBIA", "IDENTIFICACION", "PERSONAL", 
        "CEDULA", "CIUDADANIA", "APELLIDOS", "NOMBRES", 
        "NACIMIENTO", "REGISTRADOR", "EXPEDICION",
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
    """
    lineas = [linea.strip() for linea in texto.split('\n') if linea.strip()]

    documento = None
    fecha_nacimiento = None
    
    # Detectar la cara de la cédula
    cara = detectar_cara_cedula(texto)

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
            documento = match_generico.group(0).replace('.', '')

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
                                if candidato_upper != apellidos_encontrado:
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
                
            # Filtro de Línea Anterior (Contexto): si la línea anterior es una etiqueta de metadato,
            # esta línea es el valor del metadato (como estatura, sexo, registrador o ubicación) y no un nombre.
            if idx - 1 >= 0:
                prev_linea_norm = normalizar_texto(lineas[idx-1])
                if any(k in prev_linea_norm for k in ["LUGAR", "NACIMIENTO", "FECHA", "EXPEDICION", "EXPEDICIÓ", "ESTATURA", "SEXO", "REGISTRADOR", "FIRMA"]):
                    continue
                
            digits = sum(c.isdigit() for c in linea)
            if digits > len(linea) * 0.3:
                continue
            linea_limpia = re.sub(r'[^A-Za-zÁÉÍÓÚñÑ ]', '', linea).strip().upper()
            linea_limpia = re.sub(r'\s+', ' ', linea_limpia).strip()
            if len(linea_limpia) > 4 and linea_limpia not in ("DE", "EL", "LA", "LOS", "DEL", "GM", "COL", "GS"):
                posibles_nombres.append(linea_limpia)
        nombre_completo = " ".join(posibles_nombres[:2]).strip() if posibles_nombres else ""
        
    return documento, nombre_completo, fecha_nacimiento, cara
