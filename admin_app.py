import os
import json
import html
import csv
import io
from html import escape
import hmac
import hashlib
import secrets
import time
import traceback
import urllib.parse
import urllib.request
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
import psycopg2

from db_schema import DATABASE_URL, get_db_connection, init_db
from shop_settings import ADMIN_PANEL_TITLE, CURRENCY_SYMBOL

app = FastAPI()
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_MAX_AGE = 86400
MASTER_SESSION_COOKIE = "master_session"
MASTER_SESSION_MAX_AGE = 86400


@app.on_event("startup")
async def startup_db_init():
    init_db()

PAGE_STYLE = """
<style>
  body { font-family: Arial, sans-serif; background: #f4f6f8; color: #2b2b2b; margin: 0; padding: 0; }
  .container { max-width: 1100px; margin: 30px auto; padding: 24px; background: #ffffff; box-shadow: 0 10px 24px rgba(0,0,0,0.08); border-radius: 12px; }
  h1, h2 { margin: 0 0 16px; color: #1f2937; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border: 1px solid #d1d5db; padding: 12px 14px; }
  th { background: #f3f4f6; text-align: left; }
  tr:nth-child(even) td { background: #f9fafb; }
  .button, .button-link { display: inline-block; padding: 7px 12px; margin: 2px 4px 2px 0; color: #ffffff; background: #2563eb; text-decoration: none; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; }
  .button.secondary { background: #6b7280; }
  .card { padding: 18px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; margin-top: 16px; }
  .field { margin: 10px 0; }
  .field strong { color: #374151; }
  .status { font-weight: 700; color: #0f172a; }
  a { color: #2563eb; }
</style>
"""

def admin_css():
    return """
<style>
  :root {
    --bg: #121212;
    --panel: #1c1c1f;
    --panel-soft: #252529;
    --text: #f7f7f7;
    --muted: #b7b7bd;
    --line: #34343a;
    --accent: #f97316;
    --accent-hover: #ea580c;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { color: var(--accent-hover); }
  .admin-shell { min-height: 100vh; }
  .admin-topbar {
    border-bottom: 1px solid var(--line);
    background: #18181b;
  }
  .admin-nav {
    max-width: 1180px;
    margin: 0 auto;
    padding: 14px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
  }
  .admin-brand { font-weight: 700; letter-spacing: 0; color: var(--text); }
  .admin-links { display: flex; flex-wrap: wrap; gap: 14px; }
  .admin-links a { color: var(--muted); font-size: 14px; }
  .admin-links a:hover { color: var(--accent); }
  .admin-container {
    max-width: 1180px;
    margin: 0 auto;
    padding: 34px 28px;
  }
  .admin-card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 24px;
  }
  h1, h2 { margin: 0 0 16px; color: var(--text); }
  p { color: var(--muted); line-height: 1.5; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border-bottom: 1px solid var(--line); padding: 12px 14px; text-align: left; }
  th { background: var(--panel-soft); color: var(--text); }
  .button, .button-link, button {
    display: inline-block;
    padding: 8px 12px;
    border: 0;
    border-radius: 6px;
    background: var(--accent);
    color: #111111;
    font-weight: 700;
    cursor: pointer;
  }
  .button.secondary { background: #3f3f46; color: var(--text); }
  .status {
    display: inline-block;
    padding: 4px 8px;
    border: 1px solid rgba(249, 115, 22, 0.45);
    border-radius: 999px;
    background: rgba(249, 115, 22, 0.12);
    color: var(--accent);
    font-size: 13px;
    font-weight: 700;
    white-space: nowrap;
  }
  .status.warning {
    border-color: rgba(245, 158, 11, 0.45);
    background: rgba(245, 158, 11, 0.14);
    color: #fbbf24;
  }
  .status.info {
    border-color: rgba(56, 189, 248, 0.45);
    background: rgba(56, 189, 248, 0.12);
    color: #38bdf8;
  }
  .status.success {
    border-color: rgba(34, 197, 94, 0.45);
    background: rgba(34, 197, 94, 0.12);
    color: #4ade80;
  }
  .status.danger {
    border-color: rgba(248, 113, 113, 0.45);
    background: rgba(248, 113, 113, 0.12);
    color: #f87171;
  }
  .status.neutral {
    border-color: rgba(161, 161, 170, 0.35);
    background: rgba(161, 161, 170, 0.12);
    color: #d4d4d8;
  }
  .action-group {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 5px;
    min-width: 0;
    max-width: 190px;
  }
  .action-group form { display: inline-flex !important; margin: 0 !important; padding: 0 !important; }
  .action-group .button {
    margin: 0;
    padding: 5px 7px;
    font-size: 11px;
    line-height: 1.1;
    white-space: nowrap;
  }
  .dash-hero {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    margin-bottom: 22px;
  }
  .dash-kicker {
    margin: 0 0 8px;
    color: var(--accent);
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .dash-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 18px;
  }
  .dash-card {
    display: block;
    min-height: 118px;
    padding: 18px;
    border: 1px solid var(--line);
    border-top: 3px solid var(--accent);
    border-radius: 10px;
    background: var(--panel);
    color: var(--text);
  }
  .dash-card:hover { border-color: var(--accent); color: var(--text); }
  .dash-card strong { display: block; margin-bottom: 8px; font-size: 18px; }
  .dash-card span { color: var(--muted); font-size: 14px; }
  .dash-card.priority-urgent { border-top-color: #ef4444; }
  .dash-card.priority-today { border-top-color: #eab308; }
  .dash-card.priority-later { border-top-color: #3b82f6; }
  .attention-banner {
    margin: 16px 0;
    padding: 14px 16px;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-left: 4px solid var(--accent);
    border-radius: 8px;
    background: rgba(245, 158, 11, 0.10);
    color: var(--text);
    font-weight: 700;
  }
  tr.attention-row td {
    background: rgba(245, 158, 11, 0.08);
    border-bottom-color: rgba(245, 158, 11, 0.24);
  }
  .stat-value { display: block; margin-top: 10px; font-size: 28px; font-weight: 700; color: var(--text); }
  .dash-section { margin-top: 22px; }
  .dash-table-wrap { overflow-x: auto; }
  .view-link { font-weight: 700; white-space: nowrap; }
  .detail-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .detail-field {
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel-soft);
  }
  .detail-field strong {
    display: block;
    margin-bottom: 6px;
    color: var(--muted);
    font-size: 13px;
  }
  .products-table {
    min-width: 860px;
  }
  .products-table th,
  .products-table td {
    vertical-align: middle;
  }
  .products-table td:nth-child(2) {
    min-width: 180px;
    font-weight: 700;
  }
  .products-table td:nth-child(3) {
    white-space: nowrap;
  }
  .product-thumb {
    display: block;
    width: 72px;
    height: 54px;
    object-fit: cover;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-soft);
  }
  .status.active {
    border-color: rgba(34, 197, 94, 0.45);
    background: rgba(34, 197, 94, 0.12);
    color: #4ade80;
  }
  .status.inactive {
    border-color: rgba(148, 163, 184, 0.35);
    background: rgba(148, 163, 184, 0.12);
    color: #cbd5e1;
  }
  .products-table .action-group {
    max-width: 170px;
  }
  .category-row td {
    padding: 14px;
    background: rgba(249, 115, 22, 0.10);
    color: var(--text);
    font-size: 16px;
    font-weight: 700;
  }
  .quick-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
  }
  .quick-nav a {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-soft);
    color: var(--text);
    font-size: 13px;
    font-weight: 700;
  }
  .quick-nav a:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .categories-table {
    min-width: 640px;
  }
  .categories-table th,
  .categories-table td {
    vertical-align: middle;
  }
  .categories-table td:nth-child(2) {
    min-width: 180px;
    font-weight: 700;
  }
  .categories-table .action-group {
    max-width: 140px;
  }
  .admin-form {
    display: grid;
    gap: 16px;
    max-width: 720px;
  }
  .admin-form label {
    display: grid;
    gap: 7px;
    color: var(--muted);
    font-size: 14px;
    font-weight: 700;
  }
  .admin-form input {
    width: 100%;
    padding: 11px 12px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-soft);
    color: var(--text);
    font: inherit;
  }
  .admin-form input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent);
  }
  .form-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    margin-top: 4px;
  }
  @media (max-width: 720px) {
    .admin-nav { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
    .admin-container { padding: 22px 18px; }
    .admin-card { padding: 18px; }
    .dash-hero { align-items: flex-start; flex-direction: column; }
    .dash-grid { grid-template-columns: 1fr; }
    .detail-grid { grid-template-columns: 1fr; }
    .action-group { flex-direction: column; max-width: 150px; }
    table { display: block; overflow-x: auto; white-space: nowrap; }
  }
</style>
"""


def admin_layout(title, content, refresh_seconds=None):
    refresh_meta = ""
    if refresh_seconds:
        refresh_meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{title}</title>
  {admin_css()}
</head>
<body>
  <div class="admin-shell">
    <header class="admin-topbar">
      <nav class="admin-nav">
        <a class="admin-brand" href="/">🏠 Главная</a>
        <div class="admin-links">
          <a href="/orders">📦 Заказы</a>
          <a href="/products">🛒 Товары</a>
          <a href="/categories">🗂 Категории</a>
          <a href="/clients">👥 Клиенты</a>
          <a href="/broadcasts">📨 Рассылка</a>
          <a href="/channel">📢 Канал</a>
          <a href="/logs">🧾 Логи</a>
          <a href="/logout">Выйти</a>
        </div>
      </nav>
    </header>
    <main class="admin-container">{content}</main>
  </div>
</body>
</html>"""


def admin_error_page(title, message):
    content = f"""
    <section class="admin-card">
      <h1>{html.escape(str(title))}</h1>
      <p>{html.escape(str(message))}</p>
      <p><a class="button button-link" href="/">На главную</a></p>
    </section>
    """
    return admin_layout(title, content)


def master_layout(title, content, refresh_seconds=None):
    refresh_meta = ""
    if refresh_seconds:
        refresh_meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{html.escape(str(title))}</title>
  {admin_css()}
</head>
<body>
  <div class="admin-shell">
    <header class="admin-topbar">
      <nav class="admin-nav">
        <a class="admin-brand" href="/master">Master Admin</a>
        <div class="admin-links">
          <a href="/master">Shops</a>
          <a href="/master/logout">Logout</a>
        </div>
      </nav>
    </header>
    <main class="admin-container">{content}</main>
  </div>
</body>
</html>"""


def master_error_page(title, message):
    content = f"""
    <section class="admin-card">
      <h1>{html.escape(str(title))}</h1>
      <p>{html.escape(str(message))}</p>
      <p><a class="button button-link" href="/master">Back to Master Dashboard</a></p>
    </section>
    """
    return master_layout(title, content)


def admin_auth_configured():
    return bool(os.getenv("ADMIN_PASSWORD") and os.getenv("ADMIN_SESSION_SECRET"))


def master_auth_configured():
    return bool(os.getenv("MASTER_ADMIN_PASSWORD") and os.getenv("MASTER_ADMIN_SESSION_SECRET"))


def sign_admin_session():
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    expires_at = int(time.time()) + ADMIN_SESSION_MAX_AGE
    payload = f"admin:{expires_at}:{secrets.token_urlsafe(16)}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def sign_master_session():
    secret = os.getenv("MASTER_ADMIN_SESSION_SECRET", "")
    expires_at = int(time.time()) + MASTER_SESSION_MAX_AGE
    payload = f"master:{expires_at}:{secrets.token_urlsafe(16)}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return f"{payload}:{signature}"


def verify_admin_session(value):
    if not value or not admin_auth_configured():
        return False
    secret = os.getenv("ADMIN_SESSION_SECRET", "")
    try:
        payload, signature = value.rsplit(":", 1)
        expected = hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        parts = payload.split(":")
        if len(parts) < 3 or parts[0] != "admin":
            return False
        return int(parts[1]) >= int(time.time())
    except Exception:
        return False


def verify_master_session(value):
    if not value or not master_auth_configured():
        return False
    secret = os.getenv("MASTER_ADMIN_SESSION_SECRET", "")
    try:
        payload, signature = value.rsplit(":", 1)
        expected = hmac.new(
            secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        parts = payload.split(":")
        if len(parts) < 3 or parts[0] != "master":
            return False
        return int(parts[1]) >= int(time.time())
    except Exception:
        return False


def is_admin_authenticated(request):
    return verify_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE))


def is_master_authenticated(request):
    return verify_master_session(request.cookies.get(MASTER_SESSION_COOKIE))


def login_page(message=""):
    message_html = f"<p>{html.escape(message)}</p>" if message else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход</title>
  {admin_css()}
</head>
<body>
  <main class="admin-container">
    <section class="admin-card" style="max-width: 420px; margin: 8vh auto 0;">
      <h1>Вход в админ-панель</h1>
      {message_html}
      <form class="admin-form" method="post" action="/login">
        <label>Пароль
          <input type="password" name="password" autocomplete="current-password" required>
        </label>
        <div class="form-actions">
          <button type="submit">Войти</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>"""


def master_login_page(message=""):
    message_html = f"<p>{html.escape(message)}</p>" if message else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Master Login</title>
  {admin_css()}
</head>
<body>
  <main class="admin-container">
    <section class="admin-card" style="max-width: 420px; margin: 8vh auto 0;">
      <h1>Master Admin Login</h1>
      {message_html}
      <form class="admin-form" method="post" action="/master/login">
        <label>Password
          <input type="password" name="password" autocomplete="current-password" required>
        </label>
        <div class="form-actions">
          <button type="submit">Log in</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>"""


@app.middleware("http")
async def require_admin_login(request: Request, call_next):
    if request.url.path.startswith("/master"):
        public_master_paths = {"/master/login", "/master/health"}
        if request.url.path in public_master_paths:
            return await call_next(request)
        if is_master_authenticated(request):
            return await call_next(request)
        return RedirectResponse("/master/login", status_code=303)

    public_paths = {"/login", "/health"}
    if request.url.path in public_paths:
        return await call_next(request)
    if is_admin_authenticated(request):
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form():
    if not admin_auth_configured():
        return login_page("Admin auth is not configured.")
    return login_page()


@app.post("/login", response_class=HTMLResponse)
async def login(password: str = Form(...)):
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_auth_configured():
        return login_page("Admin auth is not configured.")
    if not secrets.compare_digest(password, admin_password):
        return login_page("Неверный пароль.")

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        sign_admin_session(),
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(ADMIN_SESSION_COOKIE)
    return response


@app.get("/master/login", response_class=HTMLResponse)
async def master_login_form():
    if not master_auth_configured():
        return master_login_page("Master admin auth is not configured.")
    return master_login_page()


@app.post("/master/login", response_class=HTMLResponse)
async def master_login(password: str = Form(...)):
    master_password = os.getenv("MASTER_ADMIN_PASSWORD", "")
    if not master_auth_configured():
        return master_login_page("Master admin auth is not configured.")
    if not secrets.compare_digest(password, master_password):
        return master_login_page("Invalid password.")

    response = RedirectResponse("/master", status_code=303)
    response.set_cookie(
        MASTER_SESSION_COOKIE,
        sign_master_session(),
        max_age=MASTER_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
    )
    return response


@app.get("/master/logout")
async def master_logout():
    response = RedirectResponse("/master/login", status_code=303)
    response.delete_cookie(MASTER_SESSION_COOKIE)
    return response


def admin_status_badge(status):
    status_key = str(status or "").strip()
    labels = {
        "pending": ("Ожидает выбора оплаты", "warning"),
        "awaiting_payment": ("Ожидает оплаты", "warning"),
        "payment_reported": ("Оплата заявлена", "info"),
        "cash_on_delivery": ("Наличными", "neutral"),
        "cancelled": ("Отменён", "danger"),
        "paid": ("Оплачен", "success"),
        "preparing": ("Готовится", "info"),
        "done": ("Готов", "success"),
    }
    label, status_class = labels.get(status_key, (status_key or "-", "neutral"))
    return f"<span class='status {status_class}'>{html.escape(label)}</span>"


def admin_status_label(status):
    labels = {
        "pending": "Ожидает выбора оплаты",
        "awaiting_payment": "Ожидает оплаты",
        "payment_reported": "Оплата заявлена",
        "cash_on_delivery": "Наличными",
        "paid": "Оплачен",
        "preparing": "Готовится",
        "done": "Готов",
        "cancelled": "Отменён",
    }
    return labels.get(str(status or ""), str(status or "-"))


def order_status_actions_for(status):
    return {
        "pending": [("cancelled", "Отмена")],
        "awaiting_payment": [("cancelled", "Отмена")],
        "payment_reported": [("paid", "Подтвердить оплату"), ("cancelled", "Отмена")],
        "cash_on_delivery": [("paid", "Оплачено наличными"), ("cancelled", "Отмена")],
        "paid": [("preparing", "Готовится"), ("done", "Готово"), ("cancelled", "Отмена")],
        "preparing": [("done", "Готово"), ("cancelled", "Отмена")],
        "done": [],
        "cancelled": [],
    }.get(str(status or ""), [])


