import os
import unittest
from unittest.mock import patch

from scripts import migrate_orders_v2 as migrate


class ScriptedCursor:
    def __init__(self, fetchone_values=(), fetchall_values=()):
        self.fetchone_values = list(fetchone_values)
        self.fetchall_values = list(fetchall_values)
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.fetchone_values.pop(0)

    def fetchall(self):
        return self.fetchall_values.pop(0)

    def update_queries(self):
        return [
            (query, params)
            for query, params in self.queries
            if query.strip().startswith("UPDATE")
        ]


class ScriptedConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.readonly = None

    def cursor(self):
        return self.cursor_instance

    def set_session(self, readonly=None, autocommit=None):
        self.readonly = readonly

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


VALID_TARGET_URL = "postgresql://localhost/orders_v2_disposable_test"
VALID_PRODUCTION_URL = "postgresql://prod-db.example.internal/deal_market_nl"


def _preflight_fetchone_values(require_readonly):
    values = []
    if require_readonly:
        values.append(("on",))
    values.append(("orders_v2_disposable_test",))
    return values


ORDERS_SCHEMA_ROWS = [
    ("orders", col) for col in sorted(migrate.REQUIRED_ORDERS_COLUMNS)
] + [("clients", col) for col in sorted(migrate.REQUIRED_CLIENTS_COLUMNS)]


class ImportSideEffectTests(unittest.TestCase):
    """The script must never connect to a database, and must never run its
    migration, as a side effect of being imported."""

    def test_importing_the_module_never_connects_to_a_database(self):
        with patch.object(
            migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
        ):
            import importlib

            importlib.reload(migrate)  # re-executes module top-level code only

    def test_module_defines_main_guard_not_top_level_execution(self):
        with open(migrate.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn('if __name__ == "__main__":', source)


class ComputeNewStatusMappingTests(unittest.TestCase):
    """Exact mapping for all 8 legacy statuses -- unchanged by this
    corrective follow-up."""

    def test_pending(self):
        self.assertEqual(
            migrate.compute_new_status("pending", False, None), ("unpaid", "new")
        )

    def test_awaiting_payment(self):
        self.assertEqual(
            migrate.compute_new_status("awaiting_payment", False, "iban"),
            ("unpaid", "new"),
        )

    def test_payment_reported(self):
        self.assertEqual(
            migrate.compute_new_status("payment_reported", False, "iban"),
            ("payment_reported", "new"),
        )

    def test_cash_on_delivery(self):
        self.assertEqual(
            migrate.compute_new_status("cash_on_delivery", False, "cash"),
            ("unpaid", "new"),
        )

    def test_paid(self):
        self.assertEqual(
            migrate.compute_new_status("paid", True, "iban"), ("paid", "new")
        )

    def test_preparing_and_done_use_signals_exactly_as_specified(self):
        for status, expected_fulfillment in (("preparing", "picking"), ("done", "delivered")):
            with self.subTest(status=status):
                self.assertEqual(
                    migrate.compute_new_status(status, True, "iban"),
                    ("paid", expected_fulfillment),
                )
                self.assertEqual(
                    migrate.compute_new_status(status, False, "cash"),
                    ("unpaid", expected_fulfillment),
                )
                self.assertEqual(
                    migrate.compute_new_status(status, False, "iban"),
                    ("payment_reported", expected_fulfillment),
                )
                self.assertEqual(
                    migrate.compute_new_status(status, False, None),
                    ("payment_reported", expected_fulfillment),
                )

    def test_cancelled_never_becomes_refunded(self):
        paid_before_cancel = migrate.compute_new_status("cancelled", True, "iban")
        self.assertEqual(paid_before_cancel, ("paid", "cancelled"))
        self.assertNotIn("refunded", paid_before_cancel)

        never_paid = migrate.compute_new_status("cancelled", False, "iban")
        self.assertEqual(never_paid, ("unpaid", "cancelled"))
        self.assertNotIn("refunded", never_paid)

    def test_unknown_status_raises(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.compute_new_status("some_future_status", False, None)

    def test_all_eight_known_statuses_are_exactly_the_approved_set(self):
        self.assertEqual(
            set(migrate.KNOWN_LEGACY_STATUSES),
            {
                "pending", "awaiting_payment", "payment_reported",
                "cash_on_delivery", "paid", "preparing", "done", "cancelled",
            },
        )
        for status in migrate.KNOWN_LEGACY_STATUSES:
            with self.subTest(status=status):
                migrate.compute_new_status(status, False, None)  # must not raise


class ReviewNeededReportingTests(unittest.TestCase):
    """Corrective requirement 3: ambiguous preparing/done rows that map to
    payment_reported via the inventory_deducted/payment_method fallback
    must be explicitly listed (by orders.id and legacy order_id), even
    though their computed mapping is unchanged."""

    def test_ambiguous_preparing_row_is_listed_as_review_needed(self):
        row = _fresh_order(1, "preparing", inventory_deducted=False, payment_method="iban")
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)

        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.updates[0].payment_status, "payment_reported")
        self.assertEqual(plan.review_needed_rows, ((1, 1000),))

    def test_ambiguous_done_row_with_no_payment_method_is_listed(self):
        row = _fresh_order(2, "done", inventory_deducted=False, payment_method=None)
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)

        self.assertEqual(plan.updates[0].payment_status, "payment_reported")
        self.assertEqual(plan.review_needed_rows, ((2, 2000),))

    def test_cash_preparing_row_is_not_review_needed(self):
        row = _fresh_order(3, "preparing", inventory_deducted=False, payment_method="cash")
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(plan.review_needed_rows, ())

    def test_paid_preparing_row_is_not_review_needed(self):
        row = _fresh_order(4, "preparing", inventory_deducted=True, payment_method="iban")
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(plan.review_needed_rows, ())

    def test_non_preparing_done_rows_are_never_review_needed(self):
        row = _fresh_order(5, "pending", inventory_deducted=False, payment_method=None)
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(plan.review_needed_rows, ())

    def test_mapping_itself_is_unaffected_by_review_needed_status(self):
        # The review-needed flag is purely additive reporting -- it must
        # not change payment_status/fulfillment_status vs. an identical
        # non-flagged row.
        flagged = migrate.compute_new_status("preparing", False, "iban")
        self.assertEqual(flagged, ("payment_reported", "picking"))


