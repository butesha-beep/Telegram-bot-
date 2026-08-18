import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/bot-options-mode")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

import bot


# Scoped via patch.dict (auto-restored) rather than a module-level
# os.environ mutation, which would otherwise leak into other test files
# sharing this process. See tests/test_bot_fixed_mode_display.py for the
# same pattern.
telegram_actions_enabled_for_tests = patch.dict(
    os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}
)


def per_kg_product(product_id, price_per_kg=24.0, stock_grams=1000):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Весовой товар",
        "price_per_kg": price_per_kg,
        "description": "Описание",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": stock_grams,
        "is_out_of_stock": False,
        "pricing_mode": "per_kg",
        "fixed_price": None,
        "sale_unit": None,
        "unit_weight_grams": None,
        "stock_quantity": None,
    }


def options_product(product_id, category_id=1):
    return {
        "id": product_id,
        "category_id": category_id,
        "name": "Вариативный товар",
        "price_per_kg": 0.0,
        "description": "Описание",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": 0,
        "is_out_of_stock": False,
        "pricing_mode": "options",
        "fixed_price": None,
        "sale_unit": None,
        "unit_weight_grams": None,
        "stock_quantity": None,
    }


def _fake_message():
    return SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())


def _fake_callback(data, telegram_id):
    return SimpleNamespace(
        data=data,
        message=_fake_message(),
        from_user=SimpleNamespace(id=telegram_id, username="tester"),
        answer=AsyncMock(),
    )


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        pass

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self._cursor = FakeCursor(rows)

    def cursor(self):
        return self._cursor

    def close(self):
        pass


