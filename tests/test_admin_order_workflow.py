import asyncio
import os
import re
import unittest
from unittest.mock import Mock, patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/admin-order-workflow")
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


class ScriptedConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# order_items rows: (product_id, option_id, weight, pricing_mode)
MIXED_ORDER_ITEMS = [
    (1, None, 500, "per_kg"),
    (2, None, None, "fixed"),
    (3, 50, 200, "options"),
]


def _payment_row(payment_status="unpaid", telegram_id=555):
    return (payment_status, telegram_id)


def _fulfillment_row(fulfillment_status="new", inventory_deducted=False,
                      inventory_restored=False, telegram_id=555):
    return (fulfillment_status, inventory_deducted, inventory_restored, telegram_id)


class PaymentFulfillmentIndependenceTests(unittest.TestCase):
    """Requirement 1: payment and fulfillment transitions are independent
    -- each route's UPDATE only ever touches its own column."""

    def test_payment_update_never_sets_fulfillment_status_column(self):
        cursor = ScriptedCursor(fetchone_values=[_payment_row()])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_payment_status("order-1", "payment_reported"))

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        self.assertNotIn("fulfillment_status", updates[0][0])

    def test_fulfillment_update_never_sets_payment_status_column(self):
        cursor = ScriptedCursor(fetchone_values=[_fulfillment_row(fulfillment_status="new")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "confirmed"))

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        self.assertNotIn("payment_status", updates[0][0])


class CodCashProgressionTests(unittest.TestCase):
    """Requirement 2: a cash/COD order can progress through fulfillment
    while payment_status remains 'unpaid' -- fulfillment actions must never
    be gated by payment_status."""

    def test_confirmed_transition_succeeds_while_unpaid(self):
        cursor = ScriptedCursor(
            fetchone_values=[_fulfillment_row(fulfillment_status="new")]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "confirmed")
            )

        self.assertIn("обновлён", response)
        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        self.assertIn("fulfillment_status = %s", updates[0][0])
        # Checkpoint E: legacy orders.status is never written, regardless of
        # payment_status -- the bot-side 'cash_on_delivery' value (if any)
        # is left untouched because this column is no longer written at all.
        self.assertIsNone(re.search(r"\bstatus\b", updates[0][0]))
        self.assertEqual(updates[0][1], ("confirmed", "order-1"))

    def test_picking_transition_also_succeeds_while_unpaid(self):
        cursor = ScriptedCursor(
            fetchone_values=[_fulfillment_row(fulfillment_status="confirmed")]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "picking"))

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        # Checkpoint E: fulfillment_status is written; legacy status never is,
        # even though 'picking' used to derive legacy status='preparing'.
        self.assertEqual(updates[0][1], ("picking", "order-1"))


class MarkPaidDoesNotDeductStockTests(unittest.TestCase):
    """Requirement 3: the payment route never touches inventory."""

    def test_mark_paid_does_not_call_deduct_order_inventory(self):
        cursor = ScriptedCursor(fetchone_values=[_payment_row(payment_status="unpaid")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "deduct_order_inventory") as deduct:
            asyncio.run(admin_app.update_order_payment_status("order-1", "paid"))

        deduct.assert_not_called()
        self.assertFalse(cursor.inserts_into("inventory_movements"))
        self.assertFalse(
            any("inventory_deducted" in query for query, _ in cursor.updates_of("orders"))
        )


class PackedDeductsStockTests(unittest.TestCase):
    """Requirement 4: transitioning fulfillment_status to 'packed' DOES
    deduct stock, exactly once."""

    def test_packed_transition_calls_deduct_order_inventory_once(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(fulfillment_status="picking"),
                None,  # no pending weighing
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "deduct_order_inventory", return_value=[]) as deduct:
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        deduct.assert_called_once_with(cursor, "order-1")
        self.assertIn("обновлён", response)
        self.assertTrue(
            any("inventory_deducted = TRUE" in query for query, _ in cursor.updates_of("orders"))
        )
        self.assertTrue(connection.committed)


class PackedWeighingGateTests(unittest.TestCase):
    """Requirement 5: 'packed' fails closed if per_kg weighing is
    incomplete -- no deduction, no partial status update."""

    def test_pending_weighing_blocks_packed(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(fulfillment_status="picking"),
                (1,),  # a pending-weighing row exists
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "deduct_order_inventory") as deduct:
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        self.assertIn("взвес", response.lower())
        deduct.assert_not_called()
        self.assertFalse(cursor.updates_of("orders"))
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)