# order_rows tuple shape: (id, order_id, status, inventory_deducted,
# payment_method, telegram_id, payment_status, fulfillment_status, source,
# client_id)
#
# order_id defaults to id * 1000 (deliberately different from id) so tests
# cannot pass merely because id and order_id happen to coincide -- the
# boundary and UPDATE logic must key on id specifically.

def _fresh_order(id_, status, inventory_deducted=False, payment_method=None,
                  telegram_id=100, order_id=None):
    if order_id is None:
        order_id = id_ * 1000
    return (
        id_, order_id, status, inventory_deducted, payment_method, telegram_id,
        "unpaid", "new", "telegram", None,
    )


class PlanMigrationClientBackfillTests(unittest.TestCase):
    """client_id backfill via clients.telegram_id -> clients.id, and
    unmatched clients are left NULL and reported."""

    def test_matched_client_id_is_backfilled(self):
        rows = [_fresh_order(1, "pending", telegram_id=555)]
        plan = migrate.plan_migration(rows, {555: 42}, historical_max_id=1000)

        self.assertEqual(plan.updates[0].client_id, 42)
        self.assertEqual(plan.client_matched_count, 1)
        self.assertEqual(plan.client_unmatched_count, 0)

    def test_unmatched_client_leaves_client_id_null_and_is_counted(self):
        rows = [_fresh_order(1, "pending", telegram_id=555)]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1000)

        self.assertIsNone(plan.updates[0].client_id)
        self.assertEqual(plan.client_matched_count, 0)
        self.assertEqual(plan.client_unmatched_count, 1)

    def test_no_fake_client_is_ever_created(self):
        rows = [_fresh_order(1, "pending", telegram_id=999)]
        plan = migrate.plan_migration(rows, {999: 7}, historical_max_id=1000)
        self.assertEqual(plan.updates[0].client_id, 7)
        rows2 = [_fresh_order(2, "pending", telegram_id=999)]
        plan2 = migrate.plan_migration(rows2, {}, historical_max_id=1000)
        self.assertIsNone(plan2.updates[0].client_id)

    def test_already_set_client_id_is_never_touched_or_recounted(self):
        row = (
            1, 1000, "pending", False, None, 555,
            "unpaid", "new", "telegram", 999,  # client_id already set
        )
        plan = migrate.plan_migration([row], {555: 42}, historical_max_id=1000)

        self.assertEqual(plan.updates[0].client_id, 999)  # untouched
        self.assertEqual(plan.client_matched_count, 0)
        self.assertEqual(plan.client_unmatched_count, 0)


