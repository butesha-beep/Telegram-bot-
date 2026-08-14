import asyncio
import html
import os
import re
import unittest
from decimal import Decimal
from html.parser import HTMLParser
from unittest.mock import patch

from fastapi import Request
from fastapi.responses import PlainTextResponse


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/storefront")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app
import storefront


def make_request(path):
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    })


def response_text(response):
    return response.body.decode("utf-8")


class PriceLineTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_price_line = False
        self.price_lines = []
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "").split()
        if tag == "p" and "price-line" in classes:
            self.in_price_line = True
            self.current_text = []

    def handle_endtag(self, tag):
        if tag == "p" and self.in_price_line:
            self.price_lines.append(
                " ".join(
                    html.unescape(" ".join(self.current_text))
                    .replace("\xa0", " ")
                    .split()
                )
            )
            self.in_price_line = False

    def handle_data(self, data):
        if self.in_price_line:
            self.current_text.append(data)


class FakeCursor:
    def __init__(self, datasets):
        self.datasets = list(datasets)
        self.queries = []
        self.closed = False

    def execute(self, query):
        self.queries.append(query)

    def fetchall(self):
        return self.datasets.pop(0)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, datasets):
        self.cursor_instance = FakeCursor(datasets)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class StorefrontTests(unittest.TestCase):
    def setUp(self):
        self.original_database_ready = admin_app.DATABASE_READY

    def tearDown(self):
        admin_app.DATABASE_READY = self.original_database_ready

    def sample_catalog(self):
        category_rows = [
            (1, "Активная <категория>", 2, True),
            (2, "Скрытая категория", 1, False),
        ]
        product_rows = [
            (10, 1, "Активный <товар>", "Описание <script>alert(1)</script>", 0, "javascript:alert(1)", False, 500, 2, True, "options", None, None, None, None),
            (11, 1, "Скрытый товар", "Не показывать", 0, "", False, 500, 1, False, "fixed", 99, "за упаковку", None, 10),
            (12, 2, "Товар скрытой категории", "Не показывать", 88, "", False, 500, 1, True, "per_kg", None, None, 250, None),
            (13, 1, "Нет на складе", "Остаток равен нулю", 30, "", False, 0, 3, True, "per_kg", None, None, 300, None),
            (14, 1, "Нет по статусу", "Установлен флаг отсутствия", 0, "", True, 0, 4, True, "fixed", 32, "за упаковку", None, 10),
            (15, 1, "Фиксированный товар", "Одна цена", 0, "", False, 0, 5, True, "fixed", 8.5, "за штуку", None, 5),
            (16, 1, "Весовой товар", "Цена зависит от веса", 24, "", False, 500, 6, True, "per_kg", None, None, 300, None),
        ]
        option_rows = [
            (100, 10, "100 <b>г</b>", 100, 2.5, 2, True, 4, False),
            (101, 10, "Скрытый вариант", 200, 4.5, 1, False, 3, False),
            (102, 11, "Вариант скрытого товара", 100, 9.5, 1, True, 2, False),
            (103, 10, "250 г", 250, 5.5, 3, True, 0, False),
        ]
        return storefront.assemble_catalog(category_rows, product_rows, option_rows)

    def test_shop_route_is_registered_and_public(self):
        self.assertIn("/shop", {getattr(route, "path", None) for route in admin_app.app.routes})
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return PlainTextResponse("shop")

        response = asyncio.run(admin_app.require_admin_login(make_request("/shop"), call_next))
        self.assertTrue(called)
        self.assertEqual(response.status_code, 200)

    def test_admin_pricing_normalization_keeps_modes_separate(self):
        fixed = admin_app.normalize_product_pricing(
            "fixed", "99", "12.50", "за упаковку", "500", "7"
        )
        per_kg = admin_app.normalize_product_pricing(
            "per_kg", "31.25", "12.50", "за упаковку", "300", "7"
        )
        options = admin_app.normalize_product_pricing(
            "options", "31.25", "12.50", "за упаковку", "300", "7"
        )

        self.assertEqual(fixed, ("fixed", 0.0, 12.5, "за упаковку", None, 7))
        self.assertEqual(per_kg, ("per_kg", 31.25, None, None, 300, None))
        self.assertEqual(options, ("options", 0.0, None, None, None, None))

    def test_per_kg_price_dom_is_checked_semantically(self):
        catalog = [{
            "id": 1,
            "name": "Category",
            "sort_order": 1,
            "products": [{
                "id": 19,
                "category_id": 1,
                "name": "Product 19",
                "description": "",
                "price_per_kg": 35.0,
                "image_url": "",
                "is_out_of_stock": False,
                "stock_grams": 1000,
                "sort_order": 1,
                "pricing_mode": "per_kg",
                "fixed_price": None,
                "sale_unit": None,
                "unit_weight_grams": None,
                "stock_quantity": None,
                "options": [],
            }],
        }]
        page = storefront.render_catalog_page(
            catalog,
            currency_symbol="\u20ac",
            support_username="",
        )
        parser = PriceLineTextParser()
        parser.feed(page)

        self.assertEqual(len(parser.price_lines), 1)
        visible_price = parser.price_lines[0]
        number = re.search(r"\d+(?:[.,]\d+)?", visible_price)
        self.assertIsNotNone(number)
        self.assertEqual(
            Decimal(number.group(0).replace(",", ".")),
            Decimal("35"),
        )
        self.assertIn("\u20ac", visible_price)
        self.assertIn("\u0437\u0430 \u043a\u0433", visible_price.lower())

    def test_admin_pricing_normalization_rejects_incomplete_mode(self):
        with self.assertRaisesRegex(ValueError, "фиксированная цена"):
            admin_app.normalize_product_pricing(
                "fixed", "", "", "за штуку", "", ""
            )

    def test_admin_route_remains_protected(self):
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return PlainTextResponse("admin")

        response = asyncio.run(admin_app.require_admin_login(make_request("/"), call_next))
        self.assertFalse(called)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_database_startup_success_marks_database_ready(self):
        with patch.object(admin_app, "DATABASE_URL", "postgresql://configured/test"):
            with patch.object(admin_app, "init_db") as init_db:
                with patch.object(admin_app.logger, "exception") as log_exception:
                    admin_app.DATABASE_READY = False
                    asyncio.run(admin_app.startup_db_init())

        init_db.assert_called_once_with()
        log_exception.assert_not_called()
        self.assertTrue(admin_app.DATABASE_READY)

    def test_missing_database_url_skips_connection_and_stays_unavailable(self):
        with patch.object(admin_app, "DATABASE_URL", None):
            with patch.object(admin_app, "init_db") as init_db:
                with patch.object(admin_app.logger, "error") as log_error:
                    admin_app.DATABASE_READY = True
                    asyncio.run(admin_app.startup_db_init())

        init_db.assert_not_called()
        log_error.assert_called_once_with("Database is not configured")
        self.assertFalse(admin_app.DATABASE_READY)

    def test_operational_database_error_is_logged_and_does_not_stop_startup(self):
        connection_error = admin_app.psycopg2.OperationalError("connection refused")
        with patch.object(admin_app, "DATABASE_URL", "postgresql://configured/test"):
            with patch.object(admin_app, "init_db", side_effect=connection_error):
                with patch.object(admin_app.logger, "exception") as log_exception:
                    admin_app.DATABASE_READY = True
                    asyncio.run(admin_app.startup_db_init())

        log_exception.assert_called_once_with(
            "PostgreSQL is unavailable during database initialization"
        )
        self.assertFalse(admin_app.DATABASE_READY)

    def test_unexpected_startup_error_is_logged_and_raised(self):
        with patch.object(admin_app, "DATABASE_URL", "postgresql://configured/test"):
            with patch.object(admin_app, "init_db", side_effect=RuntimeError("programming defect")):
                with patch.object(admin_app.logger, "exception") as log_exception:
                    admin_app.DATABASE_READY = True
                    with self.assertRaisesRegex(RuntimeError, "programming defect"):
                        asyncio.run(admin_app.startup_db_init())

        log_exception.assert_called_once_with("Unexpected database initialization failure")
        self.assertFalse(admin_app.DATABASE_READY)

    def test_sql_or_schema_error_is_not_classified_as_connectivity_failure(self):
        schema_error = admin_app.psycopg2.ProgrammingError("invalid schema")
        with patch.object(admin_app, "DATABASE_URL", "postgresql://configured/test"):
            with patch.object(admin_app, "init_db", side_effect=schema_error):
                with patch.object(admin_app.logger, "exception") as log_exception:
                    with self.assertRaises(admin_app.psycopg2.ProgrammingError):
                        asyncio.run(admin_app.startup_db_init())

        log_exception.assert_called_once_with("Unexpected database initialization failure")
        self.assertFalse(admin_app.DATABASE_READY)

    def test_authorized_admin_route_returns_503_when_database_is_unavailable(self):
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return PlainTextResponse("admin")

        with patch.object(admin_app, "DATABASE_READY", False):
            with patch.object(admin_app, "is_admin_authenticated", return_value=True):
                response = asyncio.run(
                    admin_app.require_admin_login(make_request("/products"), call_next)
                )

        self.assertFalse(called)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("postgresql://", response_text(response))

    def test_authorized_master_route_returns_503_when_database_is_unavailable(self):
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return PlainTextResponse("master")

        with patch.object(admin_app, "DATABASE_READY", False):
            with patch.object(admin_app, "is_master_authenticated", return_value=True):
                response = asyncio.run(
                    admin_app.require_admin_login(make_request("/master"), call_next)
                )

        self.assertFalse(called)
        self.assertEqual(response.status_code, 503)

    def test_authorized_admin_route_continues_when_database_is_ready(self):
        called = False

        async def call_next(_request):
            nonlocal called
            called = True
            return PlainTextResponse("admin")

        with patch.object(admin_app, "DATABASE_READY", True):
            with patch.object(admin_app, "is_admin_authenticated", return_value=True):
                response = asyncio.run(
                    admin_app.require_admin_login(make_request("/products"), call_next)
                )

        self.assertTrue(called)
        self.assertEqual(response.status_code, 200)

    def test_only_active_catalog_entries_are_rendered(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        self.assertIn("Активный", page)
        self.assertNotIn("Скрытый товар", page)
        self.assertNotIn("Скрытая категория", page)
        self.assertNotIn("Товар скрытой категории", page)
        self.assertNotIn("Скрытый вариант", page)

    def test_empty_categories_are_not_rendered(self):
        catalog = self.sample_catalog()
        catalog.append({
            "id": 99,
            "name": "Пустая публичная категория",
            "sort_order": 99,
            "products": [],
        })
        page = storefront.render_catalog_page(catalog)
        self.assertNotIn("Пустая публичная категория", page)
        self.assertNotIn("В этой категории пока нет доступных товаров", page)

    def test_prices_and_active_variant_are_rendered(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        self.assertIn("24.00 EUR", page)
        self.assertIn("2.50 EUR", page)
        self.assertIn("100 &lt;b&gt;г&lt;/b&gt;", page)
        self.assertNotIn("Скрытый вариант", page)

    def test_fixed_price_has_sale_unit_and_no_per_kg_label(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        fixed_card = page.split("Фиксированный товар", 1)[1].split("</article>", 1)[0]
        self.assertIn("8.50 EUR", fixed_card)
        self.assertIn("за штуку", fixed_card)
        self.assertNotIn("за кг", fixed_card)
        self.assertNotIn("variant-list", fixed_card)

    def test_per_kg_price_has_weight_explanation_and_no_options(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        per_kg_card = page.split("Весовой товар", 1)[1].split("</article>", 1)[0]
        self.assertIn("24.00 EUR", per_kg_card)
        self.assertIn("за кг", per_kg_card)
        self.assertIn("Ориентировочный вес: 300 г", per_kg_card)
        self.assertIn("зависит от фактического веса", per_kg_card)
        self.assertNotIn("variant-list", per_kg_card)

    def test_options_mode_shows_only_active_options_and_their_availability(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        options_card = page.split("Активный &lt;товар&gt;", 1)[1].split("</article>", 1)[0]
        self.assertIn("100 &lt;b&gt;г&lt;/b&gt;", options_card)
        self.assertIn("250 г", options_card)
        self.assertIn("2.50 EUR", options_card)
        self.assertIn("5.50 EUR", options_card)
        self.assertIn("Нет в наличии", options_card)
        self.assertNotIn("Скрытый вариант", options_card)
        self.assertNotIn("за кг", options_card)

    def test_zero_stock_and_explicit_flag_are_hidden(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        self.assertNotIn("Нет на складе", page)
        self.assertNotIn("Нет по статусу", page)
        self.assertNotIn('class="stock-status out"', page)

    def test_fixed_zero_stock_is_hidden(self):
        catalog = self.sample_catalog()
        fixed = next(
            product for product in catalog[0]["products"] if product["id"] == 15
        )
        fixed["stock_quantity"] = 0
        page = storefront.render_catalog_page(catalog)
        self.assertNotIn("Фиксированный товар", page)

    def test_options_require_at_least_one_available_variant(self):
        catalog = self.sample_catalog()
        options_product = catalog[0]["products"][0]
        for option in options_product["options"]:
            option["stock_quantity"] = 0
        page = storefront.render_catalog_page(catalog)
        self.assertNotIn("Активный &lt;товар&gt;", page)

        options_product["options"][0]["stock_quantity"] = 1
        page = storefront.render_catalog_page(catalog)
        self.assertIn("Активный &lt;товар&gt;", page)

    def test_category_with_only_unavailable_products_is_hidden(self):
        catalog = self.sample_catalog()
        catalog.append({
            "id": 98,
            "name": "Категория без доступных товаров",
            "sort_order": 98,
            "products": [{
                "id": 98,
                "name": "Недоступный фиксированный",
                "description": "",
                "price_per_kg": 0,
                "image_url": "",
                "is_out_of_stock": False,
                "stock_grams": 0,
                "sort_order": 1,
                "pricing_mode": "fixed",
                "fixed_price": 1,
                "sale_unit": "за штуку",
                "unit_weight_grams": None,
                "stock_quantity": 0,
                "options": [],
            }],
        })
        page = storefront.render_catalog_page(catalog)
        self.assertNotIn("Категория без доступных товаров", page)
        self.assertNotIn("Недоступный фиксированный", page)

    def test_database_content_is_html_escaped(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        self.assertIn("Активная &lt;категория&gt;", page)
        self.assertIn("Активный &lt;товар&gt;", page)
        self.assertIn("Описание &lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("<script>alert(1)</script>", page)

    def test_invalid_and_empty_images_use_placeholder(self):
        page = storefront.render_catalog_page(self.sample_catalog(), currency_symbol="EUR", support_username="")
        self.assertNotIn("javascript:alert(1)", page)
        self.assertGreaterEqual(page.count("Фото скоро"), 2)
        self.assertEqual(storefront.safe_image_url(""), "")
        self.assertEqual(storefront.safe_image_url("data:image/png;base64,AAAA"), "")

    def test_external_images_fall_back_to_placeholder_on_error(self):
        catalog = self.sample_catalog()
        product = catalog[0]["products"][0]
        product["image_url"] = "https://images.example/missing.jpg"
        page = storefront.render_catalog_page(catalog)
        self.assertIn(
            'onerror="this.hidden=true;this.nextElementSibling.hidden=false"', page
        )
        self.assertIn("Фото скоро", page)
        self.assertIn("hidden", page)

    def test_admin_images_use_the_same_safe_fallback(self):
        empty = admin_app.render_admin_product_image("", "Товар")
        broken = admin_app.render_admin_product_image(
            "https://images.example/missing.jpg", "Товар"
        )
        unsafe = admin_app.render_admin_product_image("javascript:alert(1)", "Товар")
        self.assertIn("Фото скоро", empty)
        self.assertIn("Фото скоро", broken)
        self.assertIn("onerror=", broken)
        self.assertNotIn("javascript:", unsafe)

    def test_long_descriptions_open_in_accessible_modal(self):
        catalog = self.sample_catalog()
        product = catalog[0]["products"][0]
        full_description = "Первая строка 😊\n" + ("Очень длинное описание " * 20)
        product["description"] = full_description
        page = storefront.render_catalog_page(catalog)
        self.assertIn("-webkit-line-clamp: 4", storefront.PAGE_STYLE)
        self.assertIn('class="description-toggle"', page)
        self.assertIn('hidden aria-haspopup="dialog"', page)
        self.assertIn("Подробнее", page)
        self.assertNotIn("classList.toggle('expanded')", page)
        self.assertNotIn("product-description expanded", page)
        self.assertIn('<dialog class="product-modal"', page)
        self.assertIn('aria-modal="true"', page)
        self.assertIn('aria-labelledby="product-modal-10-title"', page)
        self.assertIn('aria-describedby="product-modal-10-description"', page)
        self.assertIn("Первая строка 😊\n", page)
        self.assertIn("2.50 €", page)
        self.assertIn("Связаться для заказа", page)
        self.assertIn("dialog.showModal()", storefront.PAGE_SCRIPT)
        self.assertIn("dialog.close()", storefront.PAGE_SCRIPT)
        self.assertIn("trigger.focus()", storefront.PAGE_SCRIPT)
        self.assertIn("modal-open", storefront.PAGE_SCRIPT)

    def test_modal_escapes_user_description_without_inner_html(self):
        catalog = self.sample_catalog()
        product = catalog[0]["products"][0]
        product["description"] = '</dialog><script>window.bad = true</script> 😊'
        page = storefront.render_catalog_page(catalog)
        self.assertIn(
            "&lt;/dialog&gt;&lt;script&gt;window.bad = true&lt;/script&gt; 😊",
            page,
        )
        self.assertNotIn("<script>window.bad = true</script>", page)
        self.assertNotIn("innerHTML", storefront.PAGE_SCRIPT)

    def test_responsive_breakpoints_keep_three_two_one_columns(self):
        self.assertIn("grid-template-columns: 1fr", storefront.PAGE_STYLE)
        self.assertIn("@media (min-width: 640px)", storefront.PAGE_STYLE)
        self.assertIn("repeat(2, minmax(0, 1fr))", storefront.PAGE_STYLE)
        self.assertIn("@media (min-width: 980px)", storefront.PAGE_STYLE)
        self.assertIn("repeat(3, minmax(0, 1fr))", storefront.PAGE_STYLE)
        self.assertIn("@media (max-width: 639px)", storefront.PAGE_STYLE)
        self.assertIn(".contact-button { width: 100%; }", storefront.PAGE_STYLE)
        admin_style = admin_app.admin_css()
        self.assertIn("@media (max-width: 720px)", admin_style)
        self.assertIn(".products-desktop-table { display: none; }", admin_style)
        self.assertIn(".mobile-products-list { display: grid", admin_style)
        self.assertIn("body { overflow-x: hidden; }", admin_style)

    def test_http_and_https_images_are_allowed(self):
        self.assertEqual(storefront.safe_image_url("https://images.example/product.jpg"), "https://images.example/product.jpg")
        self.assertEqual(storefront.safe_image_url("http://images.example/product.jpg"), "http://images.example/product.jpg")

    def test_fetch_catalog_reads_only_database_tables(self):
        connection = FakeConnection((
            [(1, "Категория", 1, True)],
            [(10, 1, "Товар", "Описание", 20, "", False, 100, 1, True, "per_kg", None, None, 100, None)],
            [(100, 10, "100 г", 100, 2, 1, True, 5, False)],
        ))
        catalog = storefront.fetch_catalog(lambda: connection)
        queries = "\n".join(connection.cursor_instance.queries)
        self.assertIn("FROM categories", queries)
        self.assertIn("FROM products", queries)
        self.assertIn("FROM product_options", queries)
        self.assertIn("p.pricing_mode", queries)
        self.assertIn("po.stock_quantity", queries)
        self.assertGreaterEqual(queries.count("is_active = TRUE"), 3)
        self.assertEqual(catalog[0]["products"][0]["options"], [])
        self.assertTrue(connection.cursor_instance.closed)
        self.assertTrue(connection.closed)

    def test_missing_database_url_returns_safe_page(self):
        with patch.dict(os.environ, {"DATABASE_URL": "", "DATABASE_PUBLIC_URL": ""}):
            response = asyncio.run(storefront.shop_page())
        page = response_text(response)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Магазин временно недоступен", page)
        self.assertNotIn("postgresql://", page)

    def test_database_error_has_no_json_fallback_or_details(self):
        with patch.object(storefront, "fetch_catalog", side_effect=RuntimeError("secret database host")):
            with patch("builtins.open", side_effect=AssertionError("JSON fallback attempted")):
                response = asyncio.run(storefront.shop_page())
        page = response_text(response)
        self.assertEqual(response.status_code, 503)
        self.assertIn("Магазин временно недоступен", page)
        self.assertNotIn("secret database host", page)
        self.assertNotIn("Рыбные снеки", page)


if __name__ == "__main__":
    unittest.main()
