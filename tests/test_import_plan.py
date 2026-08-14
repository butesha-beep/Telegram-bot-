import hashlib
import json
import math
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / "staging" / "production_catalog_snapshot.json"
AUDIT_PATH = ROOT / "staging" / "production_catalog_audit.json"
PLAN_PATH = ROOT / "staging" / "production_catalog_import_plan.json"

SNAPSHOT_SHA256 = "c53c4501005dac56283a30ad4b7718b4374ec73d74e7bd7cb4b7ab7111ae158a"
AUDIT_SHA256 = "35b40dcee556dfe655c6f24be4ebaea60d538a79fdcc1f623d3b96a42b40dfd2"
INCLUDED_PRODUCT_IDS = [1, *range(9, 25)]
EXCLUDED_PRODUCT_IDS = [2, 3, 4, 5, 6, 7, 8, 25]
NEW_PRICING_FIELDS = {
    "pricing_mode": "per_kg",
    "fixed_price": None,
    "sale_unit": None,
    "unit_weight_grams": None,
    "stock_quantity": None,
}


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImportPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        cls.plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))

    def test_source_snapshot_and_audit_are_unchanged(self):
        self.assertEqual(file_sha256(SNAPSHOT_PATH), SNAPSHOT_SHA256)
        self.assertEqual(file_sha256(AUDIT_PATH), AUDIT_SHA256)

    def test_control_counts_are_7_17_0_8_89(self):
        self.assertEqual(self.plan["summary"], {
            "categories": 7,
            "products": 17,
            "product_options": 0,
            "excluded_products": 8,
            "dropped_legacy_product_options": 89,
        })
        self.assertEqual(len(self.plan["categories"]), 7)
        self.assertEqual(len(self.plan["products"]), 17)
        self.assertEqual(self.plan["product_options"], [])
        self.assertEqual(len(self.plan["exclusions"]), 8)
        self.assertEqual(self.plan["dropped_legacy_product_options"]["count"], 89)

    def test_categories_are_complete_unique_and_deterministically_ordered(self):
        plan_categories = self.plan["categories"]
        self.assertEqual(
            plan_categories,
            sorted(self.snapshot["categories"], key=lambda category: category["id"]),
        )
        category_ids = [category["id"] for category in plan_categories]
        self.assertEqual(category_ids, sorted(category_ids))
        self.assertEqual(len(category_ids), len(set(category_ids)))

    def test_only_confirmed_products_are_present_and_source_fields_are_preserved(self):
        products = self.plan["products"]
        self.assertEqual([product["id"] for product in products], INCLUDED_PRODUCT_IDS)
        snapshot_by_id = {
            product["id"]: product for product in self.snapshot["products"]
        }
        category_ids = {category["id"] for category in self.plan["categories"]}

        for product in products:
            source_product = snapshot_by_id[product["id"]]
            for field, value in source_product.items():
                self.assertEqual(product[field], value, (product["id"], field))
            for field, expected in NEW_PRICING_FIELDS.items():
                self.assertEqual(product[field], expected, (product["id"], field))
            self.assertIn(product["category_id"], category_ids)
            self.assertTrue(math.isfinite(float(product["price_per_kg"])))
            self.assertGreater(float(product["price_per_kg"]), 0)

    def test_exclusions_and_reasons_are_explicit(self):
        exclusions = self.plan["exclusions"]
        self.assertEqual(
            [exclusion["product_id"] for exclusion in exclusions],
            EXCLUDED_PRODUCT_IDS,
        )
        for exclusion in exclusions:
            if exclusion["product_id"] == 25:
                self.assertEqual(exclusion["reason"], "test_product_with_zero_prices")
            else:
                self.assertEqual(
                    exclusion["reason"], "legacy_demo_not_created_by_user"
                )

    def test_stock_review_flag_and_inactive_products_are_preserved(self):
        products = {product["id"]: product for product in self.plan["products"]}
        self.assertEqual(self.plan["review_flags"], [{
            "product_id": 11,
            "needs_stock_update": True,
            "reason": "stock_grams_is_zero_and_requires_manual_update",
        }])
        self.assertFalse(any("needs_stock_update" in product for product in products.values()))
        self.assertEqual(products[11]["stock_grams"], 0)
        self.assertFalse(products[11]["is_out_of_stock"])
        self.assertFalse(products[13]["is_active"])
        self.assertFalse(products[17]["is_active"])

    def test_product_19_uses_authoritative_per_kg_price(self):
        product = next(
            product for product in self.plan["products"] if product["id"] == 19
        )
        decisions = self.plan["decisions"]
        self.assertEqual(product["price_per_kg"], 35)
        self.assertEqual(decisions["product_19_price_per_kg"], 35)
        self.assertEqual(decisions["product_19_calculated_500g_price"], 17.5)
        self.assertTrue(decisions["product_19_legacy_500g_price_is_error"])

    def test_empty_categories_are_computed_from_filtered_products(self):
        populated = {product["category_id"] for product in self.plan["products"]}
        expected = [
            {"id": category["id"], "name": category["name"]}
            for category in self.plan["categories"]
            if category["id"] not in populated
        ]
        self.assertEqual(self.plan["empty_categories_after_filter"], expected)
        self.assertEqual([item["id"] for item in expected], [3, 5, 7])

    def test_plan_has_no_credential_or_connection_metadata(self):
        forbidden_keys = {
            "database_url",
            "database_public_url",
            "pghost",
            "pgdatabase",
            "pguser",
            "pgpassword",
            "pgport",
            "username",
            "password",
            "credential",
            "token",
            "railway_id",
            "connection_url",
        }

        def visit(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    self.assertNotIn(str(key).lower(), forbidden_keys)
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(self.plan)

    def test_plan_ordering_is_deterministic(self):
        self.assertEqual(
            self.plan["decisions"]["included_product_ids"], INCLUDED_PRODUCT_IDS
        )
        self.assertEqual(
            [product["id"] for product in self.plan["products"]],
            sorted(product["id"] for product in self.plan["products"]),
        )
        self.assertEqual(
            [item["product_id"] for item in self.plan["exclusions"]],
            sorted(item["product_id"] for item in self.plan["exclusions"]),
        )


if __name__ == "__main__":
    unittest.main()
