import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/order-creation")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import order_creation
from order_creation import OrderCreationError, insert_order, price_single_line


class ScriptedCursor:
    def __init__(self, fetchone_values=(), fetchall_values=()):
        self.fetchone_values = list(fetchone_values)
        self.fetchall_values = list(fetchall_values)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_values.pop(0)

    def inserts_into(self, table):
        return [
            (query, params)
            for query, params in self.queries
            if f"INSERT INTO {table}" in query
        ]

    def updates_of(self, table):
        return [
            (query, params)
            for query, params in self.queries
            if query.strip().startswith(f"UPDATE {table}")
        ]


def per_kg_product(price_per_kg=24.0):
    return {"pricing_mode": "per_kg", "price_per_kg": price_per_kg, "fixed_price": None}


def fixed_product(fixed_price=9.5):
    return {"pricing_mode": "fixed", "price_per_kg": None, "fixed_price": fixed_price}


def options_product():
    return {"pricing_mode": "options", "price_per_kg": None, "fixed_price": None}


def _line(**overrides):
    line = {
        "product_id": 1,
        "product_name": "Товар",
        "weight": None,
        "option_id": None,
        "price": 1.0,
        "pricing_mode": "fixed",
        "price_per_kg_snapshot": None,
    }
    line.update(overrides)
    return line


class PriceSingleLineTests(unittest.TestCase):
    """Requirements 11/12/13: per_kg/fixed/options pricing rules are
    identical to the Commerce Foundation, in one shared implementation."""

    def test_per_kg_uses_price_per_kg_and_snapshots_it(self):
        price, mode, snapshot = price_single_line(per_kg_product(24.0), 250, None, None)
        self.assertEqual(price, 24.0 * 250 / 1000)
        self.assertEqual(mode, "per_kg")
        self.assertEqual(snapshot, 24.0)

    def test_fixed_uses_fixed_price_ignores_weight_and_option(self):
        price, mode, snapshot = price_single_line(fixed_product(9.5), 999, None, None)
        self.assertEqual(price, 9.5)
        self.assertEqual(mode, "fixed")
        self.assertIsNone(snapshot)

    def test_options_uses_selected_option_price_exactly(self):
        price, mode, snapshot = price_single_line(options_product(), None, 7, 12.5)
        self.assertEqual(price, 12.5)
        self.assertEqual(mode, "options")
        self.assertIsNone(snapshot)

    def test_per_kg_product_priced_via_option_still_snapshots_price_per_kg(self):
        # "Variable weight" per_kg products sold via preset-size options:
        # the price comes from the option, but price_per_kg_snapshot is
        # still captured because the product itself is pricing_mode='per_kg'
        # -- matching bot.py's pre-extraction behavior exactly.
        price, mode, snapshot = price_single_line(per_kg_product(35.0), None, 3, 8.0)
        self.assertEqual(price, 8.0)
        self.assertEqual(mode, "per_kg")
        self.assertEqual(snapshot, 35.0)


class InsertOrderIdTests(unittest.TestCase):
    """Requirement (order number, Checkpoint F #10): when no order_id is
    supplied, orders.id becomes the canonical identifier -- the row is
    inserted first, then order_id is set equal to id so every existing
    order_id-keyed mechanism keeps working unchanged. When a channel
    supplies its own order_id (Telegram), it is preserved exactly."""

    def test_no_order_id_derives_order_id_from_generated_id(self):
        cursor = ScriptedCursor(fetchone_values=[(42,)])
        order_id, total = insert_order(
            cursor, source="website", priced_items=[_line(price=5.0)],
        )
        self.assertEqual(order_id, 42)
        self.assertEqual(total, 5.0)

        orders_insert = cursor.inserts_into("orders")
        self.assertEqual(len(orders_insert), 1)
        self.assertIn("RETURNING id", orders_insert[0][0])
        insert_columns = [
            c.strip()
            for c in orders_insert[0][0].split("(", 1)[1].split(")", 1)[0].split(",")
        ]
        self.assertNotIn("order_id", insert_columns)

        order_id_updates = cursor.updates_of("orders")
        self.assertEqual(len(order_id_updates), 1)
        self.assertIn("order_id = %s", order_id_updates[0][0])
        self.assertEqual(order_id_updates[0][1], (42, 42))

        item_inserts = cursor.inserts_into("order_items")
        self.assertEqual(len(item_inserts), 1)
        self.assertEqual(item_inserts[0][1][0], 42)  # order_id column

    def test_explicit_order_id_is_used_directly_no_returning_or_update(self):
        cursor = ScriptedCursor()
        order_id, _total = insert_order(
            cursor, source="telegram", priced_items=[_line(price=5.0)],
            order_id=999888777,
        )
        self.assertEqual(order_id, 999888777)

        orders_insert = cursor.inserts_into("orders")
        self.assertEqual(len(orders_insert), 1)
        self.assertNotIn("RETURNING", orders_insert[0][0])
        self.assertEqual(orders_insert[0][1][0], 999888777)
        self.assertEqual(cursor.updates_of("orders"), [])


