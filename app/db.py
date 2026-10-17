"""Thin database layer: plain SQL over SQLite (local) or PostgreSQL (production).

SQL in this project is written once with `?` placeholders. When running on
PostgreSQL the placeholders are translated to `%s` automatically.
"""
import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id {pk},
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_items (
    id {pk},
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price_cents INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 1,
    show_online INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id {pk},
    phone TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id {pk},
    order_date TEXT NOT NULL,
    number INTEGER NOT NULL,
    public_token TEXT NOT NULL UNIQUE,
    customer_id INTEGER,
    customer_name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    fulfillment TEXT NOT NULL DEFAULT 'pickup',
    source TEXT NOT NULL DEFAULT 'phone',
    pickup_time TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    delivery_fee_cents INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    paid INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (order_date, number)
);

CREATE TABLE IF NOT EXISTS order_items (
    id {pk},
    order_id INTEGER NOT NULL,
    menu_item_id INTEGER,
    name TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    qty INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id {pk},
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ingredients (
    id {pk},
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'kg',
    default_cost {real} NOT NULL DEFAULT 0,
    low_stock {real} NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id {pk},
    purchase_date TEXT NOT NULL,
    ingredient_id INTEGER NOT NULL,
    qty {real} NOT NULL,
    cost_cents INTEGER NOT NULL,
    supplier TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_by INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_moves (
    id {pk},
    ingredient_id INTEGER NOT NULL,
    qty {real} NOT NULL,
    reason TEXT NOT NULL,
    order_id INTEGER,
    purchase_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipe_items (
    id {pk},
    menu_item_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL,
    qty {real} NOT NULL,
    UNIQUE (menu_item_id, ingredient_id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id {pk},
    order_id INTEGER NOT NULL UNIQUE,
    customer_name TEXT NOT NULL DEFAULT '',
    rating INTEGER NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_date ON orders (order_date);
CREATE INDEX IF NOT EXISTS idx_moves_ingredient ON stock_moves (ingredient_id);
CREATE INDEX IF NOT EXISTS idx_moves_order ON stock_moves (order_id);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases (purchase_date);
CREATE INDEX IF NOT EXISTS idx_orders_phone ON orders (phone);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items (order_id);
"""


def _database_url():
    url = current_app.config.get("DATABASE_URL") or ""
    return url.strip()


def is_postgres():
    return _database_url().startswith(("postgres://", "postgresql://"))


def _sqlite_path():
    url = _database_url()
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    if url and not url.startswith("sqlite"):
        return url
    return os.path.join(current_app.instance_path, "kitchen.sqlite3")


def connect():
    """Open a new connection based on DATABASE_URL."""
    if is_postgres():
        import psycopg  # imported lazily so SQLite users do not need it
        from psycopg.rows import dict_row

        url = _database_url().replace("postgres://", "postgresql://", 1)
        return psycopg.connect(url, row_factory=dict_row)

    path = _sqlite_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _sql(sql):
    if is_postgres():
        return sql.replace("%", "%%").replace("?", "%s")
    return sql


def _row(r):
    return dict(r) if r is not None else None


def query(sql, params=(), one=False):
    cur = get_db().cursor()
    cur.execute(_sql(sql), tuple(params))
    rows = cur.fetchall()
    cur.close()
    if one:
        return _row(rows[0]) if rows else None
    return [_row(r) for r in rows]


def scalar(sql, params=()):
    cur = get_db().cursor()
    cur.execute(_sql(sql), tuple(params))
    r = cur.fetchone()
    cur.close()
    if r is None:
        return None
    return list(dict(r).values())[0]


def execute(sql, params=()):
    """Run a write statement. Returns the number of affected rows."""
    cur = get_db().cursor()
    cur.execute(_sql(sql), tuple(params))
    n = cur.rowcount
    cur.close()
    return n


def insert(table, values):
    """Insert a row and return its new id."""
    cols = list(values.keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    cur = get_db().cursor()
    if is_postgres():
        cur.execute(_sql(sql + " RETURNING id"), tuple(values.values()))
        new_id = cur.fetchone()["id"]
    else:
        cur.execute(sql, tuple(values.values()))
        new_id = cur.lastrowid
    cur.close()
    return new_id


def commit():
    get_db().commit()


def rollback():
    get_db().rollback()


def is_integrity_error(exc):
    name = type(exc).__name__
    return "Integrity" in name or "UniqueViolation" in name


def column_names(table):
    if is_postgres():
        rows = query("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,))
        return {r["column_name"] for r in rows}
    return {r["name"] for r in query(f"PRAGMA table_info({table})")}


def ensure_column(table, column, ddl):
    """Add a column to an existing table (simple forward-only migration)."""
    if column not in column_names(table):
        execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_schema():
    postgres = is_postgres()
    pk = "SERIAL PRIMARY KEY" if postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    real = "DOUBLE PRECISION" if postgres else "REAL"
    conn = get_db()
    cur = conn.cursor()
    for stmt in SCHEMA.format(pk=pk, real=real).split(";"):
        if stmt.strip():
            cur.execute(stmt)
    cur.close()
    conn.commit()

    # Migrations for databases created by earlier versions
    ensure_column("menu_items", "extra_cost_cents", "INTEGER NOT NULL DEFAULT 0")
    # Turn categories typed on dishes into managed categories
    for row in query("SELECT DISTINCT category FROM menu_items WHERE category != ''"):
        if not query("SELECT id FROM categories WHERE name = ?", (row["category"],), one=True):
            n = scalar("SELECT COUNT(*) AS n FROM categories") or 0
            insert("categories", {"name": row["category"], "sort_order": n + 1})
    conn.commit()
