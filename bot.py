import asyncio
import json
from json.tool import main
import os
import sqlite3
import psycopg2
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

if not DATABASE_URL:
    available_vars = [key for key in os.environ.keys() if "DATABASE" in key or key.startswith("PG")]
    raise ValueError(f"DATABASE_URL is not set. Available DB vars: {available_vars}")
pending_orders = {}

def load_json(filename):
    with open(filename, "r", encoding="utf-8") as file:
        return json.load(file)
    

def get_products():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, category_id, name, price_per_kg, description, image_url, is_active
            FROM products
            WHERE is_active = TRUE
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        if rows:
            products = []
            for row in rows:
                product_id, category_id, name, price_per_kg, description, image_url, is_active = row
                products.append({
                    "id": product_id,
                    "category_id": category_id,
                    "name": name,
                    "price_per_kg": price_per_kg,
                    "description": description,
                    "image_url": image_url,
                    "photo": image_url,
                    "is_active": is_active
                })
            return products
    except Exception:
        pass

    return load_json("products.json")

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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            product_id INTEGER,
            weight INTEGER
        )
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

    conn.commit()
    conn.close()


def seed_products_from_json():
    categories = load_json("categories.json")
    products = load_json("products.json")

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    for category in categories:
        cursor.execute(
            """
            INSERT INTO categories (id, name, sort_order, is_active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                sort_order = EXCLUDED.sort_order,
                is_active = EXCLUDED.is_active
            """,
            (
                category["id"],
                category["name"],
                category.get("sort_order", 0),
                category.get("is_active", True)
            )
        )

    for product in products:
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
            ON CONFLICT (id) DO UPDATE SET
                category_id = EXCLUDED.category_id,
                name = EXCLUDED.name,
                price_per_kg = EXCLUDED.price_per_kg,
                description = EXCLUDED.description,
                image_url = EXCLUDED.image_url,
                is_active = EXCLUDED.is_active,
                sort_order = EXCLUDED.sort_order
            """,
            (
                product["id"],
                product["category_id"],
                product["name"],
                product["price_per_kg"],
                product.get("description"),
                product.get("image_url"),
                product.get("is_active", True),
                product.get("sort_order", 0)
            )
        )

    conn.commit()
    conn.close()


def save_client(user):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO clients (
            telegram_id,
            username,
            first_name
        )
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
    """, (
        user.id,
        user.username,
        user.first_name
    ))

    conn.commit()
    conn.close()


def main_menu():
    categories = []

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name
            FROM categories
            WHERE is_active = TRUE
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        if rows:
            categories = [
                {"id": row[0], "name": row[1]}
                for row in rows
            ]
    except Exception:
        categories = []

    if not categories:
        categories = load_json("categories.json")

    keyboard = []

    for category in categories:
        keyboard.append([
            InlineKeyboardButton(
                text=category["name"],
                callback_data=f"category_{category['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⚽️ Поддержка",
            callback_data="support"
        )
    ])
    keyboard.append([
    InlineKeyboardButton(
        text="🛒 Корзина",
        callback_data="cart"
    )
])
    
    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


@dp.callback_query(F.data.startswith("category_"))
async def show_category(callback: types.CallbackQuery):

    category_id = int(callback.data.split("_")[1])

    products = get_products()

    category_products = [
        product
        for product in products
        if product["category_id"] == category_id
    ]

    if not category_products:
        await callback.message.answer(
            "📭 В этой категории пока нет товаров."
        )
        await callback.answer()
        return

    keyboard = []

    for product in category_products:
        keyboard.append([
            InlineKeyboardButton(
                text=product["name"],
                callback_data=f"product_{product['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="back_to_menu"
        )
    ])

    await callback.message.answer(
        "Выберите товар:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )

    await callback.answer()
@dp.callback_query(F.data.startswith("product_"))
async def show_product(callback: types.CallbackQuery):

    product_id = int(callback.data.split("_")[1])

    products = get_products()

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer(
            "❌ Товар не найден."
        )
        await callback.answer()
        return

    price_100g = product["price_per_kg"] / 10

    text = (
        f"🛒 {product['name']}\n\n"
        f"{product['description']}\n\n"
        f"💶 Цена: {product['price_per_kg']} €/кг\n"
        f"⚖️ 100 г: {price_100g:.2f} €"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="50 г",
                    callback_data=f"weight_{product_id}_50"
                ),
                InlineKeyboardButton(
                    text="100 г",
                    callback_data=f"weight_{product_id}_100"
                )
            ],
            [
                InlineKeyboardButton(
                    text="200 г",
                    callback_data=f"weight_{product_id}_200"
                ),
                InlineKeyboardButton(
                    text="500 г",
                    callback_data=f"weight_{product_id}_500"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"category_{product['category_id']}"
                )
            ]
        ]
    )

    if product.get("photo"):
        await callback.message.answer_photo(
            photo=product["photo"],
            caption=text,
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=keyboard
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("weight_"))
async def choose_weight(callback: types.CallbackQuery):

    _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    products = get_products()

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer(
            "❌ Товар не найден."
        )
        await callback.answer()
        return

    price = product["price_per_kg"] * weight / 1000

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
    text="🛒 Добавить в корзину",
    callback_data=f"cart_add_{product_id}_{weight}"
)
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"product_{product_id}"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"🧮 Расчёт заказа\n\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €",
        reply_markup=keyboard
    )

    await callback.answer()

