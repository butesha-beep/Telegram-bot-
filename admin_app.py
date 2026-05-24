import os
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
import psycopg2

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

@app.get("/", response_class=HTMLResponse)
async def root():
    return "Deal Market Admin Panel works"

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
        
        html = "<h1>Orders</h1><table border='1' cellpadding='5'>"
        html += "<tr><th>ID</th><th>Order ID</th><th>Username</th><th>Phone</th><th>Address</th><th>Total (€)</th><th>Status</th><th>Payment Method</th><th>Actions</th></tr>"
        for row in rows:
            id_, order_id, username, phone, address, total, status, payment_method = row
            actions = [f"<a href=\"/orders/{order_id}\">View</a>"]
            for s in ("paid", "preparing", "done", "cancelled"):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id}/status/{s}\" style=\"display:inline; margin:0; padding:0;\"><button type=\"submit\">{s.capitalize()}</button></form>")
            actions_html = " ".join(actions)
            html += f"<tr><td>{id_}</td><td>{order_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{address or '-'}</td><td>{total:.2f}</td><td>{status}</td><td>{payment_method or '-'}</td><td>{actions_html}</td></tr>"
        html += "</table>"
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
                FROM cart_items
                WHERE order_id = %s
                """,
                (order_id,),
            )
            items = cursor.fetchall()
        except Exception:
            items = []
        conn.close()

        html = f"<h1>Order {order_id}</h1>"
        html += f"<p>id: {id_}</p>"
        html += f"<p>username: {username or '-'}</p>"
        html += f"<p>phone: {phone or '-'}</p>"
        html += f"<p>address: {address or '-'}</p>"
        html += f"<p>total: {total:.2f}</p>"
        html += f"<p>status: {status}</p>"
        html += f"<p>payment_method: {payment_method or '-'}</p>"
        if items:
            html += "<h2>Items</h2><table border='1' cellpadding='5'>"
            html += "<tr><th>Product</th><th>Weight</th><th>Price</th></tr>"
            for product_name, weight, price in items:
                html += f"<tr><td>{product_name}</td><td>{weight}</td><td>{price:.2f}</td></tr>"
            html += "</table>"
        html += f"<p><a href=\"/orders\">Back to orders</a></p>"
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

        html = "<h1>Products</h1><table border='1' cellpadding='5'>"
        html += "<tr><th>ID</th><th>Name</th><th>Price/kg (€)</th><th>Image</th><th>Active</th><th>Category</th><th>Actions</th></tr>"
        for row in rows:
            pid, name, price, image_url, is_active, category_name = row
            img_html = f"<img src=\"{image_url}\" width=80/>" if image_url else "-"
            active_text = "yes" if is_active else "no"
            actions_html = f"<a href=\"/products/{pid}/edit\">Edit</a>"
            if is_active:
                actions_html += f" <form method=\"post\" action=\"/products/{pid}/deactivate\" style=\"display:inline; margin:0; padding:0;\"><button type=\"submit\">Deactivate</button></form>"
            else:
                actions_html += f" <form method=\"post\" action=\"/products/{pid}/activate\" style=\"display:inline; margin:0; padding:0;\"><button type=\"submit\">Activate</button></form>"
            html += f"<tr><td>{pid}</td><td>{name}</td><td>{price:.2f}</td><td>{img_html}</td><td>{active_text}</td><td>{category_name or '-'}</td><td>{actions_html}</td></tr>"
        html += "</table>"
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
        html += "<p><a href=\"/categories/new\">Create new category</a></p>"
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