def format_admin_datetime(value):
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return html.escape(value.strftime("%d.%m.%Y %H:%M"))
    return html.escape(str(value))


def format_stock_grams(stock_grams):
    stock = max(int(stock_grams or 0), 0)
    if stock >= 1000:
        return f"{stock / 1000:g} кг"
    return f"{stock} г"


def admin_event_type_label(event_type):
    labels = {
        "order_created": "Заказ создан",
        "payment_selected": "Выбран способ оплаты",
        "payment_reported": "Клиент сообщил об оплате",
        "payment_confirmed": "Оплата подтверждена",
        "status_changed": "Статус изменён",
        "inventory_deducted": "Склад списан",
        "stock_restored": "Склад восстановлен",
        "notification_sent": "Уведомление отправлено",
        "notification_failed": "Ошибка уведомления",
        "order_note_updated": "Заметка обновлена",
        "order_completed": "Заказ завершён",
        "order_cancelled": "Заказ отменён",
        "item_weighed": "Товар взвешен",
        "weighing_notification_sent": "Клиенту отправлено уведомление о взвешивании",
        "weighing_notification_failed": "Ошибка уведомления о взвешивании",
    }
    return labels.get(str(event_type or ""), str(event_type or "-"))


def log_order_event(cursor, order_id, event_type, event_text):
    cursor.execute(
        """
        INSERT INTO order_events
        (order_id, event_type, event_text)
        VALUES (%s, %s, %s)
        """,
        (order_id, event_type, event_text)
    )


