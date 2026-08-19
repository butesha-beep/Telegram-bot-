import asyncio
import os
import unittest
import urllib.parse
from unittest.mock import patch

from fastapi import Request


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/manual-order-admin")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

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


def _form_request(form_data, path="/orders/new"):
    body = urllib.parse.urlencode(form_data, doseq=True).encode("utf-8")
    raw_headers = [
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


def _get_request(path="/orders/new", query_string=""):
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
            "query_string": query_string.encode("ascii"),
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


# Row shape: id, name, pricing_mode, price_per_kg, fixed_price, sale_unit,
# category_id (category_id added for the New Order UX redesign, Checkpoint 1).
PER_KG_PRODUCT_ROW = (1, "Лосось", "per_kg", 24.0, None, None, 1)
FIXED_PRODUCT_ROW = (2, "Подарочный набор", "fixed", None, 15.0, "за упаковку", 2)
OPTIONS_PRODUCT_ROW = (3, "Рыба на вес", "options", None, None, None, 1)
OPTION_ROW = (10, 3, "Средняя 200-300г", 8.0)
CATEGORY_ROW_FISH = (1, "Рыба")
CATEGORY_ROW_SETS = (2, "Наборы")

FULL_CATALOG_PRODUCTS = [PER_KG_PRODUCT_ROW, FIXED_PRODUCT_ROW, OPTIONS_PRODUCT_ROW]
FULL_CATALOG_OPTIONS = [OPTION_ROW]
FULL_CATALOG_CATEGORIES = [CATEGORY_ROW_FISH, CATEGORY_ROW_SETS]


def _base_form(**overrides):
    form = {
        "source": "in_person",
        "source_reference": "",
        "customer_mode": "existing",
        "client_id": "5",
        "new_first_name": "",
        "new_last_name": "",
        "new_phone": "",
        "new_telegram_id": "",
        "client_query": "",
        "delivery_method": "pickup",
        "delivery_street": "",
        "delivery_house_number": "",
        "delivery_postcode": "",
        "delivery_city": "",
        "delivery_country": "",
        "delivery_notes": "",
        "payment_method": "Cash",
        "payment_status": "unpaid",
        "weight_1": "",
        "qty_2": "0",
        "optqty_10": "0",
    }
    form.update(overrides)
    return form


class AuthAndCsrfTests(unittest.TestCase):
    """Requirements 1/2/27: GET requires admin auth (same global
    middleware as every other admin route), POST requires CSRF (same
    per-route dependency as every other admin POST route), and the
    CSRF route inventory stays accurate (verified in
    tests/test_csrf_protection.py, which now includes /orders/new)."""

    def test_get_orders_new_requires_admin_auth(self):
        async def call_next(_request):
            raise AssertionError("handler must not run without authentication")

        response = asyncio.run(
            admin_app.require_admin_login(_get_request("/orders/new"), call_next)
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_post_orders_new_route_declares_csrf_dependency(self):
        found = False
        for route in admin_app.app.routes:
            if route.path == "/orders/new" and "POST" in getattr(route, "methods", set()):
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }
                self.assertIn("require_admin_csrf", dependency_names)
                found = True
        self.assertTrue(found, "POST /orders/new route was not registered")


class SourceAndReferenceTests(unittest.TestCase):
    """Requirements 3/4/19: source can be instagram (or any allowed
    channel), source_reference is stored, and the value is durable (not
    silently dropped)."""

    def test_instagram_source_and_reference_are_written(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("Ivan", "Petrov", "+31600000001", None, None),  # existing client lookup
                (101,),  # orders INSERT RETURNING id
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            source="instagram", source_reference="@dealmarket_nl", weight_1="200",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders/101")
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        params = order_insert[1]
        self.assertEqual(params[columns.index("source")], "instagram")
        self.assertEqual(params[columns.index("source_reference")], "@dealmarket_nl")


class CustomerResolutionTests(unittest.TestCase):
    """Requirements 5/6/7/8/20: existing customers are selected by
    clients.id; new customers can be created without a telegram_id;
    client_id links correctly either way; the customer_name/phone snapshot
    is captured as literal INSERT values (never a live join back to
    clients), so later editing the client row cannot rewrite historical
    order data; telegram_id may be NULL throughout."""

    def test_existing_client_selected_by_id_links_correctly(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("Ivan", "Petrov", "+31600000001", 555555, None),
                (101,),
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(client_id="5", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        select_client_queries = [
            (q, p) for q, p in cursor.queries if q.strip().startswith("SELECT") and "clients" in q
        ]
        self.assertTrue(any(p == (5,) for _q, p in select_client_queries))
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("client_id")], 5)
        self.assertEqual(order_insert[1][columns.index("customer_name")], "Ivan Petrov")
        self.assertEqual(order_insert[1][columns.index("telegram_id")], 555555)

    def test_new_client_created_without_telegram_id(self):
        cursor = ScriptedCursor(
            fetchone_values=[(77,), (102,)],  # clients INSERT RETURNING id, orders RETURNING id
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            customer_mode="new", new_first_name="Maria", new_last_name="",
            new_phone="+31611111111", new_telegram_id="", weight_1="150",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        client_insert = cursor.inserts_into("clients")[0]
        self.assertIsNone(client_insert[1][0])  # telegram_id column is first
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("client_id")], 77)
        self.assertIsNone(order_insert[1][columns.index("telegram_id")])

    def test_customer_snapshot_is_literal_values_not_a_live_join(self):
        # The order INSERT carries the client's name/phone as plain
        # parameter values, not a subquery/join back to clients -- so a
        # later UPDATE to the clients row can never change this order's
        # already-written customer_name/phone.
        cursor = ScriptedCursor(
            fetchone_values=[
                ("OldFirst", "OldLast", "+31600000000", None, None),
                (101,),
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(client_id="5", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        order_insert = cursor.inserts_into("orders")[0]
        self.assertNotIn("SELECT", order_insert[0])
        self.assertNotIn("clients", order_insert[0])
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("customer_name")], "OldFirst OldLast")
        self.assertEqual(order_insert[1][columns.index("phone")], "+31600000000")


class DeliveryTests(unittest.TestCase):
    """Requirements 9/10: pickup orders need no address fields; delivery
    orders validate their required structured fields."""

    def test_pickup_order_created_without_address_fields(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("Ivan", None, "+31600000001", None, None),
                (101,),
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(delivery_method="pickup", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertEqual(response.status_code, 303)
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("delivery_method")], "pickup")
        self.assertIsNone(order_insert[1][columns.index("delivery_street")])
        self.assertIsNone(order_insert[1][columns.index("address")])

    def test_delivery_order_missing_required_fields_is_rejected(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None)],  # existing client resolution
            fetchall_values=[
                FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES,  # catalog for the POST handler
                [(5, "Ivan", None, "+31600000001", None)],  # client search redisplay (id, first, last, phone, tg_id)
            ],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            delivery_method="delivery", delivery_street="", weight_1="200",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertIsInstance(response, str)
        self.assertIn("улицу", response)
        self.assertEqual(cursor.inserts_into("orders"), [])
        self.assertTrue(connection.rolled_back)

    def test_delivery_order_with_all_required_fields_succeeds(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("Ivan", None, "+31600000001", None, None),
                (101,),
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            delivery_method="delivery",
            delivery_street="Teststraat", delivery_house_number="12",
            delivery_postcode="1234 AB", delivery_city="Amsterdam",
            weight_1="200",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertEqual(response.status_code, 303)
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("delivery_street")], "Teststraat")
        self.assertEqual(order_insert[1][columns.index("delivery_city")], "Amsterdam")


