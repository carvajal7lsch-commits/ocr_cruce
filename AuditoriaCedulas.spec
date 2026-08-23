# -*- mode: python ; coding: utf-8 -*-
#
# Empaqueta la app como un ejecutable de escritorio (Windows) para que alguien sin
# conocimientos tecnicos pueda simplemente hacer doble clic y usarla -- sin instalar
# Python, sin abrir una terminal, sin instalar poppler por separado.
#
# Para reconstruirlo: pyinstaller AuditoriaCedulas.spec
# El resultado queda en dist/AuditoriaCedulas/ -- esa carpeta completa es lo que se
# distribuye (comprimida en un .zip); AuditoriaCedulas.exe es lo que el usuario abre.

import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None

POPPLER_BIN_SOURCE = os.environ.get("POPPLER_BIN_SOURCE", r"C:\poppler-26.02.0\Library\bin")

datas = [
    ("static/index.html", "static"),
    ("static/style.css", "static"),
    ("static/app.js", "static"),
]
if os.path.exists(POPPLER_BIN_SOURCE):
    datas.append((POPPLER_BIN_SOURCE, "poppler/bin"))

# rapidocr trae archivos que NO son .py (config.yaml, default_models.yaml, y los
# modelos .onnx en su carpeta models/) -- PyInstaller solo sigue imports de codigo por
# defecto, asi que sin esto el .exe arranca pero revienta al intentar inicializar el
# motor de OCR (FileNotFoundError buscando default_models.yaml). De paso, esto deja
# los modelos ya incluidos en el paquete -- no hace falta que el usuario final tenga
# internet la primera vez que corre la app.
datas += collect_data_files("rapidocr")

# PyInstaller arma el paquete siguiendo los "import" que encuentra LEYENDO el codigo, y
# hay dos que no puede ver:
#
#   - onnxruntime: rapidocr elige su motor de inferencia en tiempo de ejecucion, asi que
#     en el codigo no aparece ningun "import onnxruntime" que seguir. Resultado: el .exe
#     salia con los tres modelos .onnx adentro pero SIN el motor que los ejecuta, y todas
#     las paginas fallaban con "OCR no configurado correctamente".
#   - pymupdf: se importa como "fitz" dentro de un try/except, y aparte trae DLLs propias
#     que hay que arrastrar aparte.
#
# collect_all trae las tres cosas de cada paquete: datos, binarios y submodulos. Es mas
# pesado que listarlos a mano, pero no depende de adivinar cual submodulo hace falta.
#   - pythonnet / clr_loader: pywebview los usa para hablar con WebView2. Aparte del
#     Python.Runtime.dll necesitan su Python.Runtime.deps.json al lado; sin ese archivo
#     .NET carga la DLL pero no encuentra el punto de entrada y la ventana no abre
#     ("Failed to resolve Python.Runtime.Loader.Initialize"). Paso tal cual: el build
#     traia el .dll solo, y la app caia al navegador en todos los arranques.
binaries = []
hiddenimports = []
for paquete in ("onnxruntime", "pymupdf", "rapidocr", "pythonnet", "clr_loader"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(paquete)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports + [
        # src/ocr.py y server.py hacen "import fitz", que es el nombre historico del
        # modulo de PyMuPDF -- se declara explicito porque va dentro de un try/except.
        "fitz",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "multipart",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Estas librerias NO las usa la app (ni server.py ni src/*.py las importan) pero
    # quedan instaladas en el entorno de Python por otros proyectos, y el analisis
    # estatico de PyInstaller las arrastra igual -- entre las 4 sumaban ~370MB de los
    # ~800MB del build original. El OCR real corre sobre onnxruntime, no torch.
    excludes=["torch", "torchvision", "tensorflow", "pyarrow"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AuditoriaCedulas",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Modo ventana: sin consola negra al lado del navegador. La salida de diagnostico
    # no se pierde -- server.py la manda a %LOCALAPPDATA%/AuditoriaCedulas/ cuando
    # detecta que no hay consola (ver _redirigir_salida_a_archivo).
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AuditoriaCedulas",
)