def log_inventory_movement(
    cursor,
    product_id,
    movement_type,
    quantity_grams,
    stock_before=None,
    stock_after=None,
    order_id=None,
    note=""
):
    cursor.execute(
        """
        INSERT INTO inventory_movements
        (product_id, order_id, movement_type, quantity_grams, stock_before, stock_after, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (product_id, order_id, movement_type, quantity_grams, stock_before, stock_after, note),
    )


def get_admin_chat_id():
    admin_id = os.getenv("ADMIN_ID")
    if admin_id:
        return admin_id

    try:
        with open("config.json", "r", encoding="utf-8") as file:
            config = json.load(file)
    except Exception:
        return None

    return config.get("admin_id")


def send_low_stock_alert(product_name, stock_grams, threshold_grams):
    bot_token = os.getenv("BOT_TOKEN")
    admin_id = get_admin_chat_id()
    if not bot_token or not admin_id:
        return

    text = (
        "⚠️ Низкий остаток\n\n"
        f"Товар: {product_name}\n"
        f"Остаток: {stock_grams} г\n"
        f"Порог: {threshold_grams} г"
    )
    data = urllib.parse.urlencode({
        "chat_id": admin_id,
        "text": text,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()


def sync_low_stock_alert_state(cursor, product_ids):
    seen_product_ids = []
    for product_id in product_ids or []:
        if not product_id or product_id in seen_product_ids:
            continue
        seen_product_ids.append(product_id)

    for product_id in seen_product_ids:
        cursor.execute(
            """
            UPDATE products
            SET low_stock_alert_sent = FALSE,
                low_stock_alert_sent_at = NULL
            WHERE id = %s
              AND low_stock_alert_sent = TRUE
              AND (
                  low_stock_threshold_grams <= 0
                  OR stock_grams > low_stock_threshold_grams
              )
            """,
            (product_id,),
        )
        cursor.execute(
            """
            SELECT name, stock_grams, low_stock_threshold_grams
            FROM products
            WHERE id = %s
              AND is_active = TRUE
              AND is_out_of_stock = FALSE
              AND stock_grams > 0
              AND low_stock_threshold_grams > 0
              AND stock_grams <= low_stock_threshold_grams
              AND low_stock_alert_sent = FALSE
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            continue

        name, stock_grams, threshold_grams = row
        try:
            send_low_stock_alert(name, stock_grams, threshold_grams)
            cursor.execute(
                """
                UPDATE products
                SET low_stock_alert_sent = TRUE,
                    low_stock_alert_sent_at = NOW()
                WHERE id = %s
                """,
                (product_id,),
            )
        except Exception as e:
            print(f"LOW STOCK ALERT ERROR: product_id={product_id}:", e)


def send_order_status_notification(telegram_id, order_id, status):
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or not telegram_id:
        return

    messages = {
        "paid": f"✅ Ваш заказ №{order_id} подтверждён и оплачен.",
        "preparing": f"👨‍🍳 Ваш заказ №{order_id} передан в работу и готовится.",
        "done": f"📦 Ваш заказ №{order_id} готов.\nСпасибо за заказ!",
        "cancelled": f"❌ Заказ №{order_id} отменён.\nЕсли произошла ошибка — свяжитесь с администратором.",
    }
    text = messages.get(status)
    if not text:
        return

    data = urllib.parse.urlencode({
        "chat_id": telegram_id,
        "text": text,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()


def send_weighing_complete_notification(
    telegram_id,
    order_id,
    total,
    items,
    photo_bytes=None,
    photo_filename=None,
    photo_content_type=None,
):
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or not telegram_id:
        raise RuntimeError("BOT_TOKEN or telegram_id is missing")

    item_lines = ""
    for product_name, weight, price, option_label in items:
        label_or_weight = option_label if option_label else f"{weight} г"
        item_lines += (
            f"\n• {product_name}\n"
            f"Вариант/вес: {label_or_weight}\n"
            f"Вес: {weight} г\n"
            f"Стоимость: {float(price or 0):.2f} €\n"
        )

    text = (
        f"⚖️ Ваш заказ полностью собран.\n\n"
        f"📦 Номер заказа:\n#{order_id}\n"
        f"{item_lines}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Итого к оплате:\n{float(total or 0):.2f} €\n\n"
        f"👇 Выберите способ оплаты:"
    )
    inline_keyboard = [
        [{"text": "🏦 Оплата IBAN", "callback_data": "pay_iban"}],
        [{"text": "💵 Оплата наличными", "callback_data": "pay_cash"}],
        [{"text": "🅿️ PayPal", "callback_data": "pay_paypal"}],
        [{"text": "🛍 Продолжить покупки", "callback_data": "back_to_menu"}],
    ]
    reply_markup_json = json.dumps({"inline_keyboard": inline_keyboard})

    if photo_bytes and len(text) <= 1024:
        boundary = secrets.token_hex(16)
        boundary_bytes = boundary.encode("utf-8")
        parts = []

        def add_field(name, value):
            parts.append(b"--" + boundary_bytes + b"\r\n")
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            parts.append(str(value).encode("utf-8") + b"\r\n")

        add_field("chat_id", telegram_id)
        add_field("caption", text)
        add_field("reply_markup", reply_markup_json)

        parts.append(b"--" + boundary_bytes + b"\r\n")
        parts.append(
            'Content-Disposition: form-data; name="photo"; filename="{}"\r\n'.format(
                photo_filename or "photo.jpg"
            ).encode("utf-8")
        )
        parts.append(
            f"Content-Type: {photo_content_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8")
        )
        parts.append(photo_bytes + b"\r\n")
        parts.append(b"--" + boundary_bytes + b"--\r\n")

        body = b"".join(parts)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(request, timeout=15).read()
        return

    data = urllib.parse.urlencode({
        "chat_id": telegram_id,
        "text": text,
        "reply_markup": reply_markup_json,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()


def get_channel_chat_id():
    return os.getenv("TELEGRAM_CHANNEL_ID")


def send_channel_post(message_text):
    try:
        bot_token = os.getenv("BOT_TOKEN")
        channel_chat_id = get_channel_chat_id()
        if not bot_token or not channel_chat_id:
            return False, "BOT_TOKEN or TELEGRAM_CHANNEL_ID is missing"

        data = urllib.parse.urlencode({
            "chat_id": channel_chat_id,
            "text": message_text,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST"
        )
        urllib.request.urlopen(request, timeout=5).read()
        return True, None
    except Exception as error:
        return False, str(error)


def send_broadcast_message(telegram_id, message_text):
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token or not telegram_id:
            return False, "failed"

        data = urllib.parse.urlencode({
            "chat_id": telegram_id,
            "text": message_text,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST"
        )
        urllib.request.urlopen(request, timeout=5).read()
        return True, "sent"
    except Exception as error:
        error_text = str(error).lower()
        if "403" in error_text or "forbidden" in error_text:
            return False, "blocked"
        return False, "failed"


def channel_post_status_label(status):
    labels = {
        "draft": "Черновик",
        "sent": "Отправлено",
        "failed": "Ошибка",
    }
    return html.escape(labels.get(str(status or ""), str(status or "-")))


def broadcast_status_label(status):
    labels = {
        "draft": "Черновик",
        "sending": "Отправляется",
        "sent": "Отправлено",
        "failed": "Ошибка",
    }
    return html.escape(labels.get(str(status or ""), str(status or "-")))


def broadcast_target_label(target_type):
    labels = {
        "all_clients": "Все клиенты",
        "clients_with_orders": "Клиенты с заказами",
        "active_last_7_days": "Активные за 7 дней",
        "active_last_30_days": "Активные за 30 дней",
        "awaiting_payment": "Ожидают оплату",
    }
    return html.escape(labels.get(str(target_type or ""), str(target_type or "-")))


def log_admin_error(route, action, error):
    try:
        tb = traceback.format_exc()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO error_logs
            (route, action, error_message, traceback)
            VALUES (%s, %s, %s, %s)
            """,
            (route, action, str(error), tb),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as log_error:
        print(f"Failed to write admin error log: {log_error}")


def seed_default_master_shop(cursor):
    seed_rows = [
        (
            "current",
            ADMIN_PANEL_TITLE or "Current Shop",
            None,
            None,
            None,
            None,
            "live",
            "Current local shop deployment",
        ),
        (
            "deal-market-nl",
            "Deal Market NL",
            "https://admin-production-1523.up.railway.app",
            "DealMarketNL_bot",
            None,
            None,
            "live",
            "Main live Telegram shop project.",
        ),
        (
            "koptilnya-demo",
            "Koptilnya Demo",
            None,
            "zakaz_koptim_bot",
            "https://t.me/zakaz_koptim_bot",
            None,
            "demo",
            "Demo Telegram shop for smoked products.",
        ),
        (
            "angelinix-automation",
            "Angelinix Automation",
            None,
            None,
            None,
            "https://butesha-beep.github.io/AngelinixAutomation/",
            "draft",
            "Landing/demo funnel for automation services.",
        ),
        (
            "irpin-installers",
            "Irpin Installers",
            None,
            None,
            None,
            None,
            "draft",
            "Demo landing project for installer/repair services.",
        ),
    ]
    for row in seed_rows:
        cursor.execute(
            """
            INSERT INTO master_shops (
                shop_key,
                brand_name,
                admin_url,
                bot_username,
                bot_url,
                landing_url,
                status,
                notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (shop_key) DO NOTHING
            """,
            row,
        )


def create_current_master_snapshot(cursor):
    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN created_at::date = CURRENT_DATE THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status IN ('pending', 'awaiting_payment', 'payment_reported', 'cash_on_delivery') THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN status = 'done' AND date_trunc('month', created_at) = date_trunc('month', NOW()) THEN total ELSE 0 END), 0)
        FROM orders
        """
    )
    total_orders, today_orders, pending_orders, month_revenue = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM clients")
    total_clients = cursor.fetchone()[0]
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM products
        WHERE
            is_active = TRUE
            AND is_out_of_stock = FALSE
            AND low_stock_threshold_grams > 0
            AND stock_grams > 0
            AND stock_grams <= low_stock_threshold_grams
        """
    )
    low_stock_count = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO master_shop_snapshots (
            shop_key,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            "current",
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
        ),
    )


def master_snapshot_value(value):
    if value is None:
        return "-"
    return html.escape(str(value))


def master_money_value(value):
    if value is None:
        return "-"
    return f"{CURRENCY_SYMBOL}{float(value):.2f}"


def master_project_status_badge(status):
    status_key = str(status or "").strip().lower()
    labels = {
        "live": ("Live", "success"),
        "demo": ("Demo", "info"),
        "paused": ("Paused", "warning"),
        "draft": ("Draft", "neutral"),
        "error": ("Error", "danger"),
    }
    if status_key not in labels:
        return html.escape(str(status or "-"))
    label, status_class = labels[status_key]
    return f"<span class='status {status_class}'>{label}</span>"


def master_shop_rows(rows):
    if not rows:
        return """
        <section class="admin-card">
          <h1>Master Dashboard</h1>
          <p>No shops are registered yet.</p>
        </section>
        """

    rendered = ""
    for row in rows:
        (
            shop_key,
            brand_name,
            admin_url,
            bot_username,
            bot_url,
            landing_url,
            status,
            notes,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at,
        ) = row
        shop_key_text = html.escape(str(shop_key or ""))
        admin_link = "<span>-</span>"
        if admin_url:
            safe_admin_url = html.escape(str(admin_url), quote=True)
            admin_link = f'<a class="button button-link" href="{safe_admin_url}">Open admin</a>'
        bot_link = ""
        if bot_url:
            safe_bot_url = html.escape(str(bot_url), quote=True)
            bot_link = f'<a class="button button-link secondary" href="{safe_bot_url}">Open bot</a>'
        landing_link = "<span>-</span>"
        if landing_url:
            safe_landing_url = html.escape(str(landing_url), quote=True)
            landing_link = f'<a class="button button-link secondary" href="{safe_landing_url}">Open landing</a>'
        bot_username_text = html.escape(str(bot_username or "-"))
        rendered += f"""
        <article class="dash-card">
          <strong><a class="view-link" href="/master/shops/{urllib.parse.quote(str(shop_key or ''), safe='')}">{html.escape(str(brand_name or '-'))}</a></strong>
          <span>Shop key: {shop_key_text}</span><br>
          <span>Status: {master_project_status_badge(status)}</span><br>
          <span>Bot username: {bot_username_text}</span>
          <div class="detail-grid" style="margin-top: 16px;">
            <div><span>Total orders</span><strong>{master_snapshot_value(total_orders)}</strong></div>
            <div><span>Today</span><strong>{master_snapshot_value(today_orders)}</strong></div>
            <div><span>Pending</span><strong>{master_snapshot_value(pending_orders)}</strong></div>
            <div><span>Month revenue</span><strong>{master_money_value(month_revenue)}</strong></div>
            <div><span>Low stock</span><strong>{master_snapshot_value(low_stock_count)}</strong></div>
            <div><span>Clients</span><strong>{master_snapshot_value(total_clients)}</strong></div>
          </div>
          <p>Last seen: {master_snapshot_value(last_seen_at)}</p>
          <p>{html.escape(str(notes or '-'))}</p>
          <div class="form-actions">
            {bot_link}
            {admin_link}
            {landing_link}
          </div>
        </article>
        """

    return f"""
    <section class="admin-card">
      <h1>Master Dashboard</h1>
      <p>Read-only overview of registered shop deployments.</p>
      <div class="dash-grid" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));">
        {rendered}
      </div>
    </section>
    """


@app.get("/master", response_class=HTMLResponse)
async def master_dashboard():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        seed_default_master_shop(cursor)
        create_current_master_snapshot(cursor)
        conn.commit()
        cursor.execute(
            """
            SELECT
                s.shop_key,
                s.brand_name,
                s.admin_url,
                s.bot_username,
                s.bot_url,
                s.landing_url,
                s.status,
                s.notes,
                snap.total_orders,
                snap.today_orders,
                snap.pending_orders,
                snap.month_revenue,
                snap.low_stock_count,
                snap.total_clients,
                snap.last_seen_at
            FROM master_shops s
            LEFT JOIN LATERAL (
                SELECT *
                FROM master_shop_snapshots mss
                WHERE mss.shop_key = s.shop_key
                ORDER BY mss.last_seen_at DESC, mss.id DESC
                LIMIT 1
            ) snap ON TRUE
            ORDER BY s.brand_name ASC, s.shop_key ASC
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return master_layout("Master Dashboard", master_shop_rows(rows))
    except Exception as e:
        log_admin_error("/master", "master_dashboard", e)
        return master_error_page("Error", "Could not load Master Dashboard.")


@app.get("/master/shops/{shop_key}", response_class=HTMLResponse)
async def master_shop_detail(shop_key: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        seed_default_master_shop(cursor)
        conn.commit()
        cursor.execute(
            """
            SELECT
                s.shop_key,
                s.brand_name,
                s.admin_url,
                s.bot_username,
                s.bot_url,
                s.landing_url,
                s.status,
                s.notes,
                snap.total_orders,
                snap.today_orders,
                snap.pending_orders,
                snap.month_revenue,
                snap.low_stock_count,
                snap.total_clients,
                snap.last_seen_at
            FROM master_shops s
            LEFT JOIN LATERAL (
                SELECT *
                FROM master_shop_snapshots mss
                WHERE mss.shop_key = s.shop_key
                ORDER BY mss.last_seen_at DESC, mss.id DESC
                LIMIT 1
            ) snap ON TRUE
            WHERE s.shop_key = %s
            """,
            (shop_key,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return master_error_page("Shop not found", "This shop is not registered in Master Admin.")

        (
            shop_key_value,
            brand_name,
            admin_url,
            bot_username,
            bot_url,
            landing_url,
            status,
            notes,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at,
        ) = row
        admin_link = html.escape(str(admin_url or "-"))
        if admin_url:
            safe_admin_url = html.escape(str(admin_url), quote=True)
            admin_link = f'<a href="{safe_admin_url}">{safe_admin_url}</a>'
        bot_link = html.escape(str(bot_url or "-"))
        if bot_url:
            safe_bot_url = html.escape(str(bot_url), quote=True)
            bot_link = f'<a href="{safe_bot_url}">{safe_bot_url}</a>'
        landing_link = html.escape(str(landing_url or "-"))
        if landing_url:
            safe_landing_url = html.escape(str(landing_url), quote=True)
            landing_link = f'<a href="{safe_landing_url}">{safe_landing_url}</a>'

        content = f"""
        <section class="admin-card">
          <h1>{html.escape(str(brand_name or shop_key_value or 'Shop'))}</h1>
          <div class="detail-grid">
            <div class="detail-field"><strong>Shop key</strong>{html.escape(str(shop_key_value or '-'))}</div>
            <div class="detail-field"><strong>Status</strong>{master_project_status_badge(status)}</div>
            <div class="detail-field"><strong>Bot username</strong>{html.escape(str(bot_username or '-'))}</div>
            <div class="detail-field"><strong>Bot URL</strong>{bot_link}</div>
            <div class="detail-field"><strong>Admin URL</strong>{admin_link}</div>
            <div class="detail-field"><strong>Landing URL</strong>{landing_link}</div>
            <div class="detail-field"><strong>Notes</strong>{html.escape(str(notes or '-'))}</div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Latest Snapshot</h2>
          <div class="dash-grid">
            <div class="dash-card"><span>Total orders</span><strong class="stat-value">{master_snapshot_value(total_orders)}</strong></div>
            <div class="dash-card"><span>Today orders</span><strong class="stat-value">{master_snapshot_value(today_orders)}</strong></div>
            <div class="dash-card"><span>Pending orders</span><strong class="stat-value">{master_snapshot_value(pending_orders)}</strong></div>
            <div class="dash-card"><span>Month revenue</span><strong class="stat-value">{master_money_value(month_revenue)}</strong></div>
            <div class="dash-card"><span>Low stock</span><strong class="stat-value">{master_snapshot_value(low_stock_count)}</strong></div>
            <div class="dash-card"><span>Total clients</span><strong class="stat-value">{master_snapshot_value(total_clients)}</strong></div>
            <div class="dash-card"><span>Last seen</span><strong class="stat-value">{master_snapshot_value(last_seen_at)}</strong></div>
          </div>
        </section>
        """
        return master_layout(f"Master Shop: {brand_name or shop_key_value}", content)
    except Exception as e:
        log_admin_error("/master/shops/{shop_key}", "master_shop_detail", e)
        return master_error_page("Error", "Could not load shop details.")


@app.get("/", response_class=HTMLResponse)
async def root():
    stats = None
    latest_orders = []
    top_products = []
    worst_products = []
    best_customers = []
    repeat_customers = []
    low_stock_products = []
    low_stock_count = 0
    funnel_rows = []
    error_message = None
    cc_pending_weighing_count = 0
    cc_low_stock_count = 0
    cc_recent_errors_count = 0
    cc_no_recommendations_count = 0
    cc_no_promotion_count = 0
    cc_no_sales_count = 0
    cc_no_image_count = 0
    cc_no_description_count = 0
    cc_never_sold_count = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'awaiting_payment' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'payment_reported' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'cash_on_delivery' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'preparing' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN created_at::date = CURRENT_DATE THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'done' THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'done' AND created_at::date = CURRENT_DATE THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'done' AND date_trunc('month', created_at) = date_trunc('month', NOW()) THEN total ELSE 0 END), 0)
            FROM orders
            """
        )
        stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT
                oi.product_id,
                COALESCE(p.name, oi.product_name) AS product_name,
                COALESCE(SUM(oi.weight), 0) AS grams_sold,
                COALESCE(SUM(oi.price), 0) AS revenue
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE o.status = 'done'
            GROUP BY oi.product_id, COALESCE(p.name, oi.product_name)
            ORDER BY revenue DESC
            LIMIT 5
            """
        )
        top_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                p.id,
                p.name,
                COALESCE(s.grams_sold, 0) AS grams_sold,
                COALESCE(s.revenue, 0) AS revenue,
                COALESCE(s.units_sold, 0) AS units_sold
            FROM products p
            LEFT JOIN (
                SELECT
                    oi.product_id,
                    COUNT(*) AS units_sold,
                    COALESCE(SUM(oi.weight), 0) AS grams_sold,
                    COALESCE(SUM(oi.price), 0) AS revenue
                FROM order_items oi
                JOIN orders o ON o.order_id = oi.order_id
                WHERE o.status = 'done'
                GROUP BY oi.product_id
            ) s ON s.product_id = p.id
            WHERE p.is_active = TRUE
            ORDER BY revenue ASC, units_sold ASC, p.name ASC
            LIMIT 5
            """
        )
        worst_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                telegram_id,
                COALESCE(MAX(username), '-') AS username,
                COALESCE(MAX(phone), '-') AS phone,
                COUNT(*) AS orders_count,
                COALESCE(SUM(total), 0) AS total_spent
            FROM orders
            WHERE status = 'done'
            GROUP BY telegram_id
            ORDER BY total_spent DESC
            LIMIT 5
            """
        )
        best_customers = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                telegram_id,
                COALESCE(MAX(username), '-') AS username,
                COALESCE(MAX(phone), '-') AS phone,
                COUNT(*) AS completed_orders,
                COALESCE(SUM(total), 0) AS total_spent
            FROM orders
            WHERE status = 'done'
            GROUP BY telegram_id
            HAVING COUNT(*) >= 2
            ORDER BY completed_orders DESC, total_spent DESC
            LIMIT 5
            """
        )
        repeat_customers = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                id,
                name,
                stock_grams,
                low_stock_threshold_grams
            FROM products
            WHERE
                is_active = TRUE
                AND is_out_of_stock = FALSE
                AND low_stock_threshold_grams > 0
                AND stock_grams > 0
                AND stock_grams <= low_stock_threshold_grams
            ORDER BY stock_grams ASC
            LIMIT 10
            """
        )
        low_stock_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE
                is_active = TRUE
                AND is_out_of_stock = FALSE
                AND low_stock_threshold_grams > 0
                AND stock_grams > 0
                AND stock_grams <= low_stock_threshold_grams
            """
        )
        low_stock_count = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT stage, label, COALESCE(users_count, 0) AS users_count
            FROM (
                VALUES
                    (1, 'start', 'Стартовали бота'),
                    (2, 'view_category', 'Открыли категорию'),
                    (3, 'view_product', 'Открыли товар'),
                    (4, 'add_to_cart', 'Добавили в корзину'),
                    (5, 'checkout_started', 'Начали оформление'),
                    (6, 'order_created', 'Создали заказ'),
                    (7, 'payment_method_selected', 'Выбрали оплату'),
                    (8, 'payment_reported', 'Сообщили об оплате')
            ) AS funnel(sort_order, stage, label)
            LEFT JOIN (
                SELECT event_type, COUNT(DISTINCT telegram_id) AS users_count
                FROM customer_events
                WHERE created_at::date = CURRENT_DATE
                GROUP BY event_type
            ) events ON events.event_type = funnel.stage
            ORDER BY sort_order
            """
        )
        funnel_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT order_id, username, phone, address, total, status
            FROM orders
            ORDER BY id DESC
            LIMIT 5
            """
        )
        latest_orders = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(DISTINCT o.order_id)
            FROM orders o
            WHERE o.status != 'cancelled'
              AND EXISTS (
                  SELECT 1
                  FROM order_items oi
                  WHERE oi.order_id = o.order_id
                    AND oi.weight IS NULL
              )
            """
        )
        cc_pending_weighing_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND (
                  p.is_out_of_stock = TRUE
                  OR (
                      p.stock_grams IS NOT NULL
                      AND p.low_stock_threshold_grams IS NOT NULL
                      AND p.stock_grams <= p.low_stock_threshold_grams
                  )
              )
            """
        )
        cc_low_stock_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM error_logs
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            """
        )
        cc_recent_errors_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM product_recommendations pr
                  WHERE pr.product_id = p.id
                    AND pr.recommendation_type = 'frequently_bought_together'
                    AND pr.is_active = TRUE
              )
            """
        )
        cc_no_recommendations_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND is_promotion = FALSE
            """
        )
        cc_no_promotion_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  WHERE oi.product_id = p.id
                    AND o.status != 'cancelled'
                    AND o.created_at >= NOW() - INTERVAL '14 days'
              )
            """
        )
        cc_no_sales_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND (image_url IS NULL OR TRIM(image_url) = '')
            """
        )
        cc_no_image_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND (description IS NULL OR TRIM(description) = '')
            """
        )
        cc_no_description_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  WHERE oi.product_id = p.id
                    AND o.status != 'cancelled'
              )
            """
        )
        cc_never_sold_count = cursor.fetchone()[0]

        conn.close()
    except Exception as e:
        log_admin_error("/", "dashboard", e)
        error_message = str(e)

    if stats:
        (
            total_orders,
            pending_orders,
            awaiting_payment_orders,
            payment_reported_orders,
            cash_on_delivery_orders,
            paid_orders,
            preparing_orders,
            done_orders,
            cancelled_orders,
            today_orders,
            total_done_revenue,
            today_done_revenue,
            month_done_revenue,
        ) = stats
        awaiting_action = (
            pending_orders
            + awaiting_payment_orders
            + payment_reported_orders
            + cash_on_delivery_orders
        )
        stat_cards = f"""
        <h2>Обзор</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Всего заказов</span><strong class="stat-value">{total_orders}</strong></div>
          <div class="dash-card"><span>Заказов сегодня</span><strong class="stat-value">{today_orders}</strong></div>
          <div class="dash-card"><span>Требуют внимания</span><strong class="stat-value">{awaiting_action}</strong></div>
        </div>
        <h2>Требуют внимания</h2>
        <div class="dash-grid">
          <a class="dash-card" href="/orders?status_filter=pending"><span>Новые заказы</span><strong class="stat-value">{pending_orders}</strong></a>
          <a class="dash-card" href="/orders?status_filter=awaiting_payment"><span>Ожидают оплаты</span><strong class="stat-value">{awaiting_payment_orders}</strong></a>
          <a class="dash-card" href="/orders?status_filter=payment_reported"><span>Проверить оплату</span><strong class="stat-value">{payment_reported_orders}</strong></a>
          <a class="dash-card" href="/orders?status_filter=cash_on_delivery"><span>Наличными</span><strong class="stat-value">{cash_on_delivery_orders}</strong></a>
          <a class="dash-card" href="/products"><span>Низкий остаток</span><strong class="stat-value">{low_stock_count}</strong></a>
        </div>
        <h2>Выручка</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Всего завершённых</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(total_done_revenue):.2f}</strong></div>
          <div class="dash-card"><span>Сегодня</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(today_done_revenue):.2f}</strong></div>
          <div class="dash-card"><span>За месяц</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(month_done_revenue):.2f}</strong></div>
        </div>
        <h2>Статусы</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Ожидает выбора оплаты</span><strong class="stat-value">{pending_orders}</strong></div>
          <div class="dash-card"><span>Ожидает оплаты</span><strong class="stat-value">{awaiting_payment_orders}</strong></div>
          <div class="dash-card"><span>Оплата заявлена</span><strong class="stat-value">{payment_reported_orders}</strong></div>
          <div class="dash-card"><span>Наличными</span><strong class="stat-value">{cash_on_delivery_orders}</strong></div>
          <div class="dash-card"><span>Оплачен</span><strong class="stat-value">{paid_orders}</strong></div>
          <div class="dash-card"><span>Готовится</span><strong class="stat-value">{preparing_orders}</strong></div>
          <div class="dash-card"><span>Готов</span><strong class="stat-value">{done_orders}</strong></div>
          <div class="dash-card"><span>Отменён</span><strong class="stat-value">{cancelled_orders}</strong></div>
        </div>
        """
    else:
        stat_cards = f"""
        <section class="admin-card">
          <h2>Статистика недоступна</h2>
          <p>{html.escape(error_message or 'Подключение к базе данных недоступно.')}</p>
        </section>
        """

    order_rows = ""
    for order_id, username, phone, address, total, status in latest_orders:
        order_id_text = html.escape(str(order_id))
        order_rows += f"""
        <tr>
          <td>{order_id_text}</td>
          <td>{html.escape(str(username or '-'))}</td>
          <td>{html.escape(str(phone or '-'))}</td>
          <td>{html.escape(str(address or '-'))}</td>
          <td>EUR {float(total):.2f}</td>
          <td><span class="status">{html.escape(str(status or '-'))}</span></td>
          <td><a class="view-link" href="/orders/{order_id_text}">Открыть</a></td>
        </tr>
        """
    latest_section = """
        <p>Последних заказов пока нет.</p>
    """
    if order_rows:
        latest_section = f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>ID заказа</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Сумма</th><th>Статус</th><th></th></tr>
            {order_rows}
          </table>
        </div>
        """

    def format_analytics_weight(value):
        grams = int(value or 0)
        if grams >= 1000:
            return f"{grams / 1000:.1f} кг"
        return f"{grams} г"

    def product_analytics_rows(rows, include_units=False):
        if not rows:
            return "<p>Нет данных</p>"
        rendered = ""
        for row in rows:
            if include_units:
                _, product_name, grams_sold, revenue, _ = row
            else:
                _, product_name, grams_sold, revenue = row
            rendered += f"""
            <tr>
              <td>{html.escape(str(product_name or '-'))}</td>
              <td>{format_analytics_weight(grams_sold)}</td>
              <td>€{float(revenue or 0):.2f}</td>
            </tr>
            """
        return f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>Товар</th><th>Продано</th><th>Выручка</th></tr>
            {rendered}
          </table>
        </div>
        """

    def customer_analytics_rows(rows, orders_label):
        if not rows:
            return "<p>Нет данных</p>"
        rendered = ""
        for _, username, phone, orders_count, total_spent in rows:
            customer = username if username and username != "-" else phone
            rendered += f"""
            <tr>
              <td>{html.escape(str(customer or '-'))}</td>
              <td>{orders_count}</td>
              <td>€{float(total_spent or 0):.2f}</td>
            </tr>
            """
        return f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>Клиент</th><th>{orders_label}</th><th>Сумма</th></tr>
            {rendered}
          </table>
        </div>
        """

    low_stock_rows = ""
    for product_id, name, stock_grams, low_stock_threshold_grams in low_stock_products:
        low_stock_rows += f"""
        <tr>
          <td><a class="view-link" href="/products/{product_id}/edit">{html.escape(str(name or '-'))}</a></td>
          <td>{format_stock_grams(stock_grams)}</td>
          <td>{format_stock_grams(low_stock_threshold_grams)}</td>
        </tr>
        """
    low_stock_section = """
        <p>Все товары имеют достаточный остаток.</p>
    """
    if low_stock_rows:
        low_stock_section = f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>Название товара</th><th>Остаток</th><th>Порог</th></tr>
            {low_stock_rows}
          </table>
        </div>
        """

    funnel_section = """
        <section class="admin-card dash-section">
          <h2>📊 Воронка сегодня</h2>
          <p>Воронка пока недоступна.</p>
        </section>
    """
    if funnel_rows:
        funnel_table_rows = ""
        previous_count = None
        for _, label, users_count in funnel_rows:
            users_value = int(users_count or 0)
            if previous_count is None or previous_count <= 0:
                conversion_text = "-"
            else:
                conversion_text = f"{round(users_value / previous_count * 100)}%"
            funnel_table_rows += f"""
            <tr>
              <td>{html.escape(str(label or '-'))}</td>
              <td>{users_value}</td>
              <td>{conversion_text}</td>
            </tr>
            """
            previous_count = users_value
        funnel_section = f"""
        <section class="admin-card dash-section">
          <h2>📊 Воронка сегодня</h2>
          <div class="dash-table-wrap">
            <table>
              <tr><th>Этап</th><th>Клиентов</th><th>Конверсия</th></tr>
              {funnel_table_rows}
            </table>
          </div>
        </section>
        """

    analytics_section = f"""
        <section class="admin-card dash-section">
          <h2>Аналитика продаж</h2>
          <div class="dash-grid">
            <div class="dash-card">
              <strong>Топ товары</strong>
              {product_analytics_rows(top_products)}
            </div>
            <div class="dash-card">
              <strong>Слабые товары</strong>
              {product_analytics_rows(worst_products, include_units=True)}
            </div>
            <div class="dash-card">
              <strong>Лучшие клиенты</strong>
              {customer_analytics_rows(best_customers, "Заказов")}
            </div>
            <div class="dash-card">
              <strong>Повторные клиенты</strong>
              {customer_analytics_rows(repeat_customers, "Заказов")}
            </div>
          </div>
        </section>
    """

    control_center_section = f"""
        <section class="admin-card dash-section">
          <h2>🧠 Центр управления владельца</h2>

          <h3>🔴 Срочно</h3>
          <div class="dash-grid">
            <a class="dash-card priority-urgent" href="/orders?pending_weighing=1">
              <strong>⚖️ Ожидают взвешивания</strong>
              <span>Заказы с товарами, которые ещё нужно взвесить</span>
              <strong class="stat-value">{cc_pending_weighing_count}</strong>
            </a>
            <a class="dash-card priority-urgent" href="/products?filter=low_stock">
              <strong>⚠️ Низкий остаток</strong>
              <span>Товары заканчиваются или уже отсутствуют</span>
              <strong class="stat-value">{cc_low_stock_count}</strong>
            </a>
            <a class="dash-card priority-urgent" href="/logs">
              <strong>🛑 Ошибки за 24 часа</strong>
              <span>Системные ошибки за последние сутки</span>
              <strong class="stat-value">{cc_recent_errors_count}</strong>
            </a>
          </div>

          <h3>🟡 Стоит сделать сегодня</h3>
          <div class="dash-grid">
            <a class="dash-card priority-today" href="/products?filter=no_recommendations">
              <strong>🎯 Без рекомендаций</strong>
              <span>Товары без настроенных сопутствующих рекомендаций</span>
              <strong class="stat-value">{cc_no_recommendations_count}</strong>
            </a>
            <a class="dash-card priority-today" href="/products?filter=no_promotion">
              <strong>🔥 Без акции</strong>
              <span>Товары, не отмеченные как акционные</span>
              <strong class="stat-value">{cc_no_promotion_count}</strong>
            </a>
            <a class="dash-card priority-today" href="/products?filter=no_sales&days=14">
              <strong>📉 Без продаж 14 дней</strong>
              <span>Товары без продаж за последние 14 дней</span>
              <strong class="stat-value">{cc_no_sales_count}</strong>
            </a>
          </div>

          <h3>🔵 Можно улучшить позже</h3>
          <div class="dash-grid">
            <a class="dash-card priority-later" href="/products?filter=no_image">
              <strong>📷 Без фотографии</strong>
              <span>Товары без изображения в карточке</span>
              <strong class="stat-value">{cc_no_image_count}</strong>
            </a>
            <a class="dash-card priority-later" href="/products?filter=no_description">
              <strong>📝 Без описания</strong>
              <span>Товары без текстового описания</span>
              <strong class="stat-value">{cc_no_description_count}</strong>
            </a>
            <a class="dash-card priority-later" href="/products?filter=never_sold">
              <strong>📦 Никогда не продавались</strong>
              <span>Товары без единой продажи за всё время</span>
              <strong class="stat-value">{cc_never_sold_count}</strong>
            </a>
          </div>
        </section>
    """

    return admin_layout(
        ADMIN_PANEL_TITLE,
        f"""
        <section class="dash-hero">
          <div>
            <p class="dash-kicker">Панель управления</p>
            <h1>{html.escape(ADMIN_PANEL_TITLE)}</h1>
            <p>Быстрый доступ к заказам, каталогу, категориям и клиентам.</p>
          </div>
        </section>

        {control_center_section}

        <div class="dash-grid">
          <a class="dash-card" href="/orders"><strong>Заказы</strong><span>Просмотр и обновление заказов клиентов</span></a>
          <a class="dash-card" href="/products"><strong>Товары</strong><span>Редактирование товаров и наличия</span></a>
          <a class="dash-card" href="/categories"><strong>Категории</strong><span>Управление разделами каталога</span></a>
          <a class="dash-card" href="/clients"><strong>Клиенты</strong><span>Просмотр сохраненных данных клиентов</span></a>
        </div>

        {stat_cards}

        {funnel_section}

        <section class="admin-card dash-section">
          <h2>⚠️ Низкий остаток</h2>
          {low_stock_section}
        </section>

        <section class="admin-card dash-section">
          <h2>Последние заказы</h2>
          {latest_section}
        </section>

        {analytics_section}
        """,
        refresh_seconds=60,
    )

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                b.id,
                b.message_text,
                b.status,
                b.target_type,
                b.created_at,
                COUNT(br.id) AS total_recipients,
                COUNT(*) FILTER (WHERE br.status = 'sent') AS sent_count,
                COUNT(*) FILTER (WHERE br.status = 'pending') AS pending_count,
                COUNT(*) FILTER (WHERE br.status = 'blocked') AS blocked_count,
                COUNT(*) FILTER (WHERE br.status = 'failed') AS failed_count
            FROM broadcasts b
            LEFT JOIN broadcast_recipients br ON br.broadcast_id = b.id
            GROUP BY b.id
            ORDER BY b.created_at DESC
            LIMIT 50
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = """
        <section class="admin-card">
          <h1>📨 Рассылка</h1>
          <p><a class="button button-link" href="/broadcasts/new">Новая рассылка</a></p>
        """
        if not rows:
            html_content += "<p>Рассылок пока нет.</p>"
        else:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Статус</th><th>Кому</th><th>Текст</th><th>Получателей</th><th>Отправлено</th><th>Ожидает</th><th>Заблокировано</th><th>Ошибки</th><th>Действия</th></tr>"
            for broadcast_id, message_text, status, target_type, created_at, total_count, sent_count, pending_count, blocked_count, failed_count in rows:
                message_html = html.escape(str(message_text or "-")).replace("\n", "<br>")
                pending_value = int(pending_count or 0)
                action_html = ""
                if pending_value > 0:
                    button_label = ""
                    if str(status or "") == "draft":
                        button_label = "Отправить"
                    elif str(status or "") == "sending":
                        button_label = "Продолжить"
                    elif str(status or "") == "failed":
                        button_label = "Повторить"
                    if button_label:
                        action_html = (
                            f'<form method="post" action="/broadcasts/{int(broadcast_id)}/send" style="display:inline; margin:0; padding:0;">'
                            f'<button class="button secondary" type="submit">{button_label}</button>'
                            '</form>'
                        )
                html_content += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{broadcast_status_label(status)}</td>
                  <td>{broadcast_target_label(target_type)}</td>
                  <td>{message_html}</td>
                  <td>{int(total_count or 0)}</td>
                  <td>{int(sent_count or 0)}</td>
                  <td>{pending_value}</td>
                  <td>{int(blocked_count or 0)}</td>
                  <td>{int(failed_count or 0)}</td>
                  <td>{action_html or '-'}</td>
                </tr>
                """
            html_content += "</table></div>"
        html_content += "</section>"
        return admin_layout("📨 Рассылка", html_content)
    except Exception as e:
        log_admin_error("/broadcasts", "list_broadcasts", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/broadcasts/new", response_class=HTMLResponse)
async def new_broadcast_form():
    try:
        content = """
        <section class="admin-card">
          <h1>Новая рассылка</h1>
          <p><a class="button button-link secondary" href="/broadcasts">← К рассылкам</a></p>
          <form class="admin-form" method="post" action="/broadcasts/new">
            <label>Кому
              <select name="target_type">
                <option value="all_clients">Все клиенты</option>
                <option value="clients_with_orders">Клиенты с заказами</option>
                <option value="active_last_7_days">Активные за 7 дней</option>
                <option value="active_last_30_days">Активные за 30 дней</option>
                <option value="awaiting_payment">Ожидают оплату</option>
              </select>
            </label>
            <label>Текст сообщения
              <textarea name="message_text" rows="8"></textarea>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Создать рассылку</button>
            </div>
          </form>
        </section>
        """
        return admin_layout("Новая рассылка", content)
    except Exception as e:
        log_admin_error("/broadcasts/new", "new_broadcast_form", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/broadcasts/new", response_class=HTMLResponse)
async def create_broadcast(message_text: str = Form(""), target_type: str = Form("all_clients")):
    try:
        message_text = (message_text or "").strip()
        if not message_text:
            return admin_error_page("Ошибка", "Текст сообщения не может быть пустым.")
        target_type = str(target_type or "all_clients")
        target_queries = {
            "all_clients": """
                SELECT DISTINCT telegram_id
                FROM clients
                WHERE telegram_id IS NOT NULL
            """,
            "clients_with_orders": """
                SELECT DISTINCT telegram_id
                FROM orders
                WHERE telegram_id IS NOT NULL
            """,
            "active_last_7_days": """
                SELECT DISTINCT telegram_id
                FROM customer_events
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """,
            "active_last_30_days": """
                SELECT DISTINCT telegram_id
                FROM customer_events
                WHERE created_at >= NOW() - INTERVAL '30 days'
            """,
            "awaiting_payment": """
                SELECT DISTINCT telegram_id
                FROM orders
                WHERE telegram_id IS NOT NULL
                  AND status = 'awaiting_payment'
            """,
        }
        recipients_query = target_queries.get(target_type)
        if not recipients_query:
            return admin_error_page("Ошибка", "Неверный сегмент рассылки.")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO broadcasts (message_text, status, target_type)
            VALUES (%s, 'draft', %s)
            RETURNING id
            """,
            (message_text, target_type),
        )
        broadcast_id = cursor.fetchone()[0]
        cursor.execute(
            f"""
            INSERT INTO broadcast_recipients (broadcast_id, telegram_id)
            SELECT %s, telegram_id
            FROM ({recipients_query}) recipients
            ON CONFLICT (broadcast_id, telegram_id) DO NOTHING
            """,
            (broadcast_id,),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/broadcasts", status_code=303)
    except Exception as e:
        log_admin_error("/broadcasts/new", "create_broadcast", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/broadcasts/{broadcast_id}/send", response_class=HTMLResponse)
async def send_broadcast_route(broadcast_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, message_text, status
            FROM broadcasts
            WHERE id = %s
            """,
            (broadcast_id,),
        )
        broadcast = cursor.fetchone()
        if not broadcast:
            cursor.close()
            conn.close()
            return admin_error_page("Ошибка", "Рассылка не найдена.")

        _, message_text, status = broadcast
        if str(status or "") == "sent":
            cursor.close()
            conn.close()
            return RedirectResponse("/broadcasts", status_code=303)

        cursor.execute(
            "UPDATE broadcasts SET status = 'sending', error_message = NULL WHERE id = %s",
            (broadcast_id,),
        )
        cursor.execute(
            """
            SELECT id, telegram_id
            FROM broadcast_recipients
            WHERE broadcast_id = %s
              AND status = 'pending'
            ORDER BY id
            LIMIT 20
            """,
            (broadcast_id,),
        )
        recipients = cursor.fetchall()

        for recipient_id, telegram_id in recipients:
            success, result_status = send_broadcast_message(telegram_id, message_text)
            if success:
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'sent',
                        error_message = NULL,
                        sent_at = NOW()
                    WHERE id = %s
                    """,
                    (recipient_id,),
                )
            elif result_status == "blocked":
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'blocked',
                        error_message = %s
                    WHERE id = %s
                    """,
                    ("blocked", recipient_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'failed',
                        error_message = %s
                    WHERE id = %s
                    """,
                    ("failed", recipient_id),
                )

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM broadcast_recipients
            WHERE broadcast_id = %s
              AND status = 'pending'
            """,
            (broadcast_id,),
        )
        remaining_pending = cursor.fetchone()[0]
        if int(remaining_pending or 0) == 0:
            cursor.execute(
                """
                UPDATE broadcasts
                SET status = 'sent',
                    sent_at = NOW(),
                    error_message = NULL
                WHERE id = %s
                """,
                (broadcast_id,),
            )

        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/broadcasts", status_code=303)
    except Exception as e:
        log_admin_error("/broadcasts/{id}/send", "send_broadcast", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/channel", response_class=HTMLResponse)
async def channel_posts():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, message_text, status, error_message, created_at, sent_at
            FROM channel_posts
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = """
        <section class="admin-card">
          <h1>📢 Канал</h1>
          <p><a class="button button-link" href="/channel/new">Новый пост</a></p>
        """
        if not rows:
            html_content += "<p>Постов пока нет.</p>"
        else:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Статус</th><th>Текст</th><th>Отправлено</th><th>Ошибка</th><th>Действия</th></tr>"
            for post_id, message_text, status, error_message, created_at, sent_at in rows:
                post_id_int = int(post_id)
                message_html = html.escape(str(message_text or "-")).replace("\n", "<br>")
                error_html = html.escape(str(error_message or "-"))
                actions_html = "Отправлено"
                if str(status or "") in {"draft", "failed"}:
                    actions_html = (
                        f'<form method="post" action="/channel/{post_id_int}/send" style="display:inline; margin:0; padding:0;">'
                        '<button class="button secondary" type="submit">Отправить</button>'
                        '</form>'
                    )
                actions_html += (
                    f' <form method="post" action="/channel/{post_id_int}/delete" style="display:inline; margin:0; padding:0;">'
                    '<button class="button secondary" type="submit">Удалить</button>'
                    '</form>'
                )
                html_content += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{channel_post_status_label(status)}</td>
                  <td>{message_html}</td>
                  <td>{format_admin_datetime(sent_at)}</td>
                  <td>{error_html}</td>
                  <td>{actions_html}</td>
                </tr>
                """
            html_content += "</table></div>"
        html_content += "</section>"
        return admin_layout("📢 Канал", html_content)
    except Exception as e:
        log_admin_error("/channel", "list_channel_posts", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/channel/new", response_class=HTMLResponse)
async def new_channel_post_form():
    try:
        content = """
        <section class="admin-card">
          <h1>Новый пост в канал</h1>
          <p><a class="button button-link secondary" href="/channel">← К каналу</a></p>
          <form class="admin-form" method="post" action="/channel/new">
            <label>Текст сообщения
              <textarea name="message_text" rows="8"></textarea>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить черновик</button>
            </div>
          </form>
        </section>
        """
        return admin_layout("Новый пост в канал", content)
    except Exception as e:
        log_admin_error("/channel/new", "new_channel_post_form", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/channel/new", response_class=HTMLResponse)
async def create_channel_post(message_text: str = Form("")):
    try:
        message_text = (message_text or "").strip()
        if not message_text:
            return admin_error_page("Ошибка", "Текст сообщения не может быть пустым.")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO channel_posts (message_text, status)
            VALUES (%s, 'draft')
            RETURNING id
            """,
            (message_text,),
        )
        cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/channel", status_code=303)
    except Exception as e:
        log_admin_error("/channel/new", "create_channel_post", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/channel/{post_id}/send", response_class=HTMLResponse)
async def send_channel_post_route(post_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, message_text, status FROM channel_posts WHERE id = %s",
            (post_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return admin_error_page("Ошибка", "Пост не найден.")

        _, message_text, status = row
        if str(status or "") == "sent":
            cursor.close()
            conn.close()
            return RedirectResponse("/channel", status_code=303)

        success, error_message = send_channel_post(message_text)
        if success:
            cursor.execute(
                """
                UPDATE channel_posts
                SET status = 'sent', error_message = NULL, sent_at = NOW()
                WHERE id = %s
                """,
                (post_id,),
            )
        else:
            cursor.execute(
                """
                UPDATE channel_posts
                SET status = 'failed', error_message = %s
                WHERE id = %s
                """,
                (error_message, post_id),
            )
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/channel", status_code=303)
    except Exception as e:
        log_admin_error("/channel/{id}/send", "send_channel_post", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/channel/{post_id}/delete", response_class=HTMLResponse)
async def delete_channel_post(post_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM channel_posts WHERE id = %s",
            (post_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return admin_error_page("Ошибка", "Пост не найден.")

        cursor.execute("DELETE FROM channel_posts WHERE id = %s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/channel", status_code=303)
    except Exception as e:
        log_admin_error("/channel/{id}/delete", "delete_channel_post", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/logs", response_class=HTMLResponse)
async def logs():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                route,
                action,
                error_message,
                created_at
            FROM error_logs
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = "<section class='admin-card'><h1>🧾 Логи ошибок</h1>"
        if rows:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Route</th><th>Action</th><th>Ошибка</th><th></th></tr>"
            for log_id, route, action, error_message, created_at in rows:
                html_content += (
                    "<tr>"
                    f"<td>{format_admin_datetime(created_at)}</td>"
                    f"<td>{html.escape(str(route or '-'))}</td>"
                    f"<td>{html.escape(str(action or '-'))}</td>"
                    f"<td>{html.escape(str(error_message or '-'))}</td>"
                    f"<td><a class='button button-link' href='/logs/{int(log_id)}'>Открыть</a></td>"
                    "</tr>"
                )
            html_content += "</table></div>"
        else:
            html_content += "<p>Логов пока нет.</p>"
        html_content += "</section>"
        return admin_layout("🧾 Логи ошибок", html_content)
    except Exception as e:
        log_admin_error("/logs", "list_error_logs", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/logs/{log_id}", response_class=HTMLResponse)
async def log_detail(log_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                route,
                action,
                error_message,
                traceback,
                created_at
            FROM error_logs
            WHERE id = %s
            """,
            (log_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return admin_layout(
                "Лог не найден",
                "<section class='admin-card'><h1>Лог не найден</h1><p><a class='button button-link' href='/logs'>← К логам</a></p></section>",
            )

        _, route, action, error_message, traceback_text, created_at = row
        content = f"""
        <section class="admin-card">
          <h1>🧾 Лог ошибки</h1>
          <p><a class="button button-link" href="/logs">← К логам</a></p>
          <div class="detail-grid">
            <div class="detail-field"><strong>Дата</strong>{format_admin_datetime(created_at)}</div>
            <div class="detail-field"><strong>Route</strong>{html.escape(str(route or '-'))}</div>
            <div class="detail-field"><strong>Action</strong>{html.escape(str(action or '-'))}</div>
            <div class="detail-field"><strong>Ошибка</strong>{html.escape(str(error_message or '-'))}</div>
          </div>
          <h2>Traceback</h2>
          <pre>{html.escape(str(traceback_text or ''))}</pre>
        </section>
        """
        return admin_layout("🧾 Лог ошибки", content)
    except Exception as e:
        log_admin_error("/logs/{id}", "error_log_detail", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/orders", response_class=HTMLResponse)
async def orders(status_filter: str = "all", q: str = "", pending_weighing: int = 0):
    try:
        allowed_status_filters = {
            "all",
            "pending",
            "awaiting_payment",
            "payment_reported",
            "cash_on_delivery",
            "paid",
            "preparing",
            "done",
            "cancelled",
        }
        if status_filter not in allowed_status_filters:
            status_filter = "all"
        is_pending_weighing_filter = pending_weighing == 1
        search_query = q.strip()
        where_clauses = []
        params = []
        if status_filter != "all":
            where_clauses.append("status = %s")
            params.append(status_filter)
        if search_query:
            search_value = f"%{search_query}%"
            where_clauses.append(
                """
                (
                    CAST(order_id AS TEXT) ILIKE %s
                    OR username ILIKE %s
                    OR phone ILIKE %s
                    OR address ILIKE %s
                )
                """
            )
            params.extend([search_value, search_value, search_value, search_value])
        if is_pending_weighing_filter:
            where_clauses.append("status != 'cancelled'")
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM order_items oi
                    WHERE oi.order_id = orders.order_id
                      AND oi.weight IS NULL
                )
                """
            )
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, order_id, username, phone, address, total, status, payment_method, created_at
            FROM orders
            {where_sql}
            ORDER BY id DESC
            LIMIT 50
        """, params)
        rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE status IN (
                'pending',
                'awaiting_payment',
                'payment_reported',
                'cash_on_delivery'
            )
            """
        )
        attention_orders_count = cursor.fetchone()[0]
        conn.close()

        status_options = {
            "all": "Все",
            "pending": "Ожидает выбора оплаты",
            "awaiting_payment": "Ожидает оплаты",
            "payment_reported": "Оплата заявлена",
            "cash_on_delivery": "Наличными",
            "paid": "Оплачен",
            "preparing": "Готовится",
            "done": "Готов",
            "cancelled": "Отменён",
        }
        options_html = ""
        for value, label in status_options.items():
            selected = "selected" if value == status_filter else ""
            options_html += f"<option value=\"{value}\" {selected}>{label}</option>"

        pending_weighing_banner_html = ""
        if is_pending_weighing_filter:
            pending_weighing_banner_html = (
                "<div class='attention-banner'>⚖️ Показаны только заказы, ожидающие взвешивания. "
                "<a class='button button-link secondary' href='/orders'>Сбросить фильтр</a></div>"
            )

        html = f"""
        <section class='admin-card'>
          <h1>📦 Заказы</h1>
          <form class="admin-form" method="get" action="/orders">
            <label>Поиск <input name="q" value="{escape(search_query, quote=True)}" placeholder="№ заказа, клиент, телефон, адрес"/></label>
            <label>Статус
              <select name="status_filter">{options_html}</select>
            </label>
            <div class="form-actions">
              <button type="submit">Показать</button>
              <a class="button button-link secondary" href="/orders">Сбросить</a>
              <a class="button button-link secondary" href="/orders/export.csv?status_filter={escape(status_filter, quote=True)}&q={escape(search_query, quote=True)}">Экспорт CSV</a>
            </div>
          </form>
          <div class="attention-banner">⚠️ Требуют внимания: {attention_orders_count} заказов</div>
          {pending_weighing_banner_html}
        """

        if is_pending_weighing_filter and not rows:
            html += "<p>Нет заказов, ожидающих взвешивания.</p></section>"
            return admin_layout("📦 Заказы", html, refresh_seconds=60)

        html += "<div class='dash-table-wrap'><table>"
        html += "<tr><th>ID</th><th>№ заказа</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Сумма</th><th>Статус</th><th>Оплата</th><th>Создан</th><th>Действия</th></tr>"
        for row in rows:
            id_, order_id, username, phone, address, total, status, payment_method, created_at = row
            order_id_text = escape(str(order_id), quote=True)
            order_id_path = urllib.parse.quote(str(order_id), safe="")
            username_text = escape(str(username or "-"), quote=True)
            phone_text = escape(str(phone or "-"), quote=True)
            address_text = escape(str(address or "-"), quote=True)
            payment_method_text = escape(str(payment_method or "-"), quote=True)
            actions = [f"<a class=\"button\" href=\"/orders/{order_id_path}\">Открыть</a>"]
            status_key = str(status or "")
            for s, button_label in order_status_actions_for(status_key):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id_path}/status/{s}\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">{button_label}</button></form>")
            actions_html = f"<div class=\"action-group\">{' '.join(actions)}</div>"
            row_class = " class=\"attention-row\"" if status_key in {"pending", "awaiting_payment", "payment_reported", "cash_on_delivery"} else ""
            html += f"<tr{row_class}><td>{id_}</td><td>{order_id_text}</td><td>{username_text}</td><td>{phone_text}</td><td>{address_text}</td><td>{total:.2f}</td><td>{admin_status_badge(status)}</td><td>{payment_method_text}</td><td>{format_admin_datetime(created_at)}</td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("📦 Заказы", html, refresh_seconds=60)
    except Exception as e:
        log_admin_error("/orders", "list_orders", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/orders/export.csv")
async def orders_export_csv(status_filter: str = "all", q: str = ""):
    allowed_status_filters = {
        "all",
        "pending",
        "awaiting_payment",
        "payment_reported",
        "cash_on_delivery",
        "paid",
        "preparing",
        "done",
        "cancelled",
    }
    if status_filter not in allowed_status_filters:
        status_filter = "all"
    search_query = q.strip()
    where_clauses = []
    params = []
    if status_filter != "all":
        where_clauses.append("status = %s")
        params.append(status_filter)
    if search_query:
        search_value = f"%{search_query}%"
        where_clauses.append(
            """
            (
                CAST(order_id AS TEXT) ILIKE %s
                OR username ILIKE %s
                OR phone ILIKE %s
                OR address ILIKE %s
            )
            """
        )
        params.extend([search_value, search_value, search_value, search_value])
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT
            id,
            order_id,
            username,
            phone,
            address,
            total,
            status,
            payment_method,
            created_at,
            updated_at,
            payment_selected_at,
            payment_reported_at,
            inventory_deducted
        FROM orders
        {where_sql}
        ORDER BY id DESC
    """, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=';')
    writer.writerow([
        "id",
        "order_id",
        "username",
        "phone",
        "address",
        "total",
        "status",
        "payment_method",
        "created_at",
        "updated_at",
        "payment_selected_at",
        "payment_reported_at",
        "inventory_deducted",
    ])

    def excel_text(value):
        if value is None:
            return ""
        return f'="{str(value)}"'

    for row in rows:
        row = list(row)
        row[1] = excel_text(row[1])
        row[3] = excel_text(row[3])
        writer.writerow(row)

    output.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="orders.csv"'}
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers=headers
    )


@app.post("/orders/{order_id}/status/{status}", response_class=HTMLResponse)
async def update_order_status(order_id: str, status: str):
    allowed = {"paid", "preparing", "done", "cancelled"}
    if status not in allowed:
        return admin_error_page("Недопустимый статус", "Операция не может быть выполнена для этого заказа.")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, inventory_deducted, inventory_restored, telegram_id FROM orders WHERE order_id = %s",
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return RedirectResponse("/orders", status_code=303)

        current_status = str(row[0] or "")
        inventory_deducted = bool(row[1])
        inventory_restored = bool(row[2])
        telegram_id = row[3]
        allowed_transitions = {
            "pending": {"paid", "preparing", "done", "cancelled"},
            "awaiting_payment": {"paid", "preparing", "done", "cancelled"},
            "payment_reported": {"paid", "preparing", "done", "cancelled"},
            "cash_on_delivery": {"paid", "cancelled"},
            "paid": {"preparing", "done", "cancelled"},
            "preparing": {"done", "cancelled"},
            "done": set(),
            "cancelled": set(),
        }
        if status not in allowed_transitions.get(current_status, set()):
            print(f"INVALID ORDER STATUS TRANSITION: {order_id} {current_status} -> {status}")
            conn.close()
            return RedirectResponse("/orders", status_code=303)

        if status == "paid" and not inventory_deducted:
            affected_product_ids = []
            cursor.execute(
                """
                SELECT product_id, weight
                FROM order_items
                WHERE order_id = %s
                """,
                (order_id,)
            )
            order_items = cursor.fetchall()
            for product_id, weight in order_items:
                if not product_id or not weight:
                    continue
                cursor.execute(
                    """
                    WITH before_update AS (
                        SELECT stock_grams AS stock_before
                        FROM products
                        WHERE id = %s
                    ),
                    updated AS (
                        UPDATE products
                        SET stock_grams = GREATEST(stock_grams - %s, 0)
                        WHERE id = %s
                        RETURNING stock_grams AS stock_after
                    )
                    SELECT before_update.stock_before, updated.stock_after
                    FROM before_update, updated
                    """,
                    (product_id, weight, product_id)
                )
                stock_row = cursor.fetchone()
                if not stock_row:
                    continue
                affected_product_ids.append(product_id)
                stock_before, stock_after = stock_row
                log_inventory_movement(
                    cursor,
                    product_id,
                    "order_deducted",
                    int(stock_after or 0) - int(stock_before or 0),
                    stock_before,
                    stock_after,
                    order_id,
                    "Склад списан по заказу."
                )
            cursor.execute(
                """
                UPDATE products
                SET is_out_of_stock = TRUE
                WHERE stock_grams <= 0
                """
            )
            sync_low_stock_alert_state(cursor, affected_product_ids)
            cursor.execute(
                """
                UPDATE orders
                SET inventory_deducted = TRUE,
                    inventory_deducted_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,)
            )
            log_order_event(
                cursor,
                order_id,
                "inventory_deducted",
                "Склад списан по заказу."
            )

        if status == "cancelled" and inventory_deducted and not inventory_restored:
            affected_product_ids = []
            cursor.execute(
                """
                SELECT product_id, COALESCE(SUM(weight), 0) AS restore_grams
                FROM order_items
                WHERE order_id = %s
                GROUP BY product_id
                """,
                (order_id,)
            )
            restore_items = cursor.fetchall()
            for product_id, restore_grams in restore_items:
                if not product_id or not restore_grams or int(restore_grams or 0) <= 0:
                    continue
                cursor.execute(
                    """
                    WITH before_update AS (
                        SELECT stock_grams AS stock_before
                        FROM products
                        WHERE id = %s
                    ),
                    updated AS (
                        UPDATE products
                        SET stock_grams = stock_grams + %s
                        WHERE id = %s
                        RETURNING stock_grams AS stock_after
                    )
                    SELECT before_update.stock_before, updated.stock_after
                    FROM before_update, updated
                    """,
                    (product_id, restore_grams, product_id)
                )
                stock_row = cursor.fetchone()
                if not stock_row:
                    continue
                affected_product_ids.append(product_id)
                stock_before, stock_after = stock_row
                log_inventory_movement(
                    cursor,
                    product_id,
                    "stock_restored",
                    int(stock_after or 0) - int(stock_before or 0),
                    stock_before,
                    stock_after,
                    order_id,
                    "Остаток восстановлен после отмены заказа."
                )
            cursor.execute(
                """
                UPDATE products
                SET is_out_of_stock = FALSE
                WHERE stock_grams > 0
                """
            )
            sync_low_stock_alert_state(cursor, affected_product_ids)
            cursor.execute(
                """
                UPDATE orders
                SET inventory_restored = TRUE,
                    inventory_restored_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,)
            )
            log_order_event(
                cursor,
                order_id,
                "stock_restored",
                "Остаток восстановлен после отмены заказа."
            )

        cursor.execute(
            "UPDATE orders SET status = %s, updated_at = NOW() WHERE order_id = %s",
            (status, order_id)
        )
        if current_status != status:
            log_order_event(
                cursor,
                order_id,
                "status_changed",
                f"Статус изменён: {admin_status_label(current_status)} → {admin_status_label(status)}"
            )
        conn.commit()
        conn.close()
        if current_status != status:
            try:
                send_order_status_notification(telegram_id, order_id, status)
                try:
                    event_conn = psycopg2.connect(DATABASE_URL)
                    event_cursor = event_conn.cursor()
                    log_order_event(
                        event_cursor,
                        order_id,
                        "notification_sent",
                        f"Клиенту отправлено уведомление о статусе: {admin_status_label(status)}"
                    )
                    event_conn.commit()
                    event_conn.close()
                except Exception as e:
                    print(f"ORDER EVENT LOG ERROR: {order_id} notification_sent:", e)
            except Exception as e:
                print(f"ORDER STATUS NOTIFICATION ERROR: {order_id} {status}:", e)
                try:
                    event_conn = psycopg2.connect(DATABASE_URL)
                    event_cursor = event_conn.cursor()
                    log_order_event(
                        event_cursor,
                        order_id,
                        "notification_failed",
                        f"Не удалось отправить уведомление о статусе: {admin_status_label(status)}"
                    )
                    event_conn.commit()
                    event_conn.close()
                except Exception as log_error:
                    print(f"ORDER EVENT LOG ERROR: {order_id} notification_failed:", log_error)
        return admin_layout(
            "✅ Статус заказа обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Статус заказа обновлён</h1>
              <p>Статус заказа успешно изменён.</p>
              <div class="form-actions">
                <a class="button button-link" href="/orders">← К заказам</a>
                <a class="button button-link secondary" href="/orders/{order_id}">Открыть заказ</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        log_admin_error("/orders/{order_id}/status/{status}", "update_order_status", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(order_id: str):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                order_id,
                username,
                phone,
                address,
                total,
                status,
                payment_method,
                created_at,
                updated_at,
                payment_selected_at,
                payment_reminded_at,
                payment_reported_at,
                inventory_deducted,
                inventory_deducted_at,
                order_note
            FROM orders
            WHERE order_id = %s
            """,
            (order_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Заказ не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Заказ не найден</h1>
                  <p>Такой заказ не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/orders">← К заказам</a>
                  </div>
                </section>
                """,
            )

        (
            id_,
            order_id,
            username,
            phone,
            address,
            total,
            status,
            payment_method,
            created_at,
            updated_at,
            payment_selected_at,
            payment_reminded_at,
            payment_reported_at,
            inventory_deducted,
            inventory_deducted_at,
            order_note,
        ) = row
        try:
            cursor.execute(
                """
                SELECT oi.id, oi.product_name, oi.weight, oi.price, po.label, p.price_per_kg
                FROM order_items oi
                LEFT JOIN product_options po ON po.id = oi.option_id
                LEFT JOIN products p ON p.id = oi.product_id
                WHERE oi.order_id = %s
                ORDER BY oi.id
                """,
                (order_id,),
            )
            items = cursor.fetchall()
        except Exception:
            cursor.execute(
                """
                SELECT id, product_name, weight, price
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
                """,
                (order_id,),
            )
            items = [(item_id, product_name, weight, price, None, None) for item_id, product_name, weight, price in cursor.fetchall()]
        cursor.execute(
            """
            SELECT event_type, event_text, created_at
            FROM order_events
            WHERE order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        order_events = cursor.fetchall()
        conn.close()

        order_id_text = escape(str(order_id), quote=True)
        order_id_path = urllib.parse.quote(str(order_id), safe="")
        username_text = escape(str(username or "-"), quote=True)
        phone_text = escape(str(phone or "-"), quote=True)
        address_text = escape(str(address or "-"), quote=True)
        payment_method_text = escape(str(payment_method or "-"), quote=True)

        html = f"<section class='admin-card'><h1>Заказ {order_id_text}</h1><div class='detail-grid'>"
        html += f"<div class='detail-field'><strong>№ заказа</strong>{order_id_text}</div>"
        html += f"<div class='detail-field'><strong>Клиент</strong>{username_text}</div>"
        html += f"<div class='detail-field'><strong>Телефон</strong>{phone_text}</div>"
        html += f"<div class='detail-field'><strong>Адрес</strong>{address_text}</div>"
        html += f"<div class='detail-field'><strong>Статус</strong>{admin_status_badge(status)}</div>"
        html += f"<div class='detail-field'><strong>Оплата</strong>{payment_method_text}</div>"
        html += "</div></section>"
        status_action_buttons = ""
        for target_status, button_label in order_status_actions_for(status):
            status_action_buttons += (
                f'<form method="post" action="/orders/{order_id_path}/status/{target_status}" '
                'style="display:inline; margin:0; padding:0;">'
                f'<button class="button secondary" type="submit">{button_label}</button>'
                '</form>'
            )
        payment_actions = f'<div class="form-actions">{status_action_buttons}</div>' if status_action_buttons else ""
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Проверка оплаты</h2>
          <div class='detail-grid'>
            <div class='detail-field'><strong>Способ оплаты</strong>{payment_method_text}</div>
            <div class='detail-field'><strong>Оплата выбрана</strong>{format_admin_datetime(payment_selected_at)}</div>
            <div class='detail-field'><strong>Клиент сообщил об оплате</strong>{format_admin_datetime(payment_reported_at)}</div>
            <div class='detail-field'><strong>Текущий статус</strong>{admin_status_badge(status)}</div>
          </div>
          {payment_actions}
        </section>
        """
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Заметка администратора</h2>
          <form class="admin-form" method="post" action="/orders/{order_id_path}/note">
            <label>Заметка
              <textarea name="order_note" rows="4">{escape(str(order_note or ""), quote=True)}</textarea>
              <small>Видно только в админке.</small>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить заметку</button>
            </div>
          </form>
        </section>
        """
        timeline_events = [
            ("Заказ создан", created_at),
            ("Выбран способ оплаты", payment_selected_at),
            ("Отправлено напоминание об оплате", payment_reminded_at),
            ("Клиент сообщил об оплате", payment_reported_at),
            ("Склад списан", inventory_deducted_at if inventory_deducted else None),
            ("Последнее обновление", updated_at),
        ]
        timeline_events = sorted(
            [(label, value) for label, value in timeline_events if value],
            key=lambda item: item[1],
        )
        timeline_rows = ""
        for label, value in timeline_events:
            timeline_rows += f"""
            <tr>
              <td>{escape(str(label), quote=True)}</td>
              <td>{format_admin_datetime(value)}</td>
            </tr>
            """
        if not timeline_rows:
            timeline_rows = "<tr><td colspan='2'>Нет данных</td></tr>"
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Таймлайн заказа</h2>
          <p>Показаны системные отметки времени. Полный журнал действий будет добавлен позже.</p>
          <div class='dash-table-wrap'>
            <table>
              <tr><th>Событие</th><th>Время</th></tr>
              {timeline_rows}
            </table>
          </div>
        </section>
        """
        event_rows = ""
        for event_type, event_text, event_created_at in order_events:
            event_rows += f"""
            <tr>
              <td>{format_admin_datetime(event_created_at)}</td>
              <td>{escape(str(event_text or '-'), quote=True)}<br><span class="muted">{escape(admin_event_type_label(event_type), quote=True)}</span></td>
            </tr>
            """
        if not event_rows:
            event_rows = "<tr><td colspan='2'>Пока нет записей журнала.</td></tr>"
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Журнал событий</h2>
          <div class='dash-table-wrap'>
            <table>
              <tr><th>Время</th><th>Событие</th></tr>
              {event_rows}
            </table>
          </div>
        </section>
        """
        if items:
            html += "<section class='admin-card dash-section'><h2>Товары</h2><div class='dash-table-wrap'><table>"
            html += "<tr><th>Товар</th><th>Вариант / вес</th><th>Итого</th></tr>"
            for item_id, product_name, weight, price, option_label, price_per_kg in items:
                item_label = option_label if option_label else f"{weight} г"
                product_name_text = escape(str(product_name or "-"), quote=True)
                item_label_text = escape(str(item_label or "-"), quote=True)
                if weight is None:
                    price_per_kg_value = float(price_per_kg) if price_per_kg is not None else 0
                    preview_id = f"price_preview_{item_id}"
                    photo_preview_id = f"photo_preview_{item_id}"
                    html += (
                        f"<tr><td>{product_name_text}</td><td>{item_label_text}</td><td>"
                        f'<form method="post" action="/orders/{order_id_path}/items/{item_id}/weigh" '
                        'enctype="multipart/form-data" '
                        'style="display:flex; gap:4px; align-items:center; flex-wrap:wrap; margin:0;">'
                        f'<input type="number" name="final_weight_grams" placeholder="г" required style="width:70px;" '
                        f'data-price-per-kg="{price_per_kg_value}" '
                        f'oninput="document.getElementById(\'{preview_id}\').innerText = \'≈ \' + ((parseFloat(this.value || 0) / 1000) * parseFloat(this.dataset.pricePerKg || 0)).toFixed(2) + \' €\'"/>'
                        f'<span id="{preview_id}" style="min-width:70px; color:#6b7280;">≈ 0.00 €</span>'
                        '<input type="file" name="photo" accept="image/*" capture="environment" style="max-width:110px;" '
                        f'onchange="var img = document.getElementById(\'{photo_preview_id}\'); '
                        'if (this.files && this.files[0]) { img.src = URL.createObjectURL(this.files[0]); img.style.display = \'inline-block\'; } '
                        'else { img.style.display = \'none\'; }"/>'
                        f'<img id="{photo_preview_id}" style="display:none; max-width:40px; max-height:40px; border-radius:4px; object-fit:cover;"/>'
                        '<button class="button secondary" type="submit">Подтвердить вес</button>'
                        '</form>'
                        '</td></tr>'
                    )
                else:
                    html += f"<tr><td>{product_name_text}</td><td>{item_label_text}</td><td>{price:.2f} €</td></tr>"
            html += "</table></div>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        else:
            html += "<section class='admin-card dash-section'><p>Товары не найдены.</p>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        html += f"<p><a class='button button-link' href=\"/orders\">← К заказам</a></p>"
        return admin_layout(f"Заказ {order_id}", html)
    except Exception as e:
        log_admin_error("/orders/{order_id}", "order_detail", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/orders/{order_id}/note")
async def update_order_note(order_id: str, order_note: str = Form("")):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE orders
            SET order_note = %s,
                updated_at = NOW()
            WHERE order_id = %s
            """,
            (order_note, order_id),
        )
        log_order_event(
            cursor,
            order_id,
            "order_note_updated",
            "Заметка администратора обновлена."
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"ORDER NOTE UPDATE ERROR: {order_id}:", e)
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post("/orders/{order_id}/items/{item_id}/weigh")
async def weigh_order_item(
    order_id: str,
    item_id: int,
    final_weight_grams: int = Form(...),
    photo: UploadFile = File(None),
):
    has_pending_weighing = True
    telegram_id = None
    order_total = None
    order_items_summary = []
    photo_bytes = None
    photo_filename = None
    photo_content_type = None
    if photo is not None and photo.filename:
        photo_bytes = await photo.read()
        photo_filename = photo.filename
        photo_content_type = photo.content_type
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.price_per_kg
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.id = %s
              AND oi.order_id = %s
              AND oi.weight IS NULL
            """,
            (item_id, order_id),
        )
        product_row = cursor.fetchone()
        if not product_row:
            conn.close()
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        price_per_kg = product_row[0]
        final_price = round(final_weight_grams / 1000 * price_per_kg, 2)

        cursor.execute(
            """
            UPDATE order_items
            SET weight = %s,
                price = %s
            WHERE id = %s
              AND order_id = %s
              AND weight IS NULL
            """,
            (final_weight_grams, final_price, item_id, order_id),
        )
        if cursor.rowcount == 0:
            conn.close()
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        cursor.execute(
            """
            UPDATE orders
            SET total = (SELECT COALESCE(SUM(price), 0) FROM order_items WHERE order_id = %s),
                updated_at = NOW()
            WHERE order_id = %s
            """,
            (order_id, order_id),
        )
        log_order_event(
            cursor,
            order_id,
            "item_weighed",
            f"Товар взвешен: {final_weight_grams} г, {final_price:.2f} €"
        )

        cursor.execute(
            "SELECT 1 FROM order_items WHERE order_id = %s AND weight IS NULL LIMIT 1",
            (order_id,),
        )
        has_pending_weighing = cursor.fetchone() is not None

        if not has_pending_weighing:
            cursor.execute(
                "SELECT telegram_id, total FROM orders WHERE order_id = %s",
                (order_id,),
            )
            order_row = cursor.fetchone()
            if order_row:
                telegram_id, order_total = order_row

            cursor.execute(
                """
                SELECT oi.product_name, oi.weight, oi.price, po.label
                FROM order_items oi
                LEFT JOIN product_options po ON po.id = oi.option_id
                WHERE oi.order_id = %s
                ORDER BY oi.id
                """,
                (order_id,),
            )
            order_items_summary = cursor.fetchall()

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"ORDER ITEM WEIGH ERROR: {order_id}/{item_id}:", e)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)

    if not has_pending_weighing and telegram_id:
        try:
            send_weighing_complete_notification(
                telegram_id,
                order_id,
                order_total,
                order_items_summary,
                photo_bytes=photo_bytes,
                photo_filename=photo_filename,
                photo_content_type=photo_content_type,
            )
            notify_conn = psycopg2.connect(DATABASE_URL)
            notify_cursor = notify_conn.cursor()
            log_order_event(
                notify_cursor,
                order_id,
                "weighing_notification_sent",
                f"Клиенту отправлено уведомление о финальной сумме: {float(order_total or 0):.2f} €"
            )
            notify_conn.commit()
            notify_conn.close()
        except Exception as e:
            print(f"WEIGHING NOTIFICATION ERROR: {order_id}:", e)
            try:
                notify_conn = psycopg2.connect(DATABASE_URL)
                notify_cursor = notify_conn.cursor()
                log_order_event(
                    notify_cursor,
                    order_id,
                    "weighing_notification_failed",
                    "Не удалось отправить клиенту уведомление о финальной сумме."
                )
                notify_conn.commit()
                notify_conn.close()
            except Exception as log_error:
                print(f"ORDER EVENT LOG ERROR: {order_id} weighing_notification_failed:", log_error)

    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.get("/clients", response_class=HTMLResponse)
async def clients(q: str = ""):
    try:
        search_query = q.strip()[:100]
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        if search_query:
            search_param = f"%{search_query}%"
            cursor.execute(
                """
                SELECT telegram_id, username, phone, address
                FROM clients
                WHERE
                CAST(telegram_id AS TEXT) ILIKE %s
                OR username ILIKE %s
                OR phone ILIKE %s
                ORDER BY telegram_id DESC
                LIMIT 20
                """,
                (search_param, search_param, search_param),
            )
        else:
            cursor.execute("""
                SELECT telegram_id, username, phone, address
                FROM clients
                ORDER BY telegram_id DESC
                LIMIT 20
            """)
        rows = cursor.fetchall()
        conn.close()
        
        html = f"""
        <section class='admin-card'>
        <h1>👥 Клиенты</h1>
        <form class="admin-form" method="get" action="/clients">
          <label>Поиск <input name="q" value="{escape(search_query, quote=True)}" placeholder="Поиск по Telegram ID, username или телефону"/></label>
          <div class="form-actions">
            <button class="button" type="submit">Показать</button>
            <a class="button button-link secondary" href="/clients">Сбросить</a>
          </div>
        </form>
        <div class='dash-table-wrap'><table>
        """
        html += "<tr><th>Telegram ID</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Действия</th></tr>"
        for row in rows:
            telegram_id, username, phone, address = row
            telegram_id_text = escape(str(telegram_id), quote=True)
            html += (
                "<tr>"
                f"<td>{telegram_id_text}</td>"
                f"<td>{escape(str(username or '-'), quote=True)}</td>"
                f"<td>{escape(str(phone or '-'), quote=True)}</td>"
                f"<td>{escape(str(address or '-'), quote=True)}</td>"
                f"<td><a class='button button-link' href='/clients/{telegram_id_text}'>Открыть</a></td>"
                "</tr>"
            )
        html += "</table></div></section>"
        return admin_layout("👥 Клиенты", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/clients/{telegram_id}", response_class=HTMLResponse)
async def client_detail(telegram_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT telegram_id, username, first_name, phone, address, client_note
            FROM clients
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        )
        client = cursor.fetchone()
        if not client:
            conn.close()
            return admin_layout(
                "Клиент не найден",
                """
                <section class="admin-card">
                  <h1>Клиент не найден</h1>
                  <p>Такой клиент не найден.</p>
                  <p><a class="button button-link" href="/clients">Назад к клиентам</a></p>
                </section>
                """,
            )

        cursor.execute(
            """
            SELECT
              COUNT(*) AS total_orders,
              COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS completed_orders,
              COALESCE(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled_orders,
              COALESCE(SUM(CASE WHEN status = 'done' THEN total ELSE 0 END), 0) AS total_spent,
              COALESCE(AVG(CASE WHEN status = 'done' THEN total ELSE NULL END), 0) AS average_order_value,
              MIN(created_at) AS first_order_date,
              MAX(created_at) AS last_order_date
            FROM orders
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        )
        stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT order_id, total, status, payment_method, created_at
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 20
            """,
            (telegram_id,),
        )
        order_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                oi.product_id,
                COALESCE(p.name, oi.product_name) AS product_name,
                COUNT(*) AS purchase_count,
                COALESCE(SUM(oi.weight), 0) AS grams_purchased,
                COALESCE(SUM(oi.price), 0) AS revenue
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE o.telegram_id = %s
              AND o.status = 'done'
            GROUP BY
                oi.product_id,
                COALESCE(p.name, oi.product_name)
            ORDER BY
                purchase_count DESC,
                revenue DESC
            LIMIT 5
            """,
            (telegram_id,),
        )
        favorite_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT event_type, metadata, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (telegram_id,),
        )
        activity_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT event_type, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        latest_funnel_event = cursor.fetchone()
        cursor.execute(
            """
            SELECT event_type, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY
            CASE event_type
                WHEN 'payment_reported' THEN 9
                WHEN 'payment_method_selected' THEN 8
                WHEN 'order_created' THEN 7
                WHEN 'checkout_started' THEN 6
                WHEN 'open_cart' THEN 5
                WHEN 'add_to_cart' THEN 4
                WHEN 'view_product' THEN 3
                WHEN 'view_category' THEN 2
                WHEN 'start' THEN 1
                ELSE 0
            END DESC,
            created_at DESC,
            id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        highest_funnel_event = cursor.fetchone()
        conn.close()

        client_id, username, first_name, phone, address, client_note = client
        (
            total_orders,
            completed_orders,
            cancelled_orders,
            total_spent,
            average_order_value,
            first_order_date,
            last_order_date,
        ) = stats

        orders_html = "<p>Заказов пока нет.</p>"
        if order_rows:
            rows_html = ""
            for order_id, total, status, payment_method, created_at in order_rows:
                order_id_text = html.escape(str(order_id))
                rows_html += f"""
                <tr>
                  <td><a class="view-link" href="/orders/{order_id_text}">{order_id_text}</a></td>
                  <td>€{float(total or 0):.2f}</td>
                  <td>{admin_status_badge(status)}</td>
                  <td>{html.escape(str(payment_method or '-'))}</td>
                  <td>{format_admin_datetime(created_at)}</td>
                </tr>
                """
            orders_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Заказ</th><th>Сумма</th><th>Статус</th><th>Оплата</th><th>Создан</th></tr>
                {rows_html}
              </table>
            </div>
            """

        def format_client_weight(value):
            grams = int(value or 0)
            if grams >= 1000:
                return f"{grams / 1000:.1f} кг"
            return f"{grams} г"

        favorite_products_html = "<p>Нет данных</p>"
        if favorite_products:
            favorite_rows = ""
            for _, product_name, purchase_count, grams_purchased, revenue in favorite_products:
                favorite_rows += f"""
                <tr>
                  <td>{html.escape(str(product_name or '-'))}</td>
                  <td>{purchase_count}</td>
                  <td>{format_client_weight(grams_purchased)}</td>
                  <td>€{float(revenue or 0):.2f}</td>
                </tr>
                """
            favorite_products_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Товар</th><th>Покупок</th><th>Вес</th><th>Выручка</th></tr>
                {favorite_rows}
              </table>
            </div>
            """

        event_labels = {
            "start": "Открыл бота",
            "view_category": "Открыл категорию",
            "view_product": "Открыл товар",
            "add_to_cart": "Добавил в корзину",
            "open_cart": "Открыл корзину",
            "checkout_started": "Начал оформление",
            "order_created": "Создал заказ",
            "payment_method_selected": "Выбрал оплату",
            "payment_reported": "Сообщил об оплате",
        }
        funnel_stage_labels = {
            "start": "Только открыл бота",
            "view_category": "Смотрел категории",
            "view_product": "Смотрел товары",
            "add_to_cart": "Добавил в корзину",
            "open_cart": "Открыл корзину",
            "checkout_started": "Начал оформление",
            "order_created": "Создал заказ",
            "payment_method_selected": "Выбрал оплату",
            "payment_reported": "Сообщил об оплате",
        }

        def format_funnel_event(row):
            if not row:
                return None
            event_type, created_at = row
            label = funnel_stage_labels.get(str(event_type or ""), html.escape(str(event_type or "-")))
            return f"{html.escape(str(label))} / {format_admin_datetime(created_at)}"

        def format_activity_metadata(metadata):
            if not isinstance(metadata, dict):
                return "-"
            details = []
            if metadata.get("order_id") is not None:
                details.append(f"#{html.escape(str(metadata.get('order_id')))}")
            if metadata.get("product_id") is not None:
                details.append(f"product_id: {html.escape(str(metadata.get('product_id')))}")
            if metadata.get("category_id") is not None:
                details.append(f"category_id: {html.escape(str(metadata.get('category_id')))}")
            if metadata.get("payment_method") is not None:
                details.append(html.escape(str(metadata.get("payment_method"))))
            if metadata.get("option_id") is not None:
                details.append(f"option_id: {html.escape(str(metadata.get('option_id')))}")
            if metadata.get("weight") is not None:
                details.append(f"{html.escape(str(metadata.get('weight')))} г")
            return ", ".join(details) if details else "-"

        activity_html = "<p>Пока нет активности.</p>"
        if activity_rows:
            activity_table_rows = ""
            for event_type, metadata, created_at in activity_rows:
                label = event_labels.get(str(event_type or ""), html.escape(str(event_type or "-")))
                activity_table_rows += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{html.escape(str(label))}</td>
                  <td>{format_activity_metadata(metadata)}</td>
                </tr>
                """
            activity_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Дата</th><th>Событие</th><th>Детали</th></tr>
                {activity_table_rows}
              </table>
            </div>
            """

        latest_funnel_text = format_funnel_event(latest_funnel_event)
        highest_funnel_text = format_funnel_event(highest_funnel_event)
        if latest_funnel_text or highest_funnel_text:
            funnel_path_html = f"""
            <div class="detail-grid">
              <div class="detail-field"><strong>Последнее действие</strong>{latest_funnel_text or '-'}</div>
              <div class="detail-field"><strong>Самый дальний этап</strong>{highest_funnel_text or '-'}</div>
            </div>
            """
        else:
            funnel_path_html = "<p>Пока нет данных по действиям клиента.</p>"

        content = f"""
        <p><a class="button button-link" href="/clients">Назад к клиентам</a></p>
        <section class="admin-card">
          <h1>Клиент</h1>
          <div class="detail-grid">
            <div class="detail-field"><strong>Telegram ID</strong>{html.escape(str(client_id))}</div>
            <div class="detail-field"><strong>Username</strong>{html.escape(str(username or '-'))}</div>
            <div class="detail-field"><strong>Имя</strong>{html.escape(str(first_name or '-'))}</div>
            <div class="detail-field"><strong>Телефон</strong>{html.escape(str(phone or '-'))}</div>
            <div class="detail-field"><strong>Адрес</strong>{html.escape(str(address or '-'))}</div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Заметка администратора</h2>
          <form class="admin-form" method="post" action="/clients/{telegram_id}/note">
            <label>Заметка
              <textarea name="client_note" rows="4">{html.escape(str(client_note or ""))}</textarea>
              <small>Видно только в админке.</small>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить заметку</button>
            </div>
          </form>
        </section>
        <section class="admin-card dash-section">
          <h2>CRM статистика</h2>
          <div class="dash-grid">
            <div class="dash-card"><span>Всего заказов</span><strong class="stat-value">{total_orders}</strong></div>
            <div class="dash-card"><span>Завершённых</span><strong class="stat-value">{completed_orders}</strong></div>
            <div class="dash-card"><span>Отменённых</span><strong class="stat-value">{cancelled_orders}</strong></div>
            <div class="dash-card"><span>Потрачено</span><strong class="stat-value">€{float(total_spent or 0):.2f}</strong></div>
            <div class="dash-card"><span>Средний чек</span><strong class="stat-value">€{float(average_order_value or 0):.2f}</strong></div>
            <div class="dash-card"><span>Первый заказ</span><strong>{format_admin_datetime(first_order_date)}</strong></div>
            <div class="dash-card"><span>Последний заказ</span><strong>{format_admin_datetime(last_order_date)}</strong></div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Любимые товары</h2>
          {favorite_products_html}
        </section>
        <section class="admin-card dash-section">
          <h2>Путь клиента</h2>
          {funnel_path_html}
        </section>
        <section class="admin-card dash-section">
          <h2>Активность клиента</h2>
          {activity_html}
        </section>
        <section class="admin-card dash-section">
          <h2>История заказов</h2>
          {orders_html}
        </section>
        """
        return admin_layout("Клиент", content)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/clients/{telegram_id}/note")
async def update_client_note(telegram_id: int, client_note: str = Form("")):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE clients
            SET client_note = %s
            WHERE telegram_id = %s
            """,
            (client_note, telegram_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"CLIENT NOTE UPDATE ERROR: {telegram_id}:", e)
    return RedirectResponse(f"/clients/{telegram_id}", status_code=303)


@app.get("/products", response_class=HTMLResponse)
async def products(filter: str = "", days: int = 14):
    try:
        allowed_filters = {
            "low_stock",
            "no_recommendations",
            "no_promotion",
            "no_sales",
            "no_image",
            "no_description",
            "never_sold",
        }
        active_filter = filter if filter in allowed_filters else ""

        try:
            days_value = int(days)
        except (TypeError, ValueError):
            days_value = 14
        days_value = max(1, min(days_value, 365))

        where_sql = ""
        params = []

        if active_filter == "low_stock":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND (
                    p.is_out_of_stock = TRUE
                    OR (
                        p.stock_grams IS NOT NULL
                        AND p.low_stock_threshold_grams IS NOT NULL
                        AND p.stock_grams <= p.low_stock_threshold_grams
                    )
                  )
            """
        elif active_filter == "no_recommendations":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM product_recommendations pr
                    WHERE pr.product_id = p.id
                      AND pr.recommendation_type = 'frequently_bought_together'
                      AND pr.is_active = TRUE
                  )
            """
        elif active_filter == "no_promotion":
            where_sql = "WHERE p.is_active = TRUE AND p.is_promotion = FALSE"
        elif active_filter == "no_sales":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1
                    FROM order_items oi
                    JOIN orders o ON o.order_id = oi.order_id
                    WHERE oi.product_id = p.id
                      AND o.status != 'cancelled'
                      AND o.created_at >= NOW() - (%s * INTERVAL '1 day')
                  )
            """
            params.append(days_value)
        elif active_filter == "no_image":
            where_sql = "WHERE p.is_active = TRUE AND (p.image_url IS NULL OR TRIM(p.image_url) = '')"
        elif active_filter == "no_description":
            where_sql = "WHERE p.is_active = TRUE AND (p.description IS NULL OR TRIM(p.description) = '')"
        elif active_filter == "never_sold":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1
                    FROM order_items oi
                    JOIN orders o ON o.order_id = oi.order_id
                    WHERE oi.product_id = p.id
                      AND o.status != 'cancelled'
                  )
            """

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT p.id, p.name, p.price_per_kg, p.image_url, p.is_active, c.name, p.stock_grams, p.is_out_of_stock, p.low_stock_threshold_grams, p.is_promotion
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            {where_sql}
            ORDER BY p.sort_order, p.id
        """, params)
        rows = cursor.fetchall()
        conn.close()

        grouped_products = {}
        category_order = []
        for row in rows:
            category_label = row[5] or "Без категории"
            if category_label not in grouped_products:
                grouped_products[category_label] = []
                category_order.append(category_label)
            grouped_products[category_label].append(row)

        category_anchors = {}
        for index, category_label in enumerate(category_order, 1):
            anchor = "-".join("".join(ch.lower() if ch.isalnum() else "-" for ch in category_label).split("-"))
            category_anchors[category_label] = f"category-{index}-{anchor or 'items'}"

        filter_labels = {
            "low_stock": "Фильтр: товары с низким остатком",
            "no_recommendations": "Фильтр: товары без рекомендаций",
            "no_promotion": "Фильтр: товары без акций",
            "no_sales": f"Фильтр: без продаж за {days_value} дн.",
            "no_image": "Фильтр: товары без фото",
            "no_description": "Фильтр: товары без описания",
            "never_sold": "Фильтр: товары, которые ни разу не продавались",
        }

        html = "<section class='admin-card' id='all-products'><h1>🛒 Товары</h1><p><a class='button button-link' href='/products/new'>➕ Новый товар</a></p><p>Скрытые товары не показываются в каталоге, но остаются в системе.</p>"
        if active_filter:
            filter_label_text = escape(filter_labels.get(active_filter, ""), quote=True)
            html += (
                f"<div class='attention-banner'>{filter_label_text} "
                f"<a class='button button-link secondary' href='/products'>Сбросить фильтр</a></div>"
            )
        if not rows:
            html += "<p>Нет товаров по выбранному фильтру.</p></section>"
            return admin_layout("🛒 Товары", html)

        html += "<div class='quick-nav'><a href='#all-products'>Все товары</a>"
        for category_label in category_order:
            html += f"<a href='#{category_anchors[category_label]}'>📁 {category_label}</a>"
        html += "</div><div class='dash-table-wrap'><table class='products-table'>"
        html += "<tr><th>ID</th><th>Название</th><th>Цена</th><th>Фото</th><th>Статус</th><th>Категория</th><th>Остаток</th><th>Наличие</th><th>Действия</th></tr>"

        for category_label in category_order:
            html += f"<tr class='category-row' id='{category_anchors[category_label]}'><td colspan='9'>📁 {category_label}</td></tr>"
            for row in grouped_products[category_label]:
                pid, name, price, image_url, is_active, category_name, stock_grams, is_out_of_stock, low_stock_threshold_grams, is_promotion = row
                if image_url:
                    escaped_image_url = escape(str(image_url), quote=True)
                    img_html = f"<a href=\"{escaped_image_url}\" target=\"_blank\" rel=\"noopener noreferrer\"><img class=\"product-thumb\" src=\"{escaped_image_url}\" referrerpolicy=\"no-referrer\"/></a>"
                else:
                    img_html = "-"
                active_text = "Активен" if is_active else "Выключен"
                status_class = "active" if is_active else "inactive"
                actions_html = f"<a class=\"button\" href=\"/products/{pid}/edit\">Редактировать</a>"
                if is_active:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/deactivate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Отключить</button></form>"
                else:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/activate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Включить</button></form>"
                actions_html = f"<div class=\"action-group\">{actions_html}</div>"
                availability_text = "Нет в наличии" if is_out_of_stock or int(stock_grams or 0) <= 0 else "В наличии"
                availability_class = "inactive" if availability_text == "Нет в наличии" else "active"
                name_text = f"🔥 {name}" if is_promotion else name
                html += f"<tr><td>{pid}</td><td>{name_text}</td><td>{price:.2f}</td><td>{img_html}</td><td><span class='status {status_class}'>{active_text}</span></td><td>{category_label}</td><td>{format_stock_grams(stock_grams)}</td><td><span class='status {availability_class}'>{availability_text}</span></td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("🛒 Товары", html)
    except Exception as e:
        log_admin_error("/products", "list_products", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
@app.get("/products/new", response_class=HTMLResponse)
async def new_product_form():
    html = """
    <section class="admin-card">
      <h1>➕ Новый товар</h1>
      <p><a href="/products">← Назад к товарам</a></p>
      <form class="admin-form" method="post" action="/products/new">
        <label>Категория <input name="category_id"/></label>
        <label>Название товара <input name="name"/></label>
        <label>Цена за кг (€) <input name="price_per_kg"/></label>
        <label>Описание <input name="description"/></label>
        <label>Ссылка на фото <input name="image_url"/></label>
        <label>Остаток, г <input name="stock_grams" type="number" min="0" value="0"/></label>
        <label>Минимальный остаток, г <input name="low_stock_threshold_grams" type="number" min="0" value="500"/></label>
        <label>Порядок сортировки <input name="sort_order" value="0"/><small>Меньше число = выше в списке</small></label>
        <label>Товар активен <input type="checkbox" name="is_active" value="1"/></label>
        <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать товар"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новый товар", html)


