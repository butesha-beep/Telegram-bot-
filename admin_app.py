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
            SELECT order_id, username, phone, total, status
            FROM orders
            ORDER BY order_id DESC
            LIMIT 20
        """)
        rows = cursor.fetchall()
        conn.close()
        
        html = "<h1>Orders</h1><table border='1' cellpadding='5'>"
        html += "<tr><th>Order ID</th><th>Username</th><th>Phone</th><th>Total (€)</th><th>Status</th></tr>"
        for row in rows:
            order_id, username, phone, total, status = row
            html += f"<tr><td>{order_id}</td><td>{username or '-'}</td><td>{phone or '-'}</td><td>{total:.2f}</td><td>{status}</td></tr>"
        html += "</table>"
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
        html += "<tr><th>ID</th><th>Name</th><th>Price/kg (€)</th><th>Image</th><th>Active</th><th>Category</th></tr>"
        for row in rows:
            pid, name, price, image_url, is_active, category_name = row
            img_html = f"<img src=\"{image_url}\" width=80/>" if image_url else "-"
            active_text = "yes" if is_active else "no"
            html += f"<tr><td>{pid}</td><td>{name}</td><td>{price:.2f}</td><td>{img_html}</td><td>{active_text}</td><td>{category_name or '-'}</td></tr>"
        html += "</table>"
        return html
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)


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
      <label>sort_order: <input name="sort_order"/></label><br/>
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
        cursor.execute(
            """
            INSERT INTO products
                (category_id, name, price_per_kg, description, image_url, sort_order, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (category_id, name, price_per_kg, description, image_url, sort_order, active),
        )
        conn.commit()
        conn.close()
        return "<h1>Product created</h1><p><a href=\"/products\">Back to products</a></p>"
    except Exception as e:
        return f"<h1>Error</h1><p>{str(e)}</p>"
