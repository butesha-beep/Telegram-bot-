import os
import unittest


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/admin-fulfillment")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app


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

    def lock_queries(self):
        return [
            (query, params)
            for query, params in self.queries
            if "FOR UPDATE" in query
        ]


# order_items rows: (product_id, option_id, weight, pricing_mode)
MIXED_ORDER_ITEMS = [
    (1, None, 500, "per_kg"),
    (2, None, None, "fixed"),
    (2, None, None, "fixed"),  # second unit of the same fixed product
    (3, 50, 200, "options"),
]


class SufficientStockDeductionTests(unittest.TestCase):
    """Requirements 4/6/12/13: with sufficient stock everywhere, a mixed
    order deducts each mode correctly, and every lock query used FOR UPDATE
    within the caller's transaction."""

    def test_mixed_order_deducts_all_three_modes_correctly(self):
        cursor = ScriptedCursor(
            fetchone_values=[(5000,), (10,), (5,)],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        affected = admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(set(affected), {1, 2, 3})

        product_updates = cursor.updates_of("products")
        self.assertEqual(product_updates[0][1], (4500, 1))
        self.assertIn("stock_grams = %s", product_updates[0][0])
        self.assertIn("pricing_mode = 'per_kg'", product_updates[0][0])

        fixed_update = product_updates[1]
        self.assertEqual(fixed_update[1], (8, 2))
        self.assertIn("stock_quantity = %s", fixed_update[0])
        self.assertIn("pricing_mode = 'fixed'", fixed_update[0])

        option_updates = cursor.updates_of("product_options")
        self.assertEqual(len(option_updates), 1)
        self.assertEqual(option_updates[0][1], (4, 50))

    def test_movements_record_grams_for_per_kg_and_units_for_fixed_and_options(self):
        cursor = ScriptedCursor(
            fetchone_values=[(5000,), (10,), (5,)],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        admin_app.deduct_order_inventory(cursor, "order-1")

        movements = cursor.inserts_into("inventory_movements")
        self.assertEqual(len(movements), 3)

        # INSERT params order: product_id, order_id, movement_type,
        # quantity_grams, quantity_units, stock_before, stock_after, note
        per_kg_params = movements[0][1]
        self.assertEqual(per_kg_params[0], 1)
        self.assertEqual(per_kg_params[3], -500)
        self.assertIsNone(per_kg_params[4])

        fixed_params = movements[1][1]
        self.assertEqual(fixed_params[0], 2)
        self.assertIsNone(fixed_params[3])
        self.assertEqual(fixed_params[4], -2)

        options_params = movements[2][1]
        self.assertEqual(options_params[0], 3)
        self.assertIsNone(options_params[3])
        self.assertEqual(options_params[4], -1)

    def test_two_fixed_units_of_same_product_are_deducted_in_one_grouped_update(self):
        cursor = ScriptedCursor(
            fetchone_values=[(10,)],
            fetchall_values=[[(2, None, None, "fixed"), (2, None, None, "fixed")]],
        )

        admin_app.deduct_order_inventory(cursor, "order-1")

        product_updates = cursor.updates_of("products")
        self.assertEqual(len(product_updates), 1)
        self.assertEqual(product_updates[0][1], (8, 2))

    def test_pricing_mode_is_read_only_from_the_order_line_snapshot(self):
        cursor = ScriptedCursor(
            fetchone_values=[(3,)],
            fetchall_values=[[(7, None, None, "fixed")]],
        )

        admin_app.deduct_order_inventory(cursor, "order-1")

        queries_text = " ".join(query for query, _ in cursor.queries)
        self.assertNotIn("JOIN products", queries_text)
        product_updates = cursor.updates_of("products")
        self.assertEqual(len(product_updates), 1)
        self.assertIn("pricing_mode = 'fixed'", product_updates[0][0])

    def test_all_lock_queries_use_for_update(self):
        cursor = ScriptedCursor(
            fetchone_values=[(5000,), (10,), (5,)],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        admin_app.deduct_order_inventory(cursor, "order-1")

        lock_queries = cursor.lock_queries()
        # one lock per distinct product/option touched: per_kg product 1,
        # fixed product 2, options option 50.
        self.assertEqual(len(lock_queries), 3)
        self.assertTrue(all("FOR UPDATE" in query for query, _ in lock_queries))


class InsufficientStockPreventsAnyMutationTests(unittest.TestCase):
    """Requirements 1/2/3/4/5: a shortage in any single component blocks
    the whole order atomically -- no stock changed, no movements logged,
    regardless of which component (including the last one checked) is
    short."""

    def test_per_kg_shortage_raises_and_touches_nothing(self):
        cursor = ScriptedCursor(
            fetchone_values=[(100,)],  # only 100g available, 500g required
            fetchall_values=[[(1, None, 500, "per_kg")]],
        )

        with self.assertRaises(admin_app.InsufficientStockError) as ctx:
            admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(ctx.exception.shortages, [("per_kg", 1, 500, 100)])
        self.assertFalse(cursor.updates_of("products"))
        self.assertFalse(cursor.inserts_into("inventory_movements"))

    def test_fixed_shortage_raises_and_touches_nothing(self):
        cursor = ScriptedCursor(
            fetchone_values=[(1,)],  # only 1 unit available, 2 required
            fetchall_values=[[(2, None, None, "fixed"), (2, None, None, "fixed")]],
        )

        with self.assertRaises(admin_app.InsufficientStockError) as ctx:
            admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(ctx.exception.shortages, [("fixed", 2, 2, 1)])
        self.assertFalse(cursor.updates_of("products"))
        self.assertFalse(cursor.inserts_into("inventory_movements"))

    def test_options_shortage_raises_and_touches_nothing(self):
        cursor = ScriptedCursor(
            fetchone_values=[(0,)],  # 0 available, 1 required
            fetchall_values=[[(3, 50, 200, "options")]],
        )

        with self.assertRaises(admin_app.InsufficientStockError) as ctx:
            admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(ctx.exception.shortages, [("options", 50, 1, 0)])
        self.assertFalse(cursor.updates_of("products"))
        self.assertFalse(cursor.updates_of("product_options"))
        self.assertFalse(cursor.inserts_into("inventory_movements"))

    def test_mixed_order_with_last_component_short_leaves_all_stock_unchanged(self):
        # per_kg and fixed are sufficient; options (checked last) is short.
        cursor = ScriptedCursor(
            fetchone_values=[(5000,), (10,), (0,)],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        with self.assertRaises(admin_app.InsufficientStockError) as ctx:
            admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(ctx.exception.shortages, [("options", 50, 1, 0)])
        # every lock was still acquired (validation checks every component)...
        self.assertEqual(len(cursor.lock_queries()), 3)
        # ...but absolutely no mutation or movement happened for any line,
        # including the per_kg and fixed lines that were individually fine.
        self.assertFalse(cursor.updates_of("products"))
        self.assertFalse(cursor.updates_of("product_options"))
        self.assertFalse(cursor.inserts_into("inventory_movements"))

    def test_missing_product_row_counts_as_a_shortage(self):
        cursor = ScriptedCursor(
            fetchone_values=[None],
            fetchall_values=[[(99, None, 100, "per_kg")]],
        )

        with self.assertRaises(admin_app.InsufficientStockError) as ctx:
            admin_app.deduct_order_inventory(cursor, "order-1")

        self.assertEqual(ctx.exception.shortages, [("per_kg", 99, 100, 0)])
        self.assertFalse(cursor.updates_of("products"))


class UpdateOrderFulfillmentStatusInsufficientStockIntegrationTests(unittest.TestCase):
    """Checkpoint D: update_order_fulfillment_status's 'packed' action must
    turn InsufficientStockError into a rollback and a controlled
    admin-facing message, never a raw exception or the generic error page.
    (Migrated from the retired update_order_status/'paid' path -- inventory
    deduction now happens on the 'packed' fulfillment transition instead of
    on payment.)"""

    def test_packed_transition_rolls_back_and_shows_controlled_message(self):
        import asyncio
        from unittest.mock import patch

        class FakeConn:
            def __init__(self, cursor):
                self.cursor_instance = cursor
                self.committed = False
                self.rolled_back = False

            def cursor(self):
                return self.cursor_instance

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                pass

        # (fulfillment_status, inventory_deducted, inventory_restored,
        # telegram_id), then None for the no-pending-weighing check.
        cursor = ScriptedCursor(
            fetchone_values=[("picking", False, False, 123), None]
        )
        connection = FakeConn(cursor)

        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(
                 admin_app,
                 "deduct_order_inventory",
                 side_effect=admin_app.InsufficientStockError(
                     [("fixed", 2, 2, 1)]
                 ),
             ):
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        self.assertIn("Недостаточно товара на складе", response)
        self.assertNotIn("InsufficientStockError", response)
        self.assertNotIn("Traceback", response)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(
            any(
                "UPDATE orders SET inventory_deducted" in query
                or "fulfillment_status = %s" in query
                for query, _ in cursor.queries
            )
        )


class RestoreOrderInventoryUnchangedTests(unittest.TestCase):
    """Requirement 7: cancellation/restoration behavior is explicitly
    unchanged by this checkpoint."""

    def test_mixed_order_restores_all_three_modes_correctly(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                (4500, 5000),
                (8, 10),
                (4, 5),
            ],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        affected = admin_app.restore_order_inventory(cursor, "order-1")

        self.assertEqual(set(affected), {1, 2, 3})

        product_updates = [
            (q, p) for q, p in cursor.queries
            if q.strip().startswith("WITH before_update") and "products" in q
        ]
        self.assertEqual(product_updates[0][1], (1, 500, 1))
        self.assertIn("stock_grams = stock_grams + %s", product_updates[0][0])

        fixed_update = product_updates[1]
        self.assertEqual(fixed_update[1], (2, 2, 2))
        self.assertIn("stock_quantity = COALESCE(stock_quantity, 0) + %s", fixed_update[0])

    def test_movements_record_grams_for_per_kg_and_units_for_fixed_and_options(self):
        cursor = ScriptedCursor(
            fetchone_values=[(4500, 5000), (8, 10), (4, 5)],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )

        admin_app.restore_order_inventory(cursor, "order-1")

        movements = cursor.inserts_into("inventory_movements")
        self.assertEqual(movements[0][1][3], 500)
        self.assertIsNone(movements[0][1][4])
        self.assertIsNone(movements[1][1][3])
        self.assertEqual(movements[1][1][4], 2)
        self.assertIsNone(movements[2][1][3])
        self.assertEqual(movements[2][1][4], 1)


if __name__ == "__main__":
    unittest.main()
