import sqlite3
import json
import sys
import threading
import datetime
from pathlib import Path

# Mismo criterio que BASE_DIR en server.py -- ver el comentario ahi para el detalle
# de por que es sys._MEIPASS y no sys.executable en un build --onedir de PyInstaller.
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys._MEIPASS)
else:
    _BASE_DIR = Path(__file__).resolve().parent.parent

DB_PATH = _BASE_DIR / "data" / "audits.db"
_write_lock = threading.Lock()

# Columnas que se listan directamente en el historial sin tener que parsear JSON
_LIST_COLUMNS = (
    "task_id", "created_at", "excel_filename", "pdf_filename",
    "correct_count", "alerts_count", "missing_count", "huerfanos_count",
    "elapsed_seconds"
)


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea la tabla si no existe. Idempotente, se llama una vez al arrancar el server."""
    with _write_lock, _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audits (
                task_id               TEXT PRIMARY KEY,
                created_at            TEXT NOT NULL,
                excel_filename        TEXT,
                pdf_filename           TEXT,
                key_col                TEXT,
                compare_cols_json      TEXT,
                similarity_threshold   INTEGER,
                start_page              INTEGER,
                end_page                INTEGER,
                correct_count            INTEGER DEFAULT 0,
                alerts_count             INTEGER DEFAULT 0,
                missing_count             INTEGER DEFAULT 0,
                huerfanos_count           INTEGER DEFAULT 0,
                results_json               TEXT,
                live_results_json          TEXT,
                report_bytes                 BLOB,
                pdf_url                       TEXT
            )
        """)
        # Cache de OCR por pagina. La clave es el CONTENIDO del PDF (sha256 de los bytes)
        # + los ajustes que cambian lo que lee el OCR (dpi y filtro de imagen) -- no el
        # nombre del archivo, para que renombrarlo o volver a subir el mismo PDF desde
        # otra carpeta igual pegue en el cache. El Excel NO entra en la clave: el OCR no
        # depende de el, asi que el mismo PDF cruzado contra otro Excel tambien reusa.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ocr_cache (
                pdf_hash    TEXT NOT NULL,
                pagina      INTEGER NOT NULL,
                dpi         INTEGER NOT NULL,
                img_filter  TEXT NOT NULL,
                data_json   TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (pdf_hash, pagina, dpi, img_filter)
            )
        """)

        # Migracion liviana: agrega la columna si la DB ya existia de antes de este cambio.
        # SQLite no soporta "ADD COLUMN IF NOT EXISTS", asi que se intenta y se ignora
        # el error si la columna ya esta.
        try:
            conn.execute("ALTER TABLE audits ADD COLUMN elapsed_seconds REAL")
        except sqlite3.OperationalError:
            pass
        # Cuantas paginas se escanearon DE VERDAD (las que no salieron del cache de OCR).
        # Es lo que hace comparable el tiempo de una corrida contra otra desde que existe
        # el cache -- ver segundos_por_pagina_historico.
        try:
            conn.execute("ALTER TABLE audits ADD COLUMN scanned_pages INTEGER")
        except sqlite3.OperationalError:
            pass
        # Clave del cache de OCR con la que se escaneo esta auditoria. Sin guardarla, al
        # borrar la auditoria del historial no habria forma de saber que filas de
        # ocr_cache le corresponden -- el cache va por CONTENIDO del PDF, no por task_id.
        for columna, tipo in (("pdf_hash", "TEXT"), ("pdf_dpi", "INTEGER"), ("img_filter", "TEXT")):
            try:
                conn.execute(f"ALTER TABLE audits ADD COLUMN {columna} {tipo}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def save_completed_audit(task_id, excel_filename, pdf_filename, key_col,
                          compare_cols, similarity_threshold, start_page, end_page,
                          metrics, results, live_results, report_bytes, pdf_url,
                          elapsed_seconds=None, scanned_pages=None,
                          pdf_hash=None, pdf_dpi=None, img_filter=None):
    with _write_lock, _connect() as conn:
        conn.execute("""
            INSERT INTO audits (
                task_id, created_at, excel_filename, pdf_filename, key_col,
                compare_cols_json, similarity_threshold, start_page, end_page,
                correct_count, alerts_count, missing_count, huerfanos_count,
                results_json, live_results_json, report_bytes, pdf_url, elapsed_seconds,
                scanned_pages, pdf_hash, pdf_dpi, img_filter
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                created_at=excluded.created_at, excel_filename=excluded.excel_filename,
                pdf_filename=excluded.pdf_filename, key_col=excluded.key_col,
                compare_cols_json=excluded.compare_cols_json,
                similarity_threshold=excluded.similarity_threshold,
                start_page=excluded.start_page, end_page=excluded.end_page,
                correct_count=excluded.correct_count, alerts_count=excluded.alerts_count,
                missing_count=excluded.missing_count, huerfanos_count=excluded.huerfanos_count,
                results_json=excluded.results_json, live_results_json=excluded.live_results_json,
                report_bytes=excluded.report_bytes, pdf_url=excluded.pdf_url,
                elapsed_seconds=excluded.elapsed_seconds,
                scanned_pages=excluded.scanned_pages,
                pdf_hash=excluded.pdf_hash, pdf_dpi=excluded.pdf_dpi,
                img_filter=excluded.img_filter
        """, (
            task_id, datetime.datetime.now().isoformat(), excel_filename, pdf_filename, key_col,
            json.dumps(compare_cols or []), similarity_threshold, start_page, end_page,
            metrics.get("correct", 0), metrics.get("alerts", 0),
            metrics.get("missing", 0), metrics.get("huerfanos", 0),
            json.dumps(results or {}), json.dumps(live_results or []),
            report_bytes, pdf_url, elapsed_seconds, scanned_pages,
            pdf_hash, pdf_dpi, img_filter
        ))
        conn.commit()


def segundos_por_pagina_historico(muestras=5):
    """
    Ritmo real de esta maquina: segundos por pagina, promediado sobre las ultimas
    auditorias completadas. Es lo que permite estimar el tiempo ANTES de arrancar sin
    inventar una constante -- un equipo lento y uno rapido dan numeros distintos, y el
    promedio se va afinando solo a medida que se usa la app.

    Solo se toman las ultimas 'muestras' auditorias (no todo el historial) para que el
    promedio siga al equipo actual y no quede arrastrado por corridas viejas hechas con
    otra configuracion. Retorna None si todavia no hay historial utilizable.

    Se divide por las paginas REALMENTE escaneadas (scanned_pages), no por el rango: una
    corrida que salio entera del cache de OCR tarda un segundo para 42 paginas, y contarla
    como 0.02 seg/pagina dejaria la estimacion de las proximas 5 corridas por el piso. Esas
    corridas directamente no aportan muestra. Las auditorias viejas (anteriores al cache)
    tienen scanned_pages en NULL y siguen usando el rango, que para ellas es correcto.
    """
    with _connect() as conn:
        filas = conn.execute("""
            SELECT elapsed_seconds, start_page, end_page, scanned_pages FROM audits
            WHERE elapsed_seconds IS NOT NULL
              AND start_page IS NOT NULL AND end_page IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ?
        """, (muestras,)).fetchall()

    ritmos = []
    for fila in filas:
        if fila["scanned_pages"] is None:
            paginas = (fila["end_page"] or 0) - (fila["start_page"] or 0) + 1
        else:
            paginas = fila["scanned_pages"]
        segundos = fila["elapsed_seconds"] or 0
        if paginas > 0 and segundos > 0:
            ritmos.append(segundos / paginas)

    if not ritmos:
        return None
    return sum(ritmos) / len(ritmos)


def list_audits(limit=50, offset=0):
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT {", ".join(_LIST_COLUMNS)} FROM audits
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
        return [dict(row) for row in rows]


def get_audit_detail(task_id):
    """
    Retorna un dict con la MISMA forma que el payload completado de
    GET /api/status/{task_id} (status, metrics, results, live_results, ...)
    para que el frontend reutilice renderResults()/renderExtractedCards() sin adaptar nada.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM audits WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        return {
            "status": "completed",
            "progress": 100,
            "pdf_url": row["pdf_url"],
            "created_at": row["created_at"],
            "excel_filename": row["excel_filename"],
            "pdf_filename": row["pdf_filename"],
            "metrics": {
                "correct": row["correct_count"],
                "alerts": row["alerts_count"],
                "missing": row["missing_count"],
                "huerfanos": row["huerfanos_count"],
            },
            "results": json.loads(row["results_json"] or "{}"),
            "live_results": json.loads(row["live_results_json"] or "[]"),
            "elapsed_seconds": row["elapsed_seconds"],
        }


def get_report_bytes(task_id):
    with _connect() as conn:
        row = conn.execute("SELECT report_bytes FROM audits WHERE task_id = ?", (task_id,)).fetchone()
        return row["report_bytes"] if row else None


def get_all_persisted_task_ids():
    with _connect() as conn:
        rows = conn.execute("SELECT task_id FROM audits").fetchall()
        return {row["task_id"] for row in rows}


def delete_audit(task_id):
    with _write_lock, _connect() as conn:
        cur = conn.execute("DELETE FROM audits WHERE task_id = ?", (task_id,))
        conn.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------------------
# Cache de OCR
# --------------------------------------------------------------------------------------

def get_cached_pages(pdf_hash, dpi, img_filter, start_page, end_page):
    """
    Devuelve {pagina: data} de las paginas de ESTE PDF que ya se escanearon antes con
    ESTOS mismos ajustes. Lo que no este aca hay que escanearlo.
    """
    with _connect() as conn:
        filas = conn.execute("""
            SELECT pagina, data_json FROM ocr_cache
            WHERE pdf_hash = ? AND dpi = ? AND img_filter = ? AND pagina BETWEEN ? AND ?
        """, (pdf_hash, dpi, img_filter, start_page, end_page)).fetchall()

    cacheadas = {}
    for fila in filas:
        try:
            cacheadas[fila["pagina"]] = json.loads(fila["data_json"])
        except (ValueError, TypeError):
            # Fila corrupta: se ignora en silencio y esa pagina se vuelve a escanear.
            continue
    return cacheadas


def save_cached_page(pdf_hash, dpi, img_filter, pagina, data):
    """Guarda (o pisa) el resultado de una pagina ya escaneada."""
    with _write_lock, _connect() as conn:
        conn.execute("""
            INSERT INTO ocr_cache (pdf_hash, pagina, dpi, img_filter, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(pdf_hash, pagina, dpi, img_filter) DO UPDATE SET
                data_json=excluded.data_json, updated_at=excluded.updated_at
        """, (pdf_hash, pagina, dpi, img_filter, json.dumps(data),
              datetime.datetime.now().isoformat()))
        conn.commit()


def touch_cache_ocr(pdf_hash, dpi, img_filter, start_page, end_page):
    """
    Marca como usadas las paginas que se acaban de reusar. Sin esto, purgar_cache_ocr
    mira solo la fecha de ESCRITURA y terminaria borrando justo el PDF que mas se usa
    (se lee todos los dias, pero no se reescribe nunca).
    """
    with _write_lock, _connect() as conn:
        conn.execute("""
            UPDATE ocr_cache SET updated_at = ?
            WHERE pdf_hash = ? AND dpi = ? AND img_filter = ? AND pagina BETWEEN ? AND ?
        """, (datetime.datetime.now().isoformat(), pdf_hash, dpi, img_filter, start_page, end_page))
        conn.commit()


def get_audit_cache_key(task_id):
    """
    Con que clave de cache (contenido del PDF + dpi + filtro) se escaneo esta auditoria.
    Retorna None si la auditoria no existe o si es anterior a que la clave se guardara
    -- ver _backfill_claves_cache en server.py, que rellena esas.
    """
    with _connect() as conn:
        fila = conn.execute(
            "SELECT pdf_hash, pdf_dpi, img_filter FROM audits WHERE task_id = ?", (task_id,)
        ).fetchone()
    if fila is None or not fila["pdf_hash"] or fila["pdf_dpi"] is None or not fila["img_filter"]:
        return None
    return {"pdf_hash": fila["pdf_hash"], "dpi": fila["pdf_dpi"], "img_filter": fila["img_filter"]}


def set_audit_cache_key(task_id, pdf_hash, dpi, img_filter):
    """Completa la clave de cache de una auditoria vieja (solo la usa el backfill)."""
    with _write_lock, _connect() as conn:
        conn.execute("""
            UPDATE audits SET pdf_hash = ?, pdf_dpi = ?, img_filter = ? WHERE task_id = ?
        """, (pdf_hash, dpi, img_filter, task_id))
        conn.commit()


def auditorias_sin_clave_cache():
    """
    [(task_id, live_results)] de las auditorias guardadas antes de que se persistiera la
    clave del cache. live_results viene ya parseado porque es de ahi, de la URL de sus
    imagenes anotadas, de donde el backfill deduce la clave.
    """
    with _connect() as conn:
        filas = conn.execute("""
            SELECT task_id, live_results_json FROM audits
            WHERE pdf_hash IS NULL OR pdf_dpi IS NULL OR img_filter IS NULL
        """).fetchall()

    pendientes = []
    for fila in filas:
        try:
            live_results = json.loads(fila["live_results_json"] or "[]")
        except (ValueError, TypeError):
            continue
        pendientes.append((fila["task_id"], live_results))
    return pendientes


def claves_cache_ocr():
    """Todas las claves distintas que hay hoy en el cache de OCR."""
    with _connect() as conn:
        filas = conn.execute("SELECT DISTINCT pdf_hash, dpi, img_filter FROM ocr_cache").fetchall()
    return [
        {"pdf_hash": f["pdf_hash"], "dpi": f["dpi"], "img_filter": f["img_filter"]}
        for f in filas
    ]


def otra_auditoria_usa_cache(pdf_hash, dpi, img_filter, excepto_task_id=None):
    """
    Queda alguna OTRA auditoria en el historial escaneada con esta misma clave? Si la hay,
    su cache no se puede borrar: el cache es por contenido de PDF, no por auditoria, y
    llevarselo dejaria a esa otra sin las imagenes de la Vista del Documento.
    """
    with _connect() as conn:
        fila = conn.execute("""
            SELECT 1 FROM audits
            WHERE pdf_hash = ? AND pdf_dpi = ? AND img_filter = ? AND task_id IS NOT ?
            LIMIT 1
        """, (pdf_hash, dpi, img_filter, excepto_task_id)).fetchone()
    return fila is not None


def delete_cache_ocr(pdf_hash, dpi, img_filter):
    """
    Borra del cache todas las paginas de este PDF escaneadas con estos ajustes. Devuelve
    (filas_borradas, nombres_de_imagenes) -- mismo contrato que purgar_cache_ocr: las
    imagenes las borra del disco quien llama, que es el que conoce la carpeta.
    """
    huerfanas = []
    with _write_lock, _connect() as conn:
        filas = conn.execute("""
            SELECT data_json FROM ocr_cache
            WHERE pdf_hash = ? AND dpi = ? AND img_filter = ?
        """, (pdf_hash, dpi, img_filter)).fetchall()
        for fila in filas:
            try:
                url = (json.loads(fila["data_json"]) or {}).get("image_url") or ""
            except (ValueError, TypeError):
                continue
            if url:
                huerfanas.append(url.rsplit("/", 1)[-1])
        cur = conn.execute("""
            DELETE FROM ocr_cache WHERE pdf_hash = ? AND dpi = ? AND img_filter = ?
        """, (pdf_hash, dpi, img_filter))
        conn.commit()
        return cur.rowcount, huerfanas


def purgar_cache_ocr(dias=30):
    """
    Borra del cache lo que no se toca hace mas de `dias` y devuelve los nombres de las
    imagenes que quedaron sin duenio, para que quien llame las borre del disco. Sin esto
    static/ocr_cache/ creceria para siempre (un PDF de 500 paginas deja 500 jpg).
    """
    limite = (datetime.datetime.now() - datetime.timedelta(days=dias)).isoformat()
    huerfanas = []
    with _write_lock, _connect() as conn:
        for fila in conn.execute("SELECT data_json FROM ocr_cache WHERE updated_at < ?", (limite,)):
            try:
                url = (json.loads(fila["data_json"]) or {}).get("image_url") or ""
            except (ValueError, TypeError):
                continue
            if url:
                huerfanas.append(url.rsplit("/", 1)[-1])
        conn.execute("DELETE FROM ocr_cache WHERE updated_at < ?", (limite,))
        conn.commit()
    return huerfanas
