"""Orders v2 historical backfill (Checkpoint B + corrective follow-up).

Populates the Orders v2 columns added in Checkpoint A
(orders.payment_status, orders.fulfillment_status, orders.source,
orders.client_id) for a FIXED, explicitly-bounded population of EXISTING
orders, deterministically, from data already on each row. The legacy
orders.status column is never modified and stays authoritative until a
later, separate cutover checkpoint.

HISTORICAL BOUNDARY
--------------------
Eligibility is no longer "still at the Checkpoint A defaults" alone -- that
boundary is not permanent, because a *new* Orders v2 order (created after
runtime dual-write starts) can also legitimately be unpaid/new, and must
never be mistaken for a historical row.

Instead, migration is bounded by a fixed orders.id (the SERIAL primary
key -- never orders.order_id, which is neither the true row identity nor
guaranteed unique). A dry-run with no boundary supplied auto-discovers and
reports the CURRENT MAX(id) for the operator to review; it is never used
directly for --apply. --apply always requires that same boundary to be
supplied explicitly via --historical-max-id, so a later re-run with the
same boundary can never touch an order created after it, no matter how
long ago the boundary was decided.

PRODUCTION MODE
----------------
Normal mode (the default) can only ever target a loopback,
disposable/test/preview-named database -- exactly as before -- and is
meant for rehearsal. A completely separate, explicit --production branch
exists for the real target: it always requires --production and an
explicit --historical-max-id (auto-discovery is a rehearsal-only
convenience and never applies to production), and reads the target
exclusively from its own dedicated environment variable (never
DATABASE_URL, never the rehearsal target variable), failing closed if
either is missing.

--production without --apply is a real production READ-ONLY dry-run: a
readonly connection (verified via a live read-only-transaction check,
same as rehearsal dry-run), the exact same fetch/plan logic as apply,
zero mutation SQL, and it always rolls back and never commits.
--production with --apply is the real write, requiring the same
--historical-max-id, and runs as one transaction that rolls back on any
failure. Production targets are validated by an independent function with
no shared logic with normal-mode validation -- production access is a
separate branch, not a relaxation of the rehearsal path's restrictions.

This script never runs as a side effect of import, and never runs during
ordinary bot/admin startup.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import psycopg2


TARGET_ENV = "ORDERS_V2_MIGRATION_TARGET_URL"
PRODUCTION_TARGET_ENV = "ORDERS_V2_MIGRATION_PRODUCTION_TARGET_URL"
FORBIDDEN_HOST_PARTS = (
    "railway.internal",
    "proxy.rlwy.net",
    "kodama.proxy.rlwy.net",
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

KNOWN_LEGACY_STATUSES = (
    "pending",
    "awaiting_payment",
    "payment_reported",
    "cash_on_delivery",
    "paid",
    "preparing",
    "done",
    "cancelled",
)

REQUIRED_ORDERS_COLUMNS = {
    "id",
    "order_id",
    "status",
    "inventory_deducted",
    "payment_method",
    "telegram_id",
    "payment_status",
    "fulfillment_status",
    "source",
    "client_id",
}
REQUIRED_CLIENTS_COLUMNS = {"id", "telegram_id"}


class OrdersMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetIdentity:
    host: str
    database: str


@dataclass(frozen=True)
class PlannedUpdate:
    id: object
    order_id: object
    old_status: str
    payment_status: str
    fulfillment_status: str
    client_id: object


@dataclass(frozen=True)
class MigrationPlan:
    historical_max_id: int
    total_orders_examined: int
    post_boundary_count: int
    already_migrated_count: int
    status_breakdown: dict
    client_matched_count: int
    client_unmatched_count: int
    unknown_status_anomalies: tuple
    review_needed_rows: tuple
    updates: tuple

    @property
    def in_scope_count(self):
        return self.total_orders_examined - self.post_boundary_count

    @property
    def eligible_count(self):
        return len(self.updates) + len(self.unknown_status_anomalies)


# ---------------------------------------------------------------------------
# Pure mapping logic -- no database access, fully unit-testable in isolation.
# ---------------------------------------------------------------------------

def _derive_signal_based_payment_status(inventory_deducted, payment_method):
    """Used for legacy 'preparing' and 'done' orders, which the old
    transition table allowed to be reached without ever passing through an
    explicit 'paid' status (payment_reported -> preparing/done directly).
    inventory_deducted is only ever set TRUE by an explicit 'paid'
    transition and is never reset except by cancellation-restoration, so it
    is the one reliable secondary signal available."""
    if inventory_deducted:
        return "paid"
    if payment_method == "cash":
        return "unpaid"
    return "payment_reported"


def compute_new_status(old_status, inventory_deducted, payment_method):
    """Pure mapping: legacy orders.status + the two existing deterministic
    signals -> (payment_status, fulfillment_status).

    Raises OrdersMigrationError for anything outside the eight known legacy
    values -- this function must never guess at an unrecognized status."""
    if old_status == "pending":
        return "unpaid", "new"
    if old_status == "awaiting_payment":
        return "unpaid", "new"
    if old_status == "payment_reported":
        return "payment_reported", "new"
    if old_status == "cash_on_delivery":
        return "unpaid", "new"
    if old_status == "paid":
        return "paid", "new"
    if old_status == "preparing":
        return (
            _derive_signal_based_payment_status(inventory_deducted, payment_method),
            "picking",
        )
    if old_status == "done":
        return (
            _derive_signal_based_payment_status(inventory_deducted, payment_method),
            "delivered",
        )
    if old_status == "cancelled":
        # Never inferred as 'refunded': we only know money was collected
        # (inventory_deducted) or we don't -- a refund is a real-world event
        # this schema has never recorded, and this migration must not
        # invent one.
        return ("paid" if inventory_deducted else "unpaid"), "cancelled"
    raise OrdersMigrationError(f"unknown legacy status: {old_status!r}")


def _is_review_needed(old_status, inventory_deducted, payment_method):
    """True exactly for the one ambiguous historical bucket: a
    preparing/done order that skipped an explicit 'paid' transition and
    isn't cash. Its mapping (payment_reported) is unchanged -- this only
    flags it for a human to look at."""
    return (
        old_status in ("preparing", "done")
        and not inventory_deducted
        and payment_method != "cash"
    )


def _row_is_eligible(payment_status, fulfillment_status):
    """Idempotency/re-run guard: a row is only touched if BOTH
    payment_status and fulfillment_status are still exactly at the
    Checkpoint A defaults ('unpaid', 'new'). This is necessary but not
    sufficient on its own -- see the historical_max_id boundary below,
    which is what actually makes the migration safe to re-run forever
    without ever touching a new, legitimately-unpaid/new post-cutover
    order.

    orders.source is deliberately NOT part of this check: it is always
    'telegram' for every historical row regardless of migration state, so
    it carries no information about whether a row has been migrated."""
    return payment_status == "unpaid" and fulfillment_status == "new"


def plan_migration(order_rows, client_id_by_telegram_id, historical_max_id):
    """Pure planning pass over already-fetched rows, bounded by
    historical_max_id (orders.id). Any row with id > historical_max_id is
    excluded entirely, before any other check -- it is never considered
    "already migrated" or "eligible"; it simply does not exist for this
    run. This is what guarantees a post-cutover order can never be
    rewritten, regardless of its payment_status/fulfillment_status values.

    Any unknown legacy status is collected as an anomaly rather than
    raised immediately, so a single pass can report every offending row at
    once instead of stopping at the first one."""
    status_breakdown = {}
    client_matched = 0
    client_unmatched = 0
    already_migrated = 0
    post_boundary = 0
    anomalies = []
    review_needed = []
    updates = []

    for (
        row_id,
        order_id,
        status,
        inventory_deducted,
        payment_method,
        telegram_id,
        payment_status,
        fulfillment_status,
        _source,
        client_id,
    ) in order_rows:
        if row_id > historical_max_id:
            post_boundary += 1
            continue

        if not _row_is_eligible(payment_status, fulfillment_status):
            already_migrated += 1
            continue

        try:
            new_payment_status, new_fulfillment_status = compute_new_status(
                status, bool(inventory_deducted), payment_method
            )
        except OrdersMigrationError:
            anomalies.append((row_id, order_id, status))
            continue

        status_breakdown[status] = status_breakdown.get(status, 0) + 1

        if _is_review_needed(status, bool(inventory_deducted), payment_method):
            review_needed.append((row_id, order_id))

        new_client_id = client_id
        if client_id is None:
            matched = (
                client_id_by_telegram_id.get(telegram_id)
                if telegram_id is not None
                else None
            )
            if matched is not None:
                new_client_id = matched
                client_matched += 1
            else:
                client_unmatched += 1

        updates.append(
            PlannedUpdate(
                id=row_id,
                order_id=order_id,
                old_status=status,
                payment_status=new_payment_status,
                fulfillment_status=new_fulfillment_status,
                client_id=new_client_id,
            )
        )

    return MigrationPlan(
        historical_max_id=historical_max_id,
        total_orders_examined=len(order_rows),
        post_boundary_count=post_boundary,
        already_migrated_count=already_migrated,
        status_breakdown=status_breakdown,
        client_matched_count=client_matched,
        client_unmatched_count=client_unmatched,
        unknown_status_anomalies=tuple(anomalies),
        review_needed_rows=tuple(review_needed),
        updates=tuple(updates),
    )


# ---------------------------------------------------------------------------
# Target validation.
#
# validate_target_url (normal/rehearsal) and validate_production_target_url
# are deliberately independent functions with no shared logic. Production
# access is a separate branch, not a relaxation of the rehearsal path.
# ---------------------------------------------------------------------------

def validate_target_url(target_url):
    """Normal mode: only ever accepts a loopback, disposable/test/preview
    database. Suitable for rehearsal only."""
    if not target_url:
        raise OrdersMigrationError(f"{TARGET_ENV} is required")
    for production_name in ("DATABASE_URL", "DATABASE_PUBLIC_URL"):
        production_value = os.getenv(production_name)
        if production_value and target_url == production_value:
            raise OrdersMigrationError(
                "target matches a forbidden application database variable"
            )
    try:
        parsed = urlsplit(target_url)
        host = (parsed.hostname or "").lower()
        database = unquote(parsed.path.lstrip("/")).strip()
    except ValueError as exc:
        raise OrdersMigrationError("target URL is invalid") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise OrdersMigrationError("target must use PostgreSQL")
    if not host or not database:
        raise OrdersMigrationError("target host and database are required")
    if any(part in host for part in FORBIDDEN_HOST_PARTS):
        raise OrdersMigrationError("remote Railway target is forbidden")
    if host not in LOCAL_HOSTS:
        raise OrdersMigrationError("only a loopback PostgreSQL target is allowed")
    if not any(
        marker in database.lower() for marker in ("disposable", "test", "preview")
    ):
        raise OrdersMigrationError(
            "target database name must identify a test or preview database"
        )
    return TargetIdentity(host=host, database=database)


def validate_production_target_url(target_url):
    """Production mode: an entirely separate, independent validator. Does
    NOT reuse validate_target_url and does not relax any of its checks --
    it simply requires a well-formed PostgreSQL URL supplied explicitly
    through PRODUCTION_TARGET_ENV. The loopback/disposable-name
    restrictions do not apply here because they would be meaningless for a
    real production target; every other safety property (explicit flag,
    explicit env var, explicit boundary, one transaction) is enforced by
    the caller in main()/run_migration, not by relaxing this function."""
    if not target_url:
        raise OrdersMigrationError(f"{PRODUCTION_TARGET_ENV} is required for --production")
    try:
        parsed = urlsplit(target_url)
        host = (parsed.hostname or "").lower()
        database = unquote(parsed.path.lstrip("/")).strip()
    except ValueError as exc:
        raise OrdersMigrationError("production target URL is invalid") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise OrdersMigrationError("production target must use PostgreSQL")
    if not host or not database:
        raise OrdersMigrationError("production target host and database are required")
    return TargetIdentity(host=host, database=database)


# ---------------------------------------------------------------------------
# Database interaction
# ---------------------------------------------------------------------------

def _connect(target_url, readonly):
    readonly_option = "-c default_transaction_read_only=on " if readonly else ""
    connection = psycopg2.connect(
        target_url,
        connect_timeout=10,
        options=(
            readonly_option
            + "-c statement_timeout=10000 "
            "-c lock_timeout=3000 "
            "-c search_path=public,pg_catalog"
        ),
    )
    connection.set_session(readonly=readonly, autocommit=False)
    return connection


def _verify_schema(cursor):
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('orders', 'clients')
        """
    )
    columns = {"orders": set(), "clients": set()}
    for table, column in cursor.fetchall():
        columns[table].add(column)
    missing_orders = REQUIRED_ORDERS_COLUMNS - columns["orders"]
    missing_clients = REQUIRED_CLIENTS_COLUMNS - columns["clients"]
    if missing_orders or missing_clients:
        raise OrdersMigrationError(
            "target schema is missing required Orders v2 columns "
            "(run the Checkpoint A schema provisioning step first)"
        )


