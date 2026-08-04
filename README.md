# Auditoría y Verificación de Cédulas (PDF OCR vs. Excel) 🆔🔍

Esta es una aplicación web interactiva desarrollada con **Streamlit**, **Pandas**, **EasyOCR** y **pdf2image** para auditar y verificar documentos de identidad (cédulas) extraídos de un archivo PDF frente a una base de datos estructurada en Excel.

---

## 🚀 Requisitos de Instalación

### 1. Dependencias de Sistema (Obligatorio)

Para que la librería `pdf2image` pueda convertir las páginas del PDF a imágenes, requiere tener instalado **Poppler** en el sistema:

#### **Windows:**
1. Descarga la última versión de Poppler para Windows (por ejemplo, desde [GitHub Releases de poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases/)).
2. Extrae el archivo ZIP en una ruta de tu preferencia (ej: `C:\Program Files\poppler`).
3. Agrega la ruta de la carpeta `bin` (ej: `C:\Program Files\poppler\bin`) a las variables de entorno de tu sistema (`PATH`), **o cópiala y pégala en el campo correspondiente en la barra lateral de la aplicación.**

#### **Linux (Ubuntu/Debian):**
```bash
sudo apt-get install poppler-utils
```

#### **macOS (Homebrew):**
```bash
brew install poppler
```

---

### 2. Dependencias de Python (Pip)

Instala todos los paquetes necesarios ejecutando el siguiente comando en tu terminal:

```bash
pip install -r requirements.txt
```

*Nota: Al iniciar la validación por primera vez, `easyocr` descargará automáticamente el modelo de detección y reconocimiento de texto en español (esto demorará unos segundos).*

---

## 🏃‍♂️ Cómo Ejecutar la Aplicación

Una vez instaladas las dependencias, arranca el servidor web interactivo:

```bash
python -m streamlit run app.py
```

La aplicación se cargará automáticamente en tu navegador predeterminado (por defecto en `http://localhost:8501`).

---

## 🛡️ Características Principales
- **Cruce por Cédula Inteligente:** Extrae números de cédula utilizando expresiones regulares sobre los resultados del OCR y busca coincidencias en la columna llave del Excel.
- **Fuzzy Matching (Comparación Difusa):** Valida nombres, apellidos u otros campos usando coincidencia Levenshtein (`rapidfuzz`) tolerando errores leves de tilde, formato o escaneo.
- **Reporte Descargable:** Genera un archivo Excel (`Reporte_Auditoria_OCR.xlsx`) formateado profesionalmente con semáforos de estado (Correcto, Alertas de discrepancia y Faltantes).