@dp.callback_query(F.data.startswith("cart_add_"))
async def add_to_cart(callback: types.CallbackQuery):
    _, _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight
        )
        VALUES (%s, %s, %s)
    """, (
        callback.from_user.id,
        product_id,
        weight
    ))

    conn.commit()
    conn.close()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 Корзина",
                    callback_data="cart"
                )
            ]
        ]
    )

    await callback.message.answer(
        "🛒 Товар добавлен в корзину.\n\n"
        "Можешь выбрать ещё товары или открыть корзину.",
        reply_markup=keyboard
    )

    await callback.answer()

@dp.callback_query(F.data.startswith("order_"))
async def create_order(callback: types.CallbackQuery):

    _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    products = get_products()
    config = load_json("config.json")

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer("❌ Товар не найден.")
        await callback.answer()
        return

    price = product["price_per_kg"] * weight / 1000
    order_id = callback.from_user.id + int(asyncio.get_event_loop().time())

    username = callback.from_user.username
    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    order_text = (
        f"🧾 Заказ #{order_id}\n\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €\n\n"
        f"Покупатель: {user_text}"
    )

    await callback.message.answer(
        f"✅ Заказ оформлен!\n\n"
        f"Номер заказа: #{order_id}\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €\n\n"
        f"💳 Следующий шаг: оплата.\n"
        f"Отправьте номер заказа продавцу."
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=f"📦 Новый заказ!\n\n{order_text}"
    )

    await callback.answer()
@dp.callback_query(F.data == "support")
async def support(callback: types.CallbackQuery):

    config = load_json("config.json")

    await callback.message.answer(
        f"🛟 Поддержка: {config['support_username']}"
    )

    await callback.answer()
@dp.callback_query(F.data == "cart")
async def show_cart(callback: types.CallbackQuery):

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT product_id, weight
        FROM cart_items
        WHERE telegram_id = %s
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()
    conn.close()

    if not cart_items:
        await callback.message.answer(
            "🛒 Корзина пустая."
        )
        await callback.answer()
        return

    products = get_products()

    text = "🛒 Ваша корзина:\n\n"
    total = 0

    for product_id, weight in cart_items:

        product = next(
            (p for p in products if p["id"] == product_id),
            None
        )

        if not product:
            continue

        price = product["price_per_kg"] * weight / 1000
        total += price

        text += (
            f"• {product['name']}\n"
            f"  Вес: {weight} г\n"
            f"  Сумма: {price:.2f} €\n\n"
        )

    text += f"💰 Общая сумма: {total:.2f} €"

    keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Оформить заказ",
                callback_data="checkout"
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Очистить корзину",
                callback_data="clear_cart"
            )
        ],
        [
    InlineKeyboardButton(
        text="🏠 Главное меню",
        callback_data="back_to_menu"
    )
]
    ]
)

    await callback.message.answer(
    text,
    reply_markup=keyboard
)

    await callback.answer()
@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: types.CallbackQuery):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM cart_items WHERE telegram_id = %s",
        (callback.from_user.id,)
    )

    conn.commit()
    conn.close()

    await callback.message.answer(
        "🗑 Корзина очищена.",
        reply_markup=main_menu()
    )

    await callback.answer()


@dp.message(Command("start"))
async def start(message: types.Message):
    # Reset checkout state if user is in pending orders
    user_id = message.from_user.id
    if user_id in pending_orders:
        del pending_orders[user_id]
    
    save_client(message.from_user)
    
    config = load_json("config.json")

    await message.answer(
        f"👋 Добро пожаловать в {config['brand_name']}!\n\n"
        f"Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )


@dp.message(Command("orders"))
async def show_orders(message: types.Message):
    config = load_json("config.json")

    if message.from_user.id != config["admin_id"]:
        await message.answer("⛔️ У вас нет доступа.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT order_id, telegram_id, username, phone, address, total, status, payment_method
        FROM orders
        ORDER BY id DESC
        LIMIT 20
    """)

    orders = cursor.fetchall()
    conn.close()

    if not orders:
        await message.answer("📭 Заказов пока нет.")
        return

    text = "📦 Последние заказы:\n\n"

    for order_id, telegram_id, username, phone, address, total, status, payment_method in orders:

        text += (
            f"🧾 Заказ #{order_id}\n"
            f"👤 Клиент: @{username if username else 'без username'}\n"
            f"🆔 ID: {telegram_id}\n"
            f"📞 Телефон: {phone}\n"
            f"🏠 Адрес: {address}\n"
            f"💰 Сумма: {total:.2f} €\n"
            f"📌 Статус: {status}\n"
            f"💳 Оплата: {payment_method if payment_method else 'не выбрана'}\n\n"
        )

    await message.answer(text)


