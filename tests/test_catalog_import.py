import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts import import_catalog


LOCAL_TARGET = "postgresql://local-user@127.0.0.1:54321/dealmarket_b4_disposable"
PREFLIGHT_STATE = import_catalog.PreflightState(
    marker=import_catalog.DEMO_MARKER,
    reference_tables=(),
    legacy_null_marker=False,
)


class CatalogImportPlanValidationTests(unittest.TestCase):
    def test_fixed_plan_is_valid_and_has_allowlisted_rows(self):
        plan = import_catalog.load_and_validate_plan()
        self.assertEqual(len(plan["categories"]), 7)
        self.assertEqual(len(plan["products"]), 17)
        self.assertEqual(plan["product_options"], [])
        self.assertTrue(
            all(set(row) == import_catalog.CATEGORY_FIELDS for row in plan["categories"])
        )
        self.assertTrue(
            all(set(row) == import_catalog.PRODUCT_FIELDS for row in plan["products"])
        )

    def test_target_guard_accepts_only_explicit_local_disposable_postgres(self):
        identity = import_catalog.validate_target_url(LOCAL_TARGET)
        self.assertEqual(identity.host, "127.0.0.1")
        self.assertEqual(identity.database, "dealmarket_b4_disposable")
        preview_identity = import_catalog.validate_target_url(
            "postgresql://local-user@localhost:54321/dealmarket_preview"
        )
        self.assertEqual(preview_identity.database, "dealmarket_preview")

        rejected = (
            "postgresql://user:secret@postgres.railway.internal/db",
            "postgresql://user:secret@kodama.proxy.rlwy.net/db",
            "postgresql://user:secret@another.proxy.rlwy.net/db",
            "postgresql://user:secret@db.example.com/dealmarket_test",
            "postgresql://user@127.0.0.1/dealmarket",
            "sqlite:///dealmarket_b4_disposable",
        )
        for target in rejected:
            with self.subTest(target=target):
                with self.assertRaises(import_catalog.CatalogImportError):
                    import_catalog.validate_target_url(target)

    def test_target_guard_rejects_application_database_variables(self):
        with patch.dict(os.environ, {"DATABASE_URL": LOCAL_TARGET}, clear=False):
            with self.assertRaisesRegex(
                import_catalog.CatalogImportError, "forbidden application"
            ):
                import_catalog.validate_target_url(LOCAL_TARGET)
        with patch.dict(os.environ, {"DATABASE_PUBLIC_URL": LOCAL_TARGET}, clear=False):
            with self.assertRaisesRegex(
                import_catalog.CatalogImportError, "forbidden application"
            ):
                import_catalog.validate_target_url(LOCAL_TARGET)

    def test_apply_requires_explicit_replacement_mode_before_connecting(self):
        expected = import_catalog.ExpectedDemo(1, 1, 1)
        with patch.object(import_catalog, "_connect") as connect:
            with self.assertRaisesRegex(import_catalog.CatalogImportError, "replace-demo"):
                import_catalog.run_import(
                    LOCAL_TARGET, expected, apply=True, replace_demo=False
                )
        connect.assert_not_called()

    def test_failure_injection_cannot_target_preview(self):
        preview_target = "postgresql://local-user@localhost:54321/dealmarket_preview"
        with patch.object(import_catalog, "_connect") as connect:
            with self.assertRaisesRegex(import_catalog.CatalogImportError, "disposable"):
                import_catalog.run_import(
                    preview_target,
                    import_catalog.ExpectedDemo(1, 1, 1),
                    apply=True,
                    replace_demo=True,
                    fail_after_products=True,
                )
        connect.assert_not_called()

    def test_default_cli_mode_is_dry_run(self):
        result = {
            "mode": "dry-run",
            "database": "dealmarket_b4_disposable",
            "categories": 7,
            "products": 17,
            "product_options": 0,
        }
        with patch.dict(os.environ, {import_catalog.TARGET_ENV: LOCAL_TARGET}, clear=False):
            with patch.object(import_catalog, "run_import", return_value=result) as run:
                with redirect_stdout(StringIO()):
                    exit_code = import_catalog.main([
                        "--expected-category-count", "1",
                        "--expected-product-count", "1",
                        "--expected-option-count", "1",
                    ])
        self.assertEqual(exit_code, 0)
        self.assertFalse(run.call_args.kwargs["apply"])

    def test_cli_requires_all_expected_demo_counts(self):
        with patch.dict(os.environ, {import_catalog.TARGET_ENV: LOCAL_TARGET}, clear=False):
            with patch.object(import_catalog, "run_import") as run:
                with redirect_stderr(StringIO()):
                    exit_code = import_catalog.main([])
        self.assertEqual(exit_code, 2)
        run.assert_not_called()


