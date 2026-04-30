from __future__ import annotations

import calendar
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from functools import wraps
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file, session
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
                role TEXT NOT NULL DEFAULT 'admin',
                active INTEGER NOT NULL DEFAULT 1,
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
        if "previous_balance" not in earning_columns:
            conn.execute("ALTER TABLE earnings ADD COLUMN previous_balance REAL DEFAULT 0")
        if "current_balance" not in earning_columns:
            conn.execute("ALTER TABLE earnings ADD COLUMN current_balance REAL DEFAULT 0")
        if "percent" not in earning_columns:
            conn.execute("ALTER TABLE earnings ADD COLUMN percent REAL DEFAULT 0")

        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
        if "active" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE users SET role = COALESCE(NULLIF(role, ''), 'admin')")
        conn.execute("UPDATE users SET active = COALESCE(active, 1)")

        user = conn.execute("SELECT id FROM users WHERE email = ?", (DEFAULT_EMAIL,)).fetchone()
        if not user:
            conn.execute(
                "INSERT INTO users (email, password_hash, name, role, active) VALUES (?, ?, ?, 'admin', 1)",
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


def current_user_id() -> int | None:
    user_id = session.get("user_id")
    return int(user_id) if user_id else None


def current_user_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    user_id = current_user_id()
    if not user_id:
        return None
    return conn.execute(
        "SELECT id, email, name, role, active, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def serialize_user_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    data = dict(row)
    role = clean_text(data.get("role")) or "user"
    return {
        "id": int(data.get("id") or 0),
        "name": clean_text(data.get("name")),
        "email": clean_text(data.get("email")),
        "role": role,
        "active": bool(data.get("active", 1)),
        "created_at": clean_text(data.get("created_at")),
        "is_admin": role == "admin",
    }


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = current_user_id()
        if not user_id:
            return jsonify({"ok": False, "error": "Não autenticado."}), 401
        with get_db() as conn:
            user = conn.execute("SELECT role, active FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not int(user["active"] or 0):
            session.clear()
            return jsonify({"ok": False, "error": "Acesso inválido. Faça login novamente."}), 401
        if clean_text(user["role"]) != "admin":
            return jsonify({"ok": False, "error": "Acesso restrito ao administrador."}), 403
        session["user_role"] = "admin"
        return fn(*args, **kwargs)

    return wrapper


def normalize_role(value: Any) -> str:
    role = clean_text(value).lower()
    return role if role in {"admin", "user"} else "user"


def validate_password_strength(password: str) -> str | None:
    if len(password) < 8:
        return "A senha deve ter pelo menos 8 caracteres."
    return None


def active_admin_count(conn: sqlite3.Connection, exclude_user_id: int | None = None) -> int:
    if exclude_user_id:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM users WHERE role = 'admin' AND active = 1 AND id <> ?",
            (exclude_user_id,),
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'admin' AND active = 1").fetchone()
    return int(row["total"] or 0)


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, email, name, role, active, created_at FROM users ORDER BY active DESC, role DESC, name ASC, id ASC"
    ).fetchall()
    return [serialize_user_row(row) for row in rows]


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


def format_date_br(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return text


def parse_int_arg(value: Any) -> int | None:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else None
    except Exception:
        return None


def normalize_report_filters(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    data = filters or {}
    account_id = parse_int_arg(data.get("account_id"))
    application_id = parse_int_arg(data.get("application_id"))
    app_type = clean_text(data.get("app_type"))
    if app_type not in ALLOWED_TYPES:
        app_type = ""
    return {
        "account_id": account_id,
        "application_id": application_id,
        "app_type": app_type,
    }


def earning_values_from_inputs(previous_balance_raw: Any, current_balance_raw: Any, amount_raw: Any, percent_raw: Any) -> tuple[float, float, float, float]:
    previous_balance = round(float(to_float(previous_balance_raw, 0) or 0), 2)
    current_balance = round(float(to_float(current_balance_raw, 0) or 0), 2)
    amount = round(float(to_float(amount_raw, 0) or 0), 2)
    percent = round(float(to_float(percent_raw, 0) or 0), 4)

    if previous_balance > 0 and current_balance > 0:
        amount = round(current_balance - previous_balance, 2)
        percent = round((amount / previous_balance * 100), 4) if previous_balance > 0 else 0.0
    elif previous_balance > 0 and amount > 0:
        current_balance = round(previous_balance + amount, 2)
        percent = round((amount / previous_balance * 100), 4) if previous_balance > 0 else 0.0
    elif previous_balance > 0 and percent > 0:
        amount = round(previous_balance * percent / 100, 2)
        current_balance = round(previous_balance + amount, 2)
    elif current_balance > 0 and amount > 0:
        previous_balance = round(current_balance - amount, 2)
        percent = round((amount / previous_balance * 100), 4) if previous_balance > 0 else 0.0
    elif current_balance > 0 and percent > 0:
        previous_balance = round(current_balance / (1 + percent / 100), 2)
        amount = round(current_balance - previous_balance, 2)

    return max(previous_balance, 0.0), max(current_balance, 0.0), round(amount, 2), round(percent, 4)


def filtered_applications(conn: sqlite3.Connection, filters: dict[str, Any] | None = None) -> list[sqlite3.Row]:
    parsed = normalize_report_filters(filters)
    rows = conn.execute(
        """
        SELECT ap.*, a.name AS account_name, a.institution AS account_institution
        FROM applications ap
        JOIN accounts a ON a.id = ap.account_id
        WHERE (? IS NULL OR ap.account_id = ?)
          AND (? IS NULL OR ap.id = ?)
          AND (? = '' OR ap.type = ?)
        ORDER BY a.name, ap.name
        """,
        (
            parsed["account_id"], parsed["account_id"],
            parsed["application_id"], parsed["application_id"],
            parsed["app_type"], parsed["app_type"],
        ),
    ).fetchall()
    return rows


def build_monthly_report_rows(conn: sqlite3.Connection, month: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for app in filtered_applications(conn, filters):
        app_id = int(app["id"])
        all_movements = conn.execute(
            "SELECT kind, amount, competence, date FROM movements WHERE application_id = ?",
            (app_id,),
        ).fetchall()
        all_dividends = conn.execute(
            "SELECT net_amount, competence, payment_date FROM dividends WHERE application_id = ?",
            (app_id,),
        ).fetchall()
        all_earnings = conn.execute(
            "SELECT amount, competence, payment_date FROM earnings WHERE application_id = ?",
            (app_id,),
        ).fetchall()

        aportes = sum(float(item["amount"] or 0) for item in all_movements if clean_text(item["kind"]) == "aporte" and clean_text(item["competence"] or item["date"])[:7] == month)
        resgates = sum(float(item["amount"] or 0) for item in all_movements if clean_text(item["kind"]) == "resgate" and clean_text(item["competence"] or item["date"])[:7] == month)
        aporte = round(aportes - resgates, 2)
        dividendos = sum(float(item["net_amount"] or 0) for item in all_dividends if clean_text(item["competence"] or item["payment_date"])[:7] == month)
        rendimentos = sum(float(item["amount"] or 0) for item in all_earnings if clean_text(item["competence"] or item["payment_date"])[:7] == month)
        rendimento_reais = round(dividendos + rendimentos, 2)

        prev_aportes = sum(float(item["amount"] or 0) for item in all_movements if clean_text(item["kind"]) == "aporte" and clean_text(item["competence"] or item["date"])[:7] < month)
        prev_resgates = sum(float(item["amount"] or 0) for item in all_movements if clean_text(item["kind"]) == "resgate" and clean_text(item["competence"] or item["date"])[:7] < month)
        prev_dividendos = sum(float(item["net_amount"] or 0) for item in all_dividends if clean_text(item["competence"] or item["payment_date"])[:7] < month)
        prev_rendimentos = sum(float(item["amount"] or 0) for item in all_earnings if clean_text(item["competence"] or item["payment_date"])[:7] < month)

        saldo_inicial = round(float(app["initial_value"] or 0) + prev_aportes - prev_resgates + prev_dividendos + prev_rendimentos, 2)
        saldo_final = round(saldo_inicial + aporte + rendimento_reais, 2)
        rendimento_percentual = round((rendimento_reais / saldo_inicial * 100), 4) if saldo_inicial > 0 else 0.0
        total_acumulado = round(prev_dividendos + prev_rendimentos + rendimento_reais, 2)

        rows.append(
            {
                "account_id": int(app["account_id"] or 0),
                "account_name": clean_text(app["account_name"]),
                "institution": clean_text(app["account_name"]),
                "application_id": app_id,
                "application_name": clean_text(app["name"]),
                "application_type": clean_text(app["type"]),
                "saldoInicial": saldo_inicial,
                "aporte": aporte,
                "rendimentoReais": rendimento_reais,
                "rendimentoPercentual": rendimento_percentual,
                "saldoFinal": saldo_final,
                "totalAcumulado": total_acumulado,
            }
        )
    return rows


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


def monthly_report(conn: sqlite3.Connection, month: str, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed_filters = normalize_report_filters(filters)
    monthly_rows = build_monthly_report_rows(conn, month, parsed_filters)
    total_aportes = round(sum(float(item.get("aporte") or 0) for item in monthly_rows if float(item.get("aporte") or 0) > 0), 2)
    total_resgates = round(sum(abs(float(item.get("aporte") or 0)) for item in monthly_rows if float(item.get("aporte") or 0) < 0), 2)
    total_rendimentos = round(sum(float(item.get("rendimentoReais") or 0) for item in monthly_rows), 2)
    total_saldo_inicial = round(sum(float(item.get("saldoInicial") or 0) for item in monthly_rows), 2)
    total_saldo_final = round(sum(float(item.get("saldoFinal") or 0) for item in monthly_rows), 2)
    total_acumulado = round(sum(float(item.get("totalAcumulado") or 0) for item in monthly_rows), 2)
    total_rendimento_percentual = round((total_rendimentos / total_saldo_inicial * 100), 4) if total_saldo_inicial > 0 else 0.0

    filtered_apps = filtered_applications(conn, parsed_filters)
    type_totals: dict[str, float] = {}
    account_totals: dict[str, float] = {}
    for app in filtered_apps:
        value = application_market_value(conn, int(app["id"]), month)
        app_type = clean_text(app["type"])
        account_name = clean_text(app["account_name"])
        type_totals[app_type] = type_totals.get(app_type, 0.0) + value
        account_totals[account_name] = account_totals.get(account_name, 0.0) + value
    patrimonio = round(sum(type_totals.values()), 2)
    grand_total = patrimonio or 1.0
    portfolio_type = [
        {"type": key, "value": round(value, 2), "percent": round(value / grand_total * 100, 2)}
        for key, value in sorted(type_totals.items(), key=lambda item: item[1], reverse=True)
        if value > 0
    ]
    portfolio_account = [
        {"account_name": key, "value": round(value, 2)}
        for key, value in sorted(account_totals.items(), key=lambda item: item[1], reverse=True)
        if value > 0
    ]

    return {
        "month": month,
        "month_label": month_label(month),
        "filters": parsed_filters,
        "totals": {
            "aportes": total_aportes,
            "resgates": total_resgates,
            "dividendos": 0.0,
            "rendimentos": total_rendimentos,
            "patrimonio": patrimonio,
            "resultado_caixa": round(total_rendimentos - total_resgates, 2),
            "saldo_inicial": total_saldo_inicial,
            "saldo_final": total_saldo_final,
            "total_acumulado": total_acumulado,
            "rendimento_percentual": total_rendimento_percentual,
        },
        "portfolio_by_type": portfolio_type,
        "portfolio_by_account": portfolio_account,
        "monthly_rows": monthly_rows,
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
                    previous_balance = max(round(float(final_balance) - float(rendimento), 2), 0.0)
                    percent = round((float(rendimento) / previous_balance * 100), 4) if previous_balance > 0 else 0
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO earnings (application_id, payment_date, competence, previous_balance, current_balance, amount, percent, notes, origin_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            app_id,
                            month_last_day(current_date),
                            month,
                            previous_balance,
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
    return render_template("index.html", default_email=DEFAULT_EMAIL, default_password="")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "message": "InvestControl ativo."})


@app.post("/api/login")
def api_login():
    data = parse_json()
    email = clean_text(data.get("email"))
    password = clean_text(data.get("password"))
    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            return jsonify({"ok": False, "error": "E-mail ou senha inválidos."}), 400
        if not int(user["active"] or 0):
            return jsonify({"ok": False, "error": "Usuário desativado. Procure o administrador."}), 403
        session["user_id"] = int(user["id"])
        session["user_name"] = user["name"]
        session["user_email"] = user["email"]
        session["user_role"] = clean_text(user["role"]) or "user"
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
        user = current_user_row(conn)
        if not user or not int(user["active"] or 0):
            session.clear()
            return jsonify({"ok": False, "error": "Sessão inválida. Faça login novamente."}), 401
        serialized_user = serialize_user_row(user)
        session["user_name"] = serialized_user["name"]
        session["user_email"] = serialized_user["email"]
        session["user_role"] = serialized_user["role"]
        payload = {
            "ok": True,
            "user": serialized_user,
            "users": list_users(conn) if serialized_user["is_admin"] else [],
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


@app.put("/api/profile")
@login_required
def update_profile():
    data = parse_json()
    name = clean_text(data.get("name"))
    email = clean_text(data.get("email")).lower()
    user_id = current_user_id()
    if not user_id or not name or not email:
        return jsonify({"ok": False, "error": "Informe nome e login (e-mail)."}), 400
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?) AND id <> ?", (email, user_id)).fetchone()
        if existing:
            return jsonify({"ok": False, "error": "Já existe outro usuário com esse e-mail."}), 400
        conn.execute("UPDATE users SET name = ?, email = ? WHERE id = ?", (name, email, user_id))
        user = current_user_row(conn)
    serialized = serialize_user_row(user)
    session["user_name"] = serialized.get("name")
    session["user_email"] = serialized.get("email")
    return jsonify({"ok": True, "message": "Seu acesso foi atualizado.", "user": serialized})


@app.post("/api/profile/password")
@login_required
def change_profile_password():
    data = parse_json()
    current_password = clean_text(data.get("current_password"))
    new_password = clean_text(data.get("new_password"))
    user_id = current_user_id()
    error = validate_password_strength(new_password)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    with get_db() as conn:
        user = conn.execute("SELECT id, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], current_password):
            return jsonify({"ok": False, "error": "Senha atual inválida."}), 400
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), user_id))
    return jsonify({"ok": True, "message": "Senha alterada com sucesso."})


@app.get("/api/users")
@admin_required
def api_list_users():
    with get_db() as conn:
        return jsonify({"ok": True, "users": list_users(conn)})


@app.post("/api/users")
@admin_required
def create_user():
    data = parse_json()
    name = clean_text(data.get("name"))
    email = clean_text(data.get("email")).lower()
    password = clean_text(data.get("password"))
    role = normalize_role(data.get("role"))
    active = 1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0
    if not name or not email or not password:
        return jsonify({"ok": False, "error": "Informe nome, e-mail e senha inicial."}), 400
    error = validate_password_strength(password)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
        if existing:
            return jsonify({"ok": False, "error": "Já existe um usuário com esse e-mail."}), 400
        cursor = conn.execute(
            "INSERT INTO users (email, password_hash, name, role, active) VALUES (?, ?, ?, ?, ?)",
            (email, generate_password_hash(password), name, role, active),
        )
        user = conn.execute("SELECT id, email, name, role, active, created_at FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify({"ok": True, "message": "Usuário cadastrado.", "user": serialize_user_row(user)})


@app.put("/api/users/<int:item_id>")
@admin_required
def update_user(item_id: int):
    data = parse_json()
    name = clean_text(data.get("name"))
    email = clean_text(data.get("email")).lower()
    role = normalize_role(data.get("role"))
    active = 1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0
    if not name or not email:
        return jsonify({"ok": False, "error": "Informe nome e e-mail do usuário."}), 400
    current_id = current_user_id() or 0
    with get_db() as conn:
        user = conn.execute("SELECT id, role, active FROM users WHERE id = ?", (item_id,)).fetchone()
        if not user:
            return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404
        existing = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?) AND id <> ?", (email, item_id)).fetchone()
        if existing:
            return jsonify({"ok": False, "error": "Já existe outro usuário com esse e-mail."}), 400
        current_role = clean_text(user["role"]) or "user"
        current_active = int(user["active"] or 0)
        if item_id == current_id and not active:
            return jsonify({"ok": False, "error": "Você não pode desativar o próprio acesso em uso."}), 400
        if current_role == "admin" and current_active == 1 and (role != "admin" or not active) and active_admin_count(conn, exclude_user_id=item_id) <= 0:
            return jsonify({"ok": False, "error": "O sistema precisa manter pelo menos um administrador ativo."}), 400
        conn.execute(
            "UPDATE users SET name = ?, email = ?, role = ?, active = ? WHERE id = ?",
            (name, email, role, active, item_id),
        )
        updated = conn.execute("SELECT id, email, name, role, active, created_at FROM users WHERE id = ?", (item_id,)).fetchone()
    serialized = serialize_user_row(updated)
    if item_id == current_id:
        session["user_name"] = serialized.get("name")
        session["user_email"] = serialized.get("email")
        session["user_role"] = serialized.get("role")
    return jsonify({"ok": True, "message": "Usuário atualizado.", "user": serialized})


@app.post("/api/users/<int:item_id>/password")
@admin_required
def reset_user_password(item_id: int):
    data = parse_json()
    new_password = clean_text(data.get("new_password"))
    error = validate_password_strength(new_password)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    with get_db() as conn:
        user = conn.execute("SELECT id, email FROM users WHERE id = ?", (item_id,)).fetchone()
        if not user:
            return jsonify({"ok": False, "error": "Usuário não encontrado."}), 404
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_password), item_id))
    return jsonify({"ok": True, "message": "Senha do usuário redefinida."})


def build_backup_payload() -> tuple[io.BytesIO, str]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if DB_PATH.exists():
            zf.write(DB_PATH, arcname=f"data/{DB_PATH.name}")
        if UPLOAD_DIR.exists():
            for file_path in UPLOAD_DIR.rglob("*"):
                if file_path.is_file():
                    zf.write(file_path, arcname=f"uploads/{file_path.relative_to(UPLOAD_DIR).as_posix()}")
        metadata = {
            "generated_at": datetime.now().isoformat(),
            "app": "InvestControl",
            "db_filename": DB_PATH.name,
            "default_admin": DEFAULT_EMAIL,
        }
        zf.writestr("backup_metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    buffer.seek(0)
    return buffer, f"investcontrol_backup_{timestamp}.zip"


def validate_restored_db(candidate_path: Path) -> None:
    conn = sqlite3.connect(candidate_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or clean_text(row[0]).lower() != "ok":
            raise ValueError("O arquivo de backup está corrompido ou inválido.")
        conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    finally:
        conn.close()


def restore_backup_file(uploaded_file) -> dict[str, Any]:
    filename = secure_filename(uploaded_file.filename or "backup")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".zip", ".db"}:
        raise ValueError("Envie um backup .zip ou um banco .db.")

    work_dir = Path(tempfile.mkdtemp(prefix="investcontrol_restore_"))
    restored_uploads = 0
    source_db: Path | None = None
    try:
        raw = uploaded_file.read()
        if suffix == ".db":
            source_db = work_dir / DB_PATH.name
            source_db.write_bytes(raw)
        else:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            db_members = [name for name in archive.namelist() if name.lower().endswith('.db')]
            if not db_members:
                raise ValueError("O backup .zip não contém arquivo de banco .db.")
            archive.extract(db_members[0], work_dir)
            source_db = work_dir / db_members[0]
            for member in archive.namelist():
                if member.startswith("uploads/") and not member.endswith("/"):
                    archive.extract(member, work_dir)
            archive.close()
        if not source_db or not source_db.exists():
            raise ValueError("Não foi possível localizar o banco do backup.")
        validate_restored_db(source_db)

        safety_copy = None
        if DB_PATH.exists():
            safety_copy = DATA_DIR / f"pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{DB_PATH.name}"
            shutil.copy2(DB_PATH, safety_copy)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, DB_PATH)

        restored_uploads_root = work_dir / "uploads"
        if restored_uploads_root.exists():
            if UPLOAD_DIR.exists():
                shutil.rmtree(UPLOAD_DIR)
            shutil.copytree(restored_uploads_root, UPLOAD_DIR)
            restored_uploads = len([p for p in UPLOAD_DIR.rglob('*') if p.is_file()])
        else:
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        init_db()
        return {
            "restored_uploads": restored_uploads,
            "safety_copy": safety_copy.name if safety_copy else "",
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/api/backup/download")
@admin_required
def download_backup():
    backup_file, filename = build_backup_payload()
    return send_file(backup_file, mimetype="application/zip", as_attachment=True, download_name=filename)


@app.post("/api/backup/restore")
@admin_required
def restore_backup():
    uploaded_file = request.files.get("file")
    if not uploaded_file or not uploaded_file.filename:
        return jsonify({"ok": False, "error": "Selecione um arquivo de backup .zip ou .db."}), 400
    try:
        result = restore_backup_file(uploaded_file)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    session.clear()
    return jsonify({
        "ok": True,
        "message": "Backup restaurado com sucesso. Faça login novamente para continuar.",
        "result": result,
    })


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
    previous_balance, current_balance, amount, percent = earning_values_from_inputs(
        data.get("previous_balance"),
        data.get("current_balance"),
        data.get("amount"),
        data.get("percent"),
    )
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if not app_id or amount <= 0 or current_balance <= 0:
        return jsonify({"ok": False, "error": "Informe a aplicação, o saldo anterior e o saldo atual com rendimento positivo."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO earnings (application_id, payment_date, competence, previous_balance, current_balance, amount, percent, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (app_id, payment_date, competence, previous_balance, current_balance, amount, percent, clean_text(data.get("notes"))),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Rendimento salvo."})


@app.put("/api/earnings/<int:item_id>")
@login_required
def update_earning(item_id: int):
    data = parse_json()
    app_id = int(data.get("application_id") or 0)
    previous_balance, current_balance, amount, percent = earning_values_from_inputs(
        data.get("previous_balance"),
        data.get("current_balance"),
        data.get("amount"),
        data.get("percent"),
    )
    payment_date = clean_text(data.get("payment_date")) or date.today().isoformat()
    competence = clean_text(data.get("competence")) or payment_date[:7]
    if not app_id or amount <= 0 or current_balance <= 0:
        return jsonify({"ok": False, "error": "Informe a aplicação, o saldo anterior e o saldo atual com rendimento positivo."}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE earnings SET application_id = ?, payment_date = ?, competence = ?, previous_balance = ?, current_balance = ?, amount = ?, percent = ?, notes = ? WHERE id = ?",
            (app_id, payment_date, competence, previous_balance, current_balance, amount, percent, clean_text(data.get("notes")), item_id),
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
    filters = {
        "account_id": request.args.get("account_id"),
        "application_id": request.args.get("application_id"),
        "app_type": request.args.get("app_type"),
    }
    with get_db() as conn:
        return jsonify({"ok": True, "report": monthly_report(conn, month, filters)})


@app.get("/api/reports/monthly/export")
@login_required
def api_monthly_report_export():
    month = parse_month(request.args.get("month"))
    filters = {
        "account_id": request.args.get("account_id"),
        "application_id": request.args.get("application_id"),
        "app_type": request.args.get("app_type"),
    }
    with get_db() as conn:
        report = monthly_report(conn, month, filters)
    rows = report.get("monthly_rows") or []
    summary_rows = [
        {"Indicador": "Mês", "Valor": report.get("month_label")},
        {"Indicador": "Saldo inicial", "Valor": report["totals"].get("saldo_inicial", 0)},
        {"Indicador": "Aportes líquidos", "Valor": report["totals"].get("aportes", 0)},
        {"Indicador": "Resgates", "Valor": report["totals"].get("resgates", 0)},
        {"Indicador": "Rendimentos", "Valor": report["totals"].get("rendimentos", 0)},
        {"Indicador": "Rentabilidade (%)", "Valor": report["totals"].get("rendimento_percentual", 0)},
        {"Indicador": "Saldo final", "Valor": report["totals"].get("saldo_final", 0)},
        {"Indicador": "Patrimônio filtrado", "Valor": report["totals"].get("patrimonio", 0)},
    ]
    detail_rows = [
        {
            "Conta": item.get("account_name"),
            "Tipo": item.get("application_type"),
            "Aplicação": item.get("application_name"),
            "Saldo inicial": item.get("saldoInicial"),
            "Aporte líquido": item.get("aporte"),
            "Rendimento em R$": item.get("rendimentoReais"),
            "Rendimento em %": item.get("rendimentoPercentual"),
            "Saldo final": item.get("saldoFinal"),
            "Total acumulado": item.get("totalAcumulado"),
        }
        for item in rows
    ]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Resumo", index=False)
        pd.DataFrame(detail_rows).to_excel(writer, sheet_name="Detalhamento", index=False)
    buffer.seek(0)
    filename = f"relatorio_mensal_{month}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/actions/reset-launches")
@admin_required
def reset_launches():
    with get_db() as conn:
        conn.execute("DELETE FROM movements")
        conn.execute("DELETE FROM dividends")
        conn.execute("DELETE FROM earnings")
        conn.execute("DELETE FROM snapshots")
        conn.execute("DELETE FROM import_logs")
    return jsonify({"ok": True, "message": "Todos os lançamentos foram zerados. Cadastros mantidos."})


@app.post("/api/actions/reset-all")
@admin_required
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
@admin_required
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
