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
from PyInstaller.utils.hooks import collect_data_files

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

a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
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
    console=True,
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