class PlanMigrationIdempotencyTests(unittest.TestCase):
    """Already-migrated (non-default) rows are not blindly overwritten on
    a re-run."""

    def test_row_with_non_default_payment_status_is_skipped(self):
        row = (1, 1000, "paid", True, "iban", 555, "paid", "new", "telegram", None)
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(plan.already_migrated_count, 1)
        self.assertEqual(plan.updates, ())

    def test_row_with_non_default_fulfillment_status_is_skipped(self):
        row = (1, 1000, "preparing", True, "iban", 555, "unpaid", "picking", "telegram", None)
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(plan.already_migrated_count, 1)
        self.assertEqual(plan.updates, ())

    def test_fresh_row_is_eligible(self):
        plan = migrate.plan_migration([_fresh_order(1, "pending")], {}, historical_max_id=1000)
        self.assertEqual(plan.already_migrated_count, 0)
        self.assertEqual(len(plan.updates), 1)

    def test_source_does_not_affect_eligibility(self):
        row = (1, 1000, "pending", False, None, 555, "unpaid", "new", "telegram", None)
        plan = migrate.plan_migration([row], {}, historical_max_id=1000)
        self.assertEqual(len(plan.updates), 1)


class PlanMigrationUnknownStatusTests(unittest.TestCase):
    """Unknown legacy statuses are collected as anomalies, not silently
    mapped. Anomaly tuples now carry (id, order_id, status)."""

    def test_unknown_status_is_reported_not_raised_during_planning(self):
        rows = [
            _fresh_order(1, "pending"),
            _fresh_order(2, "some_new_status"),
        ]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1000)

        self.assertEqual(plan.unknown_status_anomalies, ((2, 2000, "some_new_status"),))
        self.assertEqual(len(plan.updates), 1)  # the known row is still planned

    def test_multiple_unknown_statuses_are_all_collected_in_one_pass(self):
        rows = [
            _fresh_order(1, "alpha"),
            _fresh_order(2, "beta"),
            _fresh_order(3, "pending"),
        ]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1000)
        self.assertEqual(
            plan.unknown_status_anomalies,
            ((1, 1000, "alpha"), (2, 2000, "beta")),
        )


