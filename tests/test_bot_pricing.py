import os
import unittest


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/bot-pricing")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)

import bot


def per_kg_product(product_id, price_per_kg, stock_grams=1000, is_out_of_stock=False):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Товар",
        "price_per_kg": price_per_kg,
        "description": "",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock,
        "pricing_mode": "per_kg",
        "fixed_price": None,
        "sale_unit": None,
        "unit_weight_grams": None,
        "stock_quantity": None,
    }


def fixed_product(
    product_id,
    fixed_price,
    sale_unit="шт",
    stock_quantity=10,
    is_out_of_stock=False,
    price_per_kg=0.0,
):
    return {
        "id": product_id,
        "category_id": 1,
        "name": "Штучный товар",
        "price_per_kg": price_per_kg,
        "description": "",
        "image_url": "",
        "photo": "",
        "is_active": True,
        "stock_grams": 0,
        "is_out_of_stock": is_out_of_stock,
        "pricing_mode": "fixed",
        "fixed_price": fixed_price,
        "sale_unit": sale_unit,
        "unit_weight_grams": None,
        "stock_quantity": stock_quantity,
    }


class PerKgWeightPricingTests(unittest.TestCase):
    """Locks down price = price_per_kg * weight / 1000 for the standard
    50/100/200/500g weight-button path, before any pricing_mode branching
    is added to render_product/price_cart_items."""

    def test_100g_200g_500g_prices_match_price_per_kg_formula(self):
        price_per_kg = 24.0
        product = per_kg_product(1, price_per_kg)
        products = [product]

        for weight in (100, 200, 500):
            with self.subTest(weight=weight):
                cart_items = [(1, weight, None, None, None)]
                total, priced_items = bot.price_cart_items(cart_items, products)
                expected = price_per_kg * weight / 1000
                self.assertEqual(total, expected)
                self.assertEqual(len(priced_items), 1)
                self.assertEqual(priced_items[0]["price"], expected)
                self.assertEqual(priced_items[0]["weight"], weight)
                self.assertIsNone(priced_items[0]["option_id"])
                self.assertIn(f"Вес: {weight} г", priced_items[0]["item_detail"])

    def test_multiple_per_kg_lines_sum_to_total(self):
        price_per_kg = 18.5
        product = per_kg_product(7, price_per_kg)
        products = [product]
        cart_items = [
            (7, 100, None, None, None),
            (7, 200, None, None, None),
            (7, 500, None, None, None),
        ]

        total, priced_items = bot.price_cart_items(cart_items, products)

        expected_total = sum(price_per_kg * weight / 1000 for weight in (100, 200, 500))
        self.assertEqual(total, expected_total)
        self.assertEqual(len(priced_items), 3)

    def test_cart_row_for_unknown_product_is_skipped_not_priced_as_zero(self):
        products = [per_kg_product(1, 24.0)]
        cart_items = [(1, 100, None, None, None), (999, 200, None, None, None)]

        total, priced_items = bot.price_cart_items(cart_items, products)

        self.assertEqual(len(priced_items), 1)
        self.assertEqual(priced_items[0]["product_id"], 1)
        self.assertEqual(total, 24.0 * 100 / 1000)


class ProductOptionPricingTests(unittest.TestCase):
    """Locks down that a stored product_options price is used exactly as-is,
    independent of the product's price_per_kg, matching the live catalog's
    current option-based variants (e.g. weight-range fish sizes)."""

    def test_option_price_is_used_exactly_and_ignores_price_per_kg(self):
        product = per_kg_product(3, price_per_kg=999.0)
        products = [product]
        cart_items = [(3, 250, 42, "Средняя рыба 200-300 г", 12.5)]

        total, priced_items = bot.price_cart_items(cart_items, products)

        self.assertEqual(total, 12.5)
        self.assertEqual(priced_items[0]["price"], 12.5)
        self.assertEqual(priced_items[0]["option_id"], 42)
        self.assertIn("Вариант: Средняя рыба 200-300 г", priced_items[0]["item_detail"])

    def test_option_price_is_not_recomputed_from_weight(self):
        product = per_kg_product(3, price_per_kg=10.0)
        products = [product]
        # weight is present (as stored on the cart row) but must not be used
        # for pricing once option_id/option_price are set.
        cart_items = [(3, 300, 5, "Крупная рыба", 40.0)]

        total, _ = bot.price_cart_items(cart_items, products)

        self.assertEqual(total, 40.0)
        self.assertNotEqual(total, 10.0 * 300 / 1000)