class InsertOrderInitialStateTests(unittest.TestCase):
    """Requirements 17/18: every order created by this core starts
    fulfillment_status='new'; refunded is never an allowed initial
    payment_status."""

    def test_always_writes_fulfillment_status_new(self):
        cursor = ScriptedCursor(fetchone_values=[(1,)])
        insert_order(cursor, source="in_person", priced_items=[_line()])
        query, params = cursor.inserts_into("orders")[0]
        columns = query.split("(", 1)[1].split(")", 1)[0]
        column_list = [c.strip() for c in columns.split(",")]
        fulfillment_index = column_list.index("fulfillment_status")
        self.assertEqual(params[fulfillment_index], "new")

    def test_refunded_is_rejected_at_creation(self):
        cursor = ScriptedCursor()
        with self.assertRaises(OrderCreationError):
            insert_order(
                cursor, source="in_person", priced_items=[_line()],
                payment_status="refunded",
            )
        self.assertEqual(cursor.queries, [])

    def test_payment_reported_is_also_rejected_at_creation(self):
        cursor = ScriptedCursor()
        with self.assertRaises(OrderCreationError):
            insert_order(
                cursor, source="in_person", priced_items=[_line()],
                payment_status="payment_reported",
            )

    def test_unpaid_and_paid_are_both_allowed(self):
        for status in ("unpaid", "paid"):
            with self.subTest(status=status):
                cursor = ScriptedCursor(fetchone_values=[(1,)])
                insert_order(
                    cursor, source="in_person", priced_items=[_line()],
                    payment_status=status,
                )  # must not raise

    def test_empty_priced_items_is_rejected(self):
        cursor = ScriptedCursor()
        with self.assertRaises(OrderCreationError):
            insert_order(cursor, source="in_person", priced_items=[])
        self.assertEqual(cursor.queries, [])


class InsertOrderNoInventoryTests(unittest.TestCase):
    """Requirement 15: order creation never touches inventory -- stock
    only ever moves at fulfillment_status='packed' (admin_app.py,
    unchanged by this checkpoint)."""

    def test_no_inventory_or_product_queries_at_creation(self):
        cursor = ScriptedCursor(fetchone_values=[(1,)])
        insert_order(
            cursor, source="in_person",
            priced_items=[_line(pricing_mode="per_kg", weight=200, price=4.8)],
            payment_status="paid",
        )
        for query, _params in cursor.queries:
            self.assertNotIn("inventory_movements", query)
            self.assertNotIn("UPDATE products", query)
            self.assertNotIn("UPDATE product_options", query)


class InsertOrderTotalsAndFieldsTests(unittest.TestCase):
    """Requirement 14: mixed-mode manual order total is correct; source/
    source_reference/telegram_id=None are written durably."""

    def test_mixed_mode_total_is_sum_of_line_prices(self):
        cursor = ScriptedCursor(fetchone_values=[(1,)])
        lines = [
            _line(pricing_mode="per_kg", weight=200, price=4.8, price_per_kg_snapshot=24.0),
            _line(pricing_mode="fixed", price=9.5),
            _line(pricing_mode="options", option_id=7, price=12.5),
        ]
        order_id, total = insert_order(cursor, source="in_person", priced_items=lines)
        self.assertAlmostEqual(total, 4.8 + 9.5 + 12.5)
        self.assertEqual(len(cursor.inserts_into("order_items")), 3)

    def test_source_and_source_reference_are_written(self):
        cursor = ScriptedCursor(fetchone_values=[(1,)])
        insert_order(
            cursor, source="instagram", source_reference="@dealmarket",
            priced_items=[_line()],
        )
        query, params = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in query.split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(params[columns.index("source")], "instagram")
        self.assertEqual(params[columns.index("source_reference")], "@dealmarket")

    def test_telegram_id_may_be_none(self):
        cursor = ScriptedCursor(fetchone_values=[(1,)])
        insert_order(
            cursor, source="website", priced_items=[_line()], telegram_id=None,
        )
        query, params = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in query.split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertIsNone(params[columns.index("telegram_id")])


class NoDuplicatePricingLogicTests(unittest.TestCase):
    """Requirement 25: bot.py and admin_app.py both import and call the
    exact same price_single_line/insert_order function objects from
    order_creation -- there is structurally only one implementation of
    "how much does this line cost" and "how is an order written", so they
    cannot independently drift apart."""

    def test_bot_and_admin_import_the_same_price_single_line_function(self):
        import bot
        import admin_app
        self.assertIs(bot.price_single_line, order_creation.price_single_line)
        self.assertIs(admin_app.price_single_line, order_creation.price_single_line)

    def test_bot_and_admin_import_the_same_insert_order_function(self):
        import bot
        import admin_app
        self.assertIs(bot.insert_order, order_creation.insert_order)
        self.assertIs(admin_app.insert_order, order_creation.insert_order)


if __name__ == "__main__":
    unittest.main()
