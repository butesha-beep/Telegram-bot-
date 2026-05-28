import os
import html
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import psycopg2

app = FastAPI()

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


def admin_layout(title, content):
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
        </div>
      </nav>
    </header>
    <main class="admin-container">{content}</main>
  </div>
</body>
</html>"""


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


def format_admin_datetime(value):
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return html.escape(value.strftime("%d.%m.%Y %H:%M"))
    return html.escape(str(value))


DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

@app.get("/", response_class=HTMLResponse)
async def root():
    stats = None
    latest_orders = []
    error_message = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(total), 0)
            FROM orders
            """
        )
        stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT order_id, username, phone, address, total, status
            FROM orders
            ORDER BY id DESC
            LIMIT 5
            """
        )
        latest_orders = cursor.fetchall()
        conn.close()
    except Exception as e:
        error_message = str(e)

    if stats:
        total_orders, pending_orders, paid_orders, revenue = stats
        stat_cards = f"""
        <div class="dash-grid">
          <div class="dash-card"><span>Всего заказов</span><strong class="stat-value">{total_orders}</strong></div>
          <div class="dash-card"><span>Ожидают оплаты</span><strong class="stat-value">{pending_orders}</strong></div>
          <div class="dash-card"><span>Оплачены</span><strong class="stat-value">{paid_orders}</strong></div>
          <div class="dash-card"><span>Выручка</span><strong class="stat-value">EUR {float(revenue):.2f}</strong></div>
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

    return admin_layout(
        "Deal Market NL",
        f"""
        <section class="dash-hero">
          <div>
            <p class="dash-kicker">Панель управления</p>
            <h1>Deal Market NL</h1>
            <p>Быстрый доступ к заказам, каталогу, категориям и клиентам.</p>
          </div>
        </section>

        <div class="dash-grid">
          <a class="dash-card" href="/orders"><strong>Заказы</strong><span>Просмотр и обновление заказов клиентов</span></a>
          <a class="dash-card" href="/products"><strong>Товары</strong><span>Редактирование товаров и наличия</span></a>
          <a class="dash-card" href="/categories"><strong>Категории</strong><span>Управление разделами каталога</span></a>
          <a class="dash-card" href="/clients"><strong>Клиенты</strong><span>Просмотр сохраненных данных клиентов</span></a>
        </div>

        {stat_cards}

        <section class="admin-card dash-section">
          <h2>Последние заказы</h2>
          {latest_section}
        </section>
        """,
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/orders", response_class=HTMLResponse)
async def orders():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, order_id, username, phone, address, total, status, payment_method, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT 50
        """)
        rows = cursor.fetchall()
        conn.close()
        
        html = "<section class='admin-card'><h1>📦 Заказы</h1><div class='dash-table-wrap'><table>"
        html += "<tr><th>ID</th><th>№ заказа</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Сумма</th><th>Статус</th><th>Оплата</th><th>Создан</th><th>Действия</th></tr>"
        for row in rows:
            id_, order_id, username, phone, address, total, status, payment_method, created_at = row
            status_labels = {
                "paid": "Оплачен",
                "preparing": "Готовится",
                "done": "Готов",
                "cancelled": "Отмена",
            }
            status_actions = {
                "pending": ("paid", "preparing", "done", "cancelled"),
                "awaiting_payment": ("paid", "preparing", "done", "cancelled"),
                "payment_reported": ("paid", "preparing", "done", "cancelled"),
                "cash_on_delivery": ("paid", "preparing", "done", "cancelled"),
                "paid": ("preparing", "done", "cancelled"),
                "preparing": ("done", "cancelled"),
                "done": (),
                "cancelled": (),
            }
            actions = [f"<a class=\"button\" href=\"/orders/{order_id}\">Открыть</a>"]
            for s in status_actions.get(str(status or ""), ()):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id}/status/{s}\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">{status_labels[s]}</button></form>")
            actions_html = f"<div class=\"action-group\">{' '.join(actions)}</div>"
            html += f"<tr><td>{id_}</td><td>{order_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{address or '-'}</td><td>{total:.2f}</td><td>{admin_status_badge(status)}</td><td>{payment_method or '-'}</td><td>{format_admin_datetime(created_at)}</td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("📦 Заказы", html)
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.post("/orders/{order_id}/status/{status}", response_class=HTMLResponse)
async def update_order_status(order_id: str, status: str):
    allowed = {"paid", "preparing", "done", "cancelled"}
    if status not in allowed:
        return f"<h1>Invalid status</h1><p>Allowed: {', '.join(sorted(allowed))}</p>"
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT status FROM orders WHERE order_id = %s", (order_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return RedirectResponse("/orders", status_code=303)

        current_status = str(row[0] or "")
        allowed_transitions = {
            "pending": {"paid", "preparing", "done", "cancelled"},
            "awaiting_payment": {"paid", "preparing", "done", "cancelled"},
            "payment_reported": {"paid", "preparing", "done", "cancelled"},
            "cash_on_delivery": {"paid", "preparing", "done", "cancelled"},
            "paid": {"preparing", "done", "cancelled"},
            "preparing": {"done", "cancelled"},
            "done": set(),
            "cancelled": set(),
        }
        if status not in allowed_transitions.get(current_status, set()):
            print(f"INVALID ORDER STATUS TRANSITION: {order_id} {current_status} -> {status}")
            conn.close()
            return RedirectResponse("/orders", status_code=303)

        cursor.execute(
            "UPDATE orders SET status = %s, updated_at = NOW() WHERE order_id = %s",
            (status, order_id)
        )
        conn.commit()
        conn.close()
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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
                payment_reported_at
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
        ) = row
        try:
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
            items = cursor.fetchall()
        except Exception:
            cursor.execute(
                """
                SELECT product_name, weight, price
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
                """,
                (order_id,),
            )
            items = [(product_name, weight, price, None) for product_name, weight, price in cursor.fetchall()]
        conn.close()

        html = f"<section class='admin-card'><h1>Заказ {order_id}</h1><div class='detail-grid'>"
        html += f"<div class='detail-field'><strong>№ заказа</strong>{order_id}</div>"
        html += f"<div class='detail-field'><strong>Клиент</strong>{username or '-'}</div>"
        html += f"<div class='detail-field'><strong>Телефон</strong>{phone or '-'}</div>"
        html += f"<div class='detail-field'><strong>Адрес</strong>{address or '-'}</div>"
        html += f"<div class='detail-field'><strong>Статус</strong>{admin_status_badge(status)}</div>"
        html += f"<div class='detail-field'><strong>Оплата</strong>{payment_method or '-'}</div>"
        html += "</div></section>"
        html += "<section class='admin-card dash-section'><h2>⏱️ История заказа</h2><div class='detail-grid'>"
        html += f"<div class='detail-field'><strong>Создан</strong>{format_admin_datetime(created_at)}</div>"
        html += f"<div class='detail-field'><strong>Обновлён</strong>{format_admin_datetime(updated_at)}</div>"
        html += f"<div class='detail-field'><strong>Оплата выбрана</strong>{format_admin_datetime(payment_selected_at)}</div>"
        html += f"<div class='detail-field'><strong>Напоминание отправлено</strong>{format_admin_datetime(payment_reminded_at)}</div>"
        html += f"<div class='detail-field'><strong>Оплата заявлена</strong>{format_admin_datetime(payment_reported_at)}</div>"
        html += "</div></section>"
        if items:
            html += "<section class='admin-card dash-section'><h2>Товары</h2><div class='dash-table-wrap'><table>"
            html += "<tr><th>Товар</th><th>Вариант / вес</th><th>Итого</th></tr>"
            for product_name, weight, price, option_label in items:
                item_label = option_label if option_label else f"{weight} г"
                html += f"<tr><td>{product_name}</td><td>{item_label}</td><td>{price:.2f} €</td></tr>"
            html += "</table></div>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        else:
            html += "<section class='admin-card dash-section'><p>Товары не найдены.</p>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        html += f"<p><a class='button button-link' href=\"/orders\">← К заказам</a></p>"
        return admin_layout(f"Заказ {order_id}", html)
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.get("/clients", response_class=HTMLResponse)
async def clients():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_id, username, phone, address
            FROM clients
            ORDER BY telegram_id DESC
            LIMIT 20
        """)
        rows = cursor.fetchall()
        conn.close()
        
        html = "<section class='admin-card'><h1>👥 Клиенты</h1><div class='dash-table-wrap'><table>"
        html += "<tr><th>Telegram ID</th><th>Клиент</th><th>Телефон</th><th>Адрес</th></tr>"
        for row in rows:
            telegram_id, username, phone, address = row
            html += f"<tr><td>{telegram_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{address or '-'}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("👥 Клиенты", html)
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.get("/products", response_class=HTMLResponse)
async def products():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT p.id, p.name, p.price_per_kg, p.image_url, p.is_active, c.name
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            ORDER BY p.sort_order, p.id
        """)
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

        html = "<section class='admin-card' id='all-products'><h1>🛒 Товары</h1><p><a class='button button-link' href='/products/new'>➕ Новый товар</a></p><p>Скрытые товары не показываются в каталоге, но остаются в системе.</p>"
        html += "<div class='quick-nav'><a href='#all-products'>Все товары</a>"
        for category_label in category_order:
            html += f"<a href='#{category_anchors[category_label]}'>📁 {category_label}</a>"
        html += "</div><div class='dash-table-wrap'><table class='products-table'>"
        html += "<tr><th>ID</th><th>Название</th><th>Цена</th><th>Фото</th><th>Статус</th><th>Категория</th><th>Действия</th></tr>"

        for category_label in category_order:
            html += f"<tr class='category-row' id='{category_anchors[category_label]}'><td colspan='7'>📁 {category_label}</td></tr>"
            for row in grouped_products[category_label]:
                pid, name, price, image_url, is_active, category_name = row
                img_html = f"<img class=\"product-thumb\" src=\"{image_url}\"/>" if image_url else "-"
                active_text = "Активен" if is_active else "Выключен"
                status_class = "active" if is_active else "inactive"
                actions_html = f"<a class=\"button\" href=\"/products/{pid}/edit\">Редактировать</a>"
                if is_active:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/deactivate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Скрыть</button></form>"
                else:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/activate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Вернуть</button></form>"
                actions_html = f"<div class=\"action-group\">{actions_html}</div>"
                html += f"<tr><td>{pid}</td><td>{name}</td><td>{price:.2f}</td><td>{img_html}</td><td><span class='status {status_class}'>{active_text}</span></td><td>{category_label}</td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("🛒 Товары", html)
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"
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
        <label>Порядок сортировки <input name="sort_order" value="0"/><small>Меньше число = выше в списке</small></label>
        <label>Товар активен <input type="checkbox" name="is_active" value="1"/></label>
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
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
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
    is_active,
    sort_order
)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
    new_id,
    category_id,
    name,
    price_per_kg,
    description,
    image_url,
    active,
    sort_order
),
        )
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
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT category_id, name, price_per_kg, description, image_url, sort_order, is_active
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
        conn.close()

        category_id, name, price_per_kg, description, image_url, sort_order, is_active = row
        checked = "checked" if is_active else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать товар</h1>
          <p><a href="/products">← Назад к товарам</a></p>
          <form class="admin-form" method="post" action="/products/{product_id}/edit">
            <label>Категория <input name="category_id" value="{category_id}"/></label>
            <label>Название товара <input name="name" value="{name}"/></label>
            <label>Цена за кг (€) <input name="price_per_kg" value="{price_per_kg}"/></label>
            <label>Описание <input name="description" value="{description or ''}"/></label>
            <label>Ссылка на фото <input name="image_url" value="{image_url or ''}"/></label>
            <label>Порядок сортировки <input name="sort_order" value="{sort_order}"/><small>Меньше число = выше в списке</small></label>
            <label>Товар активен <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
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
        return admin_layout("✏️ Редактировать товар", html)
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.post("/products/{product_id}/edit", response_class=HTMLResponse)
async def update_product(
    product_id: int,
    category_id: int = Form(...),
    name: str = Form(...),
    price_per_kg: float = Form(...),
    description: str = Form(''),
    image_url: str = Form(''),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE products
            SET category_id = %s,
                name = %s,
                price_per_kg = %s,
                description = %s,
                image_url = %s,
                sort_order = %s,
                is_active = %s
            WHERE id = %s
            """,
            (category_id, name, price_per_kg, description, image_url, sort_order, active, product_id),
        )
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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


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
        return f"<h1>Error</h1><p>{str(e)}</p>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