class OutOfStockCharacterizationTests(unittest.TestCase):
    """Locks down current is_product_out_of_stock() behavior for the
    per_kg/legacy product shape already used in production."""

    def test_explicit_out_of_stock_flag_wins_even_with_positive_stock(self):
        product = per_kg_product(1, 24.0, stock_grams=5000, is_out_of_stock=True)
        self.assertTrue(bot.is_product_out_of_stock(product))

    def test_zero_stock_grams_is_out_of_stock(self):
        product = per_kg_product(1, 24.0, stock_grams=0, is_out_of_stock=False)
        self.assertTrue(bot.is_product_out_of_stock(product))

    def test_positive_stock_grams_and_no_flag_is_in_stock(self):
        product = per_kg_product(1, 24.0, stock_grams=250, is_out_of_stock=False)
        self.assertFalse(bot.is_product_out_of_stock(product))

    def test_missing_stock_grams_is_treated_as_in_stock(self):
        product = per_kg_product(1, 24.0, stock_grams=None, is_out_of_stock=False)
        self.assertFalse(bot.is_product_out_of_stock(product))

    def test_missing_pricing_mode_key_defaults_to_per_kg_stock_check(self):
        # Dicts from get_promotion_products() do not carry the new pricing
        # fields; is_product_out_of_stock must keep behaving exactly as
        # before for them.
        legacy_dict = {"stock_grams": 0, "is_out_of_stock": False}
        self.assertTrue(bot.is_product_out_of_stock(legacy_dict))
        legacy_dict = {"stock_grams": 500, "is_out_of_stock": False}
        self.assertFalse(bot.is_product_out_of_stock(legacy_dict))


class FixedModePricingTests(unittest.TestCase):
    """Locks down the new fixed pricing_mode contract: fixed_price is the
    only source of truth for both display and cart pricing, independent of
    price_per_kg, which is stored as 0 for fixed-mode products."""

    def test_single_fixed_item_prices_at_exactly_fixed_price(self):
        product = fixed_product(10, fixed_price=6.0, sale_unit="шт")
        products = [product]
        cart_items = [(10, None, None, None, None)]

        total, priced_items = bot.price_cart_items(cart_items, products)

        self.assertEqual(total, 6.0)
        self.assertEqual(priced_items[0]["price"], 6.0)
        self.assertIn("шт", priced_items[0]["item_detail"])

    def test_two_fixed_cart_rows_price_at_two_times_fixed_price(self):
        product = fixed_product(10, fixed_price=6.0)
        products = [product]
        cart_items = [
            (10, None, None, None, None),
            (10, None, None, None, None),
        ]

        total, priced_items = bot.price_cart_items(cart_items, products)

        self.assertEqual(total, 12.0)
        self.assertEqual(len(priced_items), 2)
        self.assertTrue(all(item["price"] == 6.0 for item in priced_items))

    def test_fixed_item_ignores_zero_price_per_kg_entirely(self):
        # price_per_kg is stored as 0.0 for fixed-mode products in the DB
        # (admin_app.normalize_product_pricing). This is the exact condition
        # that previously produced a silent €0.00 cart line.
        product = fixed_product(10, fixed_price=6.0, price_per_kg=0.0)
        products = [product]
        cart_items = [(10, None, None, None, None)]

        total, _ = bot.price_cart_items(cart_items, products)

        self.assertEqual(total, 6.0)
        self.assertNotEqual(total, 0.0)
        self.assertGreater(total, 0.0)

    def test_fixed_product_with_missing_or_zero_price_is_treated_out_of_stock(self):
        # Defensive guard beyond the literal requirement: a misconfigured
        # fixed product (no positive fixed_price) must never be purchasable
        # at all, rather than silently defaulting to a €0.00 line.
        missing_price = fixed_product(10, fixed_price=None)
        self.assertTrue(bot.is_product_out_of_stock(missing_price))
        zero_price = fixed_product(10, fixed_price=0)
        self.assertTrue(bot.is_product_out_of_stock(zero_price))

    def test_fixed_product_out_of_stock_by_quantity(self):
        product = fixed_product(10, fixed_price=6.0, stock_quantity=0)
        self.assertTrue(bot.is_product_out_of_stock(product))

    def test_fixed_product_in_stock_by_quantity(self):
        product = fixed_product(10, fixed_price=6.0, stock_quantity=3)
        self.assertFalse(bot.is_product_out_of_stock(product))

    def test_explicit_out_of_stock_flag_overrides_positive_quantity(self):
        product = fixed_product(10, fixed_price=6.0, stock_quantity=5, is_out_of_stock=True)
        self.assertTrue(bot.is_product_out_of_stock(product))