@dp.message(Command("clients"))
async def show_clients(message: types.Message):
    config = load_json("config.json")

    if message.from_user.id != config["admin_id"]:
        await message.answer("⛔️ У вас нет доступа.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            c.telegram_id,
            c.username,
            c.first_name,
            c.phone,
            c.address,
            COUNT(o.id) as order_count,
            COALESCE(SUM(o.total), 0) as total_spent,
            MAX(o.id) as last_order_id,
            (SELECT status FROM orders WHERE telegram_id = c.telegram_id ORDER BY id DESC LIMIT 1) as last_order_status
        FROM clients c
        LEFT JOIN orders o ON c.telegram_id = o.telegram_id
        GROUP BY c.id, c.telegram_id, c.username, c.first_name, c.phone, c.address
        ORDER BY c.id DESC
        LIMIT 20
    """)

    clients = cursor.fetchall()
    conn.close()

    if not clients:
        await message.answer("📭 Клиентов пока нет.")
        return

    text = "👥 База клиентов:\n\n"

    for telegram_id, username, first_name, phone, address, order_count, total_spent, last_order_id, last_order_status in clients:
        user_display = f"@{username}" if username else "без username"

        text += (
            f"👤 {user_display}\n"
            f"📝 Имя: {first_name if first_name else 'не указано'}\n"
        )
        
        if phone:
            text += f"📞 Телефон: {phone}\n"
        if address:
            text += f"🏠 Адрес: {address}\n"
        
        text += f"🆔 ID: {telegram_id}\n"
        text += f"📦 Заказов: {order_count}\n"
        
        if order_count > 0:
            text += f"💰 Потрачено: {total_spent:.2f} €\n"
            if last_order_status:
                text += f"📌 Последний заказ: #{last_order_id} ({last_order_status})\n"
        else:
            text += "📦 Заказов пока нет\n"
        
        text += "\n"

    await message.answer(text)


@dp.message()
async def handle_order_data(message: types.Message):
    user_id = message.from_user.id

    if user_id not in pending_orders:
        return

    # Don't save commands as phone or address
    if message.text and message.text.startswith("/"):
        return

    step = pending_orders[user_id]["step"]

    if step == "phone":
        pending_orders[user_id]["phone"] = message.text
        pending_orders[user_id]["step"] = "address"

        await message.answer(
            "🏠 Теперь введите адрес доставки:"
        )
        return

    if step == "address":
        pending_orders[user_id]["address"] = message.text

        cart_items = pending_orders[user_id]["cart_items"]
        phone = pending_orders[user_id]["phone"]
        address = pending_orders[user_id]["address"]

        products = get_products()
        config = load_json("config.json")

        order_id = user_id + int(asyncio.get_event_loop().time())

        username = message.from_user.username
        if username:
            user_text = f"@{username}"
        else:
            user_text = f"ID: {user_id}"

        order_text = f"🧾 Заказ #{order_id}\n\n"
        total = 0

        for product_id, weight in cart_items:
            product = next(
                (p for p in products if p["id"] == product_id),
                None
            )

            if not product:
                continue

            price = product["price_per_kg"] * weight / 1000
            total += price

            order_text += (
                f"• {product['name']}\n"
                f"  Вес: {weight} г\n"
                f"  Сумма: {price:.2f} €\n\n"
            )

        order_text += (
            f"💰 Итого: {total:.2f} €\n\n"
            f"👤 Покупатель: {user_text}\n"
            f"📞 Телефон: {phone}\n"
            f"🏠 Адрес: {address}"
        )

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        # Update or insert client contact info
        cursor.execute("""
            INSERT INTO clients (
                telegram_id,
                username,
                first_name,
                phone,
                address
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                phone = EXCLUDED.phone,
                address = EXCLUDED.address
        """, (
            user_id,
            message.from_user.username,
            message.from_user.first_name,
            phone,
            address
        ))
        
        # Save order to database
        cursor.execute("""
            INSERT INTO orders (
                order_id,
                telegram_id,
                username,
                phone,
                address,
                total,
                status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            order_id,
            user_id,
            message.from_user.username,
            phone,
            address,
            total,
            "pending"
        ))
        
        # Delete cart items
        cursor.execute(
            "DELETE FROM cart_items WHERE telegram_id = %s",
            (user_id,)
        )
        conn.commit()
        conn.close()

        # Send confirmation to customer
        await message.answer(
            f"✅ Заказ оформлен!\n\n"
            f"Номер заказа: #{order_id}\n"
            f"Сумма: {total:.2f} €\n\n"
            f"💳 Выберите способ оплаты:",
            reply_markup=payment_menu()
        )

        # Send notification to admin
        await bot.send_message(
            chat_id=config["admin_id"],
            text=f"📦 Новый заказ!\n\n{order_text}"
        )
        
        # Clear pending order state
        del pending_orders[user_id]



@dp.callback_query(F.data == "checkout")
async def checkout(callback: types.CallbackQuery):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT product_id, weight
        FROM cart_items
        WHERE telegram_id = %s
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()

    cursor.execute(
        "SELECT phone, address FROM clients WHERE telegram_id = %s",
        (callback.from_user.id,)
    )
    client_contact = cursor.fetchone()
    conn.close()

    if not cart_items:
        await callback.message.answer("🛒 Корзина пустая.")
        await callback.answer()
        return

    products = get_products()
    config = load_json("config.json")
    minimum_order_amount = config.get("minimum_order_amount", 20)
    
    total = 0
    for product_id, weight in cart_items:
        product = next((p for p in products if p["id"] == product_id), None)
        if product:
            total += product["price_per_kg"] * weight / 1000
    
    if total < minimum_order_amount:
        missing = minimum_order_amount - total
        await callback.message.answer(
            f"🚚 Минимальная сумма заказа для доставки: {minimum_order_amount:.2f} €\n"
            f"Добавьте товаров ещё на {missing:.2f} €."
        )
        await callback.answer()
        return

    if client_contact and client_contact[0] and client_contact[1]:
        phone, address = client_contact
        pending_orders[callback.from_user.id] = {
            "cart_items": cart_items,
            "step": "use_saved",
            "phone": phone,
            "address": address
        }

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Использовать сохранённые данные",
                        callback_data="use_saved_data"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Ввести новые данные",
                        callback_data="enter_new_data"
                    )
                ]
            ]
        )

        await callback.message.answer(
            f"Мы нашли сохранённые контактные данные:\n📞 {phone}\n🏠 {address}\n\n"
            "Использовать их для оформления заказа?",
            reply_markup=keyboard
        )
        await callback.answer()
        return

    pending_orders[callback.from_user.id] = {
        "cart_items": cart_items,
        "step": "phone"
    }

    await callback.message.answer(
        "📞 Введите ваш номер телефона для связи:"
    )

    await callback.answer()


