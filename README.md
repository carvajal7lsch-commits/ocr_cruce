# Auditoría de Cédulas — OCR vs Excel 🆔🔍

### ⬇️ [**Descargar para Windows**](https://github.com/carvajal7lsch-commits/ocr_cruce/releases/download/v1.1.1/AuditoriaCedulas-Windows.zip)

No necesitas instalar Python, ni poppler, ni saber programar: descarga el `.zip` del
link de arriba, descomprímelo, y haz doble clic en `AuditoriaCedulas.exe`. Se abre en
su propia ventana. Eso es todo.

---

Aplicación web para auditar documentos de identidad colombianos (cédulas, tarjetas de
identidad, cédulas de extranjería, contraseñas) escaneados en un PDF contra una base
de datos en Excel: lee cada página con OCR, cruza los datos contra el Excel por
documento y por nombre (con coincidencia difusa), y genera un reporte descargable con
los resultados clasificados en Verificados, Alertas, Faltantes y Huérfanos.

![Consola en tiempo real](docs/screenshot-consola.png)
![Conciliación e informe](docs/screenshot-resultados.png)

## ✨ Qué hace

- **OCR en lote**: procesa todas las páginas de un PDF en paralelo, con texto embebido
  (PyMuPDF) cuando el PDF ya lo trae y OCR real (RapidOCR) cuando no.
- **Extracción estructurada**: documento, nombre, fecha de nacimiento, tipo de
  documento (CC/TI/CE/Contraseña), lugar de nacimiento, sexo, estatura, RH y fecha/lugar
  de expedición — vía zona MRZ, posición de etiquetas en la plantilla, y un heurístico
  de respaldo, con filtros específicos para no confundir ruido de la plantilla
  (encabezados, firmas, nombre del registrador) con el nombre del titular.
- **Conciliación difusa contra Excel**: detecta automáticamente la fila de encabezado y
  las columnas de documento/nombre, y compara con `rapidfuzz` tolerando reordenamientos,
  palabras fusionadas por el OCR y nombres incompletos.
- **Fusión automática de páginas huérfanas**: cuando el anverso y el reverso de una
  misma cédula quedan separados porque una cara no dejó leer el número de documento,
  el sistema las reconecta solo por similitud de nombre — con una salvaguarda de
  cercanía de página para no arriesgarse a mezclar a dos personas distintas.
- **Consola en tiempo real** con vista del documento analizado, tarjetas editables,
  buscador por cédula/nombre y filtro de sugerencias pendientes.
- **Historial persistente** (SQLite) para reabrir auditorías anteriores sin volver a
  correr el OCR.
- **Caché de OCR por contenido**: volver a subir un PDF ya procesado reusa el escaneo en
  vez de repetirlo, aunque el archivo se haya renombrado o venga de otra carpeta (la
  clave es el hash del contenido, no el nombre). Las correcciones hechas a mano también
  quedan guardadas ahí. Borrar la auditoría del historial borra su caché.
- **Reporte Excel** formateado por categoría, con el texto OCR crudo incluido para
  poder diagnosticar casos raros sin tener que re-procesar.
- **Modo claro/oscuro.**

## 🧱 Stack

Backend: FastAPI + Uvicorn · OCR: RapidOCR (ONNX) + PyMuPDF · PDF→imagen: pdf2image
(poppler) · Datos: pandas, openpyxl · Coincidencia difusa: rapidfuzz · Persistencia:
SQLite · Frontend: HTML/CSS/JS sin build step (sin frameworks) · Ventana de escritorio:
pywebview (WebView2) · Empaquetado: PyInstaller vía GitHub Actions.

---

## 🚀 Cómo correrlo

### Opción A — Ejecutable de Windows (sin instalar nada)

Es el botón de descarga de arriba. Un par de detalles adicionales:

- Al descomprimir, hay que mantener la carpeta `AuditoriaCedulas/` completa junta
  (no mover solo el `.exe` a otro lado, necesita los archivos de al lado para
  funcionar).
- Se abre en su propia ventana, como cualquier programa. **Para cerrarla, la X de la
  ventana** — o el botón *Salir* de la barra superior. No queda nada corriendo detrás.
- Los modelos de OCR ya vienen incluidos, así que no hace falta internet para usarlo.
- Busca un puerto libre sola a partir del 8000, así que no importa si ya tienes algo
  ocupándolo. Para forzar uno concreto: `set PORT=8100 && AuditoriaCedulas.exe`.
- Si algo falla, queda un registro en
  `%LOCALAPPDATA%\AuditoriaCedulas\AuditoriaCedulas.log` — es lo primero que hay que
  mirar (y lo que conviene adjuntar si vas a reportar un problema).

