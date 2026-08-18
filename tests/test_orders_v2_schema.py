import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/orders-v2-schema")

import db_schema


class RecordingCursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(query)

    def close(self):
        pass


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _run_init_db_and_capture_queries():
    """Runs the real init_db() against a mocked psycopg2.connect (never a
    real database) and returns the exact, fully-formed SQL strings it would
    send -- the only reliable way to verify generated SQL text without
    accessing a live Postgres instance."""
    connection = RecordingConnection()
    with patch.object(db_schema.psycopg2, "connect", return_value=connection):
        db_schema.init_db()
    return connection.cursor_instance.queries


ALL_QUERIES = _run_init_db_and_capture_queries()
ALL_QUERIES_TEXT = "\n".join(ALL_QUERIES)


class NewColumnsRepresentedInInitDbTests(unittest.TestCase):
    """Requirement 1: every Orders v2 column is actually added by
    init_db(), not just declared in the readiness probe."""

    ORDERS_COLUMNS = (
        "payment_status",
        "fulfillment_status",
        "source",
        "source_reference",
        "client_id",
        "customer_name",
        "delivery_method",
        "delivery_street",
        "delivery_house_number",
        "delivery_postcode",
        "delivery_city",
        "delivery_country",
        "delivery_notes",
    )

    def test_every_new_orders_column_has_an_add_column_statement(self):
        for column in self.ORDERS_COLUMNS:
            with self.subTest(column=column):
                self.assertTrue(
                    any(
                        f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {column} "
                        in query
                        for query in ALL_QUERIES
                    ),
                    f"no ADD COLUMN statement found for orders.{column}",
                )

    def test_clients_last_name_has_an_add_column_statement(self):
        self.assertTrue(
            any(
                "ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_name TEXT"
                in query
                for query in ALL_QUERIES
            )
        )


class ReadinessProbeRequiresNewColumnsTests(unittest.TestCase):
    """Requirement 2: the schema-compatibility probe will fail closed
    against a database that hasn't received the Orders v2 columns yet."""

    def test_orders_readiness_requires_exactly_the_new_columns(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["orders"],
            {
                "payment_status",
                "fulfillment_status",
                "source",
                "source_reference",
                "client_id",
                "customer_name",
                "delivery_method",
                "delivery_street",
                "delivery_house_number",
                "delivery_postcode",
                "delivery_city",
                "delivery_country",
                "delivery_notes",
            },
        )

    def test_clients_readiness_requires_last_name(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["clients"], {"last_name"}
        )


class PaymentStatusVocabularyTests(unittest.TestCase):
    """Requirement 3: payment_status allows ONLY unpaid, payment_reported,
    paid, refunded. cash_on_delivery must never appear here -- COD is
    represented by payment_method, not payment_status."""

    def test_exact_vocabulary_constant(self):
        self.assertEqual(
            db_schema.ORDER_PAYMENT_STATUS_VALUES,
            ("unpaid", "payment_reported", "paid", "refunded"),
        )
        self.assertNotIn("cash_on_delivery", db_schema.ORDER_PAYMENT_STATUS_VALUES)

    def test_executed_sql_check_constraint_matches_the_constant_exactly(self):
        expected = (
            "CHECK (payment_status IN "
            "('unpaid', 'payment_reported', 'paid', 'refunded'))"
        )
        self.assertIn(expected, ALL_QUERIES_TEXT)
        self.assertNotIn("cash_on_delivery", ALL_QUERIES_TEXT)

    def test_default_is_unpaid(self):
        self.assertIn(
            "payment_status TEXT NOT NULL DEFAULT 'unpaid'", ALL_QUERIES_TEXT
        )


class FulfillmentStatusVocabularyTests(unittest.TestCase):
    """Requirement 4: fulfillment_status allows exactly the 8 approved
    values."""

    def test_exact_vocabulary_constant(self):
        self.assertEqual(
            db_schema.ORDER_FULFILLMENT_STATUS_VALUES,
            (
                "new", "confirmed", "picking", "packed",
                "ready_to_ship", "shipped", "delivered", "cancelled",
            ),
        )

    def test_executed_sql_check_constraint_matches_the_constant_exactly(self):
        expected = (
            "CHECK (fulfillment_status IN "
            "('new', 'confirmed', 'picking', 'packed', "
            "'ready_to_ship', 'shipped', 'delivered', 'cancelled'))"
        )
        self.assertIn(expected, ALL_QUERIES_TEXT)

    def test_default_is_new(self):
        self.assertIn(
            "fulfillment_status TEXT NOT NULL DEFAULT 'new'", ALL_QUERIES_TEXT
        )


class SourceVocabularyTests(unittest.TestCase):
    """Requirement 5: source allows exactly the approved 8-channel
    vocabulary."""

    def test_exact_vocabulary_constant(self):
        self.assertEqual(
            db_schema.ORDER_SOURCE_VALUES,
            (
                "telegram", "website", "instagram", "tiktok",
                "whatsapp", "viber", "in_person", "other",
            ),
        )

    def test_executed_sql_check_constraint_matches_the_constant_exactly(self):
        expected = (
            "CHECK (source IN "
            "('telegram', 'website', 'instagram', 'tiktok', "
            "'whatsapp', 'viber', 'in_person', 'other'))"
        )
        self.assertIn(expected, ALL_QUERIES_TEXT)

    def test_default_is_telegram(self):
        self.assertIn(
            "source TEXT NOT NULL DEFAULT 'telegram'", ALL_QUERIES_TEXT
        )

    def test_source_reference_is_a_plain_nullable_column_not_an_enum(self):
        self.assertTrue(
            any(
                query.strip()
                == "ALTER TABLE orders ADD COLUMN IF NOT EXISTS source_reference TEXT"
                for query in ALL_QUERIES
            )
        )