@dp.callback_query(F.data == "use_saved_data")
async def use_saved_data(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_orders:
        await callback.answer()
        return

    if pending_orders[user_id].get("step") != "use_saved":
        await callback.answer()
        return

    cart_items = pending_orders[user_id]["cart_items"]
    phone = pending_orders[user_id]["phone"]
    address = pending_orders[user_id]["address"]

    products = get_products()
    config = load_json("config.json")

    order_id = user_id + int(asyncio.get_event_loop().time())

    username = callback.from_user.username
    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {user_id}"

    order_text = f"🧾 Заказ #{order_id}\n\n"
    total = 0

    for product_id, weight in cart_items:
        product = next(
            (p for p in products if p["id"] == product_id),
            None
        )

        if not product:
            continue

        price = product["price_per_kg"] * weight / 1000
        total += price

        order_text += (
            f"• {product['name']}\n"
            f"  Вес: {weight} г\n"
            f"  Сумма: {price:.2f} €\n\n"
        )

    order_text += (
        f"💰 Итого: {total:.2f} €\n\n"
        f"👤 Покупатель: {user_text}\n"
        f"📞 Телефон: {phone}\n"
        f"🏠 Адрес: {address}"
    )

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO orders (
            order_id,
            telegram_id,
            username,
            phone,
            address,
            total,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        order_id,
        user_id,
        callback.from_user.username,
        phone,
        address,
        total,
        "pending"
    ))
    cursor.execute(
        "DELETE FROM cart_items WHERE telegram_id = %s",
        (user_id,)
    )
    conn.commit()
    conn.close()

    await callback.message.answer(
        f"✅ Заказ оформлен!\n\n"
        f"Номер заказа: #{order_id}\n"
        f"Сумма: {total:.2f} €\n\n"
        f"💳 Выберите способ оплаты:",
        reply_markup=payment_menu()
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=f"📦 Новый заказ!\n\n{order_text}"
    )

    del pending_orders[user_id]
    await callback.answer()