@app.post("/products/new", response_class=HTMLResponse)
async def create_product(
    category_id: int = Form(...),
    name: str = Form(...),
    price_per_kg: float = Form(...),
    description: str = Form(''),
    image_url: str = Form(''),
    stock_grams: int = Form(0),
    low_stock_threshold_grams: str = Form("500"),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    is_out_of_stock: bool = Form(False),
):
    active = True if is_active else False
    stock_value = max(stock_grams, 0)
    try:
        low_stock_threshold_value = max(int(low_stock_threshold_grams or 0), 0)
    except (TypeError, ValueError):
        low_stock_threshold_value = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM products")
        new_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO products (
    id,
    category_id,
    name,
    price_per_kg,
    description,
    image_url,
    stock_grams,
    low_stock_threshold_grams,
    is_out_of_stock,
    is_active,
    sort_order
)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
    new_id,
    category_id,
    name,
    price_per_kg,
    description,
    image_url,
    stock_value,
    low_stock_threshold_value,
    is_out_of_stock,
    active,
    sort_order
),
        )
        sync_low_stock_alert_state(cursor, [new_id])
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар создан",
            """
            <section class="admin-card">
              <h1>✅ Товар создан</h1>
              <p>Товар успешно добавлен.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
                <a class="button button-link secondary" href="/products/new">➕ Добавить ещё товар</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        log_admin_error("/products/new", "create_product", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT category_id, name, price_per_kg, description, image_url, sort_order, is_active, stock_grams, is_out_of_stock, low_stock_threshold_grams, is_promotion, promotion_title, promotion_sort_order
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Товар не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Товар не найден</h1>
                  <p>Такой товар не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        cursor.execute(
            """
            SELECT id, label, weight, price, sort_order, is_active
            FROM product_options
            WHERE product_id = %s
            ORDER BY sort_order, id
            """,
            (product_id,),
        )
        options = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                movement_type,
                quantity_grams,
                stock_before,
                stock_after,
                order_id,
                note,
                created_at
            FROM inventory_movements
            WHERE product_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (product_id,),
        )
        inventory_movements = cursor.fetchall()
        conn.close()

        category_id, name, price_per_kg, description, image_url, sort_order, is_active, stock_grams, is_out_of_stock, low_stock_threshold_grams, is_promotion, promotion_title, promotion_sort_order = row
        checked = "checked" if is_active else ""
        out_of_stock_checked = "checked" if is_out_of_stock else ""
        image_url_value = escape(str(image_url or ""), quote=True)
        promotion_checked = "checked" if is_promotion else ""
        promotion_title_value = escape(str(promotion_title or ""), quote=True)
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать товар</h1>
          <p><a href="/products">← Назад к товарам</a></p>
          <form class="admin-form" method="post" action="/products/{product_id}/edit">
            <label>Категория <input name="category_id" value="{category_id}"/></label>
            <label>Название товара <input name="name" value="{name}"/></label>
            <label>Цена за кг (€) <input name="price_per_kg" value="{price_per_kg}"/></label>
            <label>Описание <input name="description" value="{description or ''}"/></label>
            <label>Ссылка на фото <input name="image_url" value="{image_url_value}"/></label>
            <label>Остаток, г <input name="stock_grams" type="number" min="0" value="{max(int(stock_grams or 0), 0)}"/></label>
            <label>Минимальный остаток, г <input name="low_stock_threshold_grams" type="number" min="0" value="{max(int(low_stock_threshold_grams or 0), 0)}"/></label>
            <label>Порядок сортировки <input name="sort_order" value="{sort_order}"/><small>Меньше число = выше в списке</small></label>
            <label>Товар активен <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true" {out_of_stock_checked}/></label>
            <fieldset>
              <legend>🔥 Продвижение товара</legend>
              <label>☑️ Показывать в разделе "Акции" <input type="checkbox" name="is_promotion" value="1" {promotion_checked}/></label>
              <label>Название акции
                <input name="promotion_title" value="{promotion_title_value}" placeholder="🔥 Хит недели / 🔥 Новинка / 🔥 Скидка 10% / 🔥 Лучший выбор"/>
              </label>
              <label>Порядок показа <input name="promotion_sort_order" type="number" value="{promotion_sort_order}"/></label>
            </fieldset>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        movement_rows = ""
        for movement_type, quantity_grams, stock_before, stock_after, order_id, note, created_at in inventory_movements:
            quantity_value = int(quantity_grams or 0)
            if quantity_value > 0:
                quantity_text = f"+{format_stock_grams(quantity_value)}"
            elif quantity_value < 0:
                quantity_text = f"-{format_stock_grams(abs(quantity_value))}"
            else:
                quantity_text = "0 г"
            order_link = "-"
            if order_id:
                order_id_text = escape(str(order_id), quote=True)
                order_link = f"<a class='view-link' href='/orders/{order_id_text}'>{order_id_text}</a>"
            movement_rows += f"""
            <tr>
              <td>{format_admin_datetime(created_at)}</td>
              <td>{escape(str(movement_type or '-'), quote=True)}</td>
              <td>{quantity_text}</td>
              <td>{format_stock_grams(stock_before)}</td>
              <td>{format_stock_grams(stock_after)}</td>
              <td>{order_link}</td>
              <td>{escape(str(note or '-'), quote=True)}</td>
            </tr>
            """
        if movement_rows:
            html += f"""
            <section class="admin-card dash-section">
              <h2>История остатков</h2>
              <div class="dash-table-wrap">
                <table>
                  <tr><th>Дата</th><th>Тип</th><th>Изменение</th><th>Было</th><th>Стало</th><th>Заказ</th><th>Комментарий</th></tr>
                  {movement_rows}
                </table>
              </div>
            </section>
            """
        else:
            html += """
            <section class="admin-card dash-section">
              <h2>История остатков</h2>
              <p>Пока нет истории остатков.</p>
            </section>
            """
        html += """
        <section class="admin-card dash-section">
          <h2>⚖️ Варианты продажи</h2>
        """
        html += f"<p><a class='button button-link' href='/products/{product_id}/options/new'>➕ Добавить вариант</a></p>"
        if options:
            html += """
          <div class="dash-table-wrap">
            <table>
              <tr><th>ID</th><th>Вариант</th><th>Вес</th><th>Цена</th><th>Сортировка</th><th>Статус</th><th>Действия</th></tr>
            """
            for option_id, label, weight, price, option_sort_order, option_is_active in options:
                option_status = "Активен" if option_is_active else "Скрыт"
                option_status_class = "active" if option_is_active else "inactive"
                weight_text = weight if weight is not None else "-"
                toggle_text = "👁 Скрыть" if option_is_active else "♻️ Включить"
                actions_html = f"<a class='button' href='/options/{option_id}/edit'>✏️ Редактировать</a>"
                actions_html += f" <form method='post' action='/options/{option_id}/toggle' style='display:inline; margin:0; padding:0;'><button class='button secondary' type='submit'>{toggle_text}</button></form>"
                html += f"<tr><td>{option_id}</td><td>{label}</td><td>{weight_text}</td><td>{price:.2f}</td><td>{option_sort_order}</td><td><span class='status {option_status_class}'>{option_status}</span></td><td><div class='action-group'>{actions_html}</div></td></tr>"
            html += """
            </table>
          </div>
            """
        else:
            html += "<p>Варианты ещё не добавлены.</p>"
        html += """
        </section>
        """
        html += f"<p><a class='button button-link' href='/products/{product_id}/recommendations'>🎯 Рекомендации продаж</a></p>"
        return admin_layout("✏️ Редактировать товар", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/products/{product_id}/recommendations", response_class=HTMLResponse)
async def product_recommendations_form(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM products WHERE id = %s",
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Товар не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Товар не найден</h1>
                  <p>Такой товар не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_name = row[0]

        cursor.execute(
            """
            SELECT id, name
            FROM products
            WHERE is_active = TRUE AND id != %s
            ORDER BY sort_order, id
            """,
            (product_id,),
        )
        other_products = cursor.fetchall()

        cursor.execute(
            """
            SELECT recommended_product_id
            FROM product_recommendations
            WHERE product_id = %s
              AND recommendation_type = 'frequently_bought_together'
              AND is_active = TRUE
            """,
            (product_id,),
        )
        recommended_ids = {recommended_row[0] for recommended_row in cursor.fetchall()}
        conn.close()

        product_name_text = escape(str(product_name or "-"), quote=True)
        checkbox_rows = ""
        for other_id, other_name in other_products:
            checked = "checked" if other_id in recommended_ids else ""
            other_name_text = escape(str(other_name or "-"), quote=True)
            checkbox_rows += (
                "<label style='display:block; margin:4px 0;'>"
                f"<input type='checkbox' name='recommended_product_id' value='{other_id}' {checked}/> "
                f"{other_name_text}"
                "</label>"
            )
        if not checkbox_rows:
            checkbox_rows = "<p>Нет других активных товаров для рекомендаций.</p>"

        html = f"""
        <section class="admin-card">
          <h1>🎯 Рекомендации продаж</h1>
          <p>Товар: <strong>{product_name_text}</strong></p>
          <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
          <form class="admin-form" method="post" action="/products/{product_id}/recommendations">
            <h2>Часто покупают вместе</h2>
            {checkbox_rows}
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить"/>
              <a class="button button-link secondary" href="/products/{product_id}/edit">← К товару</a>
            </div>
          </form>
        </section>
        """
        return admin_layout("🎯 Рекомендации продаж", html)
    except Exception as e:
        log_admin_error("/products/{product_id}/recommendations", "product_recommendations_form", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/products/{product_id}/recommendations")
async def update_product_recommendations(
    product_id: int,
    recommended_product_id: list[int] = Form([]),
):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM products WHERE id = %s",
            (product_id,),
        )
        if not cursor.fetchone():
            conn.close()
            return RedirectResponse("/products", status_code=303)

        cursor.execute(
            "SELECT id FROM products WHERE is_active = TRUE AND id != %s",
            (product_id,),
        )
        valid_ids = {valid_row[0] for valid_row in cursor.fetchall()}

        selected_ids = []
        seen_ids = set()
        for submitted_id in recommended_product_id:
            if submitted_id in valid_ids and submitted_id not in seen_ids:
                seen_ids.add(submitted_id)
                selected_ids.append(submitted_id)

        cursor.execute(
            """
            DELETE FROM product_recommendations
            WHERE product_id = %s
              AND recommendation_type = 'frequently_bought_together'
            """,
            (product_id,),
        )

        for sort_order, recommended_id in enumerate(selected_ids):
            cursor.execute(
                """
                INSERT INTO product_recommendations (
                    product_id,
                    recommended_product_id,
                    recommendation_type,
                    sort_order,
                    is_active
                )
                VALUES (%s, %s, 'frequently_bought_together', %s, TRUE)
                """,
                (product_id, recommended_id, sort_order),
            )

        conn.commit()
        conn.close()
    except Exception as e:
        log_admin_error("/products/{product_id}/recommendations", "update_product_recommendations", e)
    return RedirectResponse(f"/products/{product_id}/edit", status_code=303)


@app.get("/products/{product_id}/options/new", response_class=HTMLResponse)
async def new_product_option_form(product_id: int):
    html = f"""
    <section class="admin-card">
      <h1>➕ Новый вариант продажи</h1>
      <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
      <form class="admin-form" method="post" action="/products/{product_id}/options/new">
        <label>Название варианта <input name="label"/></label>
        <label>Вес в граммах <input name="weight"/></label>
        <label>Цена <input name="price"/></label>
        <label>Сортировка <input name="sort_order"/></label>
        <label>Активен <input type="checkbox" name="is_active" value="1"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать вариант"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новый вариант продажи", html)