class CatalogImportTransactionTests(unittest.TestCase):
    class Cursor:
        pass

    class Connection:
        def __init__(self):
            self.cursor_value = CatalogImportTransactionTests.Cursor()
            self.committed = False
            self.rolled_back = False
            self.closed = False

        def cursor(self):
            return self.cursor_value

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    def test_dry_run_is_read_only_and_always_rolled_back(self):
        connection = self.Connection()
        with patch.object(import_catalog, "_connect", return_value=connection) as connect:
            with patch.object(
                import_catalog, "_preflight", return_value=PREFLIGHT_STATE
            ):
                result = import_catalog.run_import(
                    LOCAL_TARGET, import_catalog.ExpectedDemo(1, 1, 1)
                )
        connect.assert_called_once_with(LOCAL_TARGET, readonly=True)
        self.assertEqual(result["mode"], "dry-run")
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.closed)

    def test_apply_failure_rolls_back_and_closes(self):
        connection = self.Connection()
        with patch.object(import_catalog, "_connect", return_value=connection):
            with patch.object(
                import_catalog, "_preflight", return_value=PREFLIGHT_STATE
            ):
                with patch.object(import_catalog, "_lock_catalog_and_reference_tables"):
                    with patch.object(import_catalog, "_create_catalog_backup"):
                        with patch.object(
                            import_catalog,
                            "_insert_plan",
                            side_effect=RuntimeError("injected"),
                        ):
                            with self.assertRaisesRegex(RuntimeError, "injected"):
                                import_catalog.run_import(
                                    LOCAL_TARGET,
                                    import_catalog.ExpectedDemo(1, 1, 1),
                                    apply=True,
                                    replace_demo=True,
                                )
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.closed)

    def test_apply_creates_validated_backup_before_inserting(self):
        connection = self.Connection()
        artifact = import_catalog.BackupArtifact(
            path=import_catalog.Path("backup.json"),
            sha256="a" * 64,
            counts=import_catalog.ExpectedDemo(1, 1, 1),
        )
        calls = []
        with patch.object(import_catalog, "_connect", return_value=connection):
            with patch.object(
                import_catalog, "_preflight", return_value=PREFLIGHT_STATE
            ):
                with patch.object(import_catalog, "_lock_catalog_and_reference_tables"):
                    with patch.object(
                        import_catalog,
                        "_create_catalog_backup",
                        side_effect=lambda *args, **kwargs: calls.append("backup") or artifact,
                    ):
                        with patch.object(
                            import_catalog,
                            "_insert_plan",
                            side_effect=lambda *args, **kwargs: calls.append("insert"),
                        ):
                            with patch.object(import_catalog, "_configure_sequences"):
                                with patch.object(import_catalog, "_verify_imported_catalog"):
                                    connection.cursor_value.execute = lambda *args: None
                                    result = import_catalog.run_import(
                                        LOCAL_TARGET,
                                        import_catalog.ExpectedDemo(1, 1, 1),
                                        apply=True,
                                        replace_demo=True,
                                        backup_dir=tempfile.gettempdir(),
                                    )
        self.assertEqual(calls, ["backup", "insert"])
        self.assertEqual(result["backup_sha256"], "a" * 64)
        self.assertTrue(connection.committed)

    def test_backup_failure_blocks_all_catalog_writes(self):
        connection = self.Connection()
        with patch.object(import_catalog, "_connect", return_value=connection):
            with patch.object(
                import_catalog, "_preflight", return_value=PREFLIGHT_STATE
            ):
                with patch.object(import_catalog, "_lock_catalog_and_reference_tables"):
                    with patch.object(
                        import_catalog,
                        "_create_catalog_backup",
                        side_effect=import_catalog.CatalogImportError("backup failed"),
                    ):
                        with patch.object(import_catalog, "_insert_plan") as insert:
                            with self.assertRaisesRegex(
                                import_catalog.CatalogImportError, "backup failed"
                            ):
                                import_catalog.run_import(
                                    LOCAL_TARGET,
                                    import_catalog.ExpectedDemo(1, 1, 1),
                                    apply=True,
                                    replace_demo=True,
                                )
        insert.assert_not_called()
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)


if __name__ == "__main__":
    unittest.main()