@dp.callback_query(F.data == "enter_new_data")
async def enter_new_data(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_orders:
        await callback.answer()
        return

    pending_orders[user_id]["step"] = "phone"

    await callback.message.answer(
        "📞 Введите ваш номер телефона для связи:"
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_iban")
async def pay_iban(callback: types.CallbackQuery):
    config = load_json("config.json")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        ("IBAN", "awaiting_payment", callback.from_user.id)
    )

    conn.commit()
    conn.close()

    paid_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    callback_data="payment_done"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"🏦 Оплата через IBAN / Bank Transfer\n\n"
        f"IBAN: {config['iban']}\n"
        f"Получатель: {config['receiver_name']}\n\n"
        f"В назначении платежа укажите номер заказа.\n"
        f"После оплаты нажмите кнопку ниже.",
        reply_markup=paid_keyboard
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_paypal")
async def pay_paypal(callback: types.CallbackQuery):
    config = load_json("config.json")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        ("PayPal", "awaiting_payment", callback.from_user.id)
    )

    conn.commit()
    conn.close()

    paid_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    callback_data="payment_done"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"💬 Оплата через PayPal\n\n"
        f"PayPal: {config['paypal']}\n\n"
        f"В комментарии укажите номер заказа.\n"
        f"После оплаты нажмите кнопку ниже.",
        reply_markup=paid_keyboard
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_cash")
async def pay_cash(callback: types.CallbackQuery):
    config = load_json("config.json")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        ("Cash", "cash_on_delivery", callback.from_user.id)
    )

    conn.commit()
    conn.close()

    username = callback.from_user.username

    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await callback.message.answer(
        "💵 Вы выбрали оплату наличкой при встрече.\n\n"
        "Продавец свяжется с вами для подтверждения заказа."
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💵 Клиент выбрал оплату наличкой.\n\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.answer(
        "Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )

    await callback.answer()

def payment_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏦 Оплата IBAN",
                    callback_data="pay_iban"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💵 Оплата наличкой",
                    callback_data="pay_cash"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🅿️ PayPal",
                    callback_data="pay_paypal"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )
@dp.callback_query(F.data == "payment_done")
async def payment_done(callback: types.CallbackQuery):
    config = load_json("config.json")

    username = callback.from_user.username

    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💰 Клиент сообщил об оплате.\n\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.message.answer(
        "✅ Спасибо! Продавец получил уведомление об оплате.\n\n"
        "Ожидайте подтверждения заказа."
    )

    await callback.answer()

@dp.callback_query(F.data == "payment_done")
async def payment_done(callback: types.CallbackQuery):

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET status = %s
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        ("payment_reported", callback.from_user.id)
    )

    conn.commit()
    conn.close()

    config = load_json("config.json")

    username = callback.from_user.username

    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await callback.message.answer(
        "✅ Информация об оплате отправлена продавцу.\n\n"
        "Ожидайте подтверждения заказа."
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💸 Клиент сообщил об оплате.\n\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.answer()


async def main():
    init_db()
    seed_products_from_json()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())