class PlanMigrationHistoricalBoundaryTests(unittest.TestCase):
    """Corrective requirement 1: a FIXED orders.id boundary, not
    orders.order_id and not "still at Checkpoint A defaults" alone."""

    def test_row_with_id_above_boundary_is_never_touched(self):
        rows = [
            _fresh_order(1, "pending"),
            _fresh_order(2, "pending"),  # above the boundary below
        ]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1)

        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.updates[0].id, 1)
        self.assertEqual(plan.post_boundary_count, 1)
        self.assertEqual(plan.in_scope_count, 1)

    def test_row_with_id_above_boundary_is_excluded_even_if_otherwise_eligible_and_known(self):
        # Deliberately gives the above-boundary row a large order_id so a
        # boundary check that accidentally used order_id instead of id
        # would behave differently -- proving id, not order_id, governs.
        rows = [
            _fresh_order(1, "pending", order_id=999999),
            _fresh_order(50, "pending", order_id=1),
        ]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1)

        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.updates[0].id, 1)
        self.assertEqual(plan.updates[0].order_id, 999999)

    def test_boundary_exactly_at_an_ids_value_includes_that_row(self):
        rows = [_fresh_order(7, "pending")]
        plan = migrate.plan_migration(rows, {}, historical_max_id=7)
        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.post_boundary_count, 0)

    def test_post_boundary_rows_do_not_count_as_already_migrated_or_anomalies(self):
        rows = [_fresh_order(5, "totally_unknown_status")]
        plan = migrate.plan_migration(rows, {}, historical_max_id=1)

        self.assertEqual(plan.post_boundary_count, 1)
        self.assertEqual(plan.already_migrated_count, 0)
        self.assertEqual(plan.unknown_status_anomalies, ())

    def test_dry_run_and_apply_agree_on_the_same_boundary(self):
        rows = [
            _fresh_order(1, "pending", telegram_id=555),
            _fresh_order(2, "pending", telegram_id=556),
        ]
        dry_plan = migrate.plan_migration(rows, {}, historical_max_id=1)
        apply_plan = migrate.plan_migration(rows, {}, historical_max_id=1)

        self.assertEqual(
            [u.id for u in dry_plan.updates], [u.id for u in apply_plan.updates]
        )
        self.assertEqual(dry_plan.post_boundary_count, apply_plan.post_boundary_count)


class RunMigrationDryRunTests(unittest.TestCase):
    """Dry-run performs zero writes. With no explicit boundary, the
    current MAX(id) is auto-discovered from the unbounded fetch."""

    def test_dry_run_issues_no_update_and_rolls_back(self):
        cursor = ScriptedCursor(
            fetchone_values=_preflight_fetchone_values(require_readonly=True),
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(VALID_TARGET_URL, apply=False)

        self.assertEqual(cursor.update_queries(), [])
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(connection.readonly)
        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.historical_max_id, 1)  # auto-discovered from the one fetched row

    def test_dry_run_with_explicit_boundary_does_not_recompute_it(self):
        cursor = ScriptedCursor(
            fetchone_values=_preflight_fetchone_values(require_readonly=True),
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_TARGET_URL, apply=False, historical_max_id=99
            )

        self.assertEqual(plan.historical_max_id, 99)
        # queries: [0] readonly check, [1] current_database, [2] schema
        # verification, [3] the bounded orders fetch.
        fetch_query, fetch_params = cursor.queries[3]
        self.assertIn("WHERE id <= %s", fetch_query)
        self.assertEqual(fetch_params, (99,))