def _preflight(cursor, identity, require_readonly):
    if require_readonly:
        cursor.execute("SELECT current_setting('transaction_read_only')")
        if cursor.fetchone()[0] != "on":
            raise OrdersMigrationError("read-only transaction was not confirmed")
    cursor.execute("SELECT current_database()")
    if cursor.fetchone()[0] != identity.database:
        raise OrdersMigrationError("connected database does not match the target URL")
    _verify_schema(cursor)


def _fetch_orders_for_migration(cursor, max_id):
    """max_id is None only for the auto-discovery dry-run path (no
    boundary supplied yet): every order is fetched so the current MAX(id)
    can be reported. Whenever a boundary IS known, the SELECT itself is
    bounded by it -- rows beyond the boundary are not merely left
    unmodified, they are never even read for this run."""
    if max_id is None:
        cursor.execute(
            """
            SELECT id, order_id, status, inventory_deducted, payment_method,
                   telegram_id, payment_status, fulfillment_status, source, client_id
            FROM orders
            ORDER BY id
            """
        )
    else:
        cursor.execute(
            """
            SELECT id, order_id, status, inventory_deducted, payment_method,
                   telegram_id, payment_status, fulfillment_status, source, client_id
            FROM orders
            WHERE id <= %s
            ORDER BY id
            """,
            (max_id,),
        )
    return cursor.fetchall()


