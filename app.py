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

            CREATE TABLE IF NOT EXISTS report_closures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_month TEXT NOT NULL UNIQUE,
                report_payload_json TEXT NOT NULL,
                closed_by_user_id INTEGER,
                closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (closed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS rental_properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT DEFAULT '',
                category TEXT DEFAULT 'Residencial',
                address TEXT DEFAULT '',
                district TEXT DEFAULT '',
                city TEXT DEFAULT '',
                state TEXT DEFAULT '',
                zip_code TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rental_tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                document TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rental_contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER NOT NULL,
                tenant_id INTEGER NOT NULL,
                contract_code TEXT DEFAULT '',
                start_date TEXT NOT NULL,
                end_date TEXT DEFAULT '',
                due_day INTEGER NOT NULL DEFAULT 5,
                rent_amount REAL NOT NULL DEFAULT 0,
                condo_amount REAL NOT NULL DEFAULT 0,
                iptu_amount REAL NOT NULL DEFAULT 0,
                other_amount REAL NOT NULL DEFAULT 0,
                adjustment_index TEXT DEFAULT '',
                payment_method TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'ativo',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (property_id) REFERENCES rental_properties(id) ON DELETE CASCADE,
                FOREIGN KEY (tenant_id) REFERENCES rental_tenants(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rental_charges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id INTEGER NOT NULL,
                competence TEXT NOT NULL,
                due_date TEXT NOT NULL,
                base_rent REAL NOT NULL DEFAULT 0,
                condo_amount REAL NOT NULL DEFAULT 0,
                iptu_amount REAL NOT NULL DEFAULT 0,
                other_amount REAL NOT NULL DEFAULT 0,
                discount_amount REAL NOT NULL DEFAULT 0,
                interest_amount REAL NOT NULL DEFAULT 0,
                penalty_amount REAL NOT NULL DEFAULT 0,
                total_amount REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'aberto',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (contract_id, competence),
                FOREIGN KEY (contract_id) REFERENCES rental_contracts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS rental_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_id INTEGER NOT NULL,
                receipt_date TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                payment_method TEXT DEFAULT '',
                reference TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (charge_id) REFERENCES rental_charges(id) ON DELETE CASCADE
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


def serialize_report_closure_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    data = dict(row)
    return {
        "id": int(data.get("id") or 0),
        "month": clean_text(data.get("ref_month")),
        "month_label": month_label(clean_text(data.get("ref_month"))) if clean_text(data.get("ref_month")) else "",
        "closed_at": clean_text(data.get("closed_at")),
        "updated_at": clean_text(data.get("updated_at")),
        "closed_by_user_id": int(data.get("closed_by_user_id") or 0) if data.get("closed_by_user_id") else None,
        "closed_by_name": clean_text(data.get("closed_by_name")) or "Sistema",
        "is_closed": bool(clean_text(data.get("ref_month"))),
    }


def list_report_closures(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rc.id, rc.ref_month, rc.closed_at, rc.updated_at, rc.closed_by_user_id,
               COALESCE(u.name, 'Sistema') AS closed_by_name
        FROM report_closures rc
        LEFT JOIN users u ON u.id = rc.closed_by_user_id
        ORDER BY rc.ref_month DESC
        """
    ).fetchall()
    return [serialize_report_closure_row(row) for row in rows]


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


def previous_month_key(month: str) -> str:
    year, month_num = [int(part) for part in month.split('-')]
    if month_num == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_num - 1:02d}"


def parse_report_mode(value: Any) -> str:
    mode = clean_text(value).lower()
    if mode in {"frozen", "fechado", "fechado/congelado", "closed"}:
        return "frozen"
    return "dynamic"


def get_report_closure_row(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT rc.id, rc.ref_month, rc.report_payload_json, rc.closed_at, rc.updated_at, rc.closed_by_user_id,
               COALESCE(u.name, 'Sistema') AS closed_by_name
        FROM report_closures rc
        LEFT JOIN users u ON u.id = rc.closed_by_user_id
        WHERE rc.ref_month = ?
        LIMIT 1
        """,
        (month,),
    ).fetchone()


def parse_report_closure_payload(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    raw = clean_text(dict(row).get("report_payload_json"))
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
    payload["closure"] = serialize_report_closure_row(row)
    payload["monthly_rows"] = payload.get("monthly_rows") if isinstance(payload.get("monthly_rows"), list) else []
    payload["month"] = clean_text(payload.get("month")) or clean_text(dict(row).get("ref_month"))
    payload["month_label"] = clean_text(payload.get("month_label")) or month_label(payload["month"])
    return payload


def latest_closure_anchor_for_application(conn: sqlite3.Connection, app_id: int, until_month: str | None) -> dict[str, Any] | None:
    if not until_month:
        return None
    rows = conn.execute(
        """
        SELECT rc.id, rc.ref_month, rc.report_payload_json, rc.closed_at, rc.updated_at, rc.closed_by_user_id,
               COALESCE(u.name, 'Sistema') AS closed_by_name
        FROM report_closures rc
        LEFT JOIN users u ON u.id = rc.closed_by_user_id
        WHERE rc.ref_month <= ?
        ORDER BY rc.ref_month DESC
        """,
        (until_month,),
    ).fetchall()
    for row in rows:
        payload = parse_report_closure_payload(row)
        for item in payload.get("monthly_rows", []):
            if int(item.get("application_id") or 0) == app_id:
                return {
                    "month": clean_text(dict(row).get("ref_month")),
                    "row": item,
                    "closure": payload.get("closure", {}),
                }
    return None


def opening_balance_from_official_anchor(conn: sqlite3.Connection, app_id: int, previous_month: str) -> float:
    anchor = latest_closure_anchor_for_application(conn, app_id, previous_month)
    if anchor:
        base_balance = float(anchor["row"].get("saldoFinal") or 0)
        adjustments = application_adjustments_total(conn, app_id, until_month=previous_month, start_after_month=anchor["month"])
        return round(base_balance + adjustments, 2)
    return round(application_market_value(conn, app_id, previous_month), 2)


def accumulated_income_from_official_anchor(conn: sqlite3.Connection, app_id: int, previous_month: str) -> float:
    anchor = latest_closure_anchor_for_application(conn, app_id, previous_month)
    if anchor:
        base_total = float(anchor["row"].get("totalAcumulado") or 0)
        extra = application_income_total(conn, app_id, until_month=previous_month, start_after_month=anchor["month"])
        return round(base_total + extra, 2)
    return round(application_income_total(conn, app_id, until_month=previous_month), 2)


def apply_report_filters(rows: list[dict[str, Any]], filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parsed = normalize_report_filters(filters)
    filtered: list[dict[str, Any]] = []
    for item in rows:
        if parsed["account_id"] and int(item.get("account_id") or 0) != parsed["account_id"]:
            continue
        if parsed["application_id"] and int(item.get("application_id") or 0) != parsed["application_id"]:
            continue
        if parsed["app_type"] and clean_text(item.get("application_type")) != parsed["app_type"]:
            continue
        filtered.append(item)
    return filtered


def summarize_monthly_rows(month: str, rows: list[dict[str, Any]], filters: dict[str, Any] | None = None, report_mode: str = "dynamic", closure: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed_filters = normalize_report_filters(filters)
    monthly_rows = apply_report_filters(rows, parsed_filters)
    total_aportes = round(sum(float(item.get("aportesBrutos") or 0) for item in monthly_rows), 2)
    total_resgates = round(sum(float(item.get("resgates") or 0) for item in monthly_rows), 2)
    total_dividendos = round(sum(float(item.get("dividendos") or 0) for item in monthly_rows), 2)
    total_rendimentos_puros = round(sum(float(item.get("rendimentos") or 0) for item in monthly_rows), 2)
    total_rendimentos = round(sum(float(item.get("rendimentoReais") or 0) for item in monthly_rows), 2)
    total_saldo_inicial = round(sum(float(item.get("saldoInicial") or 0) for item in monthly_rows), 2)
    total_saldo_final = round(sum(float(item.get("saldoFinal") or 0) for item in monthly_rows), 2)
    total_acumulado = round(sum(float(item.get("totalAcumulado") or 0) for item in monthly_rows), 2)
    total_rendimento_percentual = round((total_rendimentos / total_saldo_inicial * 100), 4) if total_saldo_inicial > 0 else 0.0

    type_totals: dict[str, float] = {}
    account_totals: dict[int, float] = {}
    account_labels: dict[int, str] = {}
    for row in monthly_rows:
        value = round(float(row.get("saldoFinal") or 0), 2)
        app_type = clean_text(row.get("application_type"))
        account_name = clean_text(row.get("account_name"))
        account_id = int(row.get("account_id") or 0)
        type_totals[app_type] = type_totals.get(app_type, 0.0) + value
        account_totals[account_id] = account_totals.get(account_id, 0.0) + value
        if account_id:
            account_labels[account_id] = account_name

    patrimonio = round(total_saldo_final, 2)
    grand_total = patrimonio or 1.0
    portfolio_type = [
        {"type": key, "value": round(value, 2), "percent": round(value / grand_total * 100, 2)}
        for key, value in sorted(type_totals.items(), key=lambda item: item[1], reverse=True)
        if value > 0
    ]
    portfolio_account = [
        {
            "account_id": account_id,
            "account_name": account_labels.get(account_id, "Conta"),
            "value": round(value, 2),
            "percent": round(value / grand_total * 100, 2),
        }
        for account_id, value in sorted(account_totals.items(), key=lambda item: item[1], reverse=True)
        if value > 0
    ]

    closure_meta = closure or {}
    return {
        "month": month,
        "month_label": month_label(month),
        "filters": parsed_filters,
        "mode": report_mode,
        "requested_mode": report_mode,
        "closure": {
            **closure_meta,
            "is_closed": bool(closure_meta.get("month")),
        } if closure_meta else {"is_closed": False},
        "totals": {
            "aportes": total_aportes,
            "resgates": total_resgates,
            "dividendos": total_dividendos,
            "rendimentos": total_rendimentos,
            "rendimentos_puros": total_rendimentos_puros,
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


def build_monthly_report_rows(conn: sqlite3.Connection, month: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_month = previous_month_key(month)
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

        saldo_inicial = opening_balance_from_official_anchor(conn, app_id, previous_month)
        saldo_final = round(saldo_inicial + aporte + rendimento_reais, 2)
        rendimento_percentual = round((rendimento_reais / saldo_inicial * 100), 4) if saldo_inicial > 0 else 0.0
        total_acumulado = round(accumulated_income_from_official_anchor(conn, app_id, previous_month) + rendimento_reais, 2)

        rows.append(
            {
                "account_id": int(app["account_id"] or 0),
                "account_name": clean_text(app["account_name"]),
                "institution": clean_text(app["account_name"]),
                "application_id": app_id,
                "application_name": clean_text(app["name"]),
                "application_type": clean_text(app["type"]),
                "aportesBrutos": round(aportes, 2),
                "resgates": round(resgates, 2),
                "dividendos": round(dividendos, 2),
                "rendimentos": round(rendimentos, 2),
                "saldoInicial": saldo_inicial,
                "aporte": aporte,
                "rendimentoReais": rendimento_reais,
                "rendimentoPercentual": rendimento_percentual,
                "saldoFinal": saldo_final,
                "totalAcumulado": total_acumulado,
            }
        )
    return rows


def application_income_total(
    conn: sqlite3.Connection,
    app_id: int,
    until_month: str | None = None,
    start_after_month: str | None = None,
) -> float:
    params_div: list[Any] = [app_id]
    params_earn: list[Any] = [app_id]
    dividends_where = ["application_id = ?"]
    earnings_where = ["application_id = ?"]

    if start_after_month:
        dividends_where.append("substr(COALESCE(competence, payment_date), 1, 7) > ?")
        earnings_where.append("substr(COALESCE(competence, payment_date), 1, 7) > ?")
        params_div.append(start_after_month)
        params_earn.append(start_after_month)

    if until_month:
        dividends_where.append("substr(COALESCE(competence, payment_date), 1, 7) <= ?")
        earnings_where.append("substr(COALESCE(competence, payment_date), 1, 7) <= ?")
        params_div.append(until_month)
        params_earn.append(until_month)

    dividendos = conn.execute(
        f"SELECT COALESCE(SUM(net_amount), 0) AS total FROM dividends WHERE {' AND '.join(dividends_where)}",
        tuple(params_div),
    ).fetchone()["total"]
    rendimentos = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS total FROM earnings WHERE {' AND '.join(earnings_where)}",
        tuple(params_earn),
    ).fetchone()["total"]
    return round(float(dividendos or 0) + float(rendimentos or 0), 2)


def application_adjustments_total(
    conn: sqlite3.Connection,
    app_id: int,
    until_month: str | None = None,
    start_after_month: str | None = None,
) -> float:
    params_mov: list[Any] = [app_id]
    params_div: list[Any] = [app_id]
    params_earn: list[Any] = [app_id]
    movements_where = ["application_id = ?"]
    dividends_where = ["application_id = ?"]
    earnings_where = ["application_id = ?"]

    if start_after_month:
        movements_where.append("substr(COALESCE(competence, date), 1, 7) > ?")
        dividends_where.append("substr(COALESCE(competence, payment_date), 1, 7) > ?")
        earnings_where.append("substr(COALESCE(competence, payment_date), 1, 7) > ?")
        params_mov.append(start_after_month)
        params_div.append(start_after_month)
        params_earn.append(start_after_month)

    if until_month:
        movements_where.append("substr(COALESCE(competence, date), 1, 7) <= ?")
        dividends_where.append("substr(COALESCE(competence, payment_date), 1, 7) <= ?")
        earnings_where.append("substr(COALESCE(competence, payment_date), 1, 7) <= ?")
        params_mov.append(until_month)
        params_div.append(until_month)
        params_earn.append(until_month)

    aporte = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE {' AND '.join(movements_where)} AND kind = 'aporte'",
        tuple(params_mov),
    ).fetchone()["total"]
    resgate = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS total FROM movements WHERE {' AND '.join(movements_where)} AND kind = 'resgate'",
        tuple(params_mov),
    ).fetchone()["total"]
    dividendos = conn.execute(
        f"SELECT COALESCE(SUM(net_amount), 0) AS total FROM dividends WHERE {' AND '.join(dividends_where)}",
        tuple(params_div),
    ).fetchone()["total"]
    rendimentos = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS total FROM earnings WHERE {' AND '.join(earnings_where)}",
        tuple(params_earn),
    ).fetchone()["total"]

    return round(float(aporte or 0) - float(resgate or 0) + float(dividendos or 0) + float(rendimentos or 0), 2)



def base_value_for_application(conn: sqlite3.Connection, app_id: int, until_month: str | None = None) -> float:
    app_row = conn.execute("SELECT initial_value FROM applications WHERE id = ?", (app_id,)).fetchone()
    if not app_row:
        return 0.0
    total = float(app_row["initial_value"] or 0)
    total += application_adjustments_total(conn, app_id, until_month=until_month)
    return round(total, 2)



def application_market_value(conn: sqlite3.Connection, app_id: int, until_month: str | None = None) -> float:
    if until_month:
        snap = conn.execute(
            "SELECT balance, ref_month FROM snapshots WHERE application_id = ? AND ref_month <= ? ORDER BY ref_month DESC, id DESC LIMIT 1",
            (app_id, until_month),
        ).fetchone()
    else:
        snap = conn.execute(
            "SELECT balance, ref_month FROM snapshots WHERE application_id = ? ORDER BY ref_month DESC, id DESC LIMIT 1",
            (app_id,),
        ).fetchone()
    if snap:
        total = float(snap["balance"] or 0)
        total += application_adjustments_total(
            conn,
            app_id,
            until_month=until_month,
            start_after_month=clean_text(snap["ref_month"]),
        )
        return round(total, 2)
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


def month_bounds(month: str) -> tuple[date, date]:
    first_day = datetime.strptime(f"{month}-01", "%Y-%m-%d").date()
    last_day = first_day.replace(day=calendar.monthrange(first_day.year, first_day.month)[1])
    return first_day, last_day



def rental_charge_paid_total(conn: sqlite3.Connection, charge_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM rental_receipts WHERE charge_id = ?",
        (charge_id,),
    ).fetchone()
    return round(float(row["total"] or 0), 2)



def compute_rental_charge_status(base_status: str, due_date: str, total_amount: float, amount_paid: float) -> str:
    status = clean_text(base_status).lower() or "aberto"
    if status == "cancelado":
        return "cancelado"
    total_amount = round(float(total_amount or 0), 2)
    amount_paid = round(float(amount_paid or 0), 2)
    if total_amount <= 0:
        return "cancelado"
    if amount_paid >= total_amount:
        return "pago"
    if amount_paid > 0:
        return "parcial"
    try:
        if due_date and datetime.strptime(due_date[:10], "%Y-%m-%d").date() < date.today():
            return "vencido"
    except Exception:
        pass
    return "aberto"



def recalc_rental_charge_total(data: dict[str, Any]) -> float:
    total = (
        float(to_float(data.get("base_rent"), 0) or 0)
        + float(to_float(data.get("condo_amount"), 0) or 0)
        + float(to_float(data.get("iptu_amount"), 0) or 0)
        + float(to_float(data.get("other_amount"), 0) or 0)
        + float(to_float(data.get("interest_amount"), 0) or 0)
        + float(to_float(data.get("penalty_amount"), 0) or 0)
        - float(to_float(data.get("discount_amount"), 0) or 0)
    )
    return round(max(total, 0.0), 2)



def refresh_rental_charge_status(conn: sqlite3.Connection, charge_id: int) -> None:
    charge = conn.execute(
        "SELECT id, due_date, total_amount, status FROM rental_charges WHERE id = ?",
        (charge_id,),
    ).fetchone()
    if not charge:
        return
    paid_total = rental_charge_paid_total(conn, charge_id)
    status = compute_rental_charge_status(charge["status"], clean_text(charge["due_date"]), float(charge["total_amount"] or 0), paid_total)
    conn.execute("UPDATE rental_charges SET status = ? WHERE id = ?", (status, charge_id))



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
        "rental_properties": "SELECT * FROM rental_properties ORDER BY active DESC, name ASC, id DESC",
        "rental_tenants": "SELECT * FROM rental_tenants ORDER BY active DESC, name ASC, id DESC",
        "rental_contracts": """
            SELECT c.*, p.name AS property_name, t.name AS tenant_name
            FROM rental_contracts c
            JOIN rental_properties p ON p.id = c.property_id
            JOIN rental_tenants t ON t.id = c.tenant_id
            ORDER BY c.status = 'ativo' DESC, c.start_date DESC, c.id DESC
        """,
        "rental_charges": """
            SELECT ch.*, c.contract_code, c.property_id, c.tenant_id,
                   p.name AS property_name, t.name AS tenant_name,
                   COALESCE((SELECT SUM(r.amount) FROM rental_receipts r WHERE r.charge_id = ch.id), 0) AS amount_paid
            FROM rental_charges ch
            JOIN rental_contracts c ON c.id = ch.contract_id
            JOIN rental_properties p ON p.id = c.property_id
            JOIN rental_tenants t ON t.id = c.tenant_id
            ORDER BY ch.due_date DESC, ch.id DESC
        """,
        "rental_receipts": """
            SELECT r.*, ch.competence, ch.total_amount, c.contract_code,
                   p.name AS property_name, t.name AS tenant_name
            FROM rental_receipts r
            JOIN rental_charges ch ON ch.id = r.charge_id
            JOIN rental_contracts c ON c.id = ch.contract_id
            JOIN rental_properties p ON p.id = c.property_id
            JOIN rental_tenants t ON t.id = c.tenant_id
            ORDER BY r.receipt_date DESC, r.id DESC
        """,
    }
    rows = [dict(row) for row in conn.execute(queries[table]).fetchall()]
    if table == "rental_charges":
        for row in rows:
            paid = round(float(row.get("amount_paid") or 0), 2)
            total = round(float(row.get("total_amount") or 0), 2)
            status = compute_rental_charge_status(row.get("status"), clean_text(row.get("due_date")), total, paid)
            row["status"] = status
            row["amount_paid"] = paid
            row["balance_due"] = round(max(total - paid, 0.0), 2)
    return rows


def rental_dashboard_payload(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    properties_total = int(conn.execute("SELECT COUNT(*) AS total FROM rental_properties WHERE active = 1").fetchone()["total"] or 0)
    contracts_active = int(conn.execute("SELECT COUNT(*) AS total FROM rental_contracts WHERE status = 'ativo'").fetchone()["total"] or 0)
    charges = serialize_table(conn, "rental_charges")
    receipts = serialize_table(conn, "rental_receipts")
    charges_open = sum(1 for item in charges if item.get("status") in {"aberto", "parcial", "vencido"})
    charges_overdue = sum(1 for item in charges if item.get("status") == "vencido")
    due_this_month = round(sum(float(item.get("total_amount") or 0) for item in charges if clean_text(item.get("competence"))[:7] == month), 2)
    received_this_month = round(sum(float(item.get("amount") or 0) for item in receipts if clean_text(item.get("receipt_date"))[:7] == month), 2)
    recent_receipts = receipts[:5]
    upcoming = sorted(
        [item for item in charges if item.get("status") in {"aberto", "parcial", "vencido"}],
        key=lambda item: (clean_text(item.get("due_date")) or '9999-12-31', -int(item.get("id") or 0))
    )[:8]
    return {
        "kpis": {
            "properties": properties_total,
            "contracts_active": contracts_active,
            "charges_open": charges_open,
            "charges_overdue": charges_overdue,
            "due_this_month": due_this_month,
            "received_this_month": received_this_month,
        },
        "upcoming_charges": upcoming,
        "recent_receipts": recent_receipts,
    }


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


def monthly_report(conn: sqlite3.Connection, month: str, filters: dict[str, Any] | None = None, report_mode: str = "dynamic") -> dict[str, Any]:
    parsed_mode = parse_report_mode(report_mode)
    closure_row = get_report_closure_row(conn, month)
    closure_payload = parse_report_closure_payload(closure_row) if closure_row else {}

    if parsed_mode == "frozen":
        if not closure_row:
            raise ValueError("A competência selecionada ainda não possui fechamento oficial.")
        base_rows = closure_payload.get("monthly_rows", [])
        return summarize_monthly_rows(month, base_rows, filters, report_mode="frozen", closure=closure_payload.get("closure", {}))

    base_rows = build_monthly_report_rows(conn, month)
    report = summarize_monthly_rows(month, base_rows, filters, report_mode="dynamic", closure=closure_payload.get("closure", {}))
    report["requested_mode"] = parsed_mode
    return report


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
            "rental_dashboard": rental_dashboard_payload(conn, month),
            "rental_properties": serialize_table(conn, "rental_properties"),
            "rental_tenants": serialize_table(conn, "rental_tenants"),
            "rental_contracts": serialize_table(conn, "rental_contracts"),
            "rental_charges": serialize_table(conn, "rental_charges"),
            "rental_receipts": serialize_table(conn, "rental_receipts"),
            "report_closures": list_report_closures(conn),
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


def generate_rental_charges_for_month(conn: sqlite3.Connection, competence: str) -> dict[str, Any]:
    competence = parse_month(competence)
    first_day, last_day = month_bounds(competence)
    contracts = conn.execute(
        """
        SELECT *
        FROM rental_contracts
        WHERE status = 'ativo'
          AND date(start_date) <= date(?)
          AND (COALESCE(NULLIF(end_date, ''), '9999-12-31') = '9999-12-31' OR date(end_date) >= date(?))
        ORDER BY id DESC
        """,
        (last_day.isoformat(), first_day.isoformat()),
    ).fetchall()
    created = 0
    skipped = 0
    for contract in contracts:
        due_day = min(max(int(contract["due_day"] or 5), 1), calendar.monthrange(first_day.year, first_day.month)[1])
        due_date = first_day.replace(day=due_day).isoformat()
        payload = {
            "base_rent": float(contract["rent_amount"] or 0),
            "condo_amount": float(contract["condo_amount"] or 0),
            "iptu_amount": float(contract["iptu_amount"] or 0),
            "other_amount": float(contract["other_amount"] or 0),
            "discount_amount": 0,
            "interest_amount": 0,
            "penalty_amount": 0,
        }
        total_amount = recalc_rental_charge_total(payload)
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO rental_charges (
                contract_id, competence, due_date, base_rent, condo_amount, iptu_amount, other_amount,
                discount_amount, interest_amount, penalty_amount, total_amount, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'aberto', ?)
            """,
            (
                int(contract["id"]), competence, due_date,
                payload["base_rent"], payload["condo_amount"], payload["iptu_amount"], payload["other_amount"],
                payload["discount_amount"], payload["interest_amount"], payload["penalty_amount"], total_amount,
                f"Cobrança gerada automaticamente para a competência {competence}.",
            ),
        )
        if cur.rowcount:
            created += 1
        else:
            skipped += 1
    return {"competence": competence, "created": created, "skipped": skipped}


@app.post("/api/rental/properties")
@login_required
def create_rental_property():
    data = parse_json()
    name = clean_text(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "Informe o nome do imóvel."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rental_properties (name, code, category, address, district, city, state, zip_code, notes, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                clean_text(data.get("code")),
                clean_text(data.get("category")) or "Residencial",
                clean_text(data.get("address")),
                clean_text(data.get("district")),
                clean_text(data.get("city")),
                clean_text(data.get("state")),
                clean_text(data.get("zip_code")),
                clean_text(data.get("notes")),
                1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0,
            ),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Imóvel cadastrado."})


@app.put("/api/rental/properties/<int:item_id>")
@login_required
def update_rental_property(item_id: int):
    data = parse_json()
    name = clean_text(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "Informe o nome do imóvel."}), 400
    with get_db() as conn:
        conn.execute(
            """
            UPDATE rental_properties
            SET name = ?, code = ?, category = ?, address = ?, district = ?, city = ?, state = ?, zip_code = ?, notes = ?, active = ?
            WHERE id = ?
            """,
            (
                name,
                clean_text(data.get("code")),
                clean_text(data.get("category")) or "Residencial",
                clean_text(data.get("address")),
                clean_text(data.get("district")),
                clean_text(data.get("city")),
                clean_text(data.get("state")),
                clean_text(data.get("zip_code")),
                clean_text(data.get("notes")),
                1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0,
                item_id,
            ),
        )
    return jsonify({"ok": True, "message": "Imóvel atualizado."})


@app.delete("/api/rental/properties/<int:item_id>")
@login_required
def delete_rental_property(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rental_properties WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Imóvel excluído."})


@app.post("/api/rental/tenants")
@login_required
def create_rental_tenant():
    data = parse_json()
    name = clean_text(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "Informe o nome do inquilino."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rental_tenants (name, document, email, phone, notes, active)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                clean_text(data.get("document")),
                clean_text(data.get("email")),
                clean_text(data.get("phone")),
                clean_text(data.get("notes")),
                1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0,
            ),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Inquilino cadastrado."})


@app.put("/api/rental/tenants/<int:item_id>")
@login_required
def update_rental_tenant(item_id: int):
    data = parse_json()
    name = clean_text(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "Informe o nome do inquilino."}), 400
    with get_db() as conn:
        conn.execute(
            """
            UPDATE rental_tenants
            SET name = ?, document = ?, email = ?, phone = ?, notes = ?, active = ?
            WHERE id = ?
            """,
            (
                name,
                clean_text(data.get("document")),
                clean_text(data.get("email")),
                clean_text(data.get("phone")),
                clean_text(data.get("notes")),
                1 if str(data.get("active", 1)).lower() not in {"0", "false", "off", "no", ""} else 0,
                item_id,
            ),
        )
    return jsonify({"ok": True, "message": "Inquilino atualizado."})


@app.delete("/api/rental/tenants/<int:item_id>")
@login_required
def delete_rental_tenant(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rental_tenants WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Inquilino excluído."})


@app.post("/api/rental/contracts")
@login_required
def create_rental_contract():
    data = parse_json()
    property_id = int(data.get("property_id") or 0)
    tenant_id = int(data.get("tenant_id") or 0)
    start_date = clean_text(data.get("start_date"))
    if not property_id or not tenant_id or not start_date:
        return jsonify({"ok": False, "error": "Selecione imóvel, inquilino e data inicial do contrato."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rental_contracts (
                property_id, tenant_id, contract_code, start_date, end_date, due_day,
                rent_amount, condo_amount, iptu_amount, other_amount, adjustment_index,
                payment_method, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                property_id,
                tenant_id,
                clean_text(data.get("contract_code")),
                start_date,
                clean_text(data.get("end_date")),
                max(int(data.get("due_day") or 5), 1),
                float(to_float(data.get("rent_amount"), 0) or 0),
                float(to_float(data.get("condo_amount"), 0) or 0),
                float(to_float(data.get("iptu_amount"), 0) or 0),
                float(to_float(data.get("other_amount"), 0) or 0),
                clean_text(data.get("adjustment_index")),
                clean_text(data.get("payment_method")),
                clean_text(data.get("status")) or "ativo",
                clean_text(data.get("notes")),
            ),
        )
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Contrato cadastrado."})


@app.put("/api/rental/contracts/<int:item_id>")
@login_required
def update_rental_contract(item_id: int):
    data = parse_json()
    property_id = int(data.get("property_id") or 0)
    tenant_id = int(data.get("tenant_id") or 0)
    start_date = clean_text(data.get("start_date"))
    if not property_id or not tenant_id or not start_date:
        return jsonify({"ok": False, "error": "Selecione imóvel, inquilino e data inicial do contrato."}), 400
    with get_db() as conn:
        conn.execute(
            """
            UPDATE rental_contracts
            SET property_id = ?, tenant_id = ?, contract_code = ?, start_date = ?, end_date = ?, due_day = ?,
                rent_amount = ?, condo_amount = ?, iptu_amount = ?, other_amount = ?, adjustment_index = ?,
                payment_method = ?, status = ?, notes = ?
            WHERE id = ?
            """,
            (
                property_id,
                tenant_id,
                clean_text(data.get("contract_code")),
                start_date,
                clean_text(data.get("end_date")),
                max(int(data.get("due_day") or 5), 1),
                float(to_float(data.get("rent_amount"), 0) or 0),
                float(to_float(data.get("condo_amount"), 0) or 0),
                float(to_float(data.get("iptu_amount"), 0) or 0),
                float(to_float(data.get("other_amount"), 0) or 0),
                clean_text(data.get("adjustment_index")),
                clean_text(data.get("payment_method")),
                clean_text(data.get("status")) or "ativo",
                clean_text(data.get("notes")),
                item_id,
            ),
        )
    return jsonify({"ok": True, "message": "Contrato atualizado."})


@app.delete("/api/rental/contracts/<int:item_id>")
@login_required
def delete_rental_contract(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rental_contracts WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Contrato excluído."})


@app.post("/api/rental/charges")
@login_required
def create_rental_charge():
    data = parse_json()
    contract_id = int(data.get("contract_id") or 0)
    competence = parse_month(clean_text(data.get("competence")))
    due_date = clean_text(data.get("due_date"))
    if not contract_id or not due_date:
        return jsonify({"ok": False, "error": "Selecione o contrato, a competência e o vencimento."}), 400
    total_amount = recalc_rental_charge_total(data)
    try:
        with get_db() as conn:
            cursor = conn.execute(
                """
                INSERT INTO rental_charges (
                    contract_id, competence, due_date, base_rent, condo_amount, iptu_amount, other_amount,
                    discount_amount, interest_amount, penalty_amount, total_amount, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    competence,
                    due_date,
                    float(to_float(data.get("base_rent"), 0) or 0),
                    float(to_float(data.get("condo_amount"), 0) or 0),
                    float(to_float(data.get("iptu_amount"), 0) or 0),
                    float(to_float(data.get("other_amount"), 0) or 0),
                    float(to_float(data.get("discount_amount"), 0) or 0),
                    float(to_float(data.get("interest_amount"), 0) or 0),
                    float(to_float(data.get("penalty_amount"), 0) or 0),
                    total_amount,
                    clean_text(data.get("status")) or "aberto",
                    clean_text(data.get("notes")),
                ),
            )
            refresh_rental_charge_status(conn, int(cursor.lastrowid))
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return jsonify({"ok": False, "error": "Já existe cobrança para esse contrato nessa competência."}), 400
        raise
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Cobrança cadastrada."})


@app.put("/api/rental/charges/<int:item_id>")
@login_required
def update_rental_charge(item_id: int):
    data = parse_json()
    contract_id = int(data.get("contract_id") or 0)
    competence = parse_month(clean_text(data.get("competence")))
    due_date = clean_text(data.get("due_date"))
    if not contract_id or not due_date:
        return jsonify({"ok": False, "error": "Selecione o contrato, a competência e o vencimento."}), 400
    total_amount = recalc_rental_charge_total(data)
    try:
        with get_db() as conn:
            conn.execute(
                """
                UPDATE rental_charges
                SET contract_id = ?, competence = ?, due_date = ?, base_rent = ?, condo_amount = ?, iptu_amount = ?, other_amount = ?,
                    discount_amount = ?, interest_amount = ?, penalty_amount = ?, total_amount = ?, status = ?, notes = ?
                WHERE id = ?
                """,
                (
                    contract_id,
                    competence,
                    due_date,
                    float(to_float(data.get("base_rent"), 0) or 0),
                    float(to_float(data.get("condo_amount"), 0) or 0),
                    float(to_float(data.get("iptu_amount"), 0) or 0),
                    float(to_float(data.get("other_amount"), 0) or 0),
                    float(to_float(data.get("discount_amount"), 0) or 0),
                    float(to_float(data.get("interest_amount"), 0) or 0),
                    float(to_float(data.get("penalty_amount"), 0) or 0),
                    total_amount,
                    clean_text(data.get("status")) or "aberto",
                    clean_text(data.get("notes")),
                    item_id,
                ),
            )
            refresh_rental_charge_status(conn, item_id)
    except Exception as exc:
        if 'UNIQUE constraint failed' in str(exc):
            return jsonify({"ok": False, "error": "Já existe cobrança para esse contrato nessa competência."}), 400
        raise
    return jsonify({"ok": True, "message": "Cobrança atualizada."})


@app.delete("/api/rental/charges/<int:item_id>")
@login_required
def delete_rental_charge(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM rental_charges WHERE id = ?", (item_id,))
    return jsonify({"ok": True, "message": "Cobrança excluída."})


@app.post("/api/rental/charges/generate")
@login_required
def generate_rental_charges_endpoint():
    data = parse_json()
    competence = parse_month(clean_text(data.get("competence")) or request.args.get("competence"))
    with get_db() as conn:
        result = generate_rental_charges_for_month(conn, competence)
    return jsonify({
        "ok": True,
        "message": f"Cobranças processadas para {competence}: {result['created']} criada(s), {result['skipped']} já existente(s).",
        "result": result,
    })


@app.post("/api/rental/receipts")
@login_required
def create_rental_receipt():
    data = parse_json()
    charge_id = int(data.get("charge_id") or 0)
    amount = float(to_float(data.get("amount"), 0) or 0)
    receipt_date = clean_text(data.get("receipt_date")) or date.today().isoformat()
    if not charge_id or amount <= 0:
        return jsonify({"ok": False, "error": "Selecione a cobrança e informe um valor recebido."}), 400
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO rental_receipts (charge_id, receipt_date, amount, payment_method, reference, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                charge_id,
                receipt_date,
                amount,
                clean_text(data.get("payment_method")),
                clean_text(data.get("reference")),
                clean_text(data.get("notes")),
            ),
        )
        refresh_rental_charge_status(conn, charge_id)
    return jsonify({"ok": True, "id": cursor.lastrowid, "message": "Recebimento cadastrado."})


@app.put("/api/rental/receipts/<int:item_id>")
@login_required
def update_rental_receipt(item_id: int):
    data = parse_json()
    charge_id = int(data.get("charge_id") or 0)
    amount = float(to_float(data.get("amount"), 0) or 0)
    receipt_date = clean_text(data.get("receipt_date")) or date.today().isoformat()
    if not charge_id or amount <= 0:
        return jsonify({"ok": False, "error": "Selecione a cobrança e informe um valor recebido."}), 400
    with get_db() as conn:
        previous = conn.execute("SELECT charge_id FROM rental_receipts WHERE id = ?", (item_id,)).fetchone()
        conn.execute(
            """
            UPDATE rental_receipts
            SET charge_id = ?, receipt_date = ?, amount = ?, payment_method = ?, reference = ?, notes = ?
            WHERE id = ?
            """,
            (
                charge_id,
                receipt_date,
                amount,
                clean_text(data.get("payment_method")),
                clean_text(data.get("reference")),
                clean_text(data.get("notes")),
                item_id,
            ),
        )
        if previous:
            refresh_rental_charge_status(conn, int(previous["charge_id"] or 0))
        refresh_rental_charge_status(conn, charge_id)
    return jsonify({"ok": True, "message": "Recebimento atualizado."})


@app.delete("/api/rental/receipts/<int:item_id>")
@login_required
def delete_rental_receipt(item_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT charge_id FROM rental_receipts WHERE id = ?", (item_id,)).fetchone()
        conn.execute("DELETE FROM rental_receipts WHERE id = ?", (item_id,))
        if row:
            refresh_rental_charge_status(conn, int(row["charge_id"] or 0))
    return jsonify({"ok": True, "message": "Recebimento excluído."})


@app.get("/api/reports/monthly")
@login_required
def api_monthly_report():
    month = parse_month(request.args.get("month"))
    filters = {
        "account_id": request.args.get("account_id"),
        "application_id": request.args.get("application_id"),
        "app_type": request.args.get("app_type"),
    }
    report_mode = parse_report_mode(request.args.get("mode"))
    with get_db() as conn:
        try:
            report = monthly_report(conn, month, filters, report_mode=report_mode)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, "report": report, "report_closures": list_report_closures(conn)})


@app.post("/api/reports/monthly/close")
@admin_required
def api_close_monthly_report():
    data = parse_json()
    month = parse_month(data.get("month"))
    overwrite = str(data.get("overwrite", "0")).lower() in {"1", "true", "yes", "on"}
    with get_db() as conn:
        existing = get_report_closure_row(conn, month)
        if existing and not overwrite:
            return jsonify({"ok": False, "error": "Essa competência já está fechada. Use a atualização do fechamento para substituir a versão congelada."}), 400
        base_rows = build_monthly_report_rows(conn, month)
        payload = {
            "version": 1,
            "month": month,
            "month_label": month_label(month),
            "monthly_rows": base_rows,
        }
        user_id = current_user_id()
        if existing:
            conn.execute(
                "UPDATE report_closures SET report_payload_json = ?, closed_by_user_id = ?, updated_at = CURRENT_TIMESTAMP WHERE ref_month = ?",
                (json.dumps(payload, ensure_ascii=False), user_id, month),
            )
            message = "Fechamento oficial atualizado com sucesso."
        else:
            conn.execute(
                "INSERT INTO report_closures (ref_month, report_payload_json, closed_by_user_id) VALUES (?, ?, ?)",
                (month, json.dumps(payload, ensure_ascii=False), user_id),
            )
            message = "Fechamento oficial gerado com sucesso."
        report = monthly_report(conn, month, report_mode="frozen")
        return jsonify({"ok": True, "message": message, "report": report, "report_closures": list_report_closures(conn)})


@app.delete("/api/reports/monthly/close")
@admin_required
def api_reopen_monthly_report():
    month = parse_month(request.args.get("month"))
    with get_db() as conn:
        row = get_report_closure_row(conn, month)
        if not row:
            return jsonify({"ok": False, "error": "Não existe fechamento oficial para a competência informada."}), 404
        conn.execute("DELETE FROM report_closures WHERE ref_month = ?", (month,))
        report = monthly_report(conn, month, report_mode="dynamic")
        return jsonify({"ok": True, "message": "Competência reaberta. O relatório voltou para o modo dinâmico.", "report": report, "report_closures": list_report_closures(conn)})


@app.get("/api/reports/monthly/export")
@login_required
def api_monthly_report_export():
    month = parse_month(request.args.get("month"))
    filters = {
        "account_id": request.args.get("account_id"),
        "application_id": request.args.get("application_id"),
        "app_type": request.args.get("app_type"),
    }
    report_mode = parse_report_mode(request.args.get("mode"))
    with get_db() as conn:
        report = monthly_report(conn, month, filters, report_mode=report_mode)
    rows = report.get("monthly_rows") or []
    summary_rows = [
        {"Indicador": "Mês", "Valor": report.get("month_label")},
        {"Indicador": "Modo do relatório", "Valor": "Fechado/congelado" if report.get("mode") == "frozen" else "Dinâmico"},
        {"Indicador": "Saldo inicial", "Valor": report["totals"].get("saldo_inicial", 0)},
        {"Indicador": "Aportes", "Valor": report["totals"].get("aportes", 0)},
        {"Indicador": "Resgates", "Valor": report["totals"].get("resgates", 0)},
        {"Indicador": "Dividendos", "Valor": report["totals"].get("dividendos", 0)},
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
        conn.execute("DELETE FROM rental_receipts")
        conn.execute("DELETE FROM rental_charges")
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
        conn.execute("DELETE FROM rental_receipts")
        conn.execute("DELETE FROM rental_charges")
        conn.execute("DELETE FROM rental_contracts")
        conn.execute("DELETE FROM rental_tenants")
        conn.execute("DELETE FROM rental_properties")
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
