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
        # Migracion liviana: agrega la columna si la DB ya existia de antes de este cambio.
        # SQLite no soporta "ADD COLUMN IF NOT EXISTS", asi que se intenta y se ignora
        # el error si la columna ya esta.
        try:
            conn.execute("ALTER TABLE audits ADD COLUMN elapsed_seconds REAL")
        except sqlite3.OperationalError:
            pass
        conn.commit()


def save_completed_audit(task_id, excel_filename, pdf_filename, key_col,
                          compare_cols, similarity_threshold, start_page, end_page,
                          metrics, results, live_results, report_bytes, pdf_url,
                          elapsed_seconds=None):
    with _write_lock, _connect() as conn:
        conn.execute("""
            INSERT INTO audits (
                task_id, created_at, excel_filename, pdf_filename, key_col,
                compare_cols_json, similarity_threshold, start_page, end_page,
                correct_count, alerts_count, missing_count, huerfanos_count,
                results_json, live_results_json, report_bytes, pdf_url, elapsed_seconds
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                elapsed_seconds=excluded.elapsed_seconds
        """, (
            task_id, datetime.datetime.now().isoformat(), excel_filename, pdf_filename, key_col,
            json.dumps(compare_cols or []), similarity_threshold, start_page, end_page,
            metrics.get("correct", 0), metrics.get("alerts", 0),
            metrics.get("missing", 0), metrics.get("huerfanos", 0),
            json.dumps(results or {}), json.dumps(live_results or []),
            report_bytes, pdf_url, elapsed_seconds
        ))
        conn.commit()


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
