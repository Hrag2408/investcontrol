from __future__ import annotations

import calendar
import json
import os
import sqlite3
import tempfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / os.getenv("DB_FILENAME", "investcontrol.db")
SAMPLE_XLSX = BASE_DIR / "sample_data" / "APLICACOES_2026.xlsx"
DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "admin@investcontrol.app")
DEFAULT_PASSWORD = os.getenv("DEFAULT_PASSWORD", "123456")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5317"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "investcontrol-real-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                institution TEXT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'BRL',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                code TEXT DEFAULT '',
                initial_value REAL NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount REAL NOT NULL,
                date TEXT NOT NULL,
                competence TEXT NOT NULL,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dividends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                competence TEXT NOT NULL,
                gross_amount REAL NOT NULL,
                net_amount REAL NOT NULL,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                competence TEXT NOT NULL,
                current_balance REAL DEFAULT 0,
                amount REAL NOT NULL,
                percent REAL DEFAULT 0,
                notes TEXT DEFAULT '',
                origin_key TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                ref_month TEXT NOT NULL,
                ref_date TEXT NOT NULL,
                balance REAL NOT NULL,
                source TEXT NOT NULL,
                notes TEXT DEFAULT '',
                origin_key TEXT UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS import_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                summary_json TEXT NOT NULL
            );
            """
        )
        earning_columns = {row["name"] for row in conn.execute("PRAGMA table_info(earnings)").fetchall()}
        if "current_balance" not in earning_columns:
            conn.execute("ALTER TABLE earnings ADD COLUMN current_balance REAL DEFAULT 0")
        if "percent" not in earning_columns:
            conn.execute("ALTER TABLE earnings ADD COLUMN percent REAL DEFAULT 0")

        user = conn.execute("SELECT id FROM users WHERE email = ?", (DEFAULT_EMAIL,)).fetchone()
        if not user:
            conn.execute(
                "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
                (DEFAULT_EMAIL, generate_password_hash(DEFAULT_PASSWORD), "Administrador"),
            )


init_db()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "Não autenticado."}), 401
        return fn(*args, **kwargs)

    return wrapper


def parse_json() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def to_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text:
        return default
    text = text.replace("R$", "").replace("%", "").replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return default


ALLOWED_TYPES = {
    "Renda Fixa",
    "Fundo de Investimento",
    "COE",
    "Tesouro",
    "Previdência",
    "Multimercado",
    "Poupança",
    "Outros",
}


def infer_type(name: str) -> str:
    text = name.lower()
    if "poupan" in text:
        return "Poupança"
    if "previd" in text:
        return "Previdência"
    if "fundo" in text:
        return "Fundo de Investimento"
    if "multimerc" in text:
        return "Multimercado"
    if "coe" in text:
        return "COE"
    if any(key in text for key in ["cdb", "lci", "lca", "lcadi", "renda fixa", "crédito privado", "titulo", "título", "cdi", "rdc"]):
        return "Renda Fixa"
    return "Outros"


def parse_month(value: str | None) -> str:
    if value:
        text = value[:7]
        try:
            datetime.strptime(text, "%Y-%m")
            return text
        except ValueError:
            pass
    return datetime.now().strftime("%Y-%m")


def parse_date_any(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()
    except Exception:
        return None


def month_last_day(dt: date) -> str:
    return dt.replace(day=calendar.monthrange(dt.year, dt.month)[1]).isoformat()


def month_label(month: str) -> str:
    y, m = month.split("-")
    nomes = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
    return f"{nomes[int(m)-1]}/{y}"


def base_value_for_application(conn: sqlite3.Connection, app_id: int, until_month: str | None = None) -> float:
    app_row = conn.execute("SELECT initial_value FROM applications WHERE id = ?", (app_id,)).fetchone()
    if not app_row:
        return 0.0
    total = float(app_row["initial_value"] or 0)
    if until_month:
        aporte = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE application_id = ? AND kind = 'aporte' AND substr(COALESCE(competence, date), 1, 7) <= ?",
            (app_id, until_month),
        ).fetchone()["total"]
        resgate = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE application_id = ? AND kind = 'resgate' AND substr(COALESCE(competence, date), 1, 7) <= ?",
            (app_id, until_month),
        ).fetchone()["total"]
    else:
        aporte = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE application_id = ? AND kind = 'aporte'",
            (app_id,),
        ).fetchone()["total"]
        resgate = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE application_id = ? AND kind = 'resgate'",
            (app_id,),
        ).fetchone()["total"]
    return round(total + float(aporte or 0) - float(resgate or 0), 2)


def application_market_value(conn: sqlite3.Connection, app_id: int, until_month: str | None = None) -> float:
    if until_month:
        snap = conn.execute(
            "SELECT balance FROM snapshots WHERE application_id = ? AND ref_month <= ? ORDER BY ref_month DESC, id DESC LIMIT 1",
            (app_id, until_month),
        ).fetchone()
    else:
        snap = conn.execute(
            "SELECT balance FROM snapshots WHERE application_id = ? ORDER BY ref_month DESC, id DESC LIMIT 1",
            (app_id,),
        ).fetchone()
    if snap:
        return round(float(snap["balance"] or 0), 2)
    return base_value_for_application(conn, app_id, until_month)


def portfolio_by_type(conn: sqlite3.Connection, until_month: str | None = None) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, type FROM applications ORDER BY type, name").fetchall()
    totals: dict[str, float] = {}
    for row in rows:
        value = application_market_value(conn, row["id"], until_month)
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    grand_total = sum(totals.values()) or 1.0
    result = [
        {
            "type": app_type,
            "value": round(value, 2),
            "percent": round(value / grand_total * 100, 2),
        }
        for app_type, value in sorted(totals.items(), key=lambda item: item[1], reverse=True)
        if value > 0
    ]
    return result


def serialize_table(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    queries = {
        "accounts": """
            SELECT a.*, COUNT(ap.id) AS applications_count
            FROM accounts a
            LEFT JOIN applications ap ON ap.account_id = a.id
            GROUP BY a.id
            ORDER BY a.id DESC
        """,
        "applications": """
            SELECT ap.*, a.name AS account_name, a.institution AS account_institution
            FROM applications ap
            JOIN accounts a ON a.id = ap.account_id
            ORDER BY ap.id DESC
        """,
        "movements": """
            SELECT m.*, ap.name AS application_name, a.name AS account_name
            FROM movements m
            JOIN applications ap ON ap.id = m.application_id
            JOIN accounts a ON a.id = ap.account_id
            ORDER BY m.date DESC, m.id DESC
        """,
        "dividends": """
            SELECT d.*, ap.name AS application_name, a.name AS account_name
            FROM dividends d
            JOIN applications ap ON ap.id = d.application_id
            JOIN accounts a ON a.id = ap.account_id
            ORDER BY d.payment_date DESC, d.id DESC
        """,
        "earnings": """
            SELECT e.*, ap.name AS application_name, a.name AS account_name
            FROM earnings e
            JOIN applications ap ON ap.id = e.application_id
            JOIN accounts a ON a.id = ap.account_id
            ORDER BY e.payment_date DESC, e.id DESC
        """,
        "snapshots": """
            SELECT s.*, ap.name AS application_name, ap.type AS application_type, a.name AS account_name
            FROM snapshots s
            JOIN applications ap ON ap.id = s.application_id
            JOIN accounts a ON a.id = ap.account_id
            ORDER BY s.ref_month DESC, s.id DESC
        """,
        "imports": "SELECT * FROM import_logs ORDER BY imported_at DESC, id DESC",
    }
    rows = conn.execute(queries[table]).fetchall()
    return [dict(row) for row in rows]


def dashboard_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    accounts = conn.execute("SELECT COUNT(*) AS total FROM accounts").fetchone()["total"]
    applications = conn.execute("SELECT COUNT(*) AS total FROM applications").fetchone()["total"]
    total_aportes = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE kind = 'aporte'").fetchone()["total"]
    total_resgates = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE kind = 'resgate'").fetchone()["total"]
    total_dividendos = conn.execute("SELECT COALESCE(SUM(net_amount), 0) AS total FROM dividends").fetchone()["total"]
    total_rendimentos = conn.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM earnings").fetchone()["total"]
    patrimonio = 0.0
    for row in conn.execute("SELECT id FROM applications").fetchall():
        patrimonio += application_market_value(conn, row["id"])
    recent_movements = conn.execute(
        """
        SELECT m.date, m.kind, m.amount, ap.name AS application_name
        FROM movements m
        JOIN applications ap ON ap.id = m.application_id
        ORDER BY m.date DESC, m.id DESC
        LIMIT 5
        """
    ).fetchall()
    recent_dividends = conn.execute(
        """
        SELECT d.payment_date, d.net_amount, ap.name AS application_name
        FROM dividends d
        JOIN applications ap ON ap.id = d.application_id
        ORDER BY d.payment_date DESC, d.id DESC
        LIMIT 5
        """
    ).fetchall()
    return {
        "kpis": {
            "accounts": int(accounts or 0),
            "applications": int(applications or 0),
            "aportes": round(float(total_aportes or 0), 2),
            "resgates": round(float(total_resgates or 0), 2),
            "dividendos": round(float(total_dividendos or 0), 2),
            "rendimentos": round(float(total_rendimentos or 0), 2),
            "patrimonio": round(float(patrimonio or 0), 2),
            "imports": int(conn.execute("SELECT COUNT(*) AS total FROM import_logs").fetchone()["total"] or 0),
        },
        "portfolio_by_type": portfolio_by_type(conn),
        "recent_movements": [dict(row) for row in recent_movements],
        "recent_dividends": [dict(row) for row in recent_dividends],
    }


def monthly_report(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    total_aportes = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE kind = 'aporte' AND substr(COALESCE(competence, date), 1, 7) = ?",
        (month,),
    ).fetchone()["total"]
    total_resgates = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE kind = 'resgate' AND substr(COALESCE(competence, date), 1, 7) = ?",
        (month,),
    ).fetchone()["total"]
    total_dividendos = conn.execute(
        "SELECT COALESCE(SUM(net_amount), 0) AS total FROM dividends WHERE substr(competence, 1, 7) = ?",
        (month,),
    ).fetchone()["total"]
    total_rendimentos = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM earnings WHERE substr(competence, 1, 7) = ?",
        (month,),
    ).fetchone()["total"]
    patrimonio = 0.0
    for row in conn.execute("SELECT id FROM applications").fetchall():
        patrimonio += application_market_value(conn, row["id"], month)
    account_rows = conn.execute("SELECT id, name FROM accounts ORDER BY name").fetchall()
    account_summary = []
    for row in account_rows:
        total = 0.0
        app_ids = conn.execute("SELECT id FROM applications WHERE account_id = ?", (row["id"],)).fetchall()
        for app_row in app_ids:
            total += application_market_value(conn, app_row["id"], month)
        account_summary.append({"account_name": row["name"], "value": round(total, 2)})
    return {
        "month": month,
        "month_label": month_label(month),
        "totals": {
            "aportes": round(float(total_aportes or 0), 2),
            "resgates": round(float(total_resgates or 0), 2),
            "dividendos": round(float(total_dividendos or 0), 2),
            "rendimentos": round(float(total_rendimentos or 0), 2),
            "patrimonio": round(float(patrimonio or 0), 2),
            "resultado_caixa": round(float(total_dividendos or 0) + float(total_rendimentos or 0) - float(total_resgates or 0), 2),
        },
        "portfolio_by_type": portfolio_by_type(conn, month),
        "portfolio_by_account": account_summary,
    }


def ensure_account(conn: sqlite3.Connection, name: str, institution: str) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT id FROM accounts WHERE lower(name) = lower(?) AND lower(institution) = lower(?)",
        (name, institution),
    ).fetchone()
    if row:
        return int(row["id"]), False
    cursor = conn.execute(
        "INSERT INTO accounts (name, institution, currency, notes) VALUES (?, ?, 'BRL', 'Conta criada por importação.')",
        (name, institution),
    )
    return int(cursor.lastrowid), True


def ensure_application(conn: sqlite3.Connection, account_id: int, name: str, app_type: str) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT id, initial_value FROM applications WHERE account_id = ? AND lower(name) = lower(?)",
        (account_id, name),
    ).fetchone()
    if row:
        return int(row["id"]), False
    cursor = conn.execute(
        "INSERT INTO applications (account_id, type, name, code, initial_value, notes) VALUES (?, ?, ?, '', 0, 'Aplicação criada por importação.')",
        (account_id, app_type, name),
    )
    return int(cursor.lastrowid), True


def row_value(row: pd.Series, idx: int) -> Any:
    try:
        value = row.iloc[idx]
    except Exception:
        return None
    if pd.isna(value):
        return None
    return value


def import_workbook(file_path: str, filename: str) -> dict[str, Any]:
    summary = {
        "filename": filename,
        "accounts_created": 0,
        "applications_created": 0,
        "snapshots_imported": 0,
        "earnings_imported": 0,
        "sheets": [],
    }
    xl = pd.ExcelFile(file_path)
    with get_db() as conn:
        for sheet in xl.sheet_names:
            if sheet.strip().upper().startswith("RESUMO"):
                continue
            df = xl.parse(sheet_name=sheet, header=None)
            account_name = sheet.title().replace("  ", " ")
            account_id, created_account = ensure_account(conn, account_name, account_name)
            if created_account:
                summary["accounts_created"] += 1
            current_date: date | None = None
            sheet_apps = 0
            sheet_snaps = 0
            sheet_earn = 0
            for _, row in df.iterrows():
                first = row_value(row, 0)
                parsed_date = parse_date_any(first)
                if parsed_date:
                    current_date = parsed_date
                    continue
                app_name = clean_text(first)
                if not current_date or not app_name or app_name == "0" or app_name.upper().startswith("TOTAL"):
                    continue
                final_balance = to_float(row_value(row, 7), None)
                if final_balance is None:
                    nums = [to_float(v, None) for v in row.tolist()[1:] if to_float(v, None) is not None]
                    final_balance = nums[-1] if nums else None
                if final_balance is None:
                    continue
                app_type = infer_type(app_name)
                app_id, created_app = ensure_application(conn, account_id, app_name, app_type)
                if created_app:
                    summary["applications_created"] += 1
                    sheet_apps += 1
                month = current_date.strftime("%Y-%m")
                snap_origin = f"snapshot|{sheet}|{month}|{app_name}"
                cur = conn.execute(
                    "INSERT OR IGNORE INTO snapshots (application_id, ref_month, ref_date, balance, source, notes, origin_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        app_id,
                        month,
                        current_date.isoformat(),
                        float(final_balance),
                        filename,
                        f"Importado da aba {sheet}.",
                        snap_origin,
                    ),
                )
                if cur.rowcount:
                    sheet_snaps += 1
                    summary["snapshots_imported"] += 1
                rendimento = to_float(row_value(row, 5), 0) or 0
                if rendimento > 0:
                    earning_origin = f"earning|{sheet}|{month}|{app_name}"
                    percent = round((float(rendimento) / float(final_balance) * 100), 4) if float(final_balance) > 0 else 0
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO earnings (application_id, payment_date, competence, current_balance, amount, percent, notes, origin_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            app_id,
                            month_last_day(current_date),
                            month,
                            float(final_balance),
                            float(rendimento),
                            percent,
                            f"Importado automaticamente da planilha {filename}.",
                            earning_origin,
                        ),
                    )
                    if cur.rowcount:
                        sheet_earn += 1
                        summary["earnings_imported"] += 1
            summary["sheets"].append(
                {
                    "sheet": sheet,
                    "applications_created": sheet_apps,
                    "snapshots_imported": sheet_snaps,
                    "earnings_imported": sheet_earn,
                }
            )
        conn.execute(
            "INSERT INTO import_logs (filename, summary_json) VALUES (?, ?)",
            (filename, json.dumps(summary, ensure_ascii=False)),
        )
    return summary


@app.get("/")
def home():
    return render_template("index.html", default_email=DEFAULT_EMAIL, default_password=DEFAULT_PASSWORD)


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "message": "InvestControl ativo."})


@app.post("/api/login")
def api_login():
    data = parse_json()
    email = clean_text(data.get("email"))
    password = clean_text(data.get("password"))
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify({"ok": False, "error": "E-mail ou senha inválidos."}), 400
        session["user_id"] = int(user["id"])
        session["user_name"] = user["name"]
    return jsonify({"ok": True, "message": "Login realizado com sucesso."})


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/bootstrap")
@login_required
def api_bootstrap():
    month = parse_month(request.args.get("month"))
    with get_db() as conn:
        payload = {
            "ok": True,
            "user": {"name": session.get("user_name", "Administrador"), "email": DEFAULT_EMAIL},
            "dashboard": dashboard_payload(conn),
            "accounts": serialize_table(conn, "accounts"),
            "applications": serialize_table(conn, "applications"),
            "movements": serialize_table(conn, "movements"),
            "dividends": serialize_table(conn, "dividends"),
            "earnings": serialize_table(conn, "earnings"),
            "snapshots": serialize_table(conn, "snapshots"),
            "imports": serialize_table(conn, "imports"),
            "report": monthly_report(conn, month),
        }
    return jsonify(payload)


@app.post("/api/accounts")
@login_required
def create_account():
    data = parse_json()
    name = clean_text(data.get("name"))
    institution = clean_text(data.get("institution"))
    if not name or not institution:
        return jsonify({"ok": False, "error": "Informe nome e instituição."}), 400
    currency = clean_text(data.get("currency")) or "BRL"
    notes = clean_text(data.get("notes"))
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO accounts (name, institution, currency, notes) VALUES (?, ?, ?, ?)",
            (name, institution, currency, notes),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Conta cadastrada."})


@app.put("/api/accounts/<int:item_id>")
@login_required
def update_account(item_id: int):
    data = parse_json()
    name = clean_text(data.get("name"))
    institution = clean_text(data.get("institution"))
    if not name or not institution:
        return jsonify({"ok": False, "error": "Informe nome e instituição."}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE accounts SET name = ?, institution = ?, currency = ?, notes = ? WHERE id = ?",
            (
                name,
                institution,
                clean_text(data.get("currency")) or "BRL",
                clean_text(data.get("notes")),
                item_id,
            ),
        )
    return jsonify({"ok": True, "message": "Conta atualizada."})


@app.delete("/api/accounts/<int:item_id>")
@login_required
def delete_account(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Conta excluída."})


@app.post("/api/applications")
@login_required
def create_application():
    data = parse_json()
    account_id = int(data.get("account_id") or 0)
    app_type = clean_text(data.get("type")) or "Outros"
    name = clean_text(data.get("name"))
    if not account_id or not name:
        return jsonify({"ok": False, "error": "Selecione a conta e informe o nome da aplicação."}), 400
    if app_type not in ALLOWED_TYPES:
        app_type = "Outros"
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO applications (account_id, type, name, code, initial_value, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (
                account_id,
                app_type,
                name,
                clean_text(data.get("code")),
                float(data.get("initial_value") or 0),
                clean_text(data.get("notes")),
            ),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Aplicação cadastrada."})


@app.put("/api/applications/<int:item_id>")
@login_required
def update_application(item_id: int):
    data = parse_json()
    account_id = int(data.get("account_id") or 0)
    name = clean_text(data.get("name"))
    if not account_id or not name:
        return jsonify({"ok": False, "error": "Selecione a conta e informe o nome da aplicação."}), 400
    app_type = clean_text(data.get("type")) or "Outros"
    if app_type not in ALLOWED_TYPES:
        app_type = "Outros"
    with get_db() as conn:
        conn.execute(
            "UPDATE applications SET account_id = ?, type = ?, name = ?, code = ?, initial_value = ?, notes = ? WHERE id = ?",
            (
                account_id,
                app_type,
                name,
                clean_text(data.get("code")),
                float(data.get("initial_value") or 0),
                clean_text(data.get("notes")),
                item_id,
            ),
        )
    return jsonify({"ok": True, "message": "Aplicação atualizada."})


@app.delete("/api/applications/<int:item_id>")
@login_required
def delete_application(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Aplicação excluída."})


@app.post("/api/movements")
@login_required
def create_movement():
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    if not app_id:
        return jsonify({"ok": False, "error": "Selecione a aplicação."}), 400
    amount = float(data.get("amount") or 0)
    if amount <= 0:
        return jsonify({"ok": False, "error": "Informe um valor maior que zero."}), 400
    kind = clean_text(data.get("kind")) or "aporte"
    if kind not in {"aporte", "resgate"}:
        kind = "aporte"
    movement_date = clean_text(data.get("date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or movement_date[:7]
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO movements (application_id, kind, amount, date, competence, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (app_id, kind, amount, movement_date, competence, clean_text(data.get("notes"))),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Lançamento salvo."})


@app.put("/api/movements/<int:item_id>")
@login_required
def update_movement(item_id: int):
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    amount = float(data.get("amount") or 0)
    if not app_id or amount <= 0:
        return jsonify({"ok": False, "error": "Revise aplicação e valor."}), 400
    kind = clean_text(data.get("kind")) or "aporte"
    if kind not in {"aporte", "resgate"}:
        kind = "aporte"
    movement_date = clean_text(data.get("date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or movement_date[:7]
    with get_db() as conn:
        conn.execute(
            "UPDATE movements SET application_id = ?, kind = ?, amount = ?, date = ?, competence = ?, notes = ? WHERE id = ?",
            (app_id, kind, amount, movement_date, competence, clean_text(data.get("notes")), item_id),
        )
    return jsonify({"ok": True, "message": "Lançamento atualizado."})


@app.delete("/api/movements/<int:item_id>")
@login_required
def delete_movement(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM movements WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Lançamento excluído."})


@app.post("/api/dividends")
@login_required
def create_dividend():
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    gross = float(data.get("gross_amount") or 0)
    net = float(data.get("net_amount") or 0)
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if not app_id or gross <= 0 or net <= 0:
        return jsonify({"ok": False, "error": "Preencha aplicação e valores válidos."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO dividends (application_id, payment_date, competence, gross_amount, net_amount, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (app_id, payment_date, competence, gross, net, clean_text(data.get("notes"))),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Dividendo salvo."})


@app.put("/api/dividends/<int:item_id>")
@login_required
def update_dividend(item_id: int):
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    gross = float(data.get("gross_amount") or 0)
    net = float(data.get("net_amount") or 0)
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if not app_id or gross <= 0 or net <= 0:
        return jsonify({"ok": False, "error": "Preencha aplicação e valores válidos."}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE dividends SET application_id = ?, payment_date = ?, competence = ?, gross_amount = ?, net_amount = ?, notes = ? WHERE id = ?",
            (app_id, payment_date, competence, gross, net, clean_text(data.get("notes")), item_id),
        )
    return jsonify({"ok": True, "message": "Dividendo atualizado."})


@app.delete("/api/dividends/<int:item_id>")
@login_required
def delete_dividend(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM dividends WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Dividendo excluído."})


@app.post("/api/earnings")
@login_required
def create_earning():
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    amount = float(data.get("amount") or 0)
    current_balance = float(data.get("current_balance") or 0)
    percent = float(data.get("percent") or 0)
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if current_balance > 0 and percent > 0 and amount <= 0:
        amount = round(current_balance * percent / 100, 2)
    if current_balance > 0 and amount > 0 and percent <= 0:
        percent = round(amount / current_balance * 100, 4)
    if not app_id or amount <= 0:
        return jsonify({"ok": False, "error": "Preencha aplicação e valor válido."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO earnings (application_id, payment_date, competence, current_balance, amount, percent, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (app_id, payment_date, competence, current_balance, amount, percent, clean_text(data.get("notes"))),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Rendimento salvo."})


@app.put("/api/earnings/<int:item_id>")
@login_required
def update_earning(item_id: int):
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    amount = float(data.get("amount") or 0)
    current_balance = float(data.get("current_balance") or 0)
    percent = float(data.get("percent") or 0)
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if current_balance > 0 and percent > 0 and amount <= 0:
        amount = round(current_balance * percent / 100, 2)
    if current_balance > 0 and amount > 0 and percent <= 0:
        percent = round(amount / current_balance * 100, 4)
    if not app_id or amount <= 0:
        return jsonify({"ok": False, "error": "Preencha aplicação e valor válido."}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE earnings SET application_id = ?, payment_date = ?, competence = ?, current_balance = ?, amount = ?, percent = ?, notes = ? WHERE id = ?",
            (app_id, payment_date, competence, current_balance, amount, percent, clean_text(data.get("notes")), item_id),
        )
    return jsonify({"ok": True, "message": "Rendimento atualizado."})


@app.delete("/api/earnings/<int:item_id>")
@login_required
def delete_earning(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM earnings WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Rendimento excluído."})


@app.get("/api/reports/monthly")
@login_required
def api_monthly_report():
    month = parse_month(request.args.get("month"))
    with get_db() as conn:
        return jsonify({"ok": True, "report": monthly_report(conn, month)})


@app.post("/api/actions/reset-launches")
@login_required
def reset_launches():
    with get_db() as conn:
        conn.execute("DELETE FROM movements")
        conn.execute("DELETE FROM dividends")
        conn.execute("DELETE FROM earnings")
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM import_logs")
    return jsonify({"ok": True, "message": "Todos os lançamentos foram zerados. Cadastros mantidos."})


@app.post("/api/actions/reset-all")
@login_required
def reset_all():
    with get_db() as conn:
        conn.execute("DELETE FROM movements")
        conn.execute("DELETE FROM dividends")
        conn.execute("DELETE FROM earnings")
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM applications")
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM import_logs")
    return jsonify({"ok": True, "message": "Base inteira limpa com sucesso."})


@app.post("/api/import/sample")
@login_required
def import_sample():
    if not SAMPLE_XLSX.exists():
        return jsonify({"ok": False, "error": "Planilha exemplo não encontrada no pacote."}), 404
    summary = import_workbook(str(SAMPLE_XLSX), SAMPLE_XLSX.name)
    return jsonify({"ok": True, "message": "Planilha exemplo importada.", "summary": summary})


@app.post("/api/import/xlsx")
@login_required
def import_uploaded_xlsx():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"ok": False, "error": "Selecione um arquivo .xlsx."}), 400
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(".xlsx"):
        return jsonify({"ok": False, "error": "Envie um arquivo .xlsx."}), 400
    target = UPLOAD_DIR / filename
    file.save(target)
    summary = import_workbook(str(target), filename)
    return jsonify({"ok": True, "message": "Planilha importada com sucesso.", "summary": summary})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)