class MixedModePackedAtomicityTests(unittest.TestCase):
    """Requirement 6: mixed-mode packed deduction remains atomic -- proven
    by wiring the fulfillment route to the real (unmocked)
    deduct_order_inventory, reusing the same fixture shape already
    exhaustively covered in tests/test_admin_fulfillment.py."""

    def test_packed_transition_deducts_all_three_modes_via_real_function(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(fulfillment_status="picking"),
                None,  # no pending weighing
                (5000,), (10,), (5,),  # per_kg / fixed / options stock locks
            ],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "packed"))

        movements = cursor.inserts_into("inventory_movements")
        self.assertEqual(len(movements), 3)
        product_updates = cursor.updates_of("products")
        # per_kg deduction + fixed deduction + the is_out_of_stock sync
        # update (affected_product_ids is non-empty, so this always runs;
        # its own WHERE clause is what actually limits real-world impact).
        self.assertEqual(len(product_updates), 3)
        self.assertIn("stock_grams = %s", product_updates[0][0])
        self.assertIn("stock_quantity = %s", product_updates[1][0])
        self.assertIn("is_out_of_stock = TRUE", product_updates[2][0])
        option_updates = cursor.updates_of("product_options")
        self.assertEqual(len(option_updates), 1)
        self.assertTrue(connection.committed)

    def test_insufficient_stock_on_one_line_deducts_nothing_and_rolls_back(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(fulfillment_status="picking"),
                None,
                (5000,), (10,), (0,),  # options short: 0 available, 1 required
            ],
            fetchall_values=[list(MIXED_ORDER_ITEMS)],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        self.assertIn("Недостаточно товара", response)
        self.assertFalse(cursor.updates_of("products"))
        self.assertFalse(cursor.updates_of("product_options"))
        self.assertFalse(cursor.inserts_into("inventory_movements"))
        self.assertFalse(cursor.updates_of("orders"))
        self.assertTrue(connection.rolled_back)


class CancellationRestoreTests(unittest.TestCase):
    """Requirements 7/8: cancellation after packed restores inventory
    exactly once; cancellation before packed neither restores nor
    deducts."""

    def test_cancel_after_packed_restores_inventory_once(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(
                    fulfillment_status="packed",
                    inventory_deducted=True,
                    inventory_restored=False,
                )
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "restore_order_inventory", return_value=[]) as restore, \
             patch.object(admin_app, "deduct_order_inventory") as deduct:
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "cancelled"))

        restore.assert_called_once_with(cursor, "order-1")
        deduct.assert_not_called()
        self.assertTrue(
            any("inventory_restored = TRUE" in query for query, _ in cursor.updates_of("orders"))
        )

    def test_cancel_before_packed_does_not_touch_inventory(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(
                    fulfillment_status="confirmed",
                    inventory_deducted=False,
                    inventory_restored=False,
                )
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "restore_order_inventory") as restore, \
             patch.object(admin_app, "deduct_order_inventory") as deduct:
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "cancelled"))

        restore.assert_not_called()
        deduct.assert_not_called()
        self.assertFalse(
            any("inventory_restored" in query for query, _ in cursor.updates_of("orders"))
        )

    def test_already_restored_order_is_not_restored_again(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(
                    fulfillment_status="packed",
                    inventory_deducted=True,
                    inventory_restored=True,
                )
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "restore_order_inventory") as restore:
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "cancelled"))

        restore.assert_not_called()


