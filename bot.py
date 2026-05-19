import asyncio
import json
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()


def load_json(filename):
    with open(filename, "r", encoding="utf-8") as file:
        return json.load(file)


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
        InlineKeyboardButton(text="🛟 Поддержка", callback_data="support")
    ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@dp.message(Command("start"))
async def start(message: types.Message):
    config = load_json("config.json")

    await message.answer(
        f"👋 Добро пожаловать в {config['brand_name']}!\n\n"
        "Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )


@dp.callback_query(F.data.startswith("category_"))
async def show_category(callback: types.CallbackQuery):
    category_id = int(callback.data.split("_")[1])
    products = load_json("products.json")

    category_products = [
        product for product in products
        if product["category_id"] == category_id and product["available"] is True
    ]

    if not category_products:
        await callback.message.answer("В этой категории пока нет товаров.")
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
        InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")
    ])

    await callback.message.edit_text(
        "Выберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("product_"))
async def show_product(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_")[1])
    products = load_json("products.json")

    product = next((p for p in products if p["id"] == product_id), None)

    if not product:
        await callback.message.answer("Товар не найден.")
        await callback.answer()
        return

    price_100g = product["price_per_kg"] / 10

    text = (
        f"🛒 {product['name']}\n\n"
        f"{product['description']}\n\n"
        f"💶 Цена: {product['price_per_kg']} €/кг\n"
        f"⚖️ 100 г: {price_100g:.2f} €"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="50 г", callback_data=f"weight_{product_id}_50"),
            InlineKeyboardButton(text="100 г", callback_data=f"weight_{product_id}_100"),
        ],
        [
            InlineKeyboardButton(text="200 г", callback_data=f"weight_{product_id}_200"),
            InlineKeyboardButton(text="500 г", callback_data=f"weight_{product_id}_500"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"category_{product['category_id']}")
        ]
    ])

    if product.get("photo"):
        await callback.message.answer_photo(
            photo=product["photo"],
            caption=text,
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(text, reply_markup=keyboard)

    await callback.answer()


@dp.callback_query(F.data.startswith("weight_"))
async def choose_weight(callback: types.CallbackQuery):
    _, product_id, weight = callback.data.split("_")
    product_id = int(product_id)
    weight = int(weight)

    products = load_json("products.json")
    product = next((p for p in products if p["id"] == product_id), None)

    if not product:
        await callback.message.
 answer("Товар не найден.")
        await callback.answer()
        return

    price = product["price_per_kg"] * weight / 1000

    await callback.message.answer(
        f"✅ Вы выбрали:\n\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €\n\n"
        f"Следующий шаг: оформление заказа."
    )

    await callback.answer()


@dp.callback_query(F.data == "support")
async def support(callback: types.CallbackQuery):
    config = load_json("config.json")
    await callback.message.answer(f"🛟 Поддержка: {config['support_username']}")
    await callback.answer()


@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Выберите категорию товара ниже:",
        reply_markup=main_menu()
    )
    await callback.answer()


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())