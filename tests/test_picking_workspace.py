import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import RedirectResponse


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/picking-workspace")
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


def _get_request(path="/picking"):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


# orders row shape for /picking's queue queries:
# id, order_id, customer_name, username, total, payment_status,
# payment_method, fulfillment_status, source, created_at,
# delivery_method, delivery_street, delivery_city
def _order_row(order_id="42", fulfillment_status="confirmed", source="telegram",
                payment_status="unpaid", payment_method="Cash", customer_name="Ivan",
                delivery_method="pickup"):
    return (
        1, order_id, customer_name, "ivan_u", 12.5, payment_status,
        payment_method, fulfillment_status, source, None,
        delivery_method, None, None,
    )


# order_items row shape for /picking's items query:
# order_id, item_id, product_name, weight, option_id, option_label, pricing_mode
def _per_kg_item(order_id="42", item_id=10, weight=None):
    return (order_id, item_id, "Лосось", weight, None, None, "per_kg")


def _fixed_item(order_id="42", item_id=11):
    return (order_id, item_id, "Подарочный набор", None, None, None, "fixed")


def _options_item(order_id="42", item_id=12, option_id=5, label="Средняя"):
    return (order_id, item_id, "Рыба на вес", None, option_id, label, "options")


def _strip_order(row):
    """DB item rows carry order_id as their first column (needed to
    group by order in the GET /picking query); _picking_order_lines and
    _picking_order_card_html operate on one order's items at a time and
    don't take order_id in the tuple -- this drops it for tests that call
    those functions directly."""
    return row[1:]


class AuthAndCsrfTests(unittest.TestCase):
    """Requirements 1/2/25: GET requires the same global admin-auth
    middleware as every other admin route, and every picking mutation
    route declares the same require_admin_csrf dependency used
    everywhere else (the full CSRF route inventory is asserted in
    tests/test_csrf_protection.py, which now includes all three)."""

    def test_get_picking_requires_admin_auth(self):
        async def call_next(_request):
            raise AssertionError("handler must not run without authentication")

        response = asyncio.run(
            admin_app.require_admin_login(_get_request("/picking"), call_next)
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_picking_mutation_routes_declare_csrf_dependency(self):
        expected_paths = {
            "/picking/{order_id}/start",
            "/picking/{order_id}/pack",
            "/picking/{order_id}/items/{item_id}/weigh",
        }
        found_paths = set()
        for route in admin_app.app.routes:
            if route.path in expected_paths and "POST" in getattr(route, "methods", set()):
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }
                self.assertIn("require_admin_csrf", dependency_names)
                found_paths.add(route.path)
        self.assertEqual(found_paths, expected_paths)


class QueueSectionTests(unittest.TestCase):
    """Requirements 3/4/5/20: confirmed orders queue under 'to pick',
    picking orders queue under active picking, packed orders show only in
    the small recent-packed view, and cancelled/delivered orders are
    never fetched into any picking queue at all."""

    def _run_picking(self, confirmed_rows, picking_rows, packed_rows, items_rows):
        cursor = ScriptedCursor(
            fetchall_values=[confirmed_rows, picking_rows, packed_rows, items_rows],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.picking_workspace())
        return page, cursor

    def test_confirmed_orders_appear_in_to_pick_section(self):
        row = _order_row(order_id="42", fulfillment_status="confirmed")
        page, _cursor = self._run_picking([row], [], [], [_fixed_item(order_id="42")])
        self.assertIn("К сборке", page)
        self.assertIn("DM-000001", page)
        self.assertIn("Начать сборку", page)

    def test_picking_orders_appear_in_active_picking_section(self):
        row = _order_row(order_id="42", fulfillment_status="picking")
        page, _cursor = self._run_picking([], [row], [], [_fixed_item(order_id="42")])
        self.assertIn("В сборке", page)
        self.assertIn("Собрано", page)

    def test_packed_orders_appear_only_in_recently_packed_section(self):
        row = _order_row(order_id="42", fulfillment_status="packed")
        page, _cursor = self._run_picking([], [], [row], [_fixed_item(order_id="42")])
        self.assertIn("Недавно собрано", page)
        # A packed order has no start/pack action -- it's display-only here.
        self.assertNotIn("Начать сборку", page)
        self.assertNotIn(">✅ Собрано<", page)

    def test_only_confirmed_picking_packed_statuses_are_ever_queried(self):
        _page, cursor = self._run_picking([], [], [], [])
        queried_statuses = {
            params[0]
            for query, params in cursor.queries
            if "FROM orders" in query and params
        }
        self.assertEqual(queried_statuses, {"confirmed", "picking", "packed"})
        for query, _params in cursor.queries:
            if "FROM orders" in query:
                self.assertNotIn("cancelled", query)
                self.assertNotIn("delivered", query)