class RefundedRequiresExplicitActionTests(unittest.TestCase):
    """Requirement 9: refunded is only reachable via an explicit payment
    action, and cancellation never infers it."""

    def test_cancellation_never_writes_refunded(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                _fulfillment_row(
                    fulfillment_status="packed",
                    inventory_deducted=True, inventory_restored=False,
                )
            ]
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "restore_order_inventory", return_value=[]):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "cancelled"))

        for query, _params in cursor.queries:
            self.assertNotIn("refunded", query)

    def test_refunded_reachable_only_from_paid(self):
        self.assertEqual(admin_app.PAYMENT_TRANSITIONS["paid"], {"refunded"})
        self.assertNotIn("refunded", admin_app.PAYMENT_TRANSITIONS["unpaid"])
        self.assertNotIn("refunded", admin_app.PAYMENT_TRANSITIONS["payment_reported"])

    def test_explicit_refund_action_succeeds_from_paid(self):
        cursor = ScriptedCursor(fetchone_values=[_payment_row(payment_status="paid")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_payment_status("order-1", "refunded"))

        updates = cursor.updates_of("orders")
        self.assertEqual(updates[0][1][0], "refunded")


class InvalidTransitionRejectionTests(unittest.TestCase):
    """Requirements 10/11: invalid transitions are rejected before any
    write, for both payment and fulfillment."""

    def test_unpaid_to_refunded_is_rejected(self):
        cursor = ScriptedCursor(fetchone_values=[_payment_row(payment_status="unpaid")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_payment_status("order-1", "refunded"))

        self.assertFalse(cursor.updates_of("orders"))
        self.assertFalse(connection.committed)

    def test_new_to_packed_skipping_confirmed_and_picking_is_rejected(self):
        cursor = ScriptedCursor(fetchone_values=[_fulfillment_row(fulfillment_status="new")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "deduct_order_inventory") as deduct:
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "packed"))

        deduct.assert_not_called()
        self.assertFalse(cursor.updates_of("orders"))

    def test_delivered_is_terminal(self):
        cursor = ScriptedCursor(fetchone_values=[_fulfillment_row(fulfillment_status="delivered")])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "cancelled"))

        self.assertFalse(cursor.updates_of("orders"))

    def test_unknown_payment_action_is_rejected_before_any_query(self):
        with patch.object(admin_app.psycopg2, "connect") as connect:
            response = asyncio.run(
                admin_app.update_order_payment_status("order-1", "not-a-real-action")
            )
        connect.assert_not_called()
        self.assertIn("Недопустимое действие", response)

    def test_unknown_fulfillment_action_is_rejected_before_any_query(self):
        with patch.object(admin_app.psycopg2, "connect") as connect:
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "not-a-real-action")
            )
        connect.assert_not_called()
        self.assertIn("Недопустимое действие", response)


class BadgeAndActionRenderingTests(unittest.TestCase):
    """Requirements 12/13: badge/action rendering matches the given
    payment_status/fulfillment_status, pure-function level."""

    def test_payment_badge_labels(self):
        self.assertIn("Не оплачен", admin_app.payment_status_badge("unpaid"))
        self.assertIn("Оплата заявлена", admin_app.payment_status_badge("payment_reported"))
        self.assertIn("Оплачен", admin_app.payment_status_badge("paid"))
        self.assertIn("Возврат оформлен", admin_app.payment_status_badge("refunded"))

    def test_fulfillment_badge_labels(self):
        self.assertIn("Новый", admin_app.fulfillment_status_badge("new"))
        self.assertIn("Упакован", admin_app.fulfillment_status_badge("packed"))
        self.assertIn("Доставлен", admin_app.fulfillment_status_badge("delivered"))
        self.assertIn("Отменён", admin_app.fulfillment_status_badge("cancelled"))

    def test_payment_actions_for_matches_transition_table(self):
        self.assertEqual(
            [action for action, _label in admin_app.payment_actions_for("unpaid")],
            ["payment_reported", "paid"],
        )
        self.assertEqual(
            [action for action, _label in admin_app.payment_actions_for("paid")],
            ["refunded"],
        )
        self.assertEqual(admin_app.payment_actions_for("refunded"), [])

    def test_fulfillment_actions_for_matches_transition_table(self):
        self.assertEqual(
            [action for action, _label in admin_app.fulfillment_actions_for("new")],
            ["confirmed", "cancelled"],
        )
        self.assertEqual(
            [action for action, _label in admin_app.fulfillment_actions_for("shipped")],
            ["delivered", "cancelled"],
        )
        self.assertEqual(admin_app.fulfillment_actions_for("delivered"), [])
        self.assertEqual(admin_app.fulfillment_actions_for("cancelled"), [])