@app.post("/products/{product_id}/options/new", response_class=HTMLResponse)
async def create_product_option(
    product_id: int,
    label: str = Form(...),
    weight: str = Form(None),
    price: str = Form(...),
    sort_order: str = Form("0"),
    is_active: str = Form(None),
):
    active = True if is_active else False
    weight_value = int(weight) if weight else None
    sort_value = int(sort_order) if sort_order else 0
    price_value = float(price)
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO product_options (
                product_id,
                label,
                weight,
                price,
                sort_order,
                is_active
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (product_id, label, weight_value, price_value, sort_value, active),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Вариант создан",
            f"""
            <section class="admin-card">
              <h1>✅ Вариант создан</h1>
              <p>Вариант продажи успешно добавлен.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
                <a class="button button-link secondary" href="/products/{product_id}/options/new">➕ Добавить ещё вариант</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/options/{option_id}/edit", response_class=HTMLResponse)
async def edit_product_option_form(option_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT product_id, label, weight, price, sort_order, is_active
            FROM product_options
            WHERE id = %s
            """,
            (option_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )

        product_id, label, weight, price, sort_order, is_active = row
        checked = "checked" if is_active else ""
        weight_value = weight if weight is not None else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать вариант</h1>
          <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
          <form class="admin-form" method="post" action="/options/{option_id}/edit">
            <label>Название варианта <input name="label" value="{label}"/></label>
            <label>Вес в граммах <input name="weight" value="{weight_value}"/></label>
            <label>Цена <input name="price" value="{price}"/></label>
            <label>Сортировка <input name="sort_order" value="{sort_order}"/></label>
            <label>Активен <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        return admin_layout("✏️ Редактировать вариант", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/options/{option_id}/edit", response_class=HTMLResponse)
async def update_product_option(
    option_id: int,
    label: str = Form(...),
    weight: str = Form(None),
    price: str = Form(...),
    sort_order: str = Form("0"),
    is_active: str = Form(None),
):
    active = True if is_active else False
    weight_value = int(weight) if weight else None
    sort_value = int(sort_order) if sort_order else 0
    price_value = float(price)
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT product_id FROM product_options WHERE id = %s", (option_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_id = row[0]
        cursor.execute(
            """
            UPDATE product_options
            SET label = %s,
                weight = %s,
                price = %s,
                sort_order = %s,
                is_active = %s
            WHERE id = %s
            """,
            (label, weight_value, price_value, sort_value, active, option_id),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Вариант обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Вариант обновлён</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
                <a class="button button-link secondary" href="/options/{option_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/options/{option_id}/toggle", response_class=HTMLResponse)
async def toggle_product_option(option_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE product_options SET is_active = NOT is_active WHERE id = %s RETURNING product_id, is_active",
            (option_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_id, is_active = row
        title = "✅ Вариант включён" if is_active else "✅ Вариант скрыт"
        message = "Вариант снова доступен для выбора." if is_active else "Вариант скрыт из выбора."
        return admin_layout(
            title,
            f"""
            <section class="admin-card">
              <h1>{title}</h1>
              <p>{message}</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/products/{product_id}/edit", response_class=HTMLResponse)
async def update_product(
    product_id: int,
    category_id: int = Form(...),
    name: str = Form(...),
    price_per_kg: float = Form(...),
    description: str = Form(''),
    image_url: str = Form(''),
    stock_grams: int = Form(0),
    low_stock_threshold_grams: str = Form("0"),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    is_out_of_stock: bool = Form(False),
    is_promotion: bool = Form(False),
    promotion_title: str = Form(''),
    promotion_sort_order: int = Form(0),
):
    active = True if is_active else False
    stock_value = max(stock_grams, 0)
    try:
        low_stock_threshold_value = max(int(low_stock_threshold_grams or 0), 0)
    except (TypeError, ValueError):
        low_stock_threshold_value = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT image_url, stock_grams FROM products WHERE id = %s", (product_id,))
        row = cursor.fetchone()
        existing_image_url = row[0] if row else ""
        old_stock = int(row[1] or 0) if row else 0
        submitted_image_url = image_url.strip()
        saved_image_url = submitted_image_url if submitted_image_url else existing_image_url
        cursor.execute(
            """
            UPDATE products
            SET category_id = %s,
                name = %s,
                price_per_kg = %s,
                description = %s,
                image_url = %s,
                stock_grams = %s,
                low_stock_threshold_grams = %s,
                is_out_of_stock = %s,
                sort_order = %s,
                is_active = %s,
                is_promotion = %s,
                promotion_title = %s,
                promotion_sort_order = %s
            WHERE id = %s
            """,
            (category_id, name, price_per_kg, description, saved_image_url, stock_value, low_stock_threshold_value, is_out_of_stock, sort_order, active, is_promotion, promotion_title or None, promotion_sort_order, product_id),
        )
        if old_stock != stock_value:
            log_inventory_movement(
                cursor,
                product_id,
                "manual_set",
                stock_value - old_stock,
                old_stock,
                stock_value,
                None,
                "Остаток изменён вручную в админке."
            )
        sync_low_stock_alert_state(cursor, [product_id])
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Товар обновлён</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
                <a class="button button-link secondary" href="/products/{product_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        log_admin_error("/products/{product_id}/edit", "update_product", e)
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/products/{product_id}/deactivate", response_class=HTMLResponse)
async def deactivate_product(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET is_active = FALSE WHERE id = %s", (product_id,))
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар скрыт",
            """
            <section class="admin-card">
              <h1>✅ Товар скрыт</h1>
              <p>Товар скрыт из каталога.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/products/{product_id}/activate", response_class=HTMLResponse)
async def activate_product(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET is_active = TRUE WHERE id = %s", (product_id,))
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар возвращён",
            """
            <section class="admin-card">
              <h1>✅ Товар возвращён</h1>
              <p>Товар снова отображается в каталоге.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories", response_class=HTMLResponse)
async def categories():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, sort_order, is_active
            FROM categories
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        html = "<section class='admin-card'><h1>🗂 Категории</h1><p><a class=\"button button-link\" href=\"/categories/new\">➕ Новая категория</a></p><div class='dash-table-wrap'><table class='categories-table'>"
        html += "<tr><th>ID</th><th>Название</th><th>Порядок</th><th>Статус</th><th>Действия</th></tr>"
        for row in rows:
            cid, name, sort_order, is_active = row
            active_text = "Активна" if is_active else "Скрыта"
            status_class = "active" if is_active else "inactive"
            actions_html = f"<div class=\"action-group\"><a class=\"button\" href=\"/categories/{cid}/edit\">Редактировать</a></div>"
            html += f"<tr><td>{cid}</td><td>{name}</td><td>{sort_order}</td><td><span class='status {status_class}'>{active_text}</span></td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("🗂 Категории", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories/new", response_class=HTMLResponse)
async def new_category_form():
    html = """
    <section class="admin-card">
      <h1>➕ Новая категория</h1>
      <p><a href="/categories">← Назад к категориям</a></p>
      <form class="admin-form" method="post" action="/categories/new">
        <label>Название категории <input name="name"/></label>
        <label>Порядок сортировки <input name="sort_order"/></label>
        <label>Активна <input type="checkbox" name="is_active" value="1"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать категорию"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новая категория", html)


@app.post("/categories/new", response_class=HTMLResponse)
async def create_category(
    name: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO categories (name, sort_order, is_active)
            VALUES (%s, %s, %s)
            """,
            (name, sort_order, active),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Категория создана",
            """
            <section class="admin-card">
              <h1>✅ Категория создана</h1>
              <p>Категория успешно добавлена.</p>
              <div class="form-actions">
                <a class="button button-link" href="/categories">← К категориям</a>
                <a class="button button-link secondary" href="/categories/new">➕ Добавить ещё</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories/{category_id}/edit", response_class=HTMLResponse)
async def edit_category_form(category_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name, sort_order, is_active
            FROM categories
            WHERE id = %s
            """,
            (category_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Категория не найдена",
                """
                <section class="admin-card">
                  <h1>⚠️ Категория не найдена</h1>
                  <p>Такая категория не найдена.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/categories">← К категориям</a>
                  </div>
                </section>
                """,
            )

        name, sort_order, is_active = row
        checked = "checked" if is_active else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать категорию</h1>
          <p><a href="/categories">← Назад к категориям</a></p>
          <form class="admin-form" method="post" action="/categories/{category_id}/edit">
            <label>Название категории <input name="name" value="{name}"/></label>
            <label>Порядок сортировки <input name="sort_order" value="{sort_order}"/></label>
            <label>Активна <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        return admin_layout("✏️ Редактировать категорию", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post("/categories/{category_id}/edit", response_class=HTMLResponse)
async def update_category(
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE categories
            SET name = %s,
                sort_order = %s,
                is_active = %s
            WHERE id = %s
            """,
            (name, sort_order, active, category_id),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Категория обновлена",
            f"""
            <section class="admin-card">
              <h1>✅ Категория обновлена</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/categories">← К категориям</a>
                <a class="button button-link secondary" href="/categories/{category_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