class SourceHandlingTests(unittest.TestCase):
    """Requirements 6/7/10 (manual+Telegram): the picking card renders
    every order through the exact same function regardless of source --
    source is shown as an informational badge only and never changes
    which actions are offered."""

    def test_telegram_and_manual_orders_render_through_the_same_card_function(self):
        telegram_row = _order_row(order_id="42", fulfillment_status="confirmed", source="telegram")
        manual_row = _order_row(order_id="43", fulfillment_status="confirmed", source="instagram")
        telegram_card = admin_app._picking_order_card_html(telegram_row, [_strip_order(_fixed_item(order_id="42"))])
        manual_card = admin_app._picking_order_card_html(manual_row, [_strip_order(_fixed_item(order_id="43"))])
        # Same structural markup (action button, items list, order link) --
        # only the badge text and identifiers legitimately differ.
        self.assertIn("Начать сборку", telegram_card)
        self.assertIn("Начать сборку", manual_card)
        self.assertIn("picking-items", telegram_card)
        self.assertIn("picking-items", manual_card)

    def test_source_badge_is_informational_and_does_not_gate_actions(self):
        for source in ("telegram", "website", "instagram", "whatsapp", "in_person"):
            with self.subTest(source=source):
                row = _order_row(order_id="42", fulfillment_status="confirmed", source=source)
                card = admin_app._picking_order_card_html(row, [_strip_order(_fixed_item(order_id="42"))])
                self.assertIn("Начать сборку", card)


class ItemRenderingTests(unittest.TestCase):
    """Requirements 8/9/10/11: per_kg/fixed/options lines are each
    rendered so the operator immediately knows what to collect."""

    def test_per_kg_item_with_weight_shows_grams(self):
        lines = admin_app._picking_order_lines([_strip_order(_per_kg_item(weight=350))])
        self.assertEqual(lines, [{
            "type": "per_kg", "item_id": 10, "product_name": "Лосось",
            "weight": 350, "needs_weighing": False,
        }])
        html = admin_app._picking_item_line_html("42", lines[0])
        self.assertIn("350", html)
        self.assertNotIn("требует взвешивания", html)

    def test_per_kg_item_without_weight_shows_pending_weighing_and_form(self):
        lines = admin_app._picking_order_lines([_strip_order(_per_kg_item(weight=None))])
        self.assertTrue(lines[0]["needs_weighing"])
        html = admin_app._picking_item_line_html("42", lines[0])
        self.assertIn("требует взвешивания", html)
        self.assertIn("/picking/42/items/10/weigh", html)
        self.assertIn('name="final_weight_grams"', html)

    def test_fixed_items_are_grouped_into_a_unit_quantity(self):
        rows = [
            _strip_order(_fixed_item(order_id="42", item_id=11)),
            _strip_order(_fixed_item(order_id="42", item_id=12)),
        ]
        lines = admin_app._picking_order_lines(rows)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["type"], "fixed")
        self.assertEqual(lines[0]["quantity"], 2)
        html = admin_app._picking_item_line_html("42", lines[0])
        self.assertIn("× 2", html)

    def test_options_items_are_grouped_by_option_label_and_quantity(self):
        rows = [
            _strip_order(_options_item(order_id="42", item_id=13, option_id=5, label="Средняя")),
            _strip_order(_options_item(order_id="42", item_id=14, option_id=5, label="Средняя")),
            _strip_order(_options_item(order_id="42", item_id=15, option_id=6, label="Крупная")),
        ]
        lines = admin_app._picking_order_lines(rows)
        by_label = {line["option_label"]: line["quantity"] for line in lines}
        self.assertEqual(by_label, {"Средняя": 2, "Крупная": 1})
        html = "".join(admin_app._picking_item_line_html("42", line) for line in lines)
        self.assertIn("Средняя × 2", html)
        self.assertIn("Крупная × 1", html)