@telegram_actions_enabled_for_tests
class RenderProductOptionsModeFailClosedTests(unittest.IsolatedAsyncioTestCase):
    """Requirements 7/9: an options-mode product with no purchasable options
    must fail closed (no gram buttons, no price_per_kg, no €0.00 order), and
    must never show the misleading 0 €/kg caption."""

    async def test_zero_options_rows_shows_unavailable_not_gram_buttons(self):
        message = _fake_message()
        product = options_product(20)

        with patch.object(bot, "get_products", return_value=[product]), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot.psycopg2, "connect", return_value=FakeConnection([])), \
             patch.object(bot, "get_alternative_products", return_value=[]):
            await bot.render_product(message, 20, 555)

        text = message.answer.await_args.args[0]
        self.assertIn(bot.OUT_OF_STOCK_TEXT, text)
        self.assertNotIn("€/кг", text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
        for weight_label in ("50 г", "100 г", "200 г", "500 г"):
            self.assertNotIn(weight_label, button_texts)

    async def test_all_options_out_of_stock_shows_unavailable_not_gram_buttons(self):
        message = _fake_message()
        product = options_product(20)
        # (id, label, weight, price, stock_quantity, is_out_of_stock)
        rows = [
            (1, "Малая", 100, 5.0, 0, False),      # zero stock
            (2, "Большая", 300, 12.0, None, False),  # untracked = unavailable
            (3, "Средняя", 200, 8.0, 5, True),      # explicit flag wins
        ]

        with patch.object(bot, "get_products", return_value=[product]), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot.psycopg2, "connect", return_value=FakeConnection(rows)), \
             patch.object(bot, "get_alternative_products", return_value=[]):
            await bot.render_product(message, 20, 555)

        text = message.answer.await_args.args[0]
        self.assertIn(bot.OUT_OF_STOCK_TEXT, text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [
            b.callback_data
            for row in keyboard.inline_keyboard
            for b in row
            if b.callback_data
        ]
        self.assertFalse(any(cb.startswith("option_") for cb in callbacks))
        self.assertFalse(any(cb.startswith("weight_") for cb in callbacks))

    async def test_available_options_are_shown_without_price_per_kg_caption(self):
        message = _fake_message()
        product = options_product(20)
        rows = [
            (1, "Малая", 100, 5.0, 3, False),
            (2, "Большая", 300, 12.0, 0, False),  # excluded: zero stock
        ]

        with patch.object(bot, "get_products", return_value=[product]), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot.psycopg2, "connect", return_value=FakeConnection(rows)):
            await bot.render_product(message, 20, 555)

        text = message.answer.await_args.args[0]
        self.assertIn("Выберите вариант", text)
        self.assertNotIn("€/кг", text)
        self.assertNotIn("0.0 €", text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("option_1", callbacks)
        self.assertNotIn("option_2", callbacks)

    async def test_available_options_never_show_pending_weighing_text(self):
        # An options-mode option with no weight is just "not applicable",
        # never "will be weighed after order" (that is per_kg-only).
        message = _fake_message()
        product = options_product(20)
        rows = [(1, "Комплект", None, 9.5, 4, False)]

        with patch.object(bot, "get_products", return_value=[product]), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot.psycopg2, "connect", return_value=FakeConnection(rows)):
            await bot.render_product(message, 20, 555)

        text = message.answer.await_args.args[0]
        self.assertNotIn("взвеш", text.lower())

    async def test_per_kg_product_still_shows_price_per_kg_caption(self):
        message = _fake_message()
        product = per_kg_product(1, price_per_kg=24.0)

        with patch.object(bot, "get_products", return_value=[product]), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot.psycopg2, "connect", return_value=FakeConnection([])):
            await bot.render_product(message, 1, 555)

        text = message.answer.await_args.args[0]
        self.assertIn("24.0 €/кг", text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        button_texts = [b.text for row in keyboard.inline_keyboard for b in row]
        self.assertIn("100 г", button_texts)


@telegram_actions_enabled_for_tests
class OptionStockSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 8: an inactive or zero-stock option cannot be selected,
    previewed, or added to cart in options mode; per_kg's existing
    variable-weight-option overlay is unaffected."""

    async def test_choose_option_rejects_out_of_stock_options_mode_option(self):
        callback = _fake_callback("option_5", telegram_id=701)
        # (product_id, label, weight, price, stock_quantity, is_out_of_stock)
        row = (20, "Малая", 100, 5.0, 0, False)
        product = options_product(20)

        with patch.object(bot.psycopg2, "connect", return_value=FakeConnection([row])), \
             patch.object(bot, "get_products", return_value=[product]):
            await bot.choose_option(callback)

        text = callback.message.answer.await_args.args[0]
        self.assertEqual(text, bot.OUT_OF_STOCK_TEXT)

    async def test_choose_option_allows_available_options_mode_option(self):
        callback = _fake_callback("option_5", telegram_id=702)
        row = (20, "Малая", 100, 5.0, 3, False)
        product = options_product(20)

        with patch.object(bot.psycopg2, "connect", return_value=FakeConnection([row])), \
             patch.object(bot, "get_products", return_value=[product]):
            await bot.choose_option(callback)

        text = callback.message.answer.await_args.args[0]
        self.assertIn("5.00 €", text)
        self.assertNotIn(bot.OUT_OF_STOCK_TEXT, text)

    async def test_choose_option_untracked_stock_on_per_kg_option_is_unaffected(self):
        # Variable-weight fish overlay on a per_kg product: option-level
        # stock_quantity is not tracked there and must not block selection.
        callback = _fake_callback("option_9", telegram_id=703)
        row = (1, "Средняя рыба 200-300г", None, 15.0, None, False)
        product = per_kg_product(1)

        with patch.object(bot.psycopg2, "connect", return_value=FakeConnection([row])), \
             patch.object(bot, "get_products", return_value=[product]):
            await bot.choose_option(callback)

        text = callback.message.answer.await_args.args[0]
        self.assertNotEqual(text, bot.OUT_OF_STOCK_TEXT)

    async def test_add_option_to_cart_rejects_out_of_stock_option_before_insert(self):
        callback = _fake_callback("cart_add_option_5", telegram_id=704)
        # (product_id, weight, price, label, category_id, stock_grams,
        #  is_out_of_stock, pricing_mode, option_stock_quantity, option_is_out_of_stock)
        row = (20, 100, 5.0, "Малая", 1, 0, False, "options", 0, False)
        cursor = FakeCursor([row])
        connection = FakeConnection([row])
        connection._cursor = cursor

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.add_option_to_cart(callback)

        text = callback.message.answer.await_args.args[0]
        self.assertEqual(text, bot.OUT_OF_STOCK_TEXT)

    async def test_cart_plus_option_rejects_out_of_stock_option(self):
        callback = _fake_callback("cart_plus_option_5", telegram_id=705)
        count_cursor_row = (0,)
        option_row = (20, 100, 1, 0, False, "options", 0, False)

        class TwoStepCursor:
            def __init__(self):
                self.calls = 0

            def execute(self, query, params=None):
                pass

            def fetchone(self):
                self.calls += 1
                if self.calls == 1:
                    return count_cursor_row
                return option_row

            def close(self):
                pass

        cursor = TwoStepCursor()
        connection = SimpleNamespace(cursor=lambda: cursor, close=lambda: None)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.cart_plus_option(callback)

        text = callback.message.answer.await_args.args[0]
        self.assertEqual(text, bot.OUT_OF_STOCK_TEXT)


if __name__ == "__main__":
    unittest.main()
