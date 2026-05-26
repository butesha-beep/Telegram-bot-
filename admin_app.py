import os
import html
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
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
  .status { color: var(--accent); font-weight: 700; }
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
  @media (max-width: 720px) {
    .admin-nav { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
    .admin-container { padding: 22px 18px; }
    .admin-card { padding: 18px; }
    .dash-hero { align-items: flex-start; flex-direction: column; }
    .dash-grid { grid-template-columns: 1fr; }
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
            SELECT id, order_id, username, phone, address, total, status, payment_method
            FROM orders
            ORDER BY id DESC
            LIMIT 50
        """)
        rows = cursor.fetchall()
        conn.close()
        
        html = f"<html><head><title>Orders</title>{PAGE_STYLE}</head><body><div class='container'><h1>Orders</h1><table>"
        html += "<tr><th>ID</th><th>Order ID</th><th>Username</th><th>Phone</th><th>Address</th><th>Total (€)</th><th>Status</th><th>Payment Method</th><th>Actions</th></tr>"
        for row in rows:
            id_, order_id, username, phone, address, total, status, payment_method = row
            actions = [f"<a class=\"button\" href=\"/orders/{order_id}\">View</a>"]
            for s in ("paid", "preparing", "done", "cancelled"):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id}/status/{s}\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">{s.capitalize()}</button></form>")
            actions_html = " ".join(actions)
            html += f"<tr><td>{id_}</td><td>{order_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{address or '-'}</td><td>{total:.2f}</td><td><span class='status'>{status}</span></td><td>{payment_method or '-'}</td><td>{actions_html}</td></tr>"
        html += "</table></div></body></html>"
        return html
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
        cursor.execute("UPDATE orders SET status = %s WHERE order_id = %s", (status, order_id))
        conn.commit()
        conn.close()
        return f"<h1>Order status updated</h1><p><a href=\"/orders\">Back to orders</a></p>"
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(order_id: str):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, order_id, username, phone, address, total, status, payment_method
            FROM orders
            WHERE order_id = %s
            """,
            (order_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return f"<h1>Order not found</h1><p><a href=\"/orders\">Back to orders</a></p>"

        id_, order_id, username, phone, address, total, status, payment_method = row
        try:
            cursor.execute(
                """
                SELECT product_name, weight, price
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
                """,
                (order_id,),
            )
            items = cursor.fetchall()
        except Exception:
            items = []
        conn.close()

        html = f"<html><head><title>Order {order_id}</title>{PAGE_STYLE}</head><body><div class='container'><h1>Order {order_id}</h1><div class='card'>"
        html += f"<div class='field'><strong>Order ID:</strong> {order_id}</div>"
        html += f"<div class='field'><strong>Client:</strong> {username or '-'}</div>"
        html += f"<div class='field'><strong>Phone:</strong> {phone or '-'}</div>"
        html += f"<div class='field'><strong>Address:</strong> {address or '-'}</div>"
        html += f"<div class='field'><strong>Status:</strong> <span class='status'>{status}</span></div>"
        html += f"<div class='field'><strong>Payment:</strong> {payment_method or '-'}</div>"
        html += "</div>"
        if items:
            html += "<h2>Items</h2><table>"
            html += "<tr><th>Product</th><th>Weight</th></tr>"
            for product_name, weight, price in items:
                html += f"<tr><td>{product_name}</td><td>{weight}</td></tr>"
            html += "</table>"
            html += f"<p><strong>Total: €{total:.2f}</strong></p>"
        else:
            html += "<p>No order items found</p>"
            html += f"<p><strong>Total: €{total:.2f}</strong></p>"
        html += f"<p><a class='button button-link' href=\"/orders\">Back to orders</a></p></div></body></html>"
        return html
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
        
        html = "<h1>Clients</h1><table border='1' cellpadding='5'>"
        html += "<tr><th>Telegram ID</th><th>Username</th><th>Phone</th><th>Address</th></tr>"
        for row in rows:
            telegram_id, username, phone, address = row
            html += f"<tr><td>{telegram_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{address or '-'}</td></tr>"
        html += "</table>"
        return html
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

        html = f"<html><head><title>Products</title>{PAGE_STYLE}</head><body><div class='container'><h1>Products</h1><p><a class='button button-link' href='/products/new'>➕ Новый товар</a></p><table>"
        html += "<tr><th>ID</th><th>Name</th><th>Price/kg (€)</th><th>Image</th><th>Active</th><th>Category</th><th>Actions</th></tr>"
        for row in rows:
            pid, name, price, image_url, is_active, category_name = row
            img_html = f"<img src=\"{image_url}\" width=80 style=\"border-radius:6px;\"/>" if image_url else "-"
            active_text = "yes" if is_active else "no"
            actions_html = f"<a class=\"button\" href=\"/products/{pid}/edit\">Edit</a>"
            if is_active:
                actions_html += f" <form method=\"post\" action=\"/products/{pid}/deactivate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Deactivate</button></form>"
            else:
                actions_html += f" <form method=\"post\" action=\"/products/{pid}/activate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Activate</button></form>"
            html += f"<tr><td>{pid}</td><td>{name}</td><td>{price:.2f}</td><td>{img_html}</td><td>{active_text}</td><td>{category_name or '-'}</td><td>{actions_html}</td></tr>"
        html += "</table></div></body></html>"
        return html
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"
@app.get("/products/new", response_class=HTMLResponse)
async def new_product_form():
    html = """
    <h1>New Product</h1>
    <form method="post" action="/products/new">
      <label>category_id: <input name="category_id"/></label><br/>
      <label>name: <input name="name"/></label><br/>
      <label>price_per_kg: <input name="price_per_kg"/></label><br/>
      <label>description: <input name="description"/></label><br/>
      <label>image_url: <input name="image_url"/></label><br/>
      <label>sort_order: <input name="sort_order" value="0"/></label><br/>
      <label>is_active: <input type="checkbox" name="is_active" value="1"/></label><br/>
      <input type="submit" value="Create"/>
    </form>
    """
    return html


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
        return "<h1>Product created</h1><p><a href=\"/products\">Back to products</a></p>"
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
        conn.close()
        if not row:
            return "<h1>Product not found</h1>"

        category_id, name, price_per_kg, description, image_url, sort_order, is_active = row
        checked = "checked" if is_active else ""
        html = f"""
        <h1>Edit Product</h1>
        <form method="post" action="/products/{product_id}/edit">
          <label>category_id: <input name="category_id" value="{category_id}"/></label><br/>
          <label>name: <input name="name" value="{name}"/></label><br/>
          <label>price_per_kg: <input name="price_per_kg" value="{price_per_kg}"/></label><br/>
          <label>description: <input name="description" value="{description or ''}"/></label><br/>
          <label>image_url: <input name="image_url" value="{image_url or ''}"/></label><br/>
          <label>sort_order: <input name="sort_order" value="{sort_order}"/></label><br/>
          <label>is_active: <input type="checkbox" name="is_active" value="1" {checked}/></label><br/>
          <input type="submit" value="Update"/>
        </form>
        """
        return html
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
        return f"<h1>Product updated</h1><p><a href=\"/products\">Back to products</a></p>"
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
        return f"<h1>Product deactivated</h1><p><a href=\"/products\">Back to products</a></p>"
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
        return f"<h1>Product activated</h1><p><a href=\"/products\">Back to products</a></p>"
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

        html = "<h1>Categories</h1>"
        html += "<p><a class=\"button button-link\" href=\"/categories/new\">➕ Новая категория</a></p>"
        html += "<table border='1' cellpadding='5'>"
        html += "<tr><th>ID</th><th>Name</th><th>Sort Order</th><th>Active</th><th>Actions</th></tr>"
        for row in rows:
            cid, name, sort_order, is_active = row
            active_text = "yes" if is_active else "no"
            html += f"<tr><td>{cid}</td><td>{name}</td><td>{sort_order}</td><td>{active_text}</td><td><a href=\"/categories/{cid}/edit\">Edit</a></td></tr>"
        html += "</table>"
        return html
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


@app.get("/categories/new", response_class=HTMLResponse)
async def new_category_form():
    html = """
    <h1>New Category</h1>
    <form method="post" action="/categories/new">
      <label>name: <input name="name"/></label><br/>
      <label>sort_order: <input name="sort_order"/></label><br/>
      <label>is_active: <input type="checkbox" name="is_active" value="1"/></label><br/>
      <input type="submit" value="Create"/>
    </form>
    """
    return html


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
        return "<h1>Category created</h1><p><a href=\"/categories\">Back to categories</a></p>"
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
            return "<h1>Category not found</h1>"

        name, sort_order, is_active = row
        checked = "checked" if is_active else ""
        html = f"""
        <h1>Edit Category</h1>
        <form method="post" action="/categories/{category_id}/edit">
          <label>name: <input name="name" value="{name}"/></label><br/>
          <label>sort_order: <input name="sort_order" value="{sort_order}"/></label><br/>
          <label>is_active: <input type="checkbox" name="is_active" value="1" {checked}/></label><br/>
          <input type="submit" value="Update"/>
        </form>
        """
        return html
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
        return f"<h1>Category updated</h1><p><a href=\"/categories\">Back to categories</a></p>"
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