def _fetch_client_ids_by_telegram_id(cursor):
    cursor.execute("SELECT telegram_id, id FROM clients WHERE telegram_id IS NOT NULL")
    return {telegram_id: client_id for telegram_id, client_id in cursor.fetchall()}


def _apply_updates(cursor, updates, historical_max_id):
    for update in updates:
        cursor.execute(
            """
            UPDATE orders
            SET source = 'telegram',
                payment_status = %s,
                fulfillment_status = %s,
                client_id = COALESCE(client_id, %s)
            WHERE id = %s
              AND id <= %s
              AND payment_status = 'unpaid'
              AND fulfillment_status = 'new'
            """,
            (
                update.payment_status,
                update.fulfillment_status,
                update.client_id,
                update.id,
                historical_max_id,
            ),
        )


def run_migration(target_url, apply=False, historical_max_id=None, production=False):
    """Owns every fail-closed check, so a direct caller (not only main()'s
    CLI parsing) gets the same guarantees:

    - production=True always requires an explicit historical_max_id
      boundary, whether or not apply is also True -- rehearsal's
      auto-discovery dry-run (no boundary) is a normal-mode-only
      convenience and never applies to a production target.
    - production=True with apply=False is a real production READ-ONLY
      dry-run: connects with readonly=True (see _connect/_preflight),
      runs the exact same fetch/plan logic as a production apply, never
      calls _apply_updates, and always rolls back -- identical in kind to
      a normal-mode dry-run, just against the production target.
    - apply=True always requires an explicit historical_max_id boundary
      (production or not).
    - target_url is validated by validate_production_target_url when
      production=True, and by validate_target_url otherwise -- two
      independent functions, so production is a separate branch, not a
      relaxation of normal mode's restrictions.

    Returns (identity, plan)."""
    if production and historical_max_id is None:
        raise OrdersMigrationError(
            "--production requires an explicit historical_max_id boundary "
            "(auto-discovery is a rehearsal-only convenience)"
        )
    if apply and historical_max_id is None:
        raise OrdersMigrationError(
            "apply requires an explicit historical_max_id boundary "
            "(run a dry-run first to determine it, then pass it back explicitly)"
        )
    if historical_max_id is not None and (
        not isinstance(historical_max_id, int) or historical_max_id < 0
    ):
        raise OrdersMigrationError("historical_max_id must be a non-negative integer")

    identity = (
        validate_production_target_url(target_url)
        if production
        else validate_target_url(target_url)
    )

    connection = None
    try:
        connection = _connect(target_url, readonly=not apply)
        cursor = connection.cursor()
        _preflight(cursor, identity, require_readonly=not apply)

        order_rows = _fetch_orders_for_migration(cursor, max_id=historical_max_id)
        effective_max_id = (
            historical_max_id
            if historical_max_id is not None
            else max((row[0] for row in order_rows), default=0)
        )

        client_map = _fetch_client_ids_by_telegram_id(cursor)
        plan = plan_migration(order_rows, client_map, effective_max_id)

        if plan.unknown_status_anomalies:
            connection.rollback()
            details = ", ".join(
                f"id={row_id} order_id={order_id} status={status!r}"
                for row_id, order_id, status in plan.unknown_status_anomalies
            )
            raise OrdersMigrationError(
                f"unknown legacy status(es) found, aborting before any writes: {details}"
            )

        if not apply:
            connection.rollback()
            return identity, plan

        _apply_updates(cursor, plan.updates, effective_max_id)
        connection.commit()
        return identity, plan
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(identity, plan, mode, boundary_was_explicit):
    print(f"Orders v2 migration ({mode}) against {identity.database}")
    boundary_note = "explicitly confirmed" if boundary_was_explicit else "auto-discovered, NOT yet confirmed"
    print(f"  historical_max_id (orders.id) = {plan.historical_max_id}  [{boundary_note}]")
    print(f"  orders_in_scope = {plan.in_scope_count}")
    print(f"  post-boundary orders excluded (untouched, unread if bounded): {plan.post_boundary_count}")
    print(f"  already migrated (untouched):   {plan.already_migrated_count}")
    print(f"  eligible for migration:         {plan.eligible_count}")
    print("  eligible rows by legacy status:")
    for status in KNOWN_LEGACY_STATUSES:
        count = plan.status_breakdown.get(status, 0)
        if count:
            print(f"    {status}: {count}")
    print(f"  client_id matched:              {plan.client_matched_count}")
    print(f"  client_id left NULL (no match): {plan.client_unmatched_count}")
    if plan.review_needed_rows:
        print(
            "  REVIEW NEEDED (mapped to payment_reported from an ambiguous "
            f"historical state, mapping unchanged): {len(plan.review_needed_rows)}"
        )
        for row_id, order_id in plan.review_needed_rows:
            print(f"    orders.id={row_id} order_id={order_id}")
    if plan.unknown_status_anomalies:
        print("  UNKNOWN LEGACY STATUSES (migration aborted, zero writes):")
        for row_id, order_id, status in plan.unknown_status_anomalies:
            print(f"    orders.id={row_id} order_id={order_id} status={status!r}")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Safely backfill Orders v2 columns for existing orders/clients"
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--production",
        action="store_true",
        help="Target the real production database via a dedicated environment "
        "variable. Always requires --historical-max-id. Add --apply for the "
        "real write; omit --apply for a production read-only dry-run.",
    )
    parser.add_argument(
        "--historical-max-id",
        type=int,
        default=None,
        help="Fixed orders.id boundary (NOT order_id). Required for --apply. "
        "If omitted on a dry-run, the current MAX(id) is auto-discovered and "
        "reported, but never used implicitly for --apply.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)

    if args.historical_max_id is not None and args.historical_max_id < 0:
        print("ERROR: --historical-max-id must not be negative", file=sys.stderr)
        return 2

    # Each mode reads its target exclusively from its own dedicated
    # environment variable. Production mode never consults TARGET_ENV, and
    # neither mode ever consults DATABASE_URL/DATABASE_PUBLIC_URL directly
    # here -- validate_target_url separately rejects a target that merely
    # matches DATABASE_URL's value.
    target_url = os.getenv(PRODUCTION_TARGET_ENV if args.production else TARGET_ENV)

    try:
        identity, plan = run_migration(
            target_url,
            apply=args.apply,
            historical_max_id=args.historical_max_id,
            production=args.production,
        )
    except OrdersMigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except psycopg2.Error as exc:
        print(f"ERROR: database operation failed safely ({type(exc).__name__})", file=sys.stderr)
        return 3

    if args.production:
        mode = "production-apply" if args.apply else "production-dry-run"
    else:
        mode = "apply" if args.apply else "dry-run"
    _print_report(
        identity, plan, mode, boundary_was_explicit=args.historical_max_id is not None
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