class OrdersListFilterTests(unittest.TestCase):
    """Requirements 14/15: /orders can filter independently by
    payment_status and by fulfillment_status."""

    def test_filters_by_payment_status(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (0,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders(payment_status_filter="paid"))

        select_query, select_params = cursor.execute.call_args_list[0].args
        self.assertIn("payment_status = %s", select_query)
        self.assertIn("paid", select_params)

    def test_filters_by_fulfillment_status(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (0,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders(fulfillment_status_filter="packed"))

        select_query, select_params = cursor.execute.call_args_list[0].args
        self.assertIn("fulfillment_status = %s", select_query)
        self.assertIn("packed", select_params)

    def test_invalid_filter_values_fall_back_to_all(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (0,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders(
                payment_status_filter="bogus", fulfillment_status_filter="bogus",
            ))

        select_query, _select_params = cursor.execute.call_args_list[0].args
        self.assertNotIn("payment_status = %s", select_query)
        self.assertNotIn("fulfillment_status = %s", select_query)

    def test_payment_and_fulfillment_filters_combine(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (0,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders(
                payment_status_filter="paid", fulfillment_status_filter="new",
            ))

        select_query, select_params = cursor.execute.call_args_list[0].args
        self.assertIn("payment_status = %s", select_query)
        self.assertIn("fulfillment_status = %s", select_query)
        self.assertEqual(select_params, ["paid", "new"])

    def test_legacy_status_filter_is_no_longer_an_accepted_parameter(self):
        # Checkpoint E: operational filtering no longer depends on legacy
        # orders.status -- the route no longer even declares a
        # status_filter parameter (an old bookmarked URL with
        # ?status_filter=... is simply ignored by FastAPI, not an error).
        import inspect
        signature = inspect.signature(admin_app.orders)
        self.assertNotIn("status_filter", signature.parameters)


class SourceVisibilityTests(unittest.TestCase):
    """Requirement 16: source remains visible/readable, and never affects
    transition validation."""

    def test_source_badge_renders_every_known_source(self):
        for source in admin_app.ORDER_SOURCE_VALUES:
            with self.subTest(source=source):
                badge = admin_app.order_source_badge(source)
                self.assertIn("status", badge)
                self.assertNotIn("-</span>", badge)

    def test_source_does_not_appear_in_transition_tables(self):
        transition_text = str(admin_app.PAYMENT_TRANSITIONS) + str(admin_app.FULFILLMENT_TRANSITIONS)
        for source in admin_app.ORDER_SOURCE_VALUES:
            self.assertNotIn(source, transition_text)


class CsrfProtectionTests(unittest.TestCase):
    """Requirement 18: both new POST routes are registered behind the same
    require_admin_csrf dependency as every other unsafe admin route."""

    def test_payment_and_fulfillment_routes_require_admin_csrf(self):
        expected = {
            ("POST", "/orders/{order_id}/payment/{action}"),
            ("POST", "/orders/{order_id}/fulfillment/{action}"),
        }
        found = set()
        for route in admin_app.app.routes:
            methods = set(getattr(route, "methods", set()))
            if "POST" not in methods:
                continue
            if (route.methods and "POST" in route.methods) and (
                route.path in {path for _method, path in expected}
            ):
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }
                self.assertIn("require_admin_csrf", dependency_names)
                found.add(("POST", route.path))
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
