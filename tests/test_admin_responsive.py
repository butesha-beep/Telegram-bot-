import asyncio
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/admin-responsive")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app


class DashboardCursor:
    def __init__(self, long_product_name):
        self.long_product_name = long_product_name
        self.query = ""

    def execute(self, query, params=None):
        self.query = query

    def fetchone(self):
        if "COALESCE(SUM(CASE WHEN payment_status = 'unpaid' AND payment_method IS NULL" in self.query:
            return (10, 1, 1, 1, 1, 1, 1, 4, 0, 2, 100, 10, 40)
        return (0,)

    def fetchall(self):
        if "ORDER BY revenue DESC" in self.query:
            return [(1, self.long_product_name, 1500, 12.5)]
        if "ORDER BY revenue ASC" in self.query:
            return [(2, self.long_product_name, 250, 3.75, 1)]
        return []


class DashboardConnection:
    def __init__(self, long_product_name):
        self.cursor_instance = DashboardCursor(long_product_name)
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self):
        self.closed = True


class AdminResponsiveTests(unittest.TestCase):
    def test_product_analytics_has_desktop_table_and_labeled_mobile_rows(self):
        long_name = "ОченьДлинноеНазваниеБезПробелов" * 8 + "<товар>"
        markup = admin_app.render_product_analytics(
            [(1, long_name, 1500, 12.5, 3)]
        )

        self.assertIn('class="dash-table-wrap analytics-desktop-table"', markup)
        self.assertIn('class="analytics-table"', markup)
        self.assertIn('class="analytics-mobile-list"', markup)
        self.assertIn('class="analytics-mobile-row"', markup)
        self.assertIn("<dt>Товар</dt>", markup)
        self.assertIn("<dt>Продано</dt>", markup)
        self.assertIn("<dt>Выручка</dt>", markup)
        self.assertIn("1.5 кг", markup)
        self.assertIn("€12.50", markup)
        self.assertIn("&lt;товар&gt;", markup)
        self.assertNotIn("<товар>", markup)
        self.assertNotIn("…", markup)

    def test_dashboard_uses_responsive_analytics_for_long_product_names(self):
        long_name = "НеразрывноеНазваниеТовара" * 10
        connection = DashboardConnection(long_name)

        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.root())

        self.assertTrue(connection.closed)
        self.assertIn("Аналитика продаж", page)
        self.assertIn("Слабые товары", page)
        self.assertIn(long_name, page)
        self.assertIn("analytics-desktop-table", page)
        self.assertIn('class="analytics-mobile-list"', page)
        self.assertIn("<dt>Товар</dt>", page)
        self.assertIn("<dt>Продано</dt>", page)
        self.assertIn("<dt>Выручка</dt>", page)

    def test_mobile_css_contains_page_and_keeps_local_tables_accessible(self):
        style = admin_app.admin_css()

        for viewport_width in (320, 360, 390, 430):
            with self.subTest(viewport_width=viewport_width):
                self.assertLessEqual(viewport_width, 720)
        self.assertIn(".dash-grid > * { min-width: 0; }", style)
        self.assertIn("width: 100%;\n      min-width: 0;\n      max-width: 100%;", style)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", style)
        self.assertIn(".dash-table-wrap > table", style)
        self.assertIn("width: max-content", style)
        self.assertIn(".analytics-desktop-table { display: none; }", style)
        self.assertIn(".analytics-mobile-list { display: grid", style)
        self.assertIn("overflow-wrap: anywhere", style)
        self.assertIn("overscroll-behavior-x: contain", style)
        self.assertIn("body { overflow-x: hidden; }", style)
        self.assertNotIn(
            "table { display: block; overflow-x: auto; white-space: nowrap; }",
            style,
        )

    def test_mobile_product_cards_and_contained_navigation_are_preserved(self):
        style = admin_app.admin_css()

        self.assertIn(".mobile-products-list { display: grid", style)
        self.assertIn(".products-desktop-table { display: none; }", style)
        self.assertIn(".mobile-product-card .action-group", style)
        self.assertIn(".admin-links", style)
        self.assertIn("overflow-x: auto", style)
        self.assertIn("overscroll-behavior-x: contain", style)


if __name__ == "__main__":
    unittest.main()
