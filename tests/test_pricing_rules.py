import asyncio
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/pricing")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app
import storefront


def catalog_with_product(product_row, option_rows=()):
    return storefront.assemble_catalog(
        [(1, "Категория", 0, True)],
        [product_row],
        list(option_rows),
    )


class ScriptedCursor:
    def __init__(self, fetchone_values=(), fetchall_values=()):
        self.fetchone_values = list(fetchone_values)
        self.fetchall_values = list(fetchall_values)
        self.queries = []
        self.rowcount = 1

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_values.pop(0)


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


class AdminPricingValidationTests(unittest.TestCase):
    def test_fixed_normalizes_only_relevant_fields_and_trims_unit(self):
        result = admin_app.normalize_product_pricing(
            "fixed", "not-used", "12.50", "  за штуку  ", "not-used", "7"
        )
        self.assertEqual(result, ("fixed", 0.0, 12.5, "за штуку", None, 7))

    def test_fixed_rejects_invalid_price_unit_and_stock(self):
        for value in ("", "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(fixed_price=value):
                with self.assertRaises(ValueError):
                    admin_app.normalize_product_pricing(
                        "fixed", "", value, "за штуку", "", ""
                    )
        with self.assertRaisesRegex(ValueError, "единица продажи"):
            admin_app.normalize_product_pricing(
                "fixed", "", "10", "   ", "", ""
            )
        with self.assertRaisesRegex(ValueError, "отрицательным"):
            admin_app.normalize_product_pricing(
                "fixed", "", "10", "за штуку", "", "-1"
            )

    def test_per_kg_normalizes_fields_and_validates_numbers(self):
        result = admin_app.normalize_product_pricing(
            "per_kg", "35", "not-used", "not-used", "250", "not-used"
        )
        self.assertEqual(result, ("per_kg", 35.0, None, None, 250, None))
        for value in ("", "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(price_per_kg=value):
                with self.assertRaises(ValueError):
                    admin_app.normalize_product_pricing(
                        "per_kg", value, "", "", "", ""
                    )
        for value in ("0", "-1"):
            with self.subTest(unit_weight_grams=value):
                with self.assertRaises(ValueError):
                    admin_app.normalize_product_pricing(
                        "per_kg", "35", "", "", value, ""
                    )
        with self.assertRaisesRegex(ValueError, "отрицательным"):
            admin_app.normalize_product_stock_grams("per_kg", "-1")

    def test_options_clear_product_pricing_without_parsing_irrelevant_fields(self):
        result = admin_app.normalize_product_pricing(
            "options", "nan", "not-a-number", "old unit", "-1", "-4"
        )
        self.assertEqual(result, ("options", 0.0, None, None, None, None))

    def test_unknown_pricing_mode_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Неизвестный режим"):
            admin_app.normalize_product_pricing(
                "legacy", "35", "", "", "", ""
            )

    def test_option_validation_and_normalization(self):
        self.assertEqual(
            admin_app.normalize_product_option("  100 г  ", "100", "3.5", "2"),
            ("100 г", 100, 3.5, 2),
        )
        invalid_cases = (
            ("   ", "100", "3.5", ""),
            ("100 г", "100", "0", ""),
            ("100 г", "100", "nan", ""),
            ("100 г", "0", "3.5", ""),
            ("100 г", "-1", "3.5", ""),
            ("100 г", "100", "3.5", "-1"),
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    admin_app.normalize_product_option(*values)

    def test_admin_warnings_cover_ignored_and_unavailable_options(self):
        available = (1, "100 г", 100, 3.5, 0, True, 2, False)
        unavailable = (2, "200 г", 200, 7, 0, True, 0, False)
        inactive = (3, "300 г", 300, 10.5, 0, False, 2, False)
        missing_stock = (4, "400 г", 400, 14, 0, True, None, False)

        self.assertIn("игнорируются", admin_app.admin_options_warning("per_kg", [available]))
        self.assertIn("Нет активных", admin_app.admin_options_warning("options", [inactive]))
        self.assertIn(
            "Нет доступных", admin_app.admin_options_warning("options", [unavailable])
        )
        self.assertIn(
            "Нет доступных", admin_app.admin_options_warning("options", [missing_stock])
        )
        self.assertEqual(admin_app.admin_options_warning("options", [available]), "")

    def test_weight_inventory_validation_rejects_fixed_and_options(self):
        admin_app.validate_weight_inventory_modes([(1, 100, "per_kg")])
        with self.assertRaisesRegex(ValueError, "2, 3"):
            admin_app.validate_weight_inventory_modes(
                [(2, 1, "fixed"), (3, 1, "options")]
            )

    def test_paid_order_rejects_unsupported_stock_mode_before_update(self):
        cursor = ScriptedCursor(
            fetchone_values=[("pending", False, False, 123)],
            fetchall_values=[[(10, 100, "fixed")]],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(admin_app.update_order_status("order-1", "paid"))

        self.assertIn("Списание не выполнено", response)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertFalse(any("UPDATE products" in query for query, _ in cursor.queries))

    def test_weighing_rejects_unsupported_stock_mode_before_update(self):
        cursor = ScriptedCursor(fetchone_values=[(20, "options", 10)])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(
                admin_app.weigh_order_item("order-1", 1, 100, photo=None)
            )

        self.assertIn("Взвешивание не выполнено", response)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertFalse(any("UPDATE order_items" in query for query, _ in cursor.queries))

    def test_admin_product_list_renders_per_kg_inventory(self):
        cursor = ScriptedCursor(fetchall_values=[[
            (
                1, "Весовой товар", 35, "", True, "Категория", 100,
                False, 500, False, "per_kg", None, None, None, 0, 0,
            )
        ]])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.products())

        self.assertIn("/products/1/edit", page)
        self.assertIn("100 г", page)
        self.assertIn("products-desktop-table", page)
        self.assertIn("mobile-products-list", page)
        self.assertIn("mobile-product-card", page)
        self.assertIn("ID 1", page)
        self.assertIn("Фото скоро", page)
        self.assertNotIn("Не удалось выполнить операцию", page)


class StorefrontPricingTests(unittest.TestCase):
    def test_unknown_mode_is_logged_and_hidden_without_per_kg_fallback(self):
        row = (
            1, 1, "Товар", "", 35, "", False, 100, 0, True,
            "legacy", None, None, None, None,
        )
        with self.assertLogs("storefront", level="ERROR") as logs:
            catalog = catalog_with_product(row)
        self.assertEqual(catalog[0]["products"], [])
        self.assertIn("Hiding product 1", "\n".join(logs.output))
        self.assertNotIn("Товар", storefront.render_catalog_page(catalog))

    def test_invalid_mode_specific_product_values_are_rejected(self):
        invalid_rows = (
            (1, 1, "Fixed", "", 0, "", False, 0, 0, True, "fixed", 0, "шт.", None, 1),
            (2, 1, "Unit", "", 0, "", False, 0, 0, True, "fixed", 10, " ", None, 1),
            (3, 1, "Per kg", "", float("inf"), "", False, 1, 0, True, "per_kg", None, None, None, None),
            (4, 1, "Stock", "", 35, "", False, -1, 0, True, "per_kg", None, None, None, None),
        )
        for row in invalid_rows:
            with self.subTest(product_id=row[0]):
                with self.assertRaises(ValueError):
                    catalog_with_product(row)

    def test_irrelevant_saved_options_do_not_affect_fixed_or_per_kg(self):
        fixed = (
            1, 1, "Fixed", "", 0, "", False, 0, 0, True,
            "fixed", 10, "за штуку", None, 1,
        )
        per_kg = (
            2, 1, "Per kg", "", 35, "", False, 100, 0, True,
            "per_kg", None, None, None, None,
        )
        malformed_old_options = [
            (1, 1, "", 0, 0, 0, True, -1, False),
            (2, 2, "", 0, 0, 0, True, -1, False),
        ]
        catalog = storefront.assemble_catalog(
            [(1, "Категория", 0, True)], [fixed, per_kg], malformed_old_options
        )
        self.assertEqual(catalog[0]["products"][0]["options"], [])
        self.assertEqual(catalog[0]["products"][1]["options"], [])

    def test_options_product_without_usable_active_options_is_out_of_stock(self):
        row = (
            1, 1, "Options", "", 0, "", False, 0, 0, True,
            "options", None, None, None, None,
        )
        no_options = catalog_with_product(row)
        self.assertTrue(storefront._product_is_out_of_stock(no_options[0]["products"][0]))

        unavailable = catalog_with_product(
            row, [(1, 1, "100 г", 100, 3.5, 0, True, 0, False)]
        )
        self.assertTrue(storefront._product_is_out_of_stock(unavailable[0]["products"][0]))

    def test_global_out_of_stock_override_and_per_kg_zero_stock(self):
        fixed = (
            1, 1, "Fixed", "", 0, "", True, 999, 0, True,
            "fixed", 10, "за штуку", None, 5,
        )
        per_kg = (
            2, 1, "Per kg", "", 35, "", False, 0, 0, True,
            "per_kg", None, None, None, None,
        )
        catalog = storefront.assemble_catalog(
            [(1, "Категория", 0, True)], [fixed, per_kg], []
        )
        self.assertTrue(storefront._product_is_out_of_stock(catalog[0]["products"][0]))
        self.assertTrue(storefront._product_is_out_of_stock(catalog[0]["products"][1]))


if __name__ == "__main__":
    unittest.main()
