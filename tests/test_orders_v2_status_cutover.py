import ast
import asyncio
import os
import re
import unittest
from unittest.mock import AsyncMock, Mock, patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/orders-v2-status-cutover")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app
import bot
import db_schema


telegram_actions_enabled_for_tests = patch.dict(
    os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}
)


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

    def updates_of(self, table):
        return [
            (query, params)
            for query, params in self.queries
            if query.strip().startswith(f"UPDATE {table}")
        ]

    def inserts_into(self, table):
        return [
            (query, params)
            for query, params in self.queries
            if f"INSERT INTO {table}" in query
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


BARE_STATUS = re.compile(r"(?<![A-Za-z_])status\s*=")


# ---------------------------------------------------------------------------
# Requirement (blunt regression): no runtime order-mutation SQL in bot.py or
# admin_app.py may SET a bare `status` column. payment_status/fulfillment_
# status are excluded by the negative lookbehind; other tables' own `status`
# columns (broadcasts, channel_posts, broadcast_recipients, master_shops)
# are excluded by only scanning strings that mention "UPDATE orders" or
# "INSERT INTO orders" -- so this can never flag legitimate status writes on
# unrelated tables or on HTTP-status-shaped local variables.
# ---------------------------------------------------------------------------

def _string_literals(module_file):
    with open(module_file, encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read())
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _orders_mutation_statements(module_file):
    literals = _string_literals(module_file)
    return [
        sql for sql in literals
        if re.search(r"\b(UPDATE|INSERT INTO)\s+orders\b", sql)
    ]


class NoRuntimeLegacyStatusWriteTests(unittest.TestCase):
    """Blunt regression: scans every string literal in bot.py/admin_app.py
    that mutates the orders table and asserts none of them sets a bare
    `status` column."""

    def test_bot_has_no_orders_mutation_setting_bare_status(self):
        statements = _orders_mutation_statements(bot.__file__)
        self.assertTrue(statements, "expected to find orders UPDATE/INSERT statements in bot.py")
        offending = [sql for sql in statements if BARE_STATUS.search(sql)]
        self.assertEqual(offending, [])

    def test_admin_app_has_no_orders_mutation_setting_bare_status(self):
        statements = _orders_mutation_statements(admin_app.__file__)
        self.assertTrue(statements, "expected to find orders UPDATE/INSERT statements in admin_app.py")
        offending = [sql for sql in statements if BARE_STATUS.search(sql)]
        self.assertEqual(offending, [])

    def test_regex_does_not_false_positive_on_payment_or_fulfillment_status(self):
        self.assertIsNone(BARE_STATUS.search("SET payment_status = %s"))
        self.assertIsNone(BARE_STATUS.search("SET fulfillment_status = %s"))
        self.assertIsNotNone(BARE_STATUS.search("SET status = %s"))
        self.assertIsNotNone(BARE_STATUS.search("SET payment_status = %s, status = %s"))


@telegram_actions_enabled_for_tests
class ReminderWorkersUseNewFieldsTests(unittest.IsolatedAsyncioTestCase):
    """Requirements 6/7/9/10: reminder workers key off payment_status/
    payment_method, never legacy status; COD (payment_method='Cash') is
    structurally excluded from the bank-transfer-only awaiting_payment
    queries, and payment_reported orders are structurally excluded by the
    payment_status='unpaid' filter."""

    async def test_pending_order_reminder_select_uses_new_fields(self):
        cursor = ScriptedCursor(fetchall_values=[[]])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.send_pending_order_reminders()

        select_query = cursor.queries[0][0]
        self.assertIn("payment_status = 'unpaid'", select_query)
        self.assertIn("payment_method IS NULL", select_query)
        self.assertIsNone(BARE_STATUS.search(select_query))

    async def test_awaiting_payment_reminder_select_excludes_cash_and_reported(self):
        cursor = ScriptedCursor(fetchall_values=[[]])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.send_awaiting_payment_reminders()

        select_query = cursor.queries[0][0]
        # payment_status='unpaid' structurally excludes payment_reported
        # orders; payment_method IN ('IBAN', 'PayPal') structurally
        # excludes cash/COD orders.
        self.assertIn("payment_status = 'unpaid'", select_query)
        self.assertIn("payment_method IN ('IBAN', 'PayPal')", select_query)
        self.assertNotIn("'Cash'", select_query)
        self.assertIsNone(BARE_STATUS.search(select_query))


@telegram_actions_enabled_for_tests
class AutoCancelUsesFulfillmentStatusOnlyTests(unittest.IsolatedAsyncioTestCase):
    """Requirements 8/9: auto-cancellation writes fulfillment_status only,
    reads payment_status/payment_method (never legacy status), and its
    bank-transfer-only variant structurally excludes COD orders."""

    async def test_cancel_expired_pending_orders_never_touches_legacy_status(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.cancel_expired_pending_orders()

        query, _params = cursor.queries[0]
        self.assertIn("fulfillment_status = 'cancelled'", query)
        self.assertIn("payment_status = 'unpaid'", query)
        self.assertIn("payment_method IS NULL", query)
        self.assertIsNone(BARE_STATUS.search(query))

    async def test_cancel_expired_awaiting_payment_orders_excludes_cash(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.cancel_expired_awaiting_payment_orders()

        query, _params = cursor.queries[0]
        self.assertIn("fulfillment_status = 'cancelled'", query)
        self.assertIn("payment_method IN ('IBAN', 'PayPal')", query)
        self.assertNotIn("'Cash'", query)
        self.assertIsNone(BARE_STATUS.search(query))


class AdminPaymentAndFulfillmentRoutesNeverWriteStatusTests(unittest.TestCase):
    """Requirements 11/12: every admin payment action and every admin
    fulfillment action writes only its own new-model column -- never legacy
    orders.status -- across the full action vocabulary."""

    def test_every_payment_action_omits_legacy_status(self):
        for current, action in (
            ("unpaid", "payment_reported"),
            ("unpaid", "paid"),
            ("payment_reported", "paid"),
            ("paid", "refunded"),
        ):
            with self.subTest(current=current, action=action):
                cursor = ScriptedCursor(fetchone_values=[(current, 555)])
                connection = ScriptedConnection(cursor)
                with patch.object(admin_app.psycopg2, "connect", return_value=connection):
                    asyncio.run(admin_app.update_order_payment_status("order-1", action))

                updates = cursor.updates_of("orders")
                self.assertEqual(len(updates), 1)
                self.assertIsNone(BARE_STATUS.search(updates[0][0]))

    def test_every_fulfillment_action_omits_legacy_status(self):
        transitions = (
            ("new", "confirmed"),
            ("confirmed", "picking"),
            ("confirmed", "cancelled"),
            ("picking", "cancelled"),
            ("ready_to_ship", "shipped"),
            ("shipped", "delivered"),
        )
        for current, action in transitions:
            with self.subTest(current=current, action=action):
                cursor = ScriptedCursor(
                    fetchone_values=[(current, False, False, 555)]
                )
                connection = ScriptedConnection(cursor)
                with patch.object(admin_app.psycopg2, "connect", return_value=connection):
                    asyncio.run(
                        admin_app.update_order_fulfillment_status("order-1", action)
                    )

                updates = cursor.updates_of("orders")
                self.assertEqual(len(updates), 1)
                self.assertIsNone(BARE_STATUS.search(updates[0][0]))


class DashboardUsesNewFieldsTests(unittest.TestCase):
    """Requirements 15/16: dashboard delivered/revenue metrics use
    fulfillment_status='delivered'; payment-attention metrics use
    payment_status, not legacy status."""

    def test_root_dashboard_stats_query_uses_new_fields(self):
        cursor = Mock()
        cursor.fetchone.return_value = (0,) * 13
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.root())

        stats_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("fulfillment_status = 'delivered'", stats_query)
        self.assertIn("payment_status = 'unpaid'", stats_query)
        self.assertIsNone(BARE_STATUS.search(stats_query))

    def test_master_snapshot_query_uses_new_fields(self):
        cursor = Mock()
        cursor.fetchone.side_effect = [(0, 0, 0, 0), (0,), (0,)]
        admin_app.create_current_master_snapshot(cursor)

        stats_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("payment_status IN ('unpaid', 'payment_reported')", stats_query)
        self.assertIn("fulfillment_status = 'delivered'", stats_query)
        self.assertIsNone(BARE_STATUS.search(stats_query))


class CsvExportExposesNewFieldsTests(unittest.TestCase):
    """Requirement 18: CSV export includes payment_status, fulfillment_status,
    and source; legacy status is retained only as a clearly-labeled
    legacy_status column, not as the primary meaning of the row."""

    def test_csv_select_and_header_include_new_fields(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders_export_csv())

        select_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("payment_status", select_query)
        self.assertIn("fulfillment_status", select_query)
        self.assertIn("source", select_query)

        with open(admin_app.__file__, encoding="utf-8") as source_file:
            admin_source = source_file.read()
        csv_export_source = admin_source.split("async def orders_export_csv")[1].split(
            "async def update_order_payment_status"
        )[0]
        self.assertIn('"legacy_status"', csv_export_source)


class NoNewStatusChangedEventTests(unittest.TestCase):
    """Requirement 20: only payment_status_changed/fulfillment_status_changed
    events are written going forward -- no new status_changed event."""

    def test_payment_route_logs_payment_status_changed_only(self):
        cursor = ScriptedCursor(fetchone_values=[("unpaid", 555)])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_payment_status("order-1", "paid"))

        events = cursor.inserts_into("order_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1][1], "payment_status_changed")

    def test_fulfillment_route_logs_fulfillment_status_changed_only(self):
        cursor = ScriptedCursor(fetchone_values=[("new", False, False, 555)])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.update_order_fulfillment_status("order-1", "confirmed"))

        events = cursor.inserts_into("order_events")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1][1], "fulfillment_status_changed")

    def test_admin_event_type_label_distinguishes_new_event_types(self):
        # status_changed (the retired legacy event type) still has a label
        # for rendering old historical events, but the two new event types
        # have their own distinct labels -- nothing new is ever logged
        # under the old key (proven by the two tests above).
        payment_label = admin_app.admin_event_type_label("payment_status_changed")
        fulfillment_label = admin_app.admin_event_type_label("fulfillment_status_changed")
        self.assertNotEqual(payment_label, "payment_status_changed")
        self.assertNotEqual(fulfillment_label, "fulfillment_status_changed")
        self.assertNotEqual(payment_label, fulfillment_label)


