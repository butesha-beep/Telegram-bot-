import asyncio
import json
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
                    text="✅ Оформить заказ",
                    callback_data=f"order_{product_id}_{weight}"
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


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):

    await callback.message.answer(
        "Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )

    await callback.answer()


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())