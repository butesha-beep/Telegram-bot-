import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/orders-v2-dual-write")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

import bot


# Scoped via patch.dict (auto-restored) rather than a module-level
# os.environ mutation, which would otherwise leak into other test files
# sharing this process. Matches the pattern already used in
# tests/test_bot_fixed_mode_display.py / tests/test_bot_options_mode.py.
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

    def close(self):
        pass

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
        self._cursor = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def per_kg_product(product_id=1, price_per_kg=24.0):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Товар",
        "price_per_kg": price_per_kg,
        "description": "",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": 1000,
        "is_out_of_stock": False,
        "pricing_mode": "per_kg",
        "fixed_price": None,
        "sale_unit": None,
        "unit_weight_grams": None,
        "stock_quantity": None,
    }


ORDERS_INSERT_COLUMNS = (
    "order_id", "telegram_id", "username", "phone", "address", "total",
    "source", "source_reference", "client_id", "payment_status",
    "fulfillment_status", "payment_method", "customer_name",
    "delivery_method", "delivery_street", "delivery_house_number",
    "delivery_postcode", "delivery_city", "delivery_country",
    "delivery_notes",
)


def _orders_insert_fields(params):
    return dict(zip(ORDERS_INSERT_COLUMNS, params))


def _fake_callback(data, telegram_id, username="tester", first_name="Ivan"):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(answer=AsyncMock()),
        from_user=SimpleNamespace(id=telegram_id, username=username, first_name=first_name),
        answer=AsyncMock(),
    )


class _FakeEventLoop:
    """create_order_from_cart derives order_id from
    asyncio.get_event_loop().time(), unrelated to this checkpoint. Outside
    a running event loop (plain unittest.TestCase, not
    IsolatedAsyncioTestCase), asyncio.get_event_loop() raises in this
    interpreter -- pre-existing, version-sensitive behavior this checkpoint
    doesn't touch. Patched here only to make order_id deterministic for
    these synchronous calls."""

    def time(self):
        return 1_000_000.0


def _create_order_from_cart(**kwargs):
    with patch.object(bot.asyncio, "get_event_loop", return_value=_FakeEventLoop()):
        return bot.create_order_from_cart(**kwargs)


class NewTelegramOrderInsertTests(unittest.TestCase):
    """The new-order INSERT populates the Orders v2 fields and still writes
    legacy telegram_id, but (Checkpoint E) no longer writes legacy
    orders.status -- payment_status/fulfillment_status are the sole runtime
    authority."""

    def test_writes_source_telegram(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["source"], "telegram")

    def test_legacy_telegram_id_still_written_status_no_longer_written(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["telegram_id"], 555)
        self.assertNotIn("status", fields)
        insert_query = cursor.inserts_into("orders")[0][0]
        self.assertIsNone(re.search(r"\bstatus\b", insert_query))

    def test_new_order_starts_fulfillment_status_new_and_payment_status_unpaid(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["fulfillment_status"], "new")
        self.assertEqual(fields["payment_status"], "unpaid")

    def test_never_sets_paid_or_refunded_at_creation(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertNotEqual(fields["payment_status"], "paid")
        self.assertNotEqual(fields["payment_status"], "refunded")


class ClientLinkingTests(unittest.TestCase):
    """Requirement 2: client_id is resolved from clients.id, without
    replacing existing telegram_id-based client behavior."""

    def test_save_contact_true_links_client_id_from_returning_id(self):
        cursor = ScriptedCursor(fetchone_values=[(42,), None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=True,
            )

        client_inserts = cursor.inserts_into("clients")
        self.assertEqual(len(client_inserts), 1)
        self.assertIn("RETURNING id", client_inserts[0][0])
        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["client_id"], 42)

    def test_save_contact_false_links_an_already_existing_client(self):
        cursor = ScriptedCursor(fetchone_values=[(99,)])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        self.assertEqual(cursor.inserts_into("clients"), [])
        select_client_queries = [
            (q, p) for q, p in cursor.queries
            if q.strip().startswith("SELECT id FROM clients")
        ]
        self.assertEqual(len(select_client_queries), 1)
        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["client_id"], 99)

    def test_no_matching_client_leaves_client_id_null_no_fake_client_created(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        self.assertEqual(cursor.inserts_into("clients"), [])
        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertIsNone(fields["client_id"])

    def test_save_contact_true_still_writes_the_same_client_columns_as_before(self):
        # Regression: the clients INSERT...ON CONFLICT columns/values must
        # be unchanged apart from the added RETURNING id.
        cursor = ScriptedCursor(fetchone_values=[(7,)])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=True,
            )

        query, params = cursor.inserts_into("clients")[0]
        self.assertIn("ON CONFLICT (telegram_id) DO UPDATE", query)
        self.assertEqual(params, (555, "tester", "Ivan", "+31600000000", "Teststraat 1"))


