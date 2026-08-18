import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.filters import ExceptionTypeFilter


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/bot-catalog")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

import bot


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        pass

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self._cursor = FakeCursor(rows)
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


class BrokenCursorConnection:
    """Simulates a connection that succeeds but fails once a cursor is used,
    to prove the failure handling isn't limited to connect() itself."""

    def cursor(self):
        raise RuntimeError("cursor unavailable")

    def close(self):
        pass


PRODUCT_ROWS = [
    (1, 1, "Лосось", 24.0, "Свежий лосось", "https://example.com/salmon.jpg", True, 5000, False,
     "per_kg", None, None, None, None),
    (2, 2, "Говядина", 18.5, "Мраморная говядина", "", True, 0, True,
     "per_kg", None, None, None, None),
]
CATEGORY_ROWS = [(1, "Рыба"), (2, "Мясо")]


class GetProductsFailureDoesNotFallBackToDemoJsonTests(unittest.TestCase):
    def test_db_connection_failure_raises_instead_of_returning_demo_json(self):
        demo_products = bot.load_json("products.json")
        self.assertTrue(
            demo_products,
            "expected the demo catalog fixture to be non-empty for this test "
            "to be meaningful",
        )

        with patch.object(
            bot.psycopg2, "connect", side_effect=RuntimeError("connection refused")
        ):
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_products()

    def test_db_failure_never_reads_the_demo_catalog_file(self):
        with patch.object(
            bot.psycopg2, "connect", side_effect=RuntimeError("connection refused")
        ), patch.object(bot, "load_json") as mock_load_json:
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_products()
            mock_load_json.assert_not_called()

    def test_query_failure_after_connect_also_raises_not_falls_back(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=BrokenCursorConnection()
        ):
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_products()

    def test_raised_error_chains_original_exception_without_exposing_it_by_default(self):
        original = RuntimeError("password authentication failed for user secretdb")

        with patch.object(bot.psycopg2, "connect", side_effect=original):
            try:
                bot.get_products()
                self.fail("expected CatalogUnavailableError")
            except bot.CatalogUnavailableError as raised:
                self.assertIs(raised.__cause__, original)
                self.assertNotIn("secretdb", str(raised))


class GetProductsSuccessPathUnchangedTests(unittest.TestCase):
    def test_successful_query_returns_expected_product_dicts(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=FakeConnection(PRODUCT_ROWS)
        ):
            products = bot.get_products()

        self.assertEqual(len(products), 2)
        self.assertEqual(products[0]["id"], 1)
        self.assertEqual(products[0]["category_id"], 1)
        self.assertEqual(products[0]["price_per_kg"], 24.0)
        self.assertEqual(products[0]["stock_grams"], 5000)
        self.assertFalse(products[0]["is_out_of_stock"])
        self.assertEqual(products[0]["pricing_mode"], "per_kg")
        self.assertIsNone(products[0]["fixed_price"])
        self.assertIsNone(products[0]["sale_unit"])
        self.assertIsNone(products[0]["unit_weight_grams"])
        self.assertIsNone(products[0]["stock_quantity"])
        self.assertEqual(products[1]["id"], 2)
        self.assertTrue(products[1]["is_out_of_stock"])


class GetProductsEmptyResultReturnsEmptyListTests(unittest.TestCase):
    """A successful query with zero rows must return [] and must NEVER read
    products.json. The old demo-catalog fallback for this case is rejected."""

    def test_empty_but_successful_query_returns_empty_list(self):
        with patch.object(bot.psycopg2, "connect", return_value=FakeConnection([])):
            products = bot.get_products()

        self.assertEqual(products, [])

    def test_empty_but_successful_query_never_reads_the_demo_catalog_file(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=FakeConnection([])
        ), patch.object(bot, "load_json") as mock_load_json:
            bot.get_products()
            mock_load_json.assert_not_called()


class GetCategoriesFailureDoesNotFallBackToDemoJsonTests(unittest.TestCase):
    def test_db_connection_failure_raises_instead_of_returning_demo_json(self):
        demo_categories = bot.load_json("categories.json")
        self.assertTrue(
            demo_categories,
            "expected the demo categories fixture to be non-empty for this "
            "test to be meaningful",
        )

        with patch.object(
            bot.psycopg2, "connect", side_effect=RuntimeError("connection refused")
        ):
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_categories()

    def test_db_failure_never_reads_the_demo_categories_file(self):
        with patch.object(
            bot.psycopg2, "connect", side_effect=RuntimeError("connection refused")
        ), patch.object(bot, "load_json") as mock_load_json:
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_categories()
            mock_load_json.assert_not_called()

    def test_query_failure_after_connect_also_raises_not_falls_back(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=BrokenCursorConnection()
        ):
            with self.assertRaises(bot.CatalogUnavailableError):
                bot.get_categories()


