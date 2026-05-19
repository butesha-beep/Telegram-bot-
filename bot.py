import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

TOKEN = "8816083749:AAE70CmQAbBXvmSTzWl60Fod2Jk5w3nKWkQ"

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "Привет! Добро пожаловать в магазин!",
        reply_markup=types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="Прайс")],
                [types.KeyboardButton(text="Сделать заказ")],
                [types.KeyboardButton(text="Контакты")]
            ],
            resize_keyboard=True
        )
    )

@dp.message(lambda m: m.text == "Прайс")
async def price(message: types.Message):
    await message.answer("Наш прайс:\n\nТовар 1 - 10 евро\nТовар 2 - 20 евро")

@dp.message(lambda m: m.text == "Сделать заказ")
async def order(message: types.Message):
    await message.answer("Напиши что хочешь заказать!")

@dp.message(lambda m: m.text == "Контакты")
async def contacts(message: types.Message):
    await message.answer("Контакты: @твой_ник")

async def main():
    await dp.start_polling(bot)

asyncio.run(main())