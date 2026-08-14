import asyncio
import html
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg2

from scripts import import_catalog
import admin_app
import storefront


TEST_URL = os.getenv("CATALOG_IMPORT_TEST_URL")


@unittest.skipUnless(TEST_URL, "disposable PostgreSQL URL is not configured")
class DisposablePostgresImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = import_catalog.validate_target_url(TEST_URL)
        cls.backup_directory = tempfile.TemporaryDirectory()
        cls._create_catalog_schema()

    @classmethod
    def tearDownClass(cls):
        cls.backup_directory.cleanup()

    @classmethod
    def connect(cls):
        return psycopg2.connect(
            TEST_URL,
            connect_timeout=10,
            options="-c statement_timeout=10000 -c lock_timeout=3000",
        )

    @classmethod
    def _assert_disposable_connection(cls, cursor):
        cursor.execute("SELECT current_database()")
        if cursor.fetchone()[0] != cls.identity.database:
            raise AssertionError("connected to an unexpected database")

    @classmethod
    def _create_catalog_schema(cls):
        connection = cls.connect()
        try:
            cursor = connection.cursor()
            cls._assert_disposable_connection(cursor)
            cursor.execute("DROP TABLE IF EXISTS public.external_catalog_reference")
            cursor.execute("DROP TABLE IF EXISTS public.product_options")
            cursor.execute("DROP TABLE IF EXISTS public.products")
            cursor.execute("DROP TABLE IF EXISTS public.categories")
            cursor.execute(
                """
                CREATE TABLE public.categories (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE public.products (
                    id SERIAL PRIMARY KEY,
                    category_id INTEGER REFERENCES public.categories(id),
                    name TEXT NOT NULL,
                    price_per_kg REAL NOT NULL,
                    description TEXT,
                    image_url TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    sort_order INTEGER DEFAULT 0,
                    stock_grams INTEGER DEFAULT 0 CHECK (stock_grams >= 0),
                    is_out_of_stock BOOLEAN DEFAULT FALSE,
                    low_stock_threshold_grams INTEGER DEFAULT 500,
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
            connection.commit()
        finally:
            connection.close()

    def prepare_demo(self):
        connection = self.connect()
        try:
            cursor = connection.cursor()
            self._assert_disposable_connection(cursor)
            cursor.execute("DROP TABLE IF EXISTS public.external_catalog_reference")
            cursor.execute("DELETE FROM public.product_options")
            cursor.execute("DELETE FROM public.products")
            cursor.execute("DELETE FROM public.categories")
            cursor.execute(
                """
                INSERT INTO public.categories (id, name, sort_order, is_active)
                VALUES (100, 'Disposable demo category', 1, TRUE)
                """
            )
            cursor.execute(
                """
                INSERT INTO public.products (
                    id, category_id, name, price_per_kg, stock_grams, pricing_mode
                )
                VALUES (100, 100, 'Disposable demo product', 1, 100, 'per_kg')
                """
            )
            cursor.execute(
                """
                INSERT INTO public.product_options (
                    id, product_id, label, weight, price, is_active
                )
                VALUES (100, 100, 'Disposable demo option', 100, 0.1, TRUE)
                """
            )
            cursor.execute(
                "COMMENT ON TABLE public.categories IS 'dealmarket-demo-catalog-v1'"
            )
            connection.commit()
        finally:
            connection.close()

    def fingerprint(self):
        connection = self.connect()
        try:
            cursor = connection.cursor()
            self._assert_disposable_connection(cursor)
            cursor.execute("SELECT id, name FROM public.categories ORDER BY id")
            categories = cursor.fetchall()
            cursor.execute("SELECT id, category_id, name FROM public.products ORDER BY id")
            products = cursor.fetchall()
            cursor.execute("SELECT id, product_id, label FROM public.product_options ORDER BY id")
            options = cursor.fetchall()
            cursor.execute(
                "SELECT obj_description('public.categories'::regclass, 'pg_class')"
            )
            marker = cursor.fetchone()[0]
            return categories, products, options, marker
        finally:
            connection.close()

    def test_full_disposable_rehearsal(self):
        expected_demo = import_catalog.ExpectedDemo(1, 1, 1)
        self.prepare_demo()
        initial_fingerprint = self.fingerprint()

        connection = self.connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE public.external_catalog_reference (
                    id SERIAL PRIMARY KEY,
                    product_id INTEGER REFERENCES public.products(id)
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "unknown external"):
            import_catalog.run_import(TEST_URL, expected_demo)
        self.assertEqual(self.fingerprint(), initial_fingerprint)

        connection = self.connect()
        try:
            cursor = connection.cursor()
            cursor.execute("DROP TABLE public.external_catalog_reference")
            connection.commit()
        finally:
            connection.close()

        dry_run = import_catalog.run_import(TEST_URL, expected_demo)
        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertEqual(self.fingerprint(), initial_fingerprint)

        applied = import_catalog.run_import(
            TEST_URL,
            expected_demo,
            apply=True,
            replace_demo=True,
            backup_dir=Path(self.backup_directory.name),
        )
        self.assertTrue(Path(applied["backup_path"]).is_file())
        self.assertEqual(applied["mode"], "apply")
        self.assertEqual(
            (applied["categories"], applied["products"], applied["product_options"]),
            (7, 17, 0),
        )
        self.verify_imported_database()
        self.verify_admin_and_storefront()
        self.verify_next_product_id()

        with self.assertRaisesRegex(import_catalog.CatalogImportError, "demo marker"):
            import_catalog.run_import(
                TEST_URL,
                expected_demo,
                apply=True,
                replace_demo=True,
                backup_dir=Path(self.backup_directory.name),
            )
        self.verify_imported_database()

        self.prepare_demo()
        before_failure = self.fingerprint()
        with self.assertRaisesRegex(import_catalog.CatalogImportError, "injected"):
            import_catalog.run_import(
                TEST_URL,
                expected_demo,
                apply=True,
                replace_demo=True,
                fail_after_products=True,
                backup_dir=Path(self.backup_directory.name),
            )
        self.assertEqual(self.fingerprint(), before_failure)

    def verify_imported_database(self):
        connection = self.connect()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM public.categories")
            self.assertEqual(cursor.fetchone()[0], 7)
            cursor.execute("SELECT COUNT(*) FROM public.products")
            self.assertEqual(cursor.fetchone()[0], 17)
            cursor.execute("SELECT COUNT(*) FROM public.product_options")
            self.assertEqual(cursor.fetchone()[0], 0)
            cursor.execute("SELECT id FROM public.products ORDER BY id")
            ids = [row[0] for row in cursor.fetchall()]
            self.assertEqual(ids, import_catalog.INCLUDED_PRODUCT_IDS)
            self.assertTrue(set(import_catalog.EXCLUDED_PRODUCT_IDS).isdisjoint(ids))
            cursor.execute(
                """
                SELECT COUNT(*) FROM public.products
                WHERE pricing_mode = 'per_kg'
                  AND price_per_kg > 0
                  AND fixed_price IS NULL
                  AND sale_unit IS NULL
                  AND unit_weight_grams IS NULL
                  AND stock_quantity IS NULL
                """
            )
            self.assertEqual(cursor.fetchone()[0], 17)
            cursor.execute("SELECT price_per_kg FROM public.products WHERE id = 19")
            price_per_kg = float(cursor.fetchone()[0])
            self.assertEqual(price_per_kg, 35)
            self.assertEqual(round(price_per_kg * 500 / 1000, 2), 17.5)
            cursor.execute(
                "SELECT stock_grams, is_out_of_stock FROM public.products WHERE id = 11"
            )
            self.assertEqual(cursor.fetchone(), (0, False))
            cursor.execute("SELECT id, is_active FROM public.products WHERE id IN (13, 17) ORDER BY id")
            self.assertEqual(cursor.fetchall(), [(13, False), (17, False)])
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM public.products product_row
                LEFT JOIN public.categories category_row
                  ON category_row.id = product_row.category_id
                WHERE category_row.id IS NULL
                """
            )
            self.assertEqual(cursor.fetchone()[0], 0)
        finally:
            connection.close()

    def verify_admin_and_storefront(self):
        catalog = storefront.fetch_catalog(self.connect)
        self.assertEqual(len(catalog), 6)
        public_products = [
            product for category in catalog for product in category["products"]
        ]
        self.assertEqual(len(public_products), 15)
        self.assertNotIn(13, {product["id"] for product in public_products})
        self.assertNotIn(17, {product["id"] for product in public_products})
        self.assertTrue(
            storefront._product_is_out_of_stock(
                next(product for product in public_products if product["id"] == 11)
            )
        )
        category_by_id = {category["id"]: category for category in catalog}
        for category_id in (3, 7):
            self.assertEqual(category_by_id[category_id]["products"], [])
        self.assertNotIn(5, category_by_id)
        page = storefront.render_catalog_page(catalog, currency_symbol="EUR")
        plan = import_catalog.load_and_validate_plan()
        category_by_plan_id = {category["id"]: category for category in plan["categories"]}
        self.assertNotIn(html.escape(category_by_plan_id[5]["name"]), page)
        product_by_id = {product["id"]: product for product in plan["products"]}
        self.assertNotIn(html.escape(product_by_id[13]["name"]), page)
        self.assertNotIn(html.escape(product_by_id[17]["name"]), page)

        with patch.object(admin_app, "DATABASE_URL", TEST_URL):
            admin_page = asyncio.run(admin_app.products())
        for product_id in import_catalog.INCLUDED_PRODUCT_IDS:
            self.assertIn(f"/products/{product_id}/edit", admin_page)

    def verify_next_product_id(self):
        connection = self.connect()
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                INSERT INTO public.products (category_id, name, price_per_kg)
                VALUES (1, 'Disposable sequence probe', 1)
                RETURNING id
                """
            )
            self.assertEqual(cursor.fetchone()[0], 25)
            connection.rollback()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