class PricingModeLinesTests(unittest.TestCase):
    """Requirements 11/12/13/14: per_kg/fixed/options lines use the exact
    Commerce Foundation rules and a mixed-mode order totals correctly."""

    def test_mixed_mode_order_lines_and_total_are_correct(self):
        cursor = ScriptedCursor(
            fetchone_values=[
                ("Ivan", None, "+31600000001", None, None),
                (101,),
            ],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            weight_1="500", qty_2="2", optqty_10="1",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        item_inserts = cursor.inserts_into("order_items")
        # 1 per_kg line + 2 fixed units + 1 options unit = 4 rows.
        self.assertEqual(len(item_inserts), 4)

        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        total = order_insert[1][columns.index("total")]
        expected_total = (24.0 * 500 / 1000) + (15.0 * 2) + (8.0 * 1)
        self.assertAlmostEqual(total, expected_total)

    def test_per_kg_line_snapshots_current_price_per_kg(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(weight_1="500"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        item_insert = cursor.inserts_into("order_items")[0]
        # (order_id, product_id, product_name, weight, price, option_id, pricing_mode, price_per_kg_snapshot)
        self.assertEqual(item_insert[1][3], 500)
        self.assertEqual(item_insert[1][4], 24.0 * 500 / 1000)
        self.assertEqual(item_insert[1][6], "per_kg")
        self.assertEqual(item_insert[1][7], 24.0)

    def test_fixed_line_uses_fixed_price(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(qty_2="1"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        item_insert = cursor.inserts_into("order_items")[0]
        self.assertEqual(item_insert[1][4], 15.0)
        self.assertEqual(item_insert[1][6], "fixed")

    def test_options_line_uses_selected_option_price(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(optqty_10="1"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        item_insert = cursor.inserts_into("order_items")[0]
        self.assertEqual(item_insert[1][4], 8.0)
        self.assertEqual(item_insert[1][5], 10)  # option_id
        self.assertEqual(item_insert[1][6], "options")


class NoStockDeductionTests(unittest.TestCase):
    """Requirements 15/16: manual creation never deducts stock, whether
    payment_status is 'unpaid' or 'paid' -- deduction only ever happens at
    fulfillment_status='packed'."""

    def test_unpaid_creation_does_not_touch_inventory(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(payment_status="unpaid", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        for query, _params in cursor.queries:
            self.assertNotIn("inventory_movements", query)
            self.assertNotIn("UPDATE products", query)
            self.assertNotIn("UPDATE product_options", query)

    def test_paid_at_creation_still_does_not_deduct_stock(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(payment_status="paid", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        for query, _params in cursor.queries:
            self.assertNotIn("inventory_movements", query)
            self.assertNotIn("UPDATE products", query)
        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("payment_status")], "paid")
        self.assertEqual(order_insert[1][columns.index("fulfillment_status")], "new")


class InitialStateAndRefundedTests(unittest.TestCase):
    """Requirements 17/18: manual order starts fulfillment_status='new';
    the form never offers 'refunded' as an initial payment_status."""

    def test_starts_fulfillment_status_new(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        order_insert = cursor.inserts_into("orders")[0]
        columns = [c.strip() for c in order_insert[0].split("(", 1)[1].split(")", 1)[0].split(",")]
        self.assertEqual(order_insert[1][columns.index("fulfillment_status")], "new")

    def test_refunded_payment_status_is_rejected(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None)],  # existing client resolution
            fetchall_values=[
                FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES,
                [(5, "Ivan", None, "+31600000001", None)],  # (id, first, last, phone, tg_id)
            ],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(payment_status="refunded", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertIn("Недопустимый статус оплаты", response)
        self.assertEqual(cursor.inserts_into("orders"), [])

    def test_form_never_renders_refunded_as_a_payment_status_option(self):
        page = admin_app._render_new_order_form([], [], [], [], "")
        self.assertNotIn('value="refunded"', page)


class TelegramNullTests(unittest.TestCase):
    """Requirements 20/21: telegram_id may be NULL and the order is still
    created and reachable through the normal admin workflow (no special
    casing anywhere in insert_order/create_manual_order)."""

    def test_order_with_null_telegram_id_is_created_successfully(self):
        cursor = ScriptedCursor(
            fetchone_values=[(77,), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(
            customer_mode="new", new_first_name="Maria", new_phone="+31611111111",
            weight_1="200",
        ))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertEqual(response.status_code, 303)
        self.assertTrue(connection.committed)


class OrderEventTests(unittest.TestCase):
    """Requirement 22: order_created event is written with human-readable,
    non-sensitive context (source + manual-creation marker)."""

    def test_order_created_event_is_logged(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (101,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(source="whatsapp", weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(admin_app.create_manual_order(request))

        events = cursor.inserts_into("order_events")
        self.assertEqual(len(events), 1)
        order_id, event_type, event_text = events[0][1]
        self.assertEqual(order_id, 101)
        self.assertEqual(event_type, "order_created")
        self.assertIn("whatsapp", event_text)
        self.assertNotIn("+31600000001", event_text)  # no raw phone in the log text


class RedirectTests(unittest.TestCase):
    """Requirement 23: a successfully created order redirects to the
    normal order detail page."""

    def test_redirects_to_order_detail(self):
        cursor = ScriptedCursor(
            fetchone_values=[("Ivan", None, "+31600000001", None, None), (555,)],
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        connection = ScriptedConnection(cursor)
        request = _form_request(_base_form(weight_1="200"))
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.create_manual_order(request))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/orders/555")


class FriendlyOrderNumberTests(unittest.TestCase):
    """Requirement 10: friendly display form for the new admin UI."""

    def test_format_order_number(self):
        self.assertEqual(admin_app.format_order_number(127), "DM-000127")
        self.assertEqual(admin_app.format_order_number(1), "DM-000001")


# ---------------------------------------------------------------------------
# New Order UX redesign -- Checkpoint 1 (backend preparation only: category
# data on the catalog read, telegram_id in client search, and the read-only
# client-autocomplete endpoint/renderer). No picker/autocomplete JS and no
# _render_new_order_form changes exist yet -- those are later checkpoints.
# ---------------------------------------------------------------------------

class ManualOrderCatalogCategoriesTests(unittest.TestCase):
    """_manual_order_catalog now also returns active categories, and the
    products query now includes category_id -- both purely additive reads
    for the future category/search product picker."""

    def test_catalog_returns_categories_as_third_element(self):
        cursor = ScriptedCursor(
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        products, options, categories = admin_app._manual_order_catalog(cursor)
        self.assertEqual(products, FULL_CATALOG_PRODUCTS)
        self.assertEqual(options, FULL_CATALOG_OPTIONS)
        self.assertEqual(categories, FULL_CATALOG_CATEGORIES)

    def test_products_query_selects_category_id(self):
        cursor = ScriptedCursor(
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        admin_app._manual_order_catalog(cursor)
        products_query = cursor.queries[0][0]
        self.assertIn("category_id", products_query)

    def test_categories_query_uses_same_availability_semantics_as_storefront(self):
        # Same WHERE is_active = TRUE / ORDER BY sort_order, id semantics
        # already used by the customer-facing storefront catalog query --
        # not a new/invented ordering rule.
        cursor = ScriptedCursor(
            fetchall_values=[FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES],
        )
        admin_app._manual_order_catalog(cursor)
        categories_query = cursor.queries[2][0]
        self.assertIn("FROM categories", categories_query)
        self.assertIn("WHERE is_active = TRUE", categories_query)
        self.assertIn("ORDER BY sort_order, id", categories_query)


class ManualOrderClientSearchTelegramIdTests(unittest.TestCase):
    """Requirement: client search must also match Telegram ID, not just
    name/phone (it was displayed but not searchable before)."""

    def test_search_query_matches_telegram_id_too(self):
        cursor = ScriptedCursor(fetchall_values=[[]])
        admin_app._search_clients_for_manual_order(cursor, "555")
        query, params = cursor.queries[0]
        self.assertIn("telegram_id", query)
        self.assertEqual(params, ("%555%", "%555%", "%555%", "%555%"))

    def test_empty_query_still_lists_recent_clients_unfiltered(self):
        cursor = ScriptedCursor(fetchall_values=[[]])
        admin_app._search_clients_for_manual_order(cursor, "")
        query, params = cursor.queries[0]
        self.assertNotIn("WHERE", query)
        self.assertIsNone(params)


class ManualOrderClientSearchResultsHtmlTests(unittest.TestCase):
    """Fragment renderer for the new autocomplete endpoint -- data-*
    attributes are the contract the (later checkpoint's) JS will read."""

    def test_empty_results_render_a_clear_empty_state(self):
        html_out = admin_app._manual_order_client_search_results_html([])
        self.assertIn("не найдены", html_out)
        self.assertNotIn("<button", html_out)

    def test_result_carries_expected_data_attributes(self):
        clients = [(5, "Ivan", "Petrov", "+31600000001", 555)]
        html_out = admin_app._manual_order_client_search_results_html(clients)
        self.assertIn('data-client-id="5"', html_out)
        self.assertIn('data-client-name="Ivan Petrov"', html_out)
        self.assertIn('data-client-phone="+31600000001"', html_out)
        self.assertIn('data-client-telegram-id="555"', html_out)
        self.assertIn("Telegram ID: 555", html_out)

    def test_client_without_telegram_id_omits_telegram_id_text(self):
        clients = [(6, "Olga", None, "+31600000002", None)]
        html_out = admin_app._manual_order_client_search_results_html(clients)
        self.assertIn('data-client-telegram-id=""', html_out)
        self.assertNotIn("Telegram ID:", html_out)

    def test_escapes_client_supplied_text(self):
        clients = [(7, "<script>", None, "+31600000003", None)]
        html_out = admin_app._manual_order_client_search_results_html(clients)
        self.assertNotIn("<script>", html_out)
        self.assertIn("&lt;script&gt;", html_out)


class ManualOrderClientSearchRouteTests(unittest.TestCase):
    """GET /orders/new/clients: read-only, admin-auth-gated (via the
    existing global middleware, no per-route code), no CSRF (GET), returns
    a small fragment rather than a full admin_layout() page."""

    def test_requires_admin_auth(self):
        async def call_next(_request):
            raise AssertionError("handler must not run without authentication")

        response = asyncio.run(
            admin_app.require_admin_login(
                _get_request("/orders/new/clients", query_string="q=ivan"), call_next
            )
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_route_declares_no_csrf_dependency(self):
        found = False
        for route in admin_app.app.routes:
            if route.path == "/orders/new/clients" and "GET" in getattr(route, "methods", set()):
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }
                self.assertNotIn("require_admin_csrf", dependency_names)
                found = True
        self.assertTrue(found, "GET /orders/new/clients route was not registered")

    def test_returns_matching_clients_fragment(self):
        cursor = ScriptedCursor(
            fetchall_values=[[(5, "Ivan", "Petrov", "+31600000001", 555)]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.new_order_client_search("ivan"))

        self.assertEqual(response.status_code, 200)
        body = response.body.decode("utf-8")
        self.assertIn('data-client-id="5"', body)
        self.assertTrue(connection.closed)

    def test_no_query_still_returns_a_fragment_not_a_full_page(self):
        cursor = ScriptedCursor(fetchall_values=[[]])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.new_order_client_search(""))

        body = response.body.decode("utf-8")
        self.assertNotIn("<html", body)
        self.assertIn("не найдены", body)

    def test_query_failure_degrades_gracefully_instead_of_500(self):
        class ExplodingCursor(ScriptedCursor):
            def execute(self, query, params=None):
                raise RuntimeError("db unavailable")

        connection = ScriptedConnection(ExplodingCursor())
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.new_order_client_search("ivan"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("Не удалось", response.body.decode("utf-8"))


# ---------------------------------------------------------------------------
# New Order UX redesign -- Checkpoint 2 (workspace layout, category/search
# product picker, order summary). Client autocomplete and delivery/payment
# redesign are explicitly deferred to a later checkpoint -- not tested here
# because they were not touched.
# ---------------------------------------------------------------------------

def _uncategorized_product_row(product_id=99, name="Пробный товар"):
    return (product_id, name, "fixed", None, 9.5, "шт", None)


class CategoryChipsAreDatabaseDrivenTests(unittest.TestCase):
    """Requirement 2: category chips come only from the categories query
    result -- nothing hardcoded, and the uncategorized chip only appears
    when a product genuinely has no category."""

    def test_chips_render_exactly_the_given_categories(self):
        html_out = admin_app._manual_order_category_chips_html(FULL_CATALOG_CATEGORIES, False)
        self.assertIn(">Все<", html_out)
        self.assertIn('data-category-filter="1"', html_out)
        self.assertIn("Рыба", html_out)
        self.assertIn('data-category-filter="2"', html_out)
        self.assertIn("Наборы", html_out)
        # Proves nothing is hardcoded: a category name never supplied by
        # the (fake, in this test) DB must never appear.
        self.assertNotIn("Рыбные снеки", html_out)
        self.assertNotIn("Мясные снеки", html_out)

    def test_uncategorized_chip_only_appears_when_flagged(self):
        with_flag = admin_app._manual_order_category_chips_html(FULL_CATALOG_CATEGORIES, True)
        without_flag = admin_app._manual_order_category_chips_html(FULL_CATALOG_CATEGORIES, False)
        self.assertIn("Без категории", with_flag)
        self.assertNotIn("Без категории", without_flag)

    def test_empty_categories_still_renders_the_all_chip(self):
        html_out = admin_app._manual_order_category_chips_html([], False)
        self.assertIn(">Все<", html_out)


class ProductCardCategoryMetadataTests(unittest.TestCase):
    """Requirement: each product card must carry the correct
    data-category-id so client-side filtering can match it against the
    active category chip."""

    def test_cards_carry_their_own_category_id(self):
        cards_html, has_uncategorized = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, {}
        )
        self.assertIn('data-category-id="1"', cards_html)  # PER_KG_PRODUCT_ROW, OPTIONS_PRODUCT_ROW
        self.assertIn('data-category-id="2"', cards_html)  # FIXED_PRODUCT_ROW
        self.assertFalse(has_uncategorized)

    def test_uncategorized_product_gets_none_marker_and_sets_flag(self):
        products = FULL_CATALOG_PRODUCTS + [_uncategorized_product_row()]
        cards_html, has_uncategorized = admin_app._manual_order_catalog_picker_html(
            products, FULL_CATALOG_OPTIONS, {}
        )
        self.assertIn('data-category-id="none"', cards_html)
        self.assertTrue(has_uncategorized)


class PickerPreservesExistingFieldContractTests(unittest.TestCase):
    """Requirement: the redesigned picker must keep emitting the exact
    same weight_{product_id}/qty_{product_id}/optqty_{option_id} field
    names POST /orders/new already parses -- only the surrounding
    presentation changed."""

    def test_per_kg_field_name_and_dom_id_match_and_carry_line_kind(self):
        cards_html, _ = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, {}
        )
        self.assertIn('id="weight_1"', cards_html)
        self.assertIn('name="weight_1"', cards_html)
        self.assertIn('data-line-kind="weight"', cards_html)

    def test_fixed_field_name_and_dom_id_match_and_carry_line_kind(self):
        cards_html, _ = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, {}
        )
        self.assertIn('id="qty_2"', cards_html)
        self.assertIn('name="qty_2"', cards_html)
        self.assertIn('data-line-kind="qty"', cards_html)

    def test_option_field_name_is_associated_with_its_own_product_only(self):
        cards_html, _ = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, {}
        )
        self.assertIn('id="optqty_10"', cards_html)
        self.assertIn('name="optqty_10"', cards_html)
        self.assertIn('data-line-kind="optqty"', cards_html)
        # OPTION_ROW belongs to product_id=3 (OPTIONS_PRODUCT_ROW) -- its
        # combined label must reference that product's own name, proving
        # the option wasn't misattributed to a different product.
        self.assertIn('data-label="Рыба на вес — Средняя 200-300г"', cards_html)

    def test_options_product_with_no_options_is_silently_skipped_not_broken(self):
        products = [(50, "Без вариантов", "options", None, None, None, 1)]
        cards_html, _ = admin_app._manual_order_catalog_picker_html(products, [], {})
        self.assertNotIn("Без вариантов", cards_html)


class PickerErrorRedisplayTests(unittest.TestCase):
    """Requirement 8: previously entered weight/quantity values must
    survive a validation-error redisplay, in the new card markup."""

    def test_previously_entered_values_are_redisplayed_in_the_right_inputs(self):
        form_values = {"weight_1": "350", "qty_2": "3", "optqty_10": "2"}
        cards_html, _ = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, form_values
        )
        self.assertIn('id="weight_1" name="weight_1"', cards_html)
        self.assertIn('value="350"', cards_html)
        self.assertIn('value="3"', cards_html)
        self.assertIn('value="2"', cards_html)

    def test_invalid_stored_quantity_falls_back_to_zero_not_a_crash(self):
        form_values = {"qty_2": "not-a-number"}
        cards_html, _ = admin_app._manual_order_catalog_picker_html(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, form_values
        )
        self.assertIn('id="qty_2" name="qty_2"', cards_html)


class RenderNewOrderFormWorkspaceStructureTests(unittest.TestCase):
    """Integration-level: the full page includes the new workspace shell,
    still as exactly one POST form so submission behavior is unchanged."""

    def test_page_contains_workspace_and_summary_structure(self):
        page = admin_app._render_new_order_form(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES, [], ""
        )
        self.assertIn('id="manualOrderForm"', page)
        self.assertIn('class="order-workspace"', page)
        self.assertIn('class="category-chips"', page)
        self.assertIn('id="productGrid"', page)
        self.assertIn('id="orderSummaryPanel"', page)
        self.assertIn('id="orderSummaryLines"', page)
        self.assertIn("Добавьте товары", page)
        self.assertEqual(
            page.count('<form class="order-workspace-form" method="post" action="/orders/new"'),
            1,
        )
        self.assertIn("<script>", page)

    def test_client_selection_functionality_is_unchanged_not_autocomplete_yet(self):
        # Checkpoint 2 explicitly does not implement client autocomplete --
        # the existing radio-based client_id selection must still render.
        page = admin_app._render_new_order_form(
            FULL_CATALOG_PRODUCTS, FULL_CATALOG_OPTIONS, FULL_CATALOG_CATEGORIES,
            [(5, "Ivan", "Petrov", "+31600000001", 555)], "",
        )
        self.assertIn('type="radio" name="client_id" value="5"', page)


class SharedOrderCoreUnmodifiedTests(unittest.TestCase):
    """Requirement: no automatic Telegram/site order flow may be touched.
    bot.py and admin_app.py must still import and call the exact same
    order_creation functions -- proves this checkpoint's UI-only changes
    never forked pricing/order-writing logic for the manual-order path."""

    def test_bot_and_admin_still_share_the_same_pricing_and_insert_functions(self):
        import bot
        import order_creation
        self.assertIs(bot.price_single_line, order_creation.price_single_line)
        self.assertIs(admin_app.price_single_line, order_creation.price_single_line)
        self.assertIs(bot.insert_order, order_creation.insert_order)
        self.assertIs(admin_app.insert_order, order_creation.insert_order)


if __name__ == "__main__":
    unittest.main()