class CustomerAndDeliverySnapshotTests(unittest.TestCase):
    """Requirement 11: customer_name/delivery snapshot fields are only
    populated from data already reliably available at checkout; anything
    not truly available (structured address components, a pickup concept
    that doesn't exist, a Telegram-specific source_reference) is left
    NULL/omitted rather than guessed or parsed."""

    def test_customer_name_snapshots_first_name(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["customer_name"], "Ivan")

    def test_no_first_name_leaves_customer_name_null(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name=None, save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertIsNone(fields["customer_name"])

    def test_delivery_method_is_delivery_no_pickup_concept_invented(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["delivery_method"], "delivery")

    def test_structured_delivery_and_source_reference_columns_are_not_written(self):
        # Only one free-text address string exists at checkout -- no risky
        # parsing is performed, so these columns are always NULL (the
        # shared order_creation core lists every column explicitly, so
        # "not written" means "value is None", not "absent from the SQL
        # text" -- an explicit NULL and an omitted column are equivalent).
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        for omitted_column in (
            "delivery_street", "delivery_house_number", "delivery_postcode",
            "delivery_city", "delivery_country", "delivery_notes",
            "source_reference",
        ):
            self.assertIsNone(fields[omitted_column])

    def test_legacy_address_field_is_preserved_exactly(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1, 1234 AB Amsterdam",
                cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1)], first_name="Ivan", save_contact=False,
            )

        fields = _orders_insert_fields(cursor.inserts_into("orders")[0][1])
        self.assertEqual(fields["address"], "Teststraat 1, 1234 AB Amsterdam")


class OrderItemsRegressionTests(unittest.TestCase):
    """Requirement 12: existing pricing/inventory-relevant order_items
    columns are unaffected by this checkpoint."""

    def test_order_items_insert_unchanged(self):
        cursor = ScriptedCursor(fetchone_values=[None])
        connection = ScriptedConnection(cursor)
        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            _create_order_from_cart(
                user_id=555, username="tester", phone="+31600000000",
                address="Teststraat 1", cart_items=[(1, 200, None, None, None)],
                products=[per_kg_product(1, price_per_kg=24.0)], first_name="Ivan",
                save_contact=False,
            )

        item_inserts = cursor.inserts_into("order_items")
        self.assertEqual(len(item_inserts), 1)
        query, params = item_inserts[0]
        self.assertIn("pricing_mode", query)
        self.assertIn("price_per_kg_snapshot", query)
        self.assertEqual(params[3], 200)  # weight
        self.assertEqual(params[6], "per_kg")  # pricing_mode
        self.assertEqual(params[7], 24.0)  # price_per_kg_snapshot


class ScopeGuardTests(unittest.TestCase):
    """Requirement 13: no manual-order or website code path is introduced
    -- bot.py must never write an orders.source value other than
    'telegram'."""

    def test_bot_module_never_references_non_telegram_order_sources(self):
        with open(bot.__file__, encoding="utf-8") as source_file:
            source_text = source_file.read()
        for other_source in (
            "website", "instagram", "tiktok", "whatsapp", "viber", "in_person",
        ):
            self.assertNotIn(f'"{other_source}"', source_text)
            self.assertNotIn(f"'{other_source}'", source_text)


@telegram_actions_enabled_for_tests
class PaymentMethodSelectionDualWriteTests(unittest.IsolatedAsyncioTestCase):
    """Checkpoint E: selecting a payment method writes payment_method and
    payment_status only -- legacy orders.status is no longer written."""

    async def test_iban_selection_writes_awaiting_payment_and_payment_status_unpaid(self):
        cursor = ScriptedCursor(fetchone_values=[(4242,)])
        connection = ScriptedConnection(cursor)
        callback = _fake_callback("pay_iban", telegram_id=555)

        with patch.object(bot, "load_json", return_value={}), \
             patch.object(
                 bot, "get_payment_details",
                 return_value={"iban": "NL00BANK", "receiver_name": "Deal Market", "paypal": None},
             ), \
             patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            await bot.pay_iban(callback)

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("payment_status = 'unpaid'", query)
        self.assertNotIn("status = %s", query)
        self.assertEqual(params, ("IBAN", 555))
        self.assertTrue(connection.committed)

    async def test_paypal_selection_writes_awaiting_payment_and_payment_status_unpaid(self):
        cursor = ScriptedCursor(fetchone_values=[(4243,)])
        connection = ScriptedConnection(cursor)
        callback = _fake_callback("pay_paypal", telegram_id=556)

        with patch.object(bot, "load_json", return_value={}), \
             patch.object(
                 bot, "get_payment_details",
                 return_value={"iban": None, "receiver_name": None, "paypal": "paypal.me/deal"},
             ), \
             patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            await bot.pay_paypal(callback)

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("payment_status = 'unpaid'", query)
        self.assertNotIn("status = %s", query)
        self.assertEqual(params, ("PayPal", 556))

    async def test_cash_selection_writes_cash_on_delivery_and_payment_status_unpaid(self):
        cursor = ScriptedCursor(fetchone_values=[(4244,)])
        connection = ScriptedConnection(cursor)
        callback = _fake_callback("pay_cash", telegram_id=557)

        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot, "send_admin_message", new=AsyncMock(return_value=False)):
            await bot.pay_cash(callback)

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("payment_status = 'unpaid'", query)
        self.assertNotIn("status = %s", query)
        self.assertEqual(params, ("Cash", 557))