class RunMigrationApplyTests(unittest.TestCase):
    """Apply mode writes inside one transaction, only ever setting
    source/payment_status/fulfillment_status/client_id -- never status,
    phone, address, or any delivery/customer_name field -- and every write
    is guarded by both the row id and the historical boundary."""

    def test_apply_requires_explicit_historical_max_id(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.run_migration(VALID_TARGET_URL, apply=True)

    def test_apply_writes_expected_columns_only_and_commits_once(self):
        cursor = ScriptedCursor(
            fetchone_values=[("orders_v2_disposable_test",)],  # no readonly check in apply mode
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_TARGET_URL, apply=True, historical_max_id=1
            )

        updates = cursor.update_queries()
        self.assertEqual(len(updates), 1)
        query, params = updates[0]
        self.assertIn("SET source = 'telegram'", query)
        self.assertIn("payment_status = %s", query)
        self.assertIn("fulfillment_status = %s", query)
        self.assertIn("client_id = COALESCE(client_id, %s)", query)
        self.assertIn("WHERE id = %s", query)
        self.assertIn("AND id <= %s", query)
        self.assertNotIn("order_id = %s", query)
        self.assertNotIn("status = %s", query.replace("payment_status", "").replace(
            "fulfillment_status", ""
        ))
        self.assertNotIn("phone", query)
        self.assertNotIn("address", query)
        self.assertNotIn("customer_name", query)
        self.assertNotIn("delivery_", query)
        self.assertEqual(params, ("unpaid", "new", 42, 1, 1))
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        self.assertFalse(connection.readonly)

    def test_apply_with_no_eligible_rows_still_commits_cleanly(self):
        cursor = ScriptedCursor(
            fetchone_values=[("orders_v2_disposable_test",)],
            fetchall_values=[ORDERS_SCHEMA_ROWS, [], []],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_TARGET_URL, apply=True, historical_max_id=100
            )

        self.assertEqual(cursor.update_queries(), [])
        self.assertTrue(connection.committed)

    def test_apply_never_writes_a_row_whose_id_exceeds_the_boundary(self):
        # Corrective requirement 2 (integration-level proof): even though
        # the SELECT is itself bounded, plan_migration's own boundary
        # filter is the thing that keeps such a row out of plan.updates in
        # the first place -- confirm the full run_migration path produces
        # exactly one UPDATE, for the in-scope row only.
        cursor = ScriptedCursor(
            fetchone_values=[("orders_v2_disposable_test",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],  # bounded fetch: only id<=1
                [],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_TARGET_URL, apply=True, historical_max_id=1
            )

        # queries: [0] current_database (no readonly check in apply mode),
        # [1] schema verification, [2] the bounded orders fetch.
        fetch_query, fetch_params = cursor.queries[2]
        self.assertIn("WHERE id <= %s", fetch_query)
        self.assertEqual(fetch_params, (1,))
        self.assertEqual(len(cursor.update_queries()), 1)


class RunMigrationUnknownStatusAbortsTests(unittest.TestCase):
    """An unknown legacy status aborts the whole run before any write, in
    both dry-run and apply mode."""

    def test_apply_mode_aborts_with_zero_writes_on_unknown_status(self):
        cursor = ScriptedCursor(
            fetchone_values=[("orders_v2_disposable_test",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "totally_unrecognized")],
                [],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            with self.assertRaises(migrate.OrdersMigrationError):
                migrate.run_migration(VALID_TARGET_URL, apply=True, historical_max_id=1)

        self.assertEqual(cursor.update_queries(), [])
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)


class RunMigrationExceptionRollsBackTests(unittest.TestCase):
    """Any exception during apply rolls back."""

    def test_exception_during_update_rolls_back_and_does_not_commit(self):
        class ExplodingCursor(ScriptedCursor):
            def execute(self, query, params=None):
                super().execute(query, params)
                if query.strip().startswith("UPDATE"):
                    raise RuntimeError("simulated failure mid-apply")

        cursor = ExplodingCursor(
            fetchone_values=[("orders_v2_disposable_test",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            with self.assertRaises(RuntimeError):
                migrate.run_migration(VALID_TARGET_URL, apply=True, historical_max_id=1)

        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)


class ValidateTargetUrlSafetyTests(unittest.TestCase):
    """Explicit DB URL / environment requirement for normal mode, mirroring
    scripts/import_catalog.py's existing guarded-tooling conventions.
    Unchanged and unweakened by the production-mode addition."""

    def test_missing_target_url_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_target_url(None)

    def test_target_matching_database_url_env_is_rejected(self):
        with patch.dict(os.environ, {"DATABASE_URL": VALID_TARGET_URL}):
            with self.assertRaises(migrate.OrdersMigrationError):
                migrate.validate_target_url(VALID_TARGET_URL)

    def test_railway_remote_host_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_target_url(
                "postgresql://foo.proxy.rlwy.net/disposable_test"
            )

    def test_non_loopback_host_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_target_url("postgresql://example.com/disposable_test")

    def test_realistic_production_shaped_url_is_still_rejected(self):
        # Corrective requirement: normal mode must still reject a
        # production-shaped target after the production-mode addition.
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_target_url(VALID_PRODUCTION_URL)

    def test_database_name_without_safety_marker_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_target_url("postgresql://localhost/production")

    def test_valid_loopback_disposable_target_is_accepted(self):
        identity = migrate.validate_target_url(VALID_TARGET_URL)
        self.assertEqual(identity.host, "localhost")
        self.assertEqual(identity.database, "orders_v2_disposable_test")


class ValidateProductionTargetUrlTests(unittest.TestCase):
    """Corrective requirement 2: production validation is an independent
    function, not a relaxed version of normal mode -- it does not apply
    the loopback/disposable-name restrictions, but still requires a
    well-formed PostgreSQL URL."""

    def test_missing_target_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_production_target_url(None)

    def test_non_postgres_scheme_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_production_target_url("mysql://prod.example.com/db")

    def test_missing_database_is_rejected(self):
        with self.assertRaises(migrate.OrdersMigrationError):
            migrate.validate_production_target_url("postgresql://prod.example.com/")

    def test_a_real_remote_non_loopback_host_is_accepted(self):
        # Deliberately the opposite of validate_target_url's behaviour:
        # production targets are, by definition, never loopback/disposable.
        identity = migrate.validate_production_target_url(VALID_PRODUCTION_URL)
        self.assertEqual(identity.host, "prod-db.example.internal")
        self.assertEqual(identity.database, "deal_market_nl")

    def test_does_not_reject_based_on_matching_database_url_env(self):
        # Independent from validate_target_url: production mode has no
        # "matches DATABASE_URL" restriction of its own -- authorization
        # for production comes from the explicit --production flag and
        # dedicated environment variable, checked by the caller, not from
        # this function rejecting a coincidental URL match.
        with patch.dict(os.environ, {"DATABASE_URL": VALID_PRODUCTION_URL}):
            identity = migrate.validate_production_target_url(VALID_PRODUCTION_URL)
        self.assertEqual(identity.database, "deal_market_nl")


class RunMigrationProductionModeTests(unittest.TestCase):
    """Corrective requirement 2: production mode must be a separate,
    explicitly-authorized branch that fails closed if any authorization
    piece is missing, and must never implicitly fall back to normal
    mode's target or to DATABASE_URL."""

    def test_production_dry_run_without_boundary_is_rejected_before_any_connection(self):
        # Corrective requirement: production always requires an explicit
        # boundary, whether or not --apply is passed -- rehearsal's
        # auto-discovery convenience never applies to production.
        with patch.object(
            migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
        ):
            with self.assertRaises(migrate.OrdersMigrationError):
                migrate.run_migration(
                    VALID_PRODUCTION_URL, apply=False, production=True,
                    historical_max_id=None,
                )

    def test_production_dry_run_with_boundary_is_readonly_writes_nothing_and_never_commits(self):
        # Corrective requirement: --production without --apply is a real
        # production READ-ONLY dry-run -- explicit authorization + boundary
        # is enough on its own, no --apply required. Proves: (1) allowed
        # with explicit production authorization + boundary, (2) opens a
        # readonly connection, (3) issues zero mutation SQL, (4) never
        # commits, and that it uses the exact same plan_migration/fetch
        # logic as apply (same ORDERS_SCHEMA_ROWS/_fresh_order fixtures,
        # same resulting plan.updates).
        cursor = ScriptedCursor(
            fetchone_values=[("on",), ("deal_market_nl",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_PRODUCTION_URL, apply=False, production=True,
                historical_max_id=1,
            )

        self.assertEqual(identity.database, "deal_market_nl")
        self.assertEqual(len(plan.updates), 1)  # same planning logic as apply
        self.assertEqual(cursor.update_queries(), [])
        self.assertTrue(connection.readonly)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_production_apply_without_historical_max_id_is_rejected(self):
        with patch.object(
            migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
        ):
            with self.assertRaises(migrate.OrdersMigrationError):
                migrate.run_migration(
                    VALID_PRODUCTION_URL, apply=True, production=True,
                    historical_max_id=None,
                )

    def test_production_apply_with_missing_target_url_is_rejected(self):
        with patch.object(
            migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
        ):
            with self.assertRaises(migrate.OrdersMigrationError):
                migrate.run_migration(
                    None, apply=True, production=True, historical_max_id=5,
                )

    def test_fully_authorized_production_apply_writes_and_commits(self):
        cursor = ScriptedCursor(
            fetchone_values=[("deal_market_nl",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.object(migrate.psycopg2, "connect", return_value=connection):
            identity, plan = migrate.run_migration(
                VALID_PRODUCTION_URL, apply=True, production=True, historical_max_id=1,
            )

        self.assertEqual(identity.database, "deal_market_nl")
        self.assertEqual(len(cursor.update_queries()), 1)
        self.assertTrue(connection.committed)

    def test_main_production_mode_never_reads_the_normal_rehearsal_env_var(self):
        with patch.dict(
            os.environ,
            {
                migrate.TARGET_ENV: VALID_TARGET_URL,
                migrate.PRODUCTION_TARGET_ENV: "",
            },
            clear=False,
        ):
            os.environ.pop(migrate.PRODUCTION_TARGET_ENV, None)
            with patch.object(
                migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
            ):
                exit_code = migrate.main(
                    ["--production", "--apply", "--historical-max-id", "5"]
                )
        self.assertEqual(exit_code, 2)

    def test_main_production_mode_never_falls_back_to_database_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": VALID_PRODUCTION_URL}, clear=False):
            os.environ.pop(migrate.PRODUCTION_TARGET_ENV, None)
            with patch.object(
                migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
            ):
                exit_code = migrate.main(
                    ["--production", "--apply", "--historical-max-id", "5"]
                )
        self.assertEqual(exit_code, 2)

    def test_main_production_without_apply_is_a_successful_readonly_dry_run(self):
        # --production no longer requires --apply -- omitting it is the
        # documented way to run a real production dry-run from the CLI.
        cursor = ScriptedCursor(
            fetchone_values=[("on",), ("deal_market_nl",)],
            fetchall_values=[
                ORDERS_SCHEMA_ROWS,
                [_fresh_order(1, "pending", telegram_id=555)],
                [(555, 42)],
            ],
        )
        connection = ScriptedConnection(cursor)
        with patch.dict(
            os.environ, {migrate.PRODUCTION_TARGET_ENV: VALID_PRODUCTION_URL}, clear=False
        ):
            with patch.object(migrate.psycopg2, "connect", return_value=connection):
                exit_code = migrate.main(
                    ["--production", "--historical-max-id", "1"]
                )
        self.assertEqual(exit_code, 0)
        self.assertEqual(cursor.update_queries(), [])
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)

    def test_main_production_dry_run_without_boundary_is_rejected(self):
        with patch.dict(
            os.environ, {migrate.PRODUCTION_TARGET_ENV: VALID_PRODUCTION_URL}, clear=False
        ):
            with patch.object(
                migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
            ):
                exit_code = migrate.main(["--production"])
        self.assertEqual(exit_code, 2)

    def test_main_normal_apply_without_boundary_is_rejected(self):
        with patch.dict(os.environ, {migrate.TARGET_ENV: VALID_TARGET_URL}, clear=False):
            with patch.object(
                migrate.psycopg2, "connect", side_effect=AssertionError("must not connect")
            ):
                exit_code = migrate.main(["--apply"])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