class GetCategoriesSuccessPathTests(unittest.TestCase):
    def test_successful_query_returns_expected_category_dicts(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=FakeConnection(CATEGORY_ROWS)
        ):
            categories = bot.get_categories()

        self.assertEqual(
            categories, [{"id": 1, "name": "Рыба"}, {"id": 2, "name": "Мясо"}]
        )


class GetCategoriesEmptyResultReturnsEmptyListTests(unittest.TestCase):
    """Same rule as products: a successful, empty query returns [] and never
    reads categories.json."""

    def test_empty_but_successful_query_returns_empty_list(self):
        with patch.object(bot.psycopg2, "connect", return_value=FakeConnection([])):
            categories = bot.get_categories()

        self.assertEqual(categories, [])

    def test_empty_but_successful_query_never_reads_the_demo_categories_file(self):
        with patch.object(
            bot.psycopg2, "connect", return_value=FakeConnection([])
        ), patch.object(bot, "load_json") as mock_load_json:
            bot.get_categories()
            mock_load_json.assert_not_called()


def _fake_message_update():
    message = SimpleNamespace(answer=AsyncMock())
    update = SimpleNamespace(message=message, callback_query=None)
    return update, message


def _fake_callback_query_update():
    message = SimpleNamespace(answer=AsyncMock())
    callback_query = SimpleNamespace(message=message, answer=AsyncMock())
    update = SimpleNamespace(message=None, callback_query=callback_query)
    return update, message, callback_query


class CatalogUnavailableCustomerMessagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_triggered_failure_gets_generic_unavailable_reply(self):
        update, message = _fake_message_update()
        error = bot.CatalogUnavailableError("Product catalog is unavailable")
        event = SimpleNamespace(update=update, exception=error)

        result = await bot.handle_catalog_unavailable_error(event)

        message.answer.assert_awaited_once_with(bot.CATALOG_UNAVAILABLE_MESSAGE)
        self.assertTrue(result)

    async def test_callback_triggered_failure_replies_and_acknowledges_callback(self):
        update, message, callback_query = _fake_callback_query_update()
        error = bot.CatalogUnavailableError("Category catalog is unavailable")
        event = SimpleNamespace(update=update, exception=error)

        await bot.handle_catalog_unavailable_error(event)

        message.answer.assert_awaited_once_with(bot.CATALOG_UNAVAILABLE_MESSAGE)
        callback_query.answer.assert_awaited_once()

    async def test_raw_db_exception_text_is_never_sent_to_the_customer(self):
        original = RuntimeError(
            "connection to server at secretdb.internal, user=admin failed"
        )
        wrapped = bot.CatalogUnavailableError("Product catalog is unavailable")
        wrapped.__cause__ = original
        update, message = _fake_message_update()
        event = SimpleNamespace(update=update, exception=wrapped)

        await bot.handle_catalog_unavailable_error(event)

        sent_text = message.answer.await_args.args[0]
        self.assertEqual(sent_text, bot.CATALOG_UNAVAILABLE_MESSAGE)
        self.assertNotIn("secretdb", sent_text)
        self.assertNotIn("admin", sent_text)

    async def test_update_with_no_message_or_callback_does_not_raise(self):
        update = SimpleNamespace(message=None, callback_query=None)
        error = bot.CatalogUnavailableError("Product catalog is unavailable")
        event = SimpleNamespace(update=update, exception=error)

        result = await bot.handle_catalog_unavailable_error(event)

        self.assertTrue(result)


class CatalogUnavailableErrorFilterTests(unittest.IsolatedAsyncioTestCase):
    """Proves the centralized handler only ever engages for
    CatalogUnavailableError, so unrelated programming errors are left to
    aiogram's default (existing, already-safe) handling untouched."""

    async def test_filter_matches_catalog_unavailable_error(self):
        filter_instance = ExceptionTypeFilter(bot.CatalogUnavailableError)
        event = SimpleNamespace(
            exception=bot.CatalogUnavailableError("Product catalog is unavailable")
        )

        self.assertTrue(await filter_instance(event))

    async def test_filter_rejects_unrelated_exception_types(self):
        filter_instance = ExceptionTypeFilter(bot.CatalogUnavailableError)
        event = SimpleNamespace(exception=ValueError("unrelated programming error"))

        self.assertFalse(await filter_instance(event))


if __name__ == "__main__":
    unittest.main()