@telegram_actions_enabled_for_tests
class PaymentReportedDualWriteTests(unittest.IsolatedAsyncioTestCase):
    """Checkpoint E: reporting payment writes payment_status='payment_reported'
    only -- legacy orders.status is no longer written -- via both the
    order-specific and generic handlers."""

    async def test_payment_done_for_order_writes_payment_reported(self):
        class RowcountCursor(ScriptedCursor):
            rowcount = 1

        cursor = RowcountCursor()
        connection = ScriptedConnection(cursor)
        callback = _fake_callback("payment_done_4242", telegram_id=555)

        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"), \
             patch.object(bot, "send_admin_message", new=AsyncMock(return_value=False)):
            await bot.payment_done_for_order(callback)

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("payment_status = 'payment_reported'", query)
        self.assertNotIn("status = %s", query)
        self.assertEqual(params, (4242, 555))
        self.assertTrue(connection.committed)

    async def test_payment_done_generic_writes_payment_reported(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)
        callback = _fake_callback("payment_done", telegram_id=555)

        with patch.object(bot.psycopg2, "connect", return_value=connection), \
             patch.object(bot, "log_customer_event"):
            await bot.payment_done(callback)

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("payment_status = 'payment_reported'", query)
        self.assertNotIn("status = %s", query)
        self.assertEqual(params, (555,))


@telegram_actions_enabled_for_tests
class AutoCancelDualWriteTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 8: bot-side auto-cancellation writes
    fulfillment_status='cancelled' alongside the legacy status write, and
    never invents a 'refunded' payment_status."""

    async def test_cancel_expired_pending_orders_sets_fulfillment_status_cancelled(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.cancel_expired_pending_orders()

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, _ = updates[0]
        self.assertIn("fulfillment_status = 'cancelled'", query)
        self.assertNotIn("refunded", query)
        self.assertTrue(connection.committed)

    async def test_cancel_expired_awaiting_payment_orders_sets_fulfillment_status_cancelled(self):
        cursor = ScriptedCursor()
        connection = ScriptedConnection(cursor)

        with patch.object(bot.psycopg2, "connect", return_value=connection):
            await bot.cancel_expired_awaiting_payment_orders()

        updates = cursor.updates_of("orders")
        self.assertEqual(len(updates), 1)
        query, _ = updates[0]
        self.assertIn("fulfillment_status = 'cancelled'", query)
        self.assertNotIn("refunded", query)
        self.assertTrue(connection.committed)


@telegram_actions_enabled_for_tests
class ReminderTelegramIdBehaviorUnchangedTests(unittest.IsolatedAsyncioTestCase):
    """Requirement 10: reminder/notification workers still key off
    telegram_id exactly as before -- this checkpoint does not touch that
    code path."""

    async def test_pending_order_reminder_still_notifies_by_telegram_id(self):
        select_cursor = ScriptedCursor(fetchall_values=[[(555,)]])
        select_connection = ScriptedConnection(select_cursor)
        update_cursor = ScriptedCursor()
        update_connection = ScriptedConnection(update_cursor)

        connections = [select_connection, update_connection]

        def fake_connect(*args, **kwargs):
            return connections.pop(0)

        with patch.object(bot.psycopg2, "connect", side_effect=fake_connect), \
             patch.object(bot.bot, "send_message", new=AsyncMock()) as send_message, \
             patch.object(bot, "log_customer_event"):
            await bot.send_pending_order_reminders()

        send_message.assert_awaited_once()
        self.assertEqual(send_message.await_args.kwargs["chat_id"], 555)
        self.assertIn("telegram_id", select_cursor.queries[0][0])


if __name__ == "__main__":
    unittest.main()
