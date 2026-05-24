import os
from fastapi import FastAPI
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