class HistoricalStatusRemainsReadableTests(unittest.TestCase):
    """Requirement 21: orders.status remains physically present and
    readable for historical/display purposes -- order_detail still selects
    it for the clearly-labeled legacy field -- while db_schema.py never
    drops or renames the column."""

    def test_order_detail_still_selects_legacy_status_for_display(self):
        cursor = Mock()
        cursor.fetchone.return_value = None
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.order_detail("42"))

        select_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("status", select_query)

    def test_db_schema_never_drops_or_renames_status_column(self):
        with open(db_schema.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertNotIn("DROP COLUMN status", source)
        self.assertNotIn("RENAME COLUMN status", source)


class CurrentStateComesFromNewFieldsTests(unittest.TestCase):
    """Requirement 19: current order state shown in /orders and the
    dashboard's recent-orders widget comes from payment_status/
    fulfillment_status, not legacy status -- proven by the list route no
    longer selecting legacy status at all."""

    def test_orders_list_select_no_longer_includes_bare_status_column(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (0,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.orders())

        select_query = cursor.execute.call_args_list[0].args[0]
        self.assertIn("payment_status", select_query)
        self.assertIn("fulfillment_status", select_query)
        select_columns = select_query.split("FROM orders")[0]
        self.assertIsNone(BARE_STATUS.search(select_columns.replace("SELECT", "")))
        self.assertNotIn(", status,", select_columns)


if __name__ == "__main__":
    unittest.main()