class StartPickingTests(unittest.TestCase):
    """Requirements 12/13/19: starting picking reuses the existing
    confirmed->picking fulfillment transition unchanged, and a cash/unpaid
    order can start picking exactly like a paid one (fulfillment is never
    gated by payment_status)."""

    def test_start_picking_calls_the_existing_fulfillment_transition(self):
        calls = []
        original = admin_app.update_order_fulfillment_status

        async def spy(order_id, action):
            calls.append((order_id, action))
            return await original(order_id, action)

        cursor = ScriptedCursor(
            fetchone_values=[
                ("confirmed", False, False, 555),  # inner transition's own row fetch
                ("picking",),  # this wrapper's post-transition status check
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "update_order_fulfillment_status", side_effect=spy):
            response = asyncio.run(admin_app.picking_start_order("order-1"))

        self.assertEqual(calls, [("order-1", "picking")])
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")
        updates = cursor.updates_of("orders")
        self.assertEqual(updates[0][1], ("picking", "order-1"))

    def test_confirmed_to_picking_succeeds_while_unpaid_cash(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("confirmed", False, False, None),
                ("picking",),
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.picking_start_order("order-1"))

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")


class PackTests(unittest.TestCase):
    """Requirements 14/15/16/17/18/19: packing reuses the existing
    picking->packed transition unchanged, which performs the real
    pending-weighing check and atomic stock deduction -- this route never
    touches inventory itself."""

    def test_pack_calls_the_existing_fulfillment_transition(self):
        calls = []
        original = admin_app.update_order_fulfillment_status

        async def spy(order_id, action):
            calls.append((order_id, action))
            return await original(order_id, action)

        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 555),
                None,  # no pending weighing
                (10,),  # fixed stock available
                ("packed",),  # wrapper's post-transition status check
            ],
            fetchall_values=[[(2, None, None, "fixed")]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "update_order_fulfillment_status", side_effect=spy):
            response = asyncio.run(admin_app.picking_pack_order("order-1"))

        self.assertEqual(calls, [("order-1", "packed")])
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")

    def test_picking_to_packed_succeeds_and_deducts_inventory(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 555),
                None,
                (10,),
                ("packed",),
            ],
            fetchall_values=[[(2, None, None, "fixed")]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.picking_pack_order("order-1"))

        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")
        product_updates = cursor.updates_of("products")
        # [0] is the real stock deduction; [1] is the existing
        # out-of-stock-sync update that always runs afterward when any
        # product was affected (per_kg-only condition in its own WHERE
        # clause, unrelated to this fixed-mode order).
        self.assertEqual(product_updates[0][1], (9, 2))  # stock 10 -> 9
        self.assertIn("stock_quantity = %s", product_updates[0][0])
        self.assertTrue(connection.committed)

    def test_unpaid_cash_order_can_pack_normally(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, None),
                None,
                (10,),
                ("packed",),
            ],
            fetchall_values=[[(2, None, None, "fixed")]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.picking_pack_order("order-1"))
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")

    def test_insufficient_stock_leaves_order_in_picking_and_stock_unchanged(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 555),
                None,  # no pending weighing
                (0,),  # only 0 in stock, 1 required -> shortage
                ("picking",),  # wrapper's status check: still picking
            ],
            fetchall_values=[[(2, None, None, "fixed")]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.picking_pack_order("order-1"))

        self.assertNotIsInstance(response, RedirectResponse)
        self.assertIn("Недостаточно товара", response)
        self.assertEqual(cursor.updates_of("products"), [])
        fulfillment_updates = [
            (q, p) for q, p in cursor.updates_of("orders")
            if "fulfillment_status" in q
        ]
        self.assertEqual(fulfillment_updates, [])
        self.assertTrue(connection.rolled_back)

    def test_pending_weighing_prevents_pack(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 555),
                (1,),  # pending weighing found
                ("picking",),  # wrapper's status check: still picking
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.picking_pack_order("order-1"))

        self.assertNotIsInstance(response, RedirectResponse)
        self.assertIn("Требуется взвешивание", response)
        self.assertEqual(cursor.updates_of("products"), [])
        self.assertTrue(connection.rolled_back)


class WeighingWrapperTests(unittest.TestCase):
    """Requirement (section 5): weighing from the picking card reuses
    weigh_order_item unchanged and returns to the picking workspace on
    success; a validation error is shown as-is."""

    def test_successful_weigh_redirects_back_to_picking(self):
        cursor = ScriptedCursor(
            fetchone_values=[(24.0, "per_kg", 1, None)],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(
                admin_app.picking_weigh_order_item("order-1", 10, 350, None)
            )
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.headers["location"], "/picking")

    def test_invalid_weight_error_is_shown_as_is(self):
        response = asyncio.run(
            admin_app.picking_weigh_order_item("order-1", 10, 0, None)
        )
        self.assertNotIsInstance(response, RedirectResponse)
        self.assertIn("Некорректный вес", response)


class OrderDetailRegressionTests(unittest.TestCase):
    """Requirement 21: the normal /orders/{id} detail page (which the
    picking card links out to) still renders correctly -- untouched by
    this checkpoint."""

    def test_order_detail_still_renders(self):
        cursor = ScriptedCursor(
            fetchone_values=[(
                1, "42", "customer", "phone", "address", 0, "paid", "cash",
                None, None, None, None, None, False, None, "", "paid", "new", "telegram",
            )],
            fetchall_values=[
                [(10, "Product", 300, 5.0, None, 24.0, "per_kg", 24.0)],
                [],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.order_detail("42"))
        self.assertIn("Заказ 42", page)


if __name__ == "__main__":
    unittest.main()
