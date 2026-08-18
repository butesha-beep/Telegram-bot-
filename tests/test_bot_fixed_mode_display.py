import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/bot-fixed-mode")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

import bot


# The handlers under test are wrapped in @telegram_action_boundary, which
# no-ops unless this flag is enabled. It only gates the boundary itself
# (already covered by tests/test_preview_foundation.py), not the pricing
# logic this file verifies. Scoped via patch.dict (auto-restored) rather
# than a module-level os.environ mutation, which would otherwise leak into
# other test files sharing this process and change their behavior.
telegram_actions_enabled_for_tests = patch.dict(
    os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}
)


def per_kg_product(product_id, price_per_kg, stock_grams=1000, is_out_of_stock=False):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Товар",
        "price_per_kg": price_per_kg,
        "description": "",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock,
        "pricing_mode": "per_kg",
        "fixed_price": None,
        "sale_unit": None,
        "unit_weight_grams": None,
        "stock_quantity": None,
    }


def fixed_product(
    product_id,
    fixed_price,
    sale_unit="шт",
    stock_quantity=10,
    is_out_of_stock=False,
    price_per_kg=0.0,
):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Штучный товар",
        "price_per_kg": price_per_kg,
        "description": "",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": 0,
        "is_out_of_stock": is_out_of_stock,
        "pricing_mode": "fixed",
        "fixed_price": fixed_price,
        "sale_unit": sale_unit,
        "unit_weight_grams": None,
        "stock_quantity": stock_quantity,
    }


def _fake_message():
    return SimpleNamespace(answer=AsyncMock(), answer_photo=AsyncMock())


def _fake_callback(data, telegram_id=555):
    return SimpleNamespace(
        data=data,
        message=_fake_message(),
        from_user=SimpleNamespace(id=telegram_id, username="tester"),
        answer=AsyncMock(),
    )


class SequencedCursor:
    def __init__(self, fetchone_values=()):
        self.fetchone_values = list(fetchone_values)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def close(self):
        pass


class SequencedConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class RenderFixedProductDisplayTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 1/2: a fixed product must display fixed_price (not
    price_per_kg) and must not offer gram/weight buttons."""

    async def test_displays_fixed_price_and_sale_unit_not_price_per_kg(self):
        message = _fake_message()
        product = fixed_product(10, fixed_price=6.0, sale_unit="шт", price_per_kg=999.0)

        await bot._render_fixed_product(message, product, 10, "🛒")

        text = message.answer.await_args.args[0]
        self.assertIn("6.00 € / шт", text)
        self.assertNotIn("999.0", text)
        self.assertNotIn("€/кг", text)

    async def test_no_gram_weight_buttons_are_offered(self):
        message = _fake_message()
        product = fixed_product(10, fixed_price=6.0)

        await bot._render_fixed_product(message, product, 10, "🛒")

        keyboard = message.answer.await_args.kwargs["reply_markup"]
        button_texts = [
            button.text
            for row in keyboard.inline_keyboard
            for button in row
        ]
        for weight_label in ("50 г", "100 г", "200 г", "500 г"):
            self.assertNotIn(weight_label, button_texts)
        self.assertIn("🛒 Добавить в корзину", button_texts)

    async def test_add_to_cart_button_uses_fixed_callback_prefix(self):
        message = _fake_message()
        product = fixed_product(10, fixed_price=6.0)

        await bot._render_fixed_product(message, product, 10, "🛒")

        keyboard = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("fixed_10", callbacks)

    async def test_out_of_stock_fixed_product_shows_unavailable_text_not_buy_button(self):
        message = _fake_message()
        product = fixed_product(10, fixed_price=6.0, stock_quantity=0)

        with patch.object(bot, "get_alternative_products", return_value=[]):
            await bot._render_fixed_product(message, product, 10, "🛒")

        text = message.answer.await_args.args[0]
        self.assertIn(bot.OUT_OF_STOCK_TEXT, text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertNotIn("fixed_10", callbacks)


@telegram_actions_enabled_for_tests
class RenderProductDispatchesToFixedBranchTests(unittest.IsolatedAsyncioTestCase):
    """Proves render_product() actually routes pricing_mode='fixed' products
    to the new branch, and leaves per_kg products on the existing path."""

    async def test_fixed_product_is_routed_to_fixed_renderer(self):
        message = _fake_message()
        product = fixed_product(10, fixed_price=6.0)

        with patch.object(bot, "get_products", return_value=[product]), patch.object(
            bot, "log_customer_event"
        ):
            await bot.render_product(message, 10, 555)

        text = message.answer.await_args.args[0]
        self.assertIn("6.00 € / шт", text)

    async def test_per_kg_product_still_uses_weight_button_path(self):
        message = _fake_message()
        product = per_kg_product(1, price_per_kg=24.0)

        with patch.object(bot, "get_products", return_value=[product]), patch.object(
            bot, "log_customer_event"
        ):
            await bot.render_product(message, 1, 555)

        text = message.answer.await_args.args[0]
        self.assertIn("24.0 €/кг", text)
        keyboard = message.answer.await_args.kwargs["reply_markup"]
        button_texts = [
            button.text
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("100 г", button_texts)


@telegram_actions_enabled_for_tests
class AddFixedToCartGuardTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 8: an out-of-stock fixed product cannot be added to the
    cart through the normal add-to-cart callback."""

    async def test_out_of_stock_by_quantity_is_rejected_before_any_insert(self):
        cursor = SequencedCursor(fetchone_values=[(1, "fixed", 6.0, 0, False)])
        connection = SequencedConnection(cursor)
        callback = _fake_callback("cart_add_fixed_10", telegram_id=601)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.add_fixed_to_cart(callback)

        callback.message.answer.assert_awaited_once_with(
            bot.OUT_OF_STOCK_TEXT, reply_markup=unittest.mock.ANY
        )
        self.assertFalse(any("INSERT" in query for query, _ in cursor.executed))
        self.assertFalse(connection.committed)

    async def test_missing_fixed_price_is_rejected_before_any_insert(self):
        cursor = SequencedCursor(fetchone_values=[(1, "fixed", None, 5, False)])
        connection = SequencedConnection(cursor)
        callback = _fake_callback("cart_add_fixed_10", telegram_id=602)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.add_fixed_to_cart(callback)

        self.assertFalse(any("INSERT" in query for query, _ in cursor.executed))
        self.assertFalse(connection.committed)

    async def test_non_fixed_product_is_rejected(self):
        cursor = SequencedCursor(fetchone_values=[(1, "per_kg", None, None, False)])
        connection = SequencedConnection(cursor)
        callback = _fake_callback("cart_add_fixed_10", telegram_id=603)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.add_fixed_to_cart(callback)

        self.assertFalse(any("INSERT" in query for query, _ in cursor.executed))

    async def test_in_stock_fixed_product_is_inserted_with_null_weight_and_option(self):
        cursor = SequencedCursor(
            fetchone_values=[(1, "fixed", 6.0, 5, False), (0,)]
        )
        connection = SequencedConnection(cursor)
        callback = _fake_callback("cart_add_fixed_10", telegram_id=604)

        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "get_cart_product_ids", return_value=set()), \
             patch.object(bot, "get_recommended_products", return_value=[]), \
             patch.object(bot, "get_automatic_recommendations", return_value=[]), \
             patch.object(bot, "has_available_promotions", return_value=False), \
             patch.object(bot, "mark_cart_active"), \
             patch.object(bot, "log_customer_event"):
            await bot.add_fixed_to_cart(callback)

        insert_calls = [
            (query, params) for query, params in cursor.executed if "INSERT" in query
        ]
        self.assertEqual(len(insert_calls), 1)
        _, params = insert_calls[0]
        self.assertEqual(params, (604, 10))
        self.assertTrue(connection.committed)
        callback.message.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
