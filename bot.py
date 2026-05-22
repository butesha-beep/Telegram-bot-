import asyncio
import json
from json.tool import main
import os
import sqlite3

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
DB_NAME = "shop.db"
pending_orders = {}

def load_json(filename):
    with open(filename, "r", encoding="utf-8") as file:
        return json.load(file)
    

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            first_name TEXT
        )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cart_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        product_id INTEGER,
        weight INTEGER
    )
""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            telegram_id INTEGER,
            username TEXT,
            phone TEXT,
            address TEXT,
            total REAL,
            status TEXT
        )
    """)
    try:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN payment_method TEXT"
    )
    except:
        pass

    conn.commit()
    conn.close()


def save_client(user):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO clients (
            telegram_id,
            username,
            first_name
        )
        VALUES (?, ?, ?)
    """, (
        user.id,
        user.username,
        user.first_name
    ))

    conn.commit()
    conn.close()


def main_menu():
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


@dp.message(Command("start"))
async def start(message: types.Message):
    save_client(message.from_user)

    config = load_json("config.json")

    await message.answer(
        f"👋 Добро пожаловать в {config['brand_name']}!\n\n"
        f"Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data.startswith("category_"))
async def show_category(callback: types.CallbackQuery):

    category_id = int(callback.data.split("_")[1])

    products = load_json("products.json")

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

    products = load_json("products.json")

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

    products = load_json("products.json")

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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight
        )
        VALUES (?, ?, ?)
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

    products = load_json("products.json")
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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT product_id, weight
        FROM cart_items
        WHERE telegram_id = ?
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()
    conn.close()

    if not cart_items:
        await callback.message.answer(
            "🛒 Корзина пустая."
        )
        await callback.answer()
        return

    products = load_json("products.json")

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
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM cart_items WHERE telegram_id = ?",
        (callback.from_user.id,)
    )

    conn.commit()
    conn.close()

    await callback.message.answer(
        "🗑 Корзина очищена.",
        reply_markup=main_menu()
    )

    await callback.answer()
@dp.message()
async def handle_order_data(message: types.Message):
    user_id = message.from_user.id

    if user_id not in pending_orders:
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

        products = load_json("products.json")
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

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cart_items WHERE telegram_id = ?",
            (user_id,)
        )
        conn.commit()
        conn.close()

    await message.answer(
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



@dp.callback_query(F.data == "checkout")
async def checkout(callback: types.CallbackQuery):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT product_id, weight
        FROM cart_items
        WHERE telegram_id = ?
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()
    conn.close()

    if not cart_items:
        await callback.message.answer("🛒 Корзина пустая.")
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


@dp.callback_query(F.data == "pay_iban")
async def pay_iban(callback: types.CallbackQuery):
    config = load_json("config.json")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
    """
    UPDATE orders
    SET payment_method = ?
    WHERE id = (
        SELECT id
        FROM orders
        WHERE telegram_id = ?
        ORDER BY id DESC
        LIMIT 1
    )
    """,
    ("IBAN", callback.from_user.id)
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

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET status = ?
        WHERE telegram_id = ?
        ORDER BY id DESC
        LIMIT 1
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())