class DeliveryMethodVocabularyTests(unittest.TestCase):
    """Requirement 6: delivery_method allows pickup/delivery and NULL
    (no NOT NULL constraint, so unset orders are valid)."""

    def test_exact_vocabulary_constant(self):
        self.assertEqual(
            db_schema.ORDER_DELIVERY_METHOD_VALUES, ("pickup", "delivery")
        )

    def test_executed_sql_check_constraint_matches_the_constant_exactly(self):
        expected = "CHECK (delivery_method IN ('pickup', 'delivery'))"
        self.assertIn(expected, ALL_QUERIES_TEXT)

    def test_delivery_method_column_is_not_declared_not_null(self):
        delivery_method_queries = [
            query for query in ALL_QUERIES if "delivery_method TEXT" in query
        ]
        self.assertTrue(delivery_method_queries)
        self.assertFalse(
            any("NOT NULL" in query for query in delivery_method_queries)
        )


class ClientIdentityNullabilityTests(unittest.TestCase):
    """Requirements 7/8: clients.telegram_id stays nullable (unchanged);
    orders.client_id is nullable and references clients(id)."""

    def test_clients_telegram_id_definition_is_unchanged_and_not_null_free(self):
        telegram_id_queries = [
            query for query in ALL_QUERIES if "telegram_id BIGINT" in query
        ]
        self.assertTrue(telegram_id_queries)
        self.assertFalse(
            any("SET NOT NULL" in query for query in telegram_id_queries)
        )
        self.assertTrue(
            any("telegram_id BIGINT UNIQUE" in query for query in telegram_id_queries)
        )

    def test_orders_client_id_is_nullable_and_references_clients(self):
        client_id_queries = [
            query for query in ALL_QUERIES if "client_id INTEGER" in query
        ]
        self.assertTrue(client_id_queries)
        self.assertTrue(
            any(
                "client_id INTEGER REFERENCES clients(id)" in query
                for query in client_id_queries
            )
        )
        self.assertFalse(
            any("client_id INTEGER NOT NULL" in query for query in client_id_queries)
        )


class ExistingCatalogRequirementsUnchangedTests(unittest.TestCase):
    """Requirement 9: pricing/inventory schema requirements established in
    earlier checkpoints are untouched by this one."""

    def test_products_requirements_unchanged(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["products"],
            {
                "id", "category_id", "name", "price_per_kg", "description",
                "image_url", "is_active", "sort_order", "stock_grams",
                "is_out_of_stock", "pricing_mode", "fixed_price", "sale_unit",
                "unit_weight_grams", "stock_quantity",
            },
        )

    def test_product_options_requirements_unchanged(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["product_options"],
            {
                "id", "product_id", "label", "weight", "price", "sort_order",
                "is_active", "stock_quantity", "is_out_of_stock",
            },
        )

    def test_order_items_requirements_unchanged(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["order_items"],
            {"pricing_mode", "price_per_kg_snapshot"},
        )

    def test_inventory_movements_requirements_unchanged(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["inventory_movements"],
            {"quantity_units"},
        )

    def test_categories_requirements_unchanged(self):
        self.assertEqual(
            db_schema.REQUIRED_CATALOG_COLUMNS["categories"],
            {"id", "name", "sort_order", "is_active"},
        )

    def test_existing_pricing_and_inventory_check_constraints_unchanged(self):
        self.assertIn(
            "products ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL "
            "DEFAULT 'per_kg' CHECK (pricing_mode IN "
            "('fixed', 'per_kg', 'options'))",
            ALL_QUERIES_TEXT,
        )
        self.assertIn(
            "order_items ADD COLUMN IF NOT EXISTS pricing_mode TEXT NOT NULL "
            "DEFAULT 'per_kg' CHECK (pricing_mode IN "
            "('fixed', 'per_kg', 'options'))",
            ALL_QUERIES_TEXT,
        )


class LegacyStatusColumnUntouchedTests(unittest.TestCase):
    """The legacy orders.status column must still be created exactly as
    before -- Checkpoint A does not redesign or drop it."""

    def test_status_column_still_created_as_plain_text(self):
        self.assertTrue(
            any(
                "status TEXT" in query and "CREATE TABLE IF NOT EXISTS orders" in query
                for query in ALL_QUERIES
            )
        )

    def test_no_drop_or_rename_of_status_column(self):
        self.assertFalse(any("DROP COLUMN status" in query for query in ALL_QUERIES))
        self.assertFalse(any("RENAME COLUMN status" in query for query in ALL_QUERIES))
        self.assertFalse(
            any("status" in query and "payment_status" not in query
                and "fulfillment_status" not in query and "ALTER COLUMN status" in query
                for query in ALL_QUERIES)
        )


if __name__ == "__main__":
    unittest.main()
