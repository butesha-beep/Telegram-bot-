import os

import psycopg2


DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            username TEXT,
            first_name TEXT
        )
    """)
    try:
        cursor.execute(
            "ALTER TABLE clients ADD COLUMN phone TEXT"
        )
    except:
        conn.rollback()

    try:
        cursor.execute(
            "ALTER TABLE clients ADD COLUMN address TEXT"
        )
    except:
        conn.rollback()
    cursor.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS client_note TEXT")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            product_id INTEGER,
            weight INTEGER
        )
    """)
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS option_id INTEGER")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ")
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_cart_items_telegram_updated
        ON cart_items (telegram_id, updated_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_events (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_events_telegram_created
        ON customer_events (telegram_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_events_type_created
        ON customer_events (event_type, created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_id BIGINT,
            telegram_id BIGINT,
            username TEXT,
            phone TEXT,
            address TEXT,
            total REAL,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL,
            product_id INTEGER,
            product_name TEXT,
            weight INTEGER,
            price REAL
        )
    """)
    cursor.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS option_id INTEGER")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_events (
            id SERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            event_text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_events_order_id_created_at
        ON order_events (order_id, created_at)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            route TEXT,
            action TEXT,
            error_message TEXT,
            traceback TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_error_logs_created_at
        ON error_logs (created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_posts (
            id SERIAL PRIMARY KEY,
            message_text TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            error_message TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_at TIMESTAMPTZ
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channel_posts_created_at
        ON channel_posts (created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id SERIAL PRIMARY KEY,
            message_text TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            error_message TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_at TIMESTAMPTZ
        )
    """)
    cursor.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS target_type TEXT DEFAULT 'all_clients'")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_recipients (
            id SERIAL PRIMARY KEY,
            broadcast_id INTEGER REFERENCES broadcasts(id),
            telegram_id BIGINT NOT NULL,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            sent_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (broadcast_id, telegram_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_broadcast
        ON broadcast_recipients (broadcast_id)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            category_id INTEGER REFERENCES categories(id),
            name TEXT NOT NULL,
            price_per_kg REAL NOT NULL,
            description TEXT,
            image_url TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 0
        )
    """)
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_grams INTEGER DEFAULT 0")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_out_of_stock BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_threshold_grams INTEGER DEFAULT 500")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_alert_sent BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_alert_sent_at TIMESTAMPTZ")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_options (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            label TEXT NOT NULL,
            weight INTEGER,
            price REAL NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            order_id BIGINT,
            movement_type TEXT NOT NULL,
            quantity_grams INTEGER NOT NULL,
            stock_before INTEGER,
            stock_after INTEGER,
            note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_inventory_movements_product_created
        ON inventory_movements (product_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_shops (
            id SERIAL PRIMARY KEY,
            shop_key TEXT UNIQUE NOT NULL,
            brand_name TEXT NOT NULL,
            admin_url TEXT,
            bot_username TEXT,
            bot_url TEXT,
            landing_url TEXT,
            status TEXT DEFAULT 'demo',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("ALTER TABLE master_shops ADD COLUMN IF NOT EXISTS bot_url TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_shop_snapshots (
            id SERIAL PRIMARY KEY,
            shop_key TEXT NOT NULL,
            total_orders INTEGER DEFAULT 0,
            today_orders INTEGER DEFAULT 0,
            pending_orders INTEGER DEFAULT 0,
            month_revenue NUMERIC DEFAULT 0,
            low_stock_count INTEGER DEFAULT 0,
            total_clients INTEGER DEFAULT 0,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_master_shop_snapshots_shop_seen
        ON master_shop_snapshots (shop_key, last_seen_at DESC, id DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_recommendations (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            recommended_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            recommendation_type TEXT NOT NULL DEFAULT 'frequently_bought_together',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_product_recommendations_unique
        ON product_recommendations (product_id, recommended_product_id, recommendation_type)
    """)
    cursor.execute("""
        INSERT INTO product_options (
            product_id,
            label,
            weight,
            price,
            sort_order,
            is_active
        )
        SELECT
            p.id,
            option_data.label,
            option_data.weight,
            p.price_per_kg * option_data.weight / 1000,
            option_data.weight,
            TRUE
        FROM products p
        CROSS JOIN (
            VALUES
                ('50 г', 50),
                ('100 г', 100),
                ('200 г', 200),
                ('500 г', 500)
        ) AS option_data(label, weight)
        WHERE p.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM product_options po
              WHERE po.product_id = p.id
          )
    """)
    try:
        cursor.execute("ALTER TABLE orders ALTER COLUMN order_id TYPE BIGINT")
        cursor.execute("ALTER TABLE orders ALTER COLUMN telegram_id TYPE BIGINT")
        conn.commit()
    except Exception as e:
        print("ORDERS BIGINT MIGRATION ERROR:", e)
        conn.rollback()

    try:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN payment_method TEXT"
        )
    except:
        conn.rollback()

    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_selected_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reminded_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reported_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_deducted BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_deducted_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_restored BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_restored_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_note TEXT")

    conn.commit()
    conn.close()