class OptionsModeStockVisibilityTests(unittest.TestCase):
    """options mode does not track stock_grams; the top-level check must not
    misuse the per_kg heuristic against it (per-option availability is
    handled separately, where it is already queried)."""

    def test_options_mode_product_with_zero_stock_grams_is_not_globally_out_of_stock(self):
        product = per_kg_product(5, price_per_kg=0.0, stock_grams=0)
        product["pricing_mode"] = "options"
        self.assertFalse(bot.is_product_out_of_stock(product))

    def test_options_mode_explicit_flag_still_wins(self):
        product = per_kg_product(5, price_per_kg=0.0, stock_grams=0)
        product["pricing_mode"] = "options"
        product["is_out_of_stock"] = True
        self.assertTrue(bot.is_product_out_of_stock(product))


class PricePerKgSnapshotTests(unittest.TestCase):
    """Requirement 8: every per_kg cart line must carry the product's
    current price_per_kg forward as a snapshot value, ready to be persisted
    onto order_items at order creation; fixed/options lines carry none,
    since their commercial price is already fully captured in `price`."""

    def test_per_kg_weight_line_snapshots_price_per_kg(self):
        product = per_kg_product(1, price_per_kg=35.0)
        cart_items = [(1, 600, None, None, None)]

        _, priced_items = bot.price_cart_items(cart_items, [product])

        self.assertEqual(priced_items[0]["price_per_kg_snapshot"], 35.0)

    def test_per_kg_variable_weight_option_line_still_snapshots_price_per_kg(self):
        # The legacy per_kg + product_options overlay (variable-weight fish)
        # is still pricing_mode='per_kg' at the product level, so it must
        # snapshot too, even though the line itself carries an option_id.
        product = per_kg_product(1, price_per_kg=35.0)
        cart_items = [(1, None, 9, "Средняя рыба", 15.0)]

        _, priced_items = bot.price_cart_items(cart_items, [product])

        self.assertEqual(priced_items[0]["price_per_kg_snapshot"], 35.0)

    def test_fixed_line_has_no_price_per_kg_snapshot(self):
        product = fixed_product(10, fixed_price=6.0)
        cart_items = [(10, None, None, None, None)]

        _, priced_items = bot.price_cart_items(cart_items, [product])

        self.assertIsNone(priced_items[0]["price_per_kg_snapshot"])

    def test_options_mode_line_has_no_price_per_kg_snapshot(self):
        product = per_kg_product(5, price_per_kg=0.0)
        product["pricing_mode"] = "options"
        cart_items = [(5, 100, 42, "Комплект", 9.5)]

        _, priced_items = bot.price_cart_items(cart_items, [product])

        self.assertIsNone(priced_items[0]["price_per_kg_snapshot"])


if __name__ == "__main__":
    unittest.main()