#### 🛡️ Si tu antivirus o Windows te alerta

Es esperable, y conviene saber por qué: el ejecutable **no está firmado digitalmente**
(un certificado de firma de código cuesta dinero y esto es un proyecto abierto). Además
está empaquetado con PyInstaller, que es la misma herramienta que usa bastante software
malicioso, así que varios antivirus marcan el *empaquetador* sin mirar el contenido.

Lo que puedes hacer para verificarlo por tu cuenta, en vez de creerme:

- **Todo el código fuente está en este repositorio.** No hay nada compilado a mano ni
  binarios metidos por ahí.
- **El `.exe` lo construye GitHub Actions**, no mi computador: sale del código que ves
  acá, con las dependencias de `requirements.txt` y nada más. El proceso completo está
  en [`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml) y el
  registro de cada compilación es público en la pestaña *Actions*.
- **Cada release publica el SHA-256** del `.zip`. Para comprobar que lo que bajaste es
  exactamente lo que salió de esa compilación:

  ```powershell
  Get-FileHash AuditoriaCedulas-Windows.zip -Algorithm SHA256
  ```

  Ese valor tiene que coincidir con el publicado en el release.

La aplicación funciona **100% local**: no envía los PDFs ni los datos de las cédulas a
ningún servidor. El servidor que levanta escucha solo en `127.0.0.1`, o sea que ni
siquiera es accesible desde otro equipo de la misma red.

### Opción B — Desde el código fuente (para desarrolladores)

Requiere **Python 3.8** y [Poppler](https://github.com/oschwartz10612/poppler-windows/releases)
instalado (o accesible por `PATH`, o apuntado con la variable de entorno
`POPPLER_PATH`).

> **Por qué 3.8 y no algo más nuevo:** OpenCV 5 exige `numpy>=2` a partir de Python 3.9
> (es una regla que trae en su propia metadata), y numpy 2 no es compatible con el
> `pandas 2.0.3` que usa este proyecto. Instalar con estos pines en 3.9+ no falla al
> ejecutar: pip aborta directamente por conflicto. Migrar es posible, pero arrastra
> pandas y obliga a re-validar la extracción de datos. Está anotado en
> `requirements.txt`.

```bash
# Windows / Linux / macOS
git clone <url-del-repo>
cd cruce_excel
pip install -r requirements.txt
python server.py
```

Se abre en `http://127.0.0.1:8000`. En Linux/macOS instala poppler con tu gestor de
paquetes (`apt install poppler-utils` / `brew install poppler`) antes de correrlo.

---

## 🛠️ Construir el ejecutable

### Automático (lo que produce los releases)

Cada push a `main` compila el `.exe` en un runner limpio de Windows y lo deja como
artifact; un tag `vX.Y.Z` además publica el release con su SHA-256. Todo está en
[`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml).

El workflow no solo compila: **verifica que el paquete quedó completo** antes de
publicarlo — que poppler esté adentro, que estén los tres modelos de OCR, que esté
`onnxruntime` (sin él los modelos no se pueden ejecutar) y que esté PyMuPDF. Todas esas
comprobaciones existen porque en algún momento faltó alguna y el `.exe` salió roto
igual: arrancaba bien y fallaba recién al auditar.

### Manual (en tu máquina)

```bash
pip install pyinstaller==6.22.2
pyinstaller AuditoriaCedulas.spec
```

El resultado queda en `dist/AuditoriaCedulas/` — esa carpeta completa (no solo el
`.exe`) es lo que hay que distribuir. El `.spec` bundlea el frontend y, si existe en
tu máquina, el `bin` de Poppler (variable `POPPLER_BIN_SOURCE` en el `.spec` para
apuntar a tu instalación).

> Los modelos de OCR **no vienen dentro del paquete de `rapidocr`**: se descargan la
> primera vez que se instancia el motor. Si compilas sin haberlo ejecutado nunca, el
> `.exe` sale sin modelos. Para forzar la descarga antes de compilar:
>
> ```bash
> python -c "import numpy as np; from rapidocr import RapidOCR; RapidOCR()(np.full((320,320,3),255,dtype=np.uint8))"
> ```

---

## 📁 Estructura

```
server.py           API (FastAPI) y orquestación del OCR
src/parser.py        Extracción de campos desde el texto OCR
src/ocr.py            Motor OCR (RapidOCR) y preprocesamiento de imagen
src/reporter.py       Generación del reporte Excel
src/db.py              Historial persistente (SQLite) y caché de OCR
static/                Frontend (HTML/CSS/JS, sin build step)
AuditoriaCedulas.spec   Receta de empaquetado (PyInstaller)
.github/workflows/       Compilación y publicación automáticas
```
