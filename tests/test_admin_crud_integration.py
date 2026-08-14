import asyncio
import os
import unittest
from urllib.parse import urlencode, urlsplit
from unittest.mock import patch

import psycopg2

import admin_app
import db_schema
import storefront


TEST_URL = os.getenv("ADMIN_CRUD_TEST_URL")


async def asgi_request(app, method, path, form=None):
    body = urlencode(form or {}, doseq=True).encode("utf-8")
    headers = [(b"host", b"testserver")]
    if body:
        headers.extend([
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode("ascii")),
        ])
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    messages = []
    request_sent = False

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, response_body.decode("utf-8")


@unittest.skipUnless(TEST_URL, "disposable admin CRUD PostgreSQL URL is not configured")
class AdminCrudIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parsed = urlsplit(TEST_URL)
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise AssertionError("CRUD target must be local")
        database = parsed.path.lstrip("/").lower()
        if "disposable" not in database or "test" not in database:
            raise AssertionError("CRUD target must be an explicit disposable test database")
        cls.url = TEST_URL
        with cls.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != parsed.path.lstrip("/"):
                    raise AssertionError("connected to an unexpected CRUD database")
                cursor.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM categories),
                        (SELECT COUNT(*) FROM products),
                        (SELECT COUNT(*) FROM product_options),
                        obj_description('public.categories'::regclass, 'pg_class')
                """)
                if cursor.fetchone() != (7, 17, 0, "dealmarket-imported-catalog-v1"):
                    raise AssertionError("CRUD database is not the verified preview clone")

    @classmethod
    def tearDownClass(cls):
        with cls.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM inventory_movements WHERE product_id >= 25")
                cursor.execute("DELETE FROM product_options WHERE product_id >= 25")
                cursor.execute("DELETE FROM products WHERE id >= 25")

    @classmethod
    def connect(cls):
        return psycopg2.connect(
            cls.url,
            connect_timeout=10,
            options="-c statement_timeout=10000 -c lock_timeout=3000",
        )

    def admin_request(self, method, path, form=None):
        with patch.object(admin_app, "DATABASE_URL", self.url):
            with patch.object(admin_app, "DATABASE_READY", True):
                with patch.object(admin_app, "is_admin_authenticated", return_value=True):
                    return asyncio.run(asgi_request(admin_app.app, method, path, form))

    def storefront_page(self):
        with patch.dict(os.environ, {"DATABASE_URL": self.url}):
            with patch.object(db_schema, "get_db_connection", self.connect):
                status, page = asyncio.run(asgi_request(admin_app.app, "GET", "/shop"))
        self.assertEqual(status, 200)
        return page

    def product_row(self, product_id):
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT name, price_per_kg, pricing_mode, fixed_price, sale_unit,
                           unit_weight_grams, stock_quantity, stock_grams, is_active
                    FROM products WHERE id = %s
                """, (product_id,))
                return cursor.fetchone()

    def test_01_per_kg_create_edit_toggle_and_validation(self):
        status, page = self.admin_request("POST", "/products/new", {
            "category_id": "1", "name": "B8 весовой товар",
            "pricing_mode": "per_kg", "price_per_kg": "12.50",
            "unit_weight_grams": "250", "stock_grams": "2000",
            "low_stock_threshold_grams": "0", "sort_order": "90",
            "is_active": "1",
        })
        self.assertEqual(status, 200)
        self.assertIn("Товар создан", page)
        self.assertEqual(self.product_row(25)[2], "per_kg")
        self.assertIn("B8 весовой товар", self.storefront_page())

        status, _ = self.admin_request("POST", "/products/25/edit", {
            "category_id": "1", "name": "B8 весовой товар обновлён",
            "pricing_mode": "per_kg", "price_per_kg": "14.75",
            "unit_weight_grams": "300", "stock_grams": "1750",
            "low_stock_threshold_grams": "0", "sort_order": "90",
            "is_active": "1",
        })
        self.assertEqual(status, 200)
        row = self.product_row(25)
        self.assertEqual(row[0], "B8 весовой товар обновлён")
        self.assertAlmostEqual(row[1], 14.75)
        self.assertEqual(row[7], 1750)

        self.admin_request("POST", "/products/25/deactivate")
        self.assertNotIn("B8 весовой товар обновлён", self.storefront_page())
        self.admin_request("POST", "/products/25/activate")
        self.assertIn("B8 весовой товар обновлён", self.storefront_page())

        before = self.product_row(25)
        for price, stock in (("-1", "1750"), ("14.75", "-1")):
            status, error_page = self.admin_request("POST", "/products/25/edit", {
                "category_id": "1", "name": "Недопустимое изменение",
                "pricing_mode": "per_kg", "price_per_kg": price,
                "stock_grams": stock, "low_stock_threshold_grams": "0",
                "sort_order": "90", "is_active": "1",
            })
            self.assertEqual(status, 200)
            self.assertIn("Некорректная цена", error_page)
            self.assertEqual(self.product_row(25), before)

    def test_02_fixed_create_render_and_validation(self):
        status, _ = self.admin_request("POST", "/products/new", {
            "category_id": "1", "name": "B8 фиксированный товар",
            "pricing_mode": "fixed", "fixed_price": "4.50",
            "sale_unit": "за штуку", "stock_quantity": "3",
            "stock_grams": "999", "low_stock_threshold_grams": "0",
            "sort_order": "91", "is_active": "1",
        })
        self.assertEqual(status, 200)
        row = self.product_row(26)
        self.assertEqual(row[2:8], ("fixed", 4.5, "за штуку", None, 3, 0))
        card = self.storefront_page().split("B8 фиксированный товар", 1)[1].split("</article>", 1)[0]
        self.assertIn("4.50", card)
        self.assertIn("за штуку", card)

        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM products")
                before_count = cursor.fetchone()[0]
        invalid_forms = (
            {"fixed_price": "", "sale_unit": "за штуку", "stock_quantity": "3"},
            {"fixed_price": "4.50", "sale_unit": "", "stock_quantity": "3"},
            {"fixed_price": "-1", "sale_unit": "за штуку", "stock_quantity": "3"},
            {"fixed_price": "4.50", "sale_unit": "за штуку", "stock_quantity": "-1"},
        )
        for invalid in invalid_forms:
            form = {"category_id": "1", "name": "Не создавать", "pricing_mode": "fixed", "is_active": "1", **invalid}
            _, error_page = self.admin_request("POST", "/products/new", form)
            self.assertIn("Некорректная цена", error_page)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM products")
                self.assertEqual(cursor.fetchone()[0], before_count)
        with self.assertRaisesRegex(ValueError, "только для режима per_kg"):
            admin_app.validate_weight_inventory_modes([(26, 1, "fixed")])
        self.assertEqual(self.product_row(26), row)

    def test_03_options_variants_mode_switching_and_validation(self):
        self.admin_request("POST", "/products/new", {
            "category_id": "1", "name": "B8 товар с вариантами",
            "pricing_mode": "options", "stock_grams": "999",
            "low_stock_threshold_grams": "0", "sort_order": "92",
            "is_active": "1",
        })
        page = self.storefront_page()
        self.assertNotIn("B8 товар с вариантами", page)

        self.admin_request("POST", "/products/27/options/new", {
            "label": "Малая упаковка", "weight": "150", "price": "3.25",
            "stock_quantity": "4", "sort_order": "1", "is_active": "1",
        })
        self.admin_request("POST", "/products/27/options/new", {
            "label": "Большая упаковка", "weight": "400", "price": "7.90",
            "stock_quantity": "0", "sort_order": "2", "is_active": "1",
        })
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, label, weight, price, stock_quantity FROM product_options WHERE product_id = 27 ORDER BY id")
                option_rows = cursor.fetchall()
        self.assertEqual(len(option_rows), 2)
        self.assertEqual(option_rows[0][1:], ("Малая упаковка", 150, 3.25, 4))
        page = self.storefront_page()
        card = page.split("B8 товар с вариантами", 1)[1].split("</article>", 1)[0]
        self.assertIn("Малая упаковка", card)
        self.assertIn("Большая упаковка", card)
        self.assertIn("3.25", card)

        first_option_id = option_rows[0][0]
        self.admin_request("POST", f"/options/{first_option_id}/edit", {
            "label": "Малая упаковка XL", "weight": "175", "price": "3.75",
            "stock_quantity": "5", "sort_order": "1", "is_active": "1",
        })
        page = self.storefront_page()
        self.assertIn("Малая упаковка XL", page)

        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM product_options WHERE product_id = 27")
                before_count = cursor.fetchone()[0]
        invalid_options = (
            {"label": "", "weight": "100", "price": "2", "stock_quantity": "1"},
            {"label": "Вариант", "weight": "0", "price": "2", "stock_quantity": "1"},
            {"label": "Вариант", "weight": "100", "price": "-2", "stock_quantity": "1"},
            {"label": "Вариант", "weight": "100", "price": "2", "stock_quantity": "-1"},
        )
        for invalid in invalid_options:
            _, error_page = self.admin_request("POST", "/products/27/options/new", {**invalid, "is_active": "1"})
            self.assertIn("Некорректный вариант", error_page)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM product_options WHERE product_id = 27")
                self.assertEqual(cursor.fetchone()[0], before_count)

        self.admin_request("POST", "/products/27/edit", {
            "category_id": "1", "name": "B8 товар переключён в fixed",
            "pricing_mode": "fixed", "fixed_price": "9.50",
            "sale_unit": "за набор", "stock_quantity": "2",
            "stock_grams": "0", "low_stock_threshold_grams": "0",
            "sort_order": "92", "is_active": "1",
        })
        fixed_page = self.storefront_page()
        fixed_card = fixed_page.split("B8 товар переключён в fixed", 1)[1].split("</article>", 1)[0]
        self.assertIn("9.50", fixed_card)
        self.assertNotIn("Малая упаковка XL", fixed_card)
        self.assertNotIn("Большая упаковка", fixed_card)
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM product_options WHERE product_id = 27")
                self.assertEqual(cursor.fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
