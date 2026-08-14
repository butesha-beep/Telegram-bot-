import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, urlunsplit

import psycopg2
from psycopg2 import sql

from scripts import import_catalog


TEST_URL = os.getenv("CATALOG_IMPORT_TEST_URL")


@unittest.skipUnless(TEST_URL, "disposable PostgreSQL URL is not configured")
class CatalogImportGuardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = TEST_URL
        parsed = urlsplit(TEST_URL)
        cls.admin_url = urlunsplit(parsed._replace(path="/postgres"))
        cls.legacy_url = urlunsplit(
            parsed._replace(path=f"/{import_catalog.LEGACY_PREVIEW_DATABASE}")
        )
        cls.backup_directory = tempfile.TemporaryDirectory()
        cls._recreate_legacy_database()

    @classmethod
    def tearDownClass(cls):
        cls._drop_all_catalog_tables(cls.base_url)
        cls._drop_legacy_database()
        cls.backup_directory.cleanup()

    @classmethod
    def connect(cls, url):
        return psycopg2.connect(
            url,
            connect_timeout=10,
            options="-c statement_timeout=10000 -c lock_timeout=3000",
        )

    @classmethod
    def _recreate_legacy_database(cls):
        connection = cls.connect(cls.admin_url)
        try:
            connection.autocommit = True
            cursor = connection.cursor()
            cursor.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(import_catalog.LEGACY_PREVIEW_DATABASE)
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(import_catalog.LEGACY_PREVIEW_DATABASE)
                )
            )
        finally:
            connection.close()

    @classmethod
    def _drop_legacy_database(cls):
        connection = cls.connect(cls.admin_url)
        try:
            connection.autocommit = True
            connection.cursor().execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(import_catalog.LEGACY_PREVIEW_DATABASE)
                )
            )
        finally:
            connection.close()

    @classmethod
    def _drop_all_catalog_tables(cls, url):
        connection = cls.connect(url)
        try:
            cursor = connection.cursor()
            for table in (
                "unknown_catalog_reference",
                "new_logical_reference",
                "inventory_movements",
                "product_recommendations",
                "cart_items",
                "order_items",
                "customer_events",
                "product_options",
                "products",
                "categories",
            ):
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS public.{} CASCADE").format(
                        sql.Identifier(table)
                    )
                )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def _create_catalog_schema(cls, url):
        cls._drop_all_catalog_tables(url)
        connection = cls.connect(url)
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE public.categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.products (
                    id INTEGER PRIMARY KEY,
                    category_id INTEGER REFERENCES public.categories(id),
                    name TEXT NOT NULL,
                    price_per_kg REAL NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INTEGER DEFAULT 0,
                    stock_grams INTEGER DEFAULT 0,
                    is_out_of_stock BOOLEAN DEFAULT FALSE,
                    low_stock_threshold_grams INTEGER DEFAULT 500,
                    low_stock_alert_sent BOOLEAN DEFAULT FALSE,
                    low_stock_alert_sent_at TIMESTAMPTZ,
                    is_promotion BOOLEAN NOT NULL DEFAULT FALSE,
                    promotion_title TEXT,
                    promotion_sort_order INTEGER NOT NULL DEFAULT 0,
                    pricing_mode TEXT NOT NULL DEFAULT 'per_kg'
                        CHECK (pricing_mode IN ('fixed', 'per_kg', 'options')),
                    fixed_price REAL,
                    sale_unit TEXT,
                    unit_weight_grams INTEGER,
                    stock_quantity INTEGER
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.product_options (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES public.products(id),
                    label TEXT NOT NULL,
                    weight INTEGER,
                    price REAL NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    stock_quantity INTEGER,
                    is_out_of_stock BOOLEAN DEFAULT FALSE
                )
                """
            )
            cls._create_reference_tables(cursor)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _create_reference_tables(cursor):
        cursor.execute(
            """
            CREATE TABLE public.inventory_movements (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES public.products(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.product_recommendations (
                id SERIAL PRIMARY KEY,
                product_id INTEGER REFERENCES public.products(id),
                recommended_product_id INTEGER REFERENCES public.products(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.cart_items (
                id SERIAL PRIMARY KEY,
                product_id INTEGER,
                option_id INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.order_items (
                id SERIAL PRIMARY KEY,
                product_id INTEGER,
                product_name TEXT,
                option_id INTEGER
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE public.customer_events (
                id SERIAL PRIMARY KEY,
                metadata JSONB
            )
            """
        )

    @classmethod
    def prepare_demo(cls, marker=import_catalog.DEMO_MARKER):
        cls._create_catalog_schema(cls.base_url)
        connection = cls.connect(cls.base_url)
        try:
            cursor = connection.cursor()
            cursor.execute(
                "INSERT INTO public.categories (id, name) VALUES (100, 'Guard demo')"
            )
            cursor.execute(
                """
                INSERT INTO public.products (
                    id, category_id, name, price_per_kg, stock_grams, pricing_mode
                ) VALUES (100, 100, 'Guard demo product', 1, 100, 'per_kg')
                """
            )
            cursor.execute(
                """
                INSERT INTO public.product_options (
                    id, product_id, label, weight, price
                ) VALUES (100, 100, 'Guard demo option', 100, 0.1)
                """
            )
            cursor.execute(
                "COMMENT ON TABLE public.categories IS %s",
                (marker,),
            )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def restore_verified_legacy_backup(cls):
        cls._create_catalog_schema(cls.legacy_url)
        raw = import_catalog.LEGACY_BACKUP_PATH.read_bytes()
        if hashlib.sha256(raw).hexdigest() != import_catalog.LEGACY_BACKUP_SHA256:
            raise AssertionError("legacy backup checksum changed")
        payload = json.loads(raw.decode("utf-8"))
        import_catalog._validate_catalog_backup(
            payload,
            expected_counts=import_catalog.LEGACY_COUNTS,
            expected_marker=None,
        )
        connection = cls.connect(cls.legacy_url)
        try:
            cursor = connection.cursor()
            for table in import_catalog.CATALOG_TABLES:
                columns = [
                    item["name"] for item in payload["tables"][table]["columns"]
                ]
                statement = sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                cursor.executemany(
                    statement,
                    [
                        tuple(row[column] for column in columns)
                        for row in payload["tables"][table]["rows"]
                    ],
                )
            for sequence in payload["sequences"]:
                cursor.execute(
                    "SELECT setval(%s::regclass, %s, %s)",
                    (
                        f"public.{sequence['name']}",
                        sequence["last_value"],
                        sequence["is_called"],
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    @classmethod
    def fingerprint(cls, url):
        connection = cls.connect(url)
        try:
            cursor = connection.cursor()
            payload = {}
            for table in import_catalog.CATALOG_TABLES:
                cursor.execute(
                    sql.SQL(
                        "SELECT to_jsonb(row_value)::text "
                        "FROM public.{} row_value ORDER BY id"
                    ).format(sql.Identifier(table))
                )
                payload[table] = [row[0] for row in cursor.fetchall()]
            cursor.execute(
                "SELECT obj_description('public.categories'::regclass, 'pg_class')"
            )
            payload["marker"] = cursor.fetchone()[0]
            cursor.execute(
                "SELECT last_value, is_called FROM public.product_options_id_seq"
            )
            payload["sequence"] = cursor.fetchone()
            return hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        finally:
            connection.close()

    @classmethod
    def run_legacy_dry_run(cls):
        with patch.object(import_catalog, "_verify_expected_preview_container"):
            return import_catalog.run_import(
                cls.legacy_url,
                import_catalog.LEGACY_COUNTS,
            )

    def test_01_null_marker_exact_backup_allows_dry_run(self):
        self.restore_verified_legacy_backup()
        before = self.fingerprint(self.legacy_url)
        result = self.run_legacy_dry_run()
        self.assertEqual(result["mode"], "dry-run")
        self.assertEqual(self.fingerprint(self.legacy_url), before)

    def test_02_null_marker_changed_row_is_rejected(self):
        self.restore_verified_legacy_backup()
        connection = self.connect(self.legacy_url)
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                UPDATE public.products
                SET name = name || ' changed'
                WHERE id = (SELECT MIN(id) FROM public.products)
                """
            )
            self.assertEqual(cursor.rowcount, 1)
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "verified backup"):
            self.run_legacy_dry_run()

    def test_03_null_marker_wrong_counts_are_rejected(self):
        self.restore_verified_legacy_backup()
        connection = self.connect(self.legacy_url)
        try:
            connection.cursor().execute(
                "DELETE FROM public.product_options WHERE id = (SELECT MIN(id) FROM public.product_options)"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "counts"):
            self.run_legacy_dry_run()

    def test_04_null_marker_other_target_is_rejected(self):
        self.prepare_demo(marker=None)
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "not allowed"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_05_unknown_marker_is_rejected(self):
        self.prepare_demo(marker="unknown-catalog-marker")
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "marker"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_06_allowed_empty_foreign_keys_are_accepted(self):
        self.prepare_demo()
        result = import_catalog.run_import(
            self.base_url,
            import_catalog.ExpectedDemo(1, 1, 1),
        )
        self.assertEqual(result["mode"], "dry-run")

    def test_07_allowed_nonempty_foreign_key_is_rejected(self):
        self.prepare_demo()
        connection = self.connect(self.base_url)
        try:
            connection.cursor().execute(
                "INSERT INTO public.inventory_movements (product_id) VALUES (100)"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "foreign key contains"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_08_unknown_empty_foreign_key_is_rejected(self):
        self.prepare_demo()
        connection = self.connect(self.base_url)
        try:
            connection.cursor().execute(
                """
                CREATE TABLE public.unknown_catalog_reference (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES public.products(id)
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "unknown external"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_09_allowed_empty_logical_references_are_accepted(self):
        self.prepare_demo()
        result = import_catalog.run_import(
            self.base_url,
            import_catalog.ExpectedDemo(1, 1, 1),
        )
        self.assertEqual(result["mode"], "dry-run")

    def test_10_nonempty_logical_reference_is_rejected(self):
        self.prepare_demo()
        connection = self.connect(self.base_url)
        try:
            connection.cursor().execute(
                "INSERT INTO public.cart_items (product_id) VALUES (100)"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "logical.*contains"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_11_unknown_similar_column_is_rejected_even_when_empty(self):
        self.prepare_demo()
        connection = self.connect(self.base_url)
        try:
            connection.cursor().execute(
                """
                CREATE TABLE public.new_logical_reference (
                    id SERIAL PRIMARY KEY,
                    catalog_product_id INTEGER
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "unknown logical"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
            )

    def test_12_disposable_apply_sets_imported_marker(self):
        self.prepare_demo()
        result = import_catalog.run_import(
            self.base_url,
            import_catalog.ExpectedDemo(1, 1, 1),
            apply=True,
            replace_demo=True,
            backup_dir=Path(self.backup_directory.name),
        )
        self.assertEqual(result["mode"], "apply")
        connection = self.connect(self.base_url)
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT obj_description('public.categories'::regclass, 'pg_class')"
            )
            self.assertEqual(cursor.fetchone()[0], import_catalog.IMPORTED_MARKER)
        finally:
            connection.close()

    def test_13_failure_after_catalog_and_marker_writes_rolls_back_everything(self):
        self.prepare_demo()
        before = self.fingerprint(self.base_url)
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "after catalog"):
            import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
                apply=True,
                replace_demo=True,
                fail_before_commit=True,
                backup_dir=Path(self.backup_directory.name),
            )
        self.assertEqual(self.fingerprint(self.base_url), before)

    def test_14_reference_table_lock_blocks_concurrent_insert(self):
        self.prepare_demo()
        original_backup = import_catalog._create_catalog_backup
        contender_result = []

        def attempt_insert():
            connection = self.connect(self.base_url)
            try:
                cursor = connection.cursor()
                cursor.execute("SET LOCAL lock_timeout = 500")
                cursor.execute(
                    "INSERT INTO public.cart_items (product_id) VALUES (100)"
                )
                connection.commit()
                contender_result.append("inserted")
            except psycopg2.errors.LockNotAvailable:
                connection.rollback()
                contender_result.append("blocked")
            finally:
                connection.close()

        def backup_after_competing_insert(*args, **kwargs):
            contender = threading.Thread(target=attempt_insert)
            contender.start()
            contender.join(timeout=5)
            self.assertFalse(contender.is_alive())
            return original_backup(*args, **kwargs)

        with patch.object(
            import_catalog,
            "_create_catalog_backup",
            side_effect=backup_after_competing_insert,
        ):
            result = import_catalog.run_import(
                self.base_url,
                import_catalog.ExpectedDemo(1, 1, 1),
                apply=True,
                replace_demo=True,
                backup_dir=Path(self.backup_directory.name),
            )
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(contender_result, ["blocked"])


if __name__ == "__main__":
    unittest.main()
