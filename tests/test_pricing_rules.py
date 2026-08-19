import asyncio
import os
import re
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/pricing")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app
import order_creation
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
            "fixed", "not-used", "12.50", "  за штуку  ", "250", "7"
        )
        self.assertEqual(result, ("fixed", 0.0, 12.5, "за штуку", 250, 7))

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

    def test_fixed_can_persist_a_known_package_weight(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (1).
        result = admin_app.normalize_product_pricing(
            "fixed", "", "12.50", "банка", "250", "40"
        )
        self.assertEqual(result, ("fixed", 0.0, 12.5, "банка", 250, 40))

    def test_fixed_still_allows_null_unit_weight_grams(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (2).
        result = admin_app.normalize_product_pricing(
            "fixed", "", "12.50", "банка", "", "40"
        )
        self.assertEqual(result, ("fixed", 0.0, 12.5, "банка", None, 40))

    def test_fixed_rejects_invalid_package_weight(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (3).
        for value in ("0", "-1", "nan", "inf", "-inf", "abc"):
            with self.subTest(unit_weight_grams=value):
                with self.assertRaises(ValueError):
                    admin_app.normalize_product_pricing(
                        "fixed", "", "12.50", "банка", value, "40"
                    )

    def test_fixed_price_math_is_unaffected_by_unit_weight_grams(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (4).
        product_without_weight = {
            "pricing_mode": "fixed", "fixed_price": 12.5, "price_per_kg": None,
        }
        product_with_weight = {
            "pricing_mode": "fixed", "fixed_price": 12.5, "price_per_kg": None,
            "unit_weight_grams": 250,
        }
        price_a, mode_a, snapshot_a = order_creation.price_single_line(
            product_without_weight, None, None, None
        )
        price_b, mode_b, snapshot_b = order_creation.price_single_line(
            product_with_weight, None, None, None
        )
        self.assertEqual((price_a, mode_a, snapshot_a), (12.5, "fixed", None))
        self.assertEqual((price_b, mode_b, snapshot_b), (12.5, "fixed", None))

    def test_fixed_unit_weight_grams_does_not_affect_stock_quantity(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (5).
        with_weight = admin_app.normalize_product_pricing(
            "fixed", "", "12.50", "банка", "250", "7"
        )
        without_weight = admin_app.normalize_product_pricing(
            "fixed", "", "12.50", "банка", "", "7"
        )
        self.assertEqual(with_weight[5], 7)
        self.assertEqual(without_weight[5], 7)
        self.assertEqual(with_weight[5], without_weight[5])

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

    def test_packed_transition_no_longer_rejects_fixed_or_options_lines(self):
        # Superseded by the per-mode fulfillment dispatch: fixed/options
        # lines used to be rejected wholesale here; they must now be
        # deducted like any other line. See tests/test_admin_fulfillment.py
        # for the detailed per-mode deduction/restoration coverage.
        # (Checkpoint D: inventory deduction moved from the legacy 'paid'
        # status transition to the fulfillment_status='packed' transition.)
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 123),
                None,  # no pending weighing
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(admin_app, "deduct_order_inventory", return_value=[10]) as deduct:
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        deduct.assert_called_once_with(cursor, "order-1")
        self.assertNotIn("Списание не выполнено", response)
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_mixed_mode_deduction_failure_rolls_back_without_partial_commit(self):
        # Requirement 13: if fulfillment fails partway through a mixed-mode
        # order, the existing transaction wrapper must still roll back the
        # whole attempt rather than leaving some lines deducted and others
        # not. (connection.committed is not asserted here: the error-logging
        # path opens its own separate connection in production and commits
        # independently; this test's simple mock happens to return the same
        # object for every psycopg2.connect() call, so that unrelated commit
        # would otherwise be conflated with the order transaction itself.)
        cursor = ScriptedCursor(
            fetchone_values=[
                ("picking", False, False, 123),
                None,  # no pending weighing
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection), \
             patch.object(
                 admin_app,
                 "deduct_order_inventory",
                 side_effect=RuntimeError("simulated mid-fulfillment failure"),
             ):
            response = asyncio.run(
                admin_app.update_order_fulfillment_status("order-1", "packed")
            )

        self.assertIn("Ошибка", response)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(
            any(
                "UPDATE orders SET inventory_deducted" in query
                or "fulfillment_status = %s" in query
                for query, _ in cursor.queries
            )
        )

    def test_weighing_rejects_unsupported_stock_mode_before_update(self):
        cursor = ScriptedCursor(fetchone_values=[(20, "options", 10, None)])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            response = asyncio.run(
                admin_app.weigh_order_item("order-1", 1, 100, photo=None)
            )

        self.assertIn("Взвешивание не выполнено", response)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertFalse(any("UPDATE order_items" in query for query, _ in cursor.queries))

    def test_weighing_reads_pricing_mode_from_the_order_line_snapshot_not_live_product(self):
        # Order-snapshot requirement: weigh_order_item must key its
        # per_kg-only guard off oi.pricing_mode (fixed at order time), not
        # the product's current pricing_mode, so a later admin edit to the
        # product can't change fulfillment semantics for an existing order.
        cursor = ScriptedCursor(fetchone_values=[(20, "per_kg", 10, None)])
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(
                admin_app.weigh_order_item("order-1", 1, 100, photo=None)
            )

        lookup_queries = [
            query for query, _ in cursor.queries if "FROM order_items oi" in query
        ]
        self.assertTrue(lookup_queries)
        self.assertIn("oi.pricing_mode", lookup_queries[0])
        self.assertNotIn("p.pricing_mode", lookup_queries[0])

    def test_weighing_uses_price_snapshot_not_live_price_per_kg(self):
        # product's CURRENT price_per_kg is 40 (as if an admin changed it
        # after the order was placed); the order line snapshotted 35 at
        # purchase time and must be the one actually charged.
        cursor = ScriptedCursor(
            fetchone_values=[(40.0, "per_kg", 10, 35.0), (1,)],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(
                admin_app.weigh_order_item("order-1", 1, 600, photo=None)
            )

        update_queries = [
            (query, params)
            for query, params in cursor.queries
            if query.strip().startswith("UPDATE order_items")
        ]
        self.assertEqual(len(update_queries), 1)
        final_weight_grams, final_price = update_queries[0][1][0], update_queries[0][1][1]
        self.assertEqual(final_weight_grams, 600)
        self.assertEqual(final_price, round(0.6 * 35.0, 2))
        self.assertNotEqual(final_price, round(0.6 * 40.0, 2))

    def test_weighing_falls_back_to_live_price_only_for_historical_null_snapshot(self):
        # Orders created before this column existed have no snapshot; that
        # legacy case is the only one allowed to use the live price.
        cursor = ScriptedCursor(
            fetchone_values=[(35.0, "per_kg", 10, None), (1,)],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            asyncio.run(
                admin_app.weigh_order_item("order-1", 1, 600, photo=None)
            )

        update_queries = [
            (query, params)
            for query, params in cursor.queries
            if query.strip().startswith("UPDATE order_items")
        ]
        final_price = update_queries[0][1][1]
        self.assertEqual(final_price, round(0.6 * 35.0, 2))

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

    def test_new_product_form_renders_fields_by_pricing_mode(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (8).
        page = asyncio.run(admin_app.new_product_form())
        self.assertIn("Как продаётся?", page)
        self.assertIn('data-mode-panel="per_kg"', page)
        self.assertIn('data-mode-panel="fixed"', page)
        self.assertIn('data-mode-panel="options"', page)

        per_kg_panel = re.search(
            r'<div class="compact-field-grid" data-mode-panel="per_kg"[^>]*>', page
        )
        fixed_panel = re.search(
            r'<div class="compact-field-grid" data-mode-panel="fixed"[^>]*>', page
        )
        options_panel = re.search(r'<div data-mode-panel="options"[^>]*>', page)
        self.assertIsNotNone(per_kg_panel)
        self.assertIsNotNone(fixed_panel)
        self.assertIsNotNone(options_panel)
        # per_kg is the default mode: its panel is visible, the others hidden.
        self.assertNotIn("hidden", per_kg_panel.group(0))
        self.assertIn("hidden", fixed_panel.group(0))
        self.assertIn("hidden", options_panel.group(0))
        # No raw technical vocabulary shown to the admin.
        self.assertNotIn("pricing_mode</", page)
        self.assertNotIn(">fixed_price<", page)

    def test_edit_product_form_preserves_fixed_unit_weight_and_known_toggle(self):
        # Checkpoint: PRODUCT SALE-UNIT CONFIGURATION V1, focused test (9).
        cursor = ScriptedCursor(
            fetchone_values=[(
                1, "Икра красная", 0, "", None, 0, True, 0, False, 0,
                False, "", 0, "fixed", 12.5, "банка", 250, 40,
            )],
            fetchall_values=[[], []],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.edit_product_form(1))

        self.assertIn('value="Икра красная"', page)
        self.assertIn('value="12.5"', page)
        self.assertIn('value="банка"', page)
        self.assertIn('value="250"', page)
        self.assertIn('id="fixedWeightKnownToggle" checked', page)
        fixed_panel = re.search(
            r'<div class="compact-field-grid" data-mode-panel="fixed"[^>]*>', page
        )
        self.assertIsNotNone(fixed_panel)
        self.assertNotIn("hidden", fixed_panel.group(0))

    def test_edit_product_form_options_mode_hides_pricing_fields(self):
        cursor = ScriptedCursor(
            fetchone_values=[(
                1, "Options product", 0, "", None, 0, True, 0, False, 0,
                False, "", 0, "options", None, None, None, None,
            )],
            fetchall_values=[[], []],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(admin_app.psycopg2, "connect", return_value=connection):
            page = asyncio.run(admin_app.edit_product_form(1))

        options_panel = re.search(r'<div data-mode-panel="options"[^>]*>', page)
        self.assertIsNotNone(options_panel)
        self.assertNotIn("hidden", options_panel.group(0))
        weight_wrap = re.search(r'<div id="weightFieldWrap"[^>]*>', page)
        self.assertIsNotNone(weight_wrap)
        self.assertIn("hidden", weight_wrap.group(0))


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
