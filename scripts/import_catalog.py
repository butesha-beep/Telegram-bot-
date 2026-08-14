import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg2
from psycopg2 import sql


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "staging" / "production_catalog_import_plan.json"
EXPECTED_PLAN_SHA256 = "de4609c2d1fb8d65d330799574ee3b2026640816c7dc9afb916328d5d7d19ced"
TARGET_ENV = "CATALOG_IMPORT_TARGET_URL"
DEMO_MARKER = "dealmarket-demo-catalog-v1"
IMPORTED_MARKER = "dealmarket-imported-catalog-v1"
BACKUP_FORMAT_VERSION = 1
DEFAULT_BACKUP_DIR = ROOT / "staging"
LEGACY_PREVIEW_DATABASE = "dealmarket_preview_db"
LEGACY_PREVIEW_CONTAINER = "dealmarket-shop-preview-postgres"
LEGACY_BACKUP_PATH = (
    DEFAULT_BACKUP_DIR
    / "preview_catalog_backup_before_import_20260814T012153Z_35fa7e76dbf6.json"
)
LEGACY_BACKUP_SHA256 = (
    "35fa7e76dbf6bffcb0a68874a796ba69756199b65a98b933f1fac29b6e7b8f77"
)
CATALOG_TABLES = ("categories", "products", "product_options")
FORBIDDEN_HOST_PARTS = (
    "railway.internal",
    "proxy.rlwy.net",
    "kodama.proxy.rlwy.net",
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
ALLOWED_EXTERNAL_FOREIGN_KEYS = {
    ("public", "inventory_movements", "product_id", "products", "id"),
    ("public", "product_recommendations", "product_id", "products", "id"),
    (
        "public",
        "product_recommendations",
        "recommended_product_id",
        "products",
        "id",
    ),
}
ALLOWED_LOGICAL_REFERENCES = {
    ("cart_items", "product_id"): {"integer"},
    ("cart_items", "option_id"): {"integer"},
    ("order_items", "product_id"): {"integer"},
    ("order_items", "product_name"): {"text"},
    ("order_items", "option_id"): {"integer"},
    ("customer_events", "metadata"): {"jsonb"},
}
INCLUDED_PRODUCT_IDS = [1, *range(9, 25)]
EXCLUDED_PRODUCT_IDS = [2, 3, 4, 5, 6, 7, 8, 25]

CATEGORY_FIELDS = {"id", "name", "sort_order", "is_active"}
PRODUCT_FIELDS = {
    "id",
    "category_id",
    "name",
    "price_per_kg",
    "description",
    "image_url",
    "is_active",
    "sort_order",
    "stock_grams",
    "is_out_of_stock",
    "low_stock_threshold_grams",
    "is_promotion",
    "promotion_title",
    "promotion_sort_order",
    "pricing_mode",
    "fixed_price",
    "sale_unit",
    "unit_weight_grams",
    "stock_quantity",
}
REQUIRED_COLUMNS = {
    "categories": CATEGORY_FIELDS,
    "products": PRODUCT_FIELDS,
    "product_options": {
        "id",
        "product_id",
        "label",
        "weight",
        "price",
        "sort_order",
        "is_active",
        "stock_quantity",
        "is_out_of_stock",
    },
}


class CatalogImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class TargetIdentity:
    host: str
    database: str


@dataclass(frozen=True)
class ExpectedDemo:
    categories: int
    products: int
    product_options: int


@dataclass(frozen=True)
class BackupArtifact:
    path: Path
    sha256: str
    counts: ExpectedDemo


@dataclass(frozen=True)
class PreflightState:
    marker: str | None
    reference_tables: tuple[str, ...]
    legacy_null_marker: bool


LEGACY_COUNTS = ExpectedDemo(5, 25, 19)


def _exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise CatalogImportError(f"invalid fields in {label}")


def _integer(value, label, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogImportError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise CatalogImportError(f"{label} is below its minimum")


def _nonblank(value, label):
    if not isinstance(value, str) or not value.strip():
        raise CatalogImportError(f"{label} must not be blank")


def _unique_ids(rows, label):
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise CatalogImportError(f"duplicate IDs in {label}")


def load_and_validate_plan():
    if not PLAN_PATH.is_file():
        raise CatalogImportError("catalog import plan is missing")
    raw_plan = PLAN_PATH.read_bytes()
    if hashlib.sha256(raw_plan).hexdigest() != EXPECTED_PLAN_SHA256:
        raise CatalogImportError("catalog import plan checksum does not match")
    try:
        plan = json.loads(raw_plan.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogImportError("catalog import plan is not valid UTF-8 JSON") from exc

    _exact_keys(
        plan,
        {
            "import_plan_format_version",
            "source_snapshot",
            "decisions",
            "categories",
            "products",
            "product_options",
            "exclusions",
            "dropped_legacy_product_options",
            "empty_categories_after_filter",
            "summary",
            "review_flags",
        },
        "import plan",
    )
    if plan["import_plan_format_version"] != 1:
        raise CatalogImportError("unsupported import plan version")

    _exact_keys(
        plan["source_snapshot"], {"path", "snapshot_format_version"}, "source_snapshot"
    )
    if plan["source_snapshot"] != {
        "path": "staging/production_catalog_snapshot.json",
        "snapshot_format_version": 1,
    }:
        raise CatalogImportError("unexpected source snapshot metadata")

    expected_decisions = {
        "included_product_ids": INCLUDED_PRODUCT_IDS,
        "pricing_mode": "per_kg",
        "authoritative_price_field": "price_per_kg",
        "copy_product_options": False,
        "product_19_legacy_500g_price_is_error": True,
        "product_19_price_per_kg": 35.0,
        "product_19_calculated_500g_price": 17.5,
    }
    if plan["decisions"] != expected_decisions:
        raise CatalogImportError("import decisions do not match the approved plan")

    expected_summary = {
        "categories": 7,
        "products": 17,
        "product_options": 0,
        "excluded_products": 8,
        "dropped_legacy_product_options": 89,
    }
    if plan["summary"] != expected_summary:
        raise CatalogImportError("import plan control counts do not match")
    if not isinstance(plan["categories"], list) or len(plan["categories"]) != 7:
        raise CatalogImportError("import plan must contain seven categories")
    if not isinstance(plan["products"], list) or len(plan["products"]) != 17:
        raise CatalogImportError("import plan must contain seventeen products")
    if plan["product_options"] != []:
        raise CatalogImportError("import plan must not contain product options")
    if not isinstance(plan["exclusions"], list) or len(plan["exclusions"]) != 8:
        raise CatalogImportError("import plan must contain eight exclusions")
    if plan["dropped_legacy_product_options"] != {
        "count": 89,
        "reason": "legacy_auto_generated_weight_options_with_potentially_stale_prices",
    }:
        raise CatalogImportError("legacy option disposition is invalid")

    for category in plan["categories"]:
        _exact_keys(category, CATEGORY_FIELDS, "category")
        _integer(category["id"], "category.id", minimum=1)
        _integer(category["sort_order"], "category.sort_order")
        _nonblank(category["name"], "category.name")
        if not isinstance(category["is_active"], bool):
            raise CatalogImportError("category.is_active must be boolean")
    _unique_ids(plan["categories"], "categories")
    category_ids = {category["id"] for category in plan["categories"]}

    for product in plan["products"]:
        _exact_keys(product, PRODUCT_FIELDS, "product")
        _integer(product["id"], "product.id", minimum=1)
        _integer(product["category_id"], "product.category_id", minimum=1)
        _integer(product["sort_order"], "product.sort_order")
        _integer(product["stock_grams"], "product.stock_grams", minimum=0)
        _integer(
            product["low_stock_threshold_grams"],
            "product.low_stock_threshold_grams",
            minimum=0,
        )
        _integer(product["promotion_sort_order"], "product.promotion_sort_order")
        _nonblank(product["name"], "product.name")
        if product["category_id"] not in category_ids:
            raise CatalogImportError("product references an unknown category")
        if product["pricing_mode"] != "per_kg":
            raise CatalogImportError("every imported product must use per_kg")
        try:
            price = float(product["price_per_kg"])
        except (TypeError, ValueError) as exc:
            raise CatalogImportError("product.price_per_kg must be numeric") from exc
        if not math.isfinite(price) or price <= 0:
            raise CatalogImportError("product.price_per_kg must be finite and positive")
        for field in ("fixed_price", "sale_unit", "unit_weight_grams", "stock_quantity"):
            if product[field] is not None:
                raise CatalogImportError(f"product.{field} must be null")
        for field in ("is_active", "is_out_of_stock", "is_promotion"):
            if not isinstance(product[field], bool):
                raise CatalogImportError(f"product.{field} must be boolean")
        if product["description"] is not None and not isinstance(product["description"], str):
            raise CatalogImportError("product.description must be text or null")
        if product["image_url"] is not None and not isinstance(product["image_url"], str):
            raise CatalogImportError("product.image_url must be text or null")
        if product["promotion_title"] is not None and not isinstance(
            product["promotion_title"], str
        ):
            raise CatalogImportError("product.promotion_title must be text or null")
    _unique_ids(plan["products"], "products")
    if [product["id"] for product in plan["products"]] != INCLUDED_PRODUCT_IDS:
        raise CatalogImportError("included product IDs do not match the approved set")

    exclusion_ids = []
    for exclusion in plan["exclusions"]:
        _exact_keys(exclusion, {"product_id", "reason"}, "exclusion")
        _integer(exclusion["product_id"], "exclusion.product_id", minimum=1)
        _nonblank(exclusion["reason"], "exclusion.reason")
        exclusion_ids.append(exclusion["product_id"])
    if exclusion_ids != EXCLUDED_PRODUCT_IDS:
        raise CatalogImportError("excluded product IDs do not match the approved set")

    if plan["review_flags"] != [{
        "product_id": 11,
        "needs_stock_update": True,
        "reason": "stock_grams_is_zero_and_requires_manual_update",
    }]:
        raise CatalogImportError("stock review flags do not match the approved plan")
    product_by_id = {product["id"]: product for product in plan["products"]}
    if product_by_id[11]["stock_grams"] != 0:
        raise CatalogImportError("product 11 stock must remain zero")
    if product_by_id[13]["is_active"] or product_by_id[17]["is_active"]:
        raise CatalogImportError("inactive products must remain inactive")
    if float(product_by_id[19]["price_per_kg"]) != 35.0:
        raise CatalogImportError("product 19 must use 35 per kilogram")

    populated_categories = {product["category_id"] for product in plan["products"]}
    expected_empty = [
        {"id": category["id"], "name": category["name"]}
        for category in plan["categories"]
        if category["id"] not in populated_categories
    ]
    for empty_category in plan["empty_categories_after_filter"]:
        _exact_keys(empty_category, {"id", "name"}, "empty category")
    if plan["empty_categories_after_filter"] != expected_empty:
        raise CatalogImportError("empty category list is inconsistent")
    if [category["id"] for category in expected_empty] != [3, 5, 7]:
        raise CatalogImportError("unexpected empty categories")
    return plan


def validate_target_url(target_url):
    if not target_url:
        raise CatalogImportError(f"{TARGET_ENV} is required")
    for production_name in ("DATABASE_URL", "DATABASE_PUBLIC_URL"):
        production_value = os.getenv(production_name)
        if production_value and target_url == production_value:
            raise CatalogImportError("target matches a forbidden application database variable")
    try:
        parsed = urlsplit(target_url)
        host = (parsed.hostname or "").lower()
        database = unquote(parsed.path.lstrip("/")).strip()
    except ValueError as exc:
        raise CatalogImportError("target URL is invalid") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise CatalogImportError("target must use PostgreSQL")
    if not host or not database:
        raise CatalogImportError("target host and database are required")
    if any(part in host for part in FORBIDDEN_HOST_PARTS):
        raise CatalogImportError("remote Railway target is forbidden")
    if host not in LOCAL_HOSTS:
        raise CatalogImportError("only a loopback PostgreSQL target is allowed")
    if not any(marker in database.lower() for marker in ("disposable", "test", "preview")):
        raise CatalogImportError("target database name must identify a test or preview database")
    return TargetIdentity(host=host, database=database)


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


def _catalog_counts(cursor):
    cursor.execute("SELECT COUNT(*) FROM public.categories")
    categories = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM public.products")
    products = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM public.product_options")
    product_options = cursor.fetchone()[0]
    return ExpectedDemo(categories, products, product_options)


def _verify_schema(cursor):
    cursor.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('categories', 'products', 'product_options')
        """
    )
    columns = {table: set() for table in CATALOG_TABLES}
    for table, column in cursor.fetchall():
        columns[table].add(column)
    for table, required in REQUIRED_COLUMNS.items():
        missing = required - columns[table]
        if missing:
            raise CatalogImportError(f"target schema is missing required {table} columns")


def _external_foreign_keys(cursor):
    cursor.execute(
        """
        SELECT source_namespace.nspname, source_table.relname,
               source_attribute.attname, target_table.relname,
               target_attribute.attname, constraint_row.conname,
               cardinality(constraint_row.conkey)
        FROM pg_constraint constraint_row
        JOIN pg_class source_table ON source_table.oid = constraint_row.conrelid
        JOIN pg_namespace source_namespace ON source_namespace.oid = source_table.relnamespace
        JOIN pg_class target_table ON target_table.oid = constraint_row.confrelid
        JOIN pg_namespace target_namespace ON target_namespace.oid = target_table.relnamespace
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
          source_key(attnum, position) ON TRUE
        JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY
          target_key(attnum, position)
          ON target_key.position = source_key.position
        JOIN pg_attribute source_attribute
          ON source_attribute.attrelid = source_table.oid
         AND source_attribute.attnum = source_key.attnum
        JOIN pg_attribute target_attribute
          ON target_attribute.attrelid = target_table.oid
         AND target_attribute.attnum = target_key.attnum
        WHERE constraint_row.contype = 'f'
          AND target_namespace.nspname = 'public'
          AND target_table.relname IN ('categories', 'products', 'product_options')
          AND NOT (
              source_namespace.nspname = 'public'
              AND source_table.relname IN ('categories', 'products', 'product_options')
          )
        ORDER BY source_namespace.nspname, source_table.relname,
                 constraint_row.conname, source_key.position
        """
    )
    return cursor.fetchall()


def _reference_candidates(cursor):
    cursor.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name NOT IN ('categories', 'products', 'product_options')
        ORDER BY table_name, ordinal_position
        """
    )
    return [
        row
        for row in cursor.fetchall()
        if (
            any(token in row[1].lower() for token in ("category", "product", "option"))
            or row[2] in {"json", "jsonb"}
        )
    ]


def _nonnull_count(cursor, table, column):
    cursor.execute(
        sql.SQL("SELECT COUNT({}) FROM public.{}").format(
            sql.Identifier(column), sql.Identifier(table)
        )
    )
    return cursor.fetchone()[0]


def _verify_reference_safety(cursor):
    external_rows = _external_foreign_keys(cursor)
    allowed_fk_columns = set()
    reference_tables = set()
    for (
        source_schema,
        source_table,
        source_column,
        target_table,
        target_column,
        _constraint_name,
        column_count,
    ) in external_rows:
        reference = (
            source_schema,
            source_table,
            source_column,
            target_table,
            target_column,
        )
        if column_count != 1 or reference not in ALLOWED_EXTERNAL_FOREIGN_KEYS:
            raise CatalogImportError("an unknown external foreign key references the catalog")
        if _nonnull_count(cursor, source_table, source_column) != 0:
            raise CatalogImportError("an allowed external foreign key contains values")
        allowed_fk_columns.add((source_table, source_column))
        reference_tables.add(source_table)

    candidates = _reference_candidates(cursor)
    for table, column, data_type in candidates:
        if (table, column) in allowed_fk_columns:
            continue
        allowed_types = ALLOWED_LOGICAL_REFERENCES.get((table, column))
        if allowed_types is None or data_type not in allowed_types:
            raise CatalogImportError("an unknown logical catalog reference was found")
        if _nonnull_count(cursor, table, column) != 0:
            raise CatalogImportError("an allowed logical catalog reference contains values")
        reference_tables.add(table)
    return tuple(sorted(reference_tables))


def _verify_expected_preview_container(target_url, identity):
    try:
        parsed = urlsplit(target_url)
        target_port = parsed.port
        inspected = subprocess.run(
            ["docker", "inspect", LEGACY_PREVIEW_CONTAINER],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        info = json.loads(inspected.stdout)[0]
        container_env = {}
        for entry in info.get("Config", {}).get("Env", []):
            key, _, value = entry.partition("=")
            container_env[key] = value
        bindings = (
            info.get("NetworkSettings", {})
            .get("Ports", {})
            .get("5432/tcp")
            or []
        )
        matching_bindings = [
            binding
            for binding in bindings
            if binding.get("HostIp") in {"127.0.0.1", "::1"}
            and int(binding.get("HostPort", -1)) == target_port
        ]
        valid = (
            identity.host in LOCAL_HOSTS
            and identity.database == LEGACY_PREVIEW_DATABASE
            and info.get("Name", "").lstrip("/") == LEGACY_PREVIEW_CONTAINER
            and info.get("Config", {}).get("Image", "").startswith("postgres:")
            and info.get("State", {}).get("Health", {}).get("Status") == "healthy"
            and len(bindings) == 1
            and len(matching_bindings) == 1
            and container_env.get("POSTGRES_DB") == LEGACY_PREVIEW_DATABASE
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError):
        valid = False
    if not valid:
        raise CatalogImportError("legacy preview container identity does not match")


def _load_verified_legacy_backup():
    try:
        raw = LEGACY_BACKUP_PATH.read_bytes()
    except OSError as exc:
        raise CatalogImportError("verified legacy backup is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != LEGACY_BACKUP_SHA256:
        raise CatalogImportError("verified legacy backup checksum does not match")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CatalogImportError("verified legacy backup is invalid") from exc
    _validate_catalog_backup(
        payload,
        expected_counts=LEGACY_COUNTS,
        expected_marker=None,
    )
    if (
        payload["source_database"] != LEGACY_PREVIEW_DATABASE
        or payload["catalog_marker"] is not None
    ):
        raise CatalogImportError("verified legacy backup identity does not match")
    return payload


def _verify_legacy_null_marker(cursor, target_url, identity, expected_demo):
    if identity.database != LEGACY_PREVIEW_DATABASE or expected_demo != LEGACY_COUNTS:
        raise CatalogImportError("legacy null marker is not allowed for this target")
    _verify_expected_preview_container(target_url, identity)
    backup = _load_verified_legacy_backup()
    current = _catalog_backup_payload(cursor, identity)
    comparable_keys = {
        "source_database",
        "catalog_marker",
        "tables",
        "constraints",
        "sequences",
    }
    if any(current[key] != backup[key] for key in comparable_keys):
        raise CatalogImportError("legacy preview catalog does not match its verified backup")


def _lock_catalog_and_reference_tables(cursor, reference_tables):
    tables = sorted(set(CATALOG_TABLES) | set(reference_tables))
    cursor.execute(
        sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
            sql.SQL(", ").join(
                sql.Identifier("public", table) for table in tables
            )
        )
    )


def _preflight(
    cursor,
    target_url,
    identity,
    expected_demo,
    require_readonly=False,
):
    if require_readonly:
        cursor.execute("SELECT current_setting('transaction_read_only')")
        if cursor.fetchone()[0] != "on":
            raise CatalogImportError("read-only transaction was not confirmed")
    cursor.execute("SELECT current_database()")
    if cursor.fetchone()[0] != identity.database:
        raise CatalogImportError("connected database does not match the target URL")
    _verify_schema(cursor)
    reference_tables = _verify_reference_safety(cursor)
    cursor.execute(
        "SELECT obj_description('public.categories'::regclass, 'pg_class')"
    )
    marker = cursor.fetchone()[0]
    legacy_null_marker = marker is None
    if not legacy_null_marker and marker != DEMO_MARKER:
        raise CatalogImportError("demo marker does not match")
    counts = _catalog_counts(cursor)
    if counts != expected_demo:
        raise CatalogImportError("demo catalog counts do not match")
    if legacy_null_marker:
        _verify_legacy_null_marker(cursor, target_url, identity, expected_demo)
    return PreflightState(
        marker=marker,
        reference_tables=reference_tables,
        legacy_null_marker=legacy_null_marker,
    )


def _catalog_backup_payload(cursor, identity):
    cursor.execute(
        """
        SELECT table_name, column_name, ordinal_position, data_type, udt_name,
               is_nullable, column_default, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name IN ('categories', 'products', 'product_options')
        ORDER BY table_name, ordinal_position
        """
    )
    columns = {table: [] for table in CATALOG_TABLES}
    for row in cursor.fetchall():
        columns[row[0]].append({
            "name": row[1],
            "position": row[2],
            "data_type": row[3],
            "udt_name": row[4],
            "nullable": row[5],
            "default": row[6],
            "identity": row[7],
            "generated": row[8],
        })

    rows = {}
    for table in CATALOG_TABLES:
        cursor.execute(
            f"SELECT to_jsonb(row_value) FROM public.{table} row_value ORDER BY id"
        )
        rows[table] = [row[0] for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT source_table.relname, constraint_row.conname,
               constraint_row.contype,
               pg_get_constraintdef(constraint_row.oid, true)
        FROM pg_constraint constraint_row
        JOIN pg_class source_table ON source_table.oid = constraint_row.conrelid
        JOIN pg_namespace source_namespace
          ON source_namespace.oid = source_table.relnamespace
        WHERE source_namespace.nspname = 'public'
          AND source_table.relname IN ('categories', 'products', 'product_options')
        ORDER BY source_table.relname, constraint_row.conname
        """
    )
    constraints = [
        {
            "table": row[0],
            "name": row[1],
            "type": row[2],
            "definition": row[3],
        }
        for row in cursor.fetchall()
    ]

    cursor.execute(
        """
        SELECT table_row.relname, attribute_row.attname, sequence_row.relname
        FROM pg_class sequence_row
        JOIN pg_depend dependency_row
          ON dependency_row.objid = sequence_row.oid
         AND dependency_row.deptype IN ('a', 'i')
        JOIN pg_class table_row ON table_row.oid = dependency_row.refobjid
        JOIN pg_namespace table_namespace
          ON table_namespace.oid = table_row.relnamespace
        JOIN pg_attribute attribute_row
          ON attribute_row.attrelid = table_row.oid
         AND attribute_row.attnum = dependency_row.refobjsubid
        WHERE sequence_row.relkind = 'S'
          AND table_namespace.nspname = 'public'
          AND table_row.relname IN ('categories', 'products', 'product_options')
        ORDER BY table_row.relname, attribute_row.attname
        """
    )
    sequences = []
    for table, column, sequence in cursor.fetchall():
        cursor.execute(
            f'SELECT last_value, is_called FROM public."{sequence}"'
        )
        last_value, is_called = cursor.fetchone()
        sequences.append({
            "table": table,
            "column": column,
            "name": sequence,
            "last_value": last_value,
            "is_called": is_called,
        })

    cursor.execute(
        "SELECT obj_description('public.categories'::regclass, 'pg_class')"
    )
    return {
        "backup_format_version": BACKUP_FORMAT_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_database": identity.database,
        "catalog_marker": cursor.fetchone()[0],
        "tables": {
            table: {
                "columns": columns[table],
                "rows": rows[table],
            }
            for table in CATALOG_TABLES
        },
        "constraints": constraints,
        "sequences": sequences,
    }


def _validate_catalog_backup(payload, expected_counts=None, expected_marker=DEMO_MARKER):
    if set(payload) != {
        "backup_format_version",
        "created_at_utc",
        "source_database",
        "catalog_marker",
        "tables",
        "constraints",
        "sequences",
    }:
        raise CatalogImportError("catalog backup metadata is invalid")
    if payload["backup_format_version"] != BACKUP_FORMAT_VERSION:
        raise CatalogImportError("catalog backup format is unsupported")
    if set(payload["tables"]) != set(CATALOG_TABLES):
        raise CatalogImportError("catalog backup contains an invalid table set")
    if payload["catalog_marker"] != expected_marker:
        raise CatalogImportError("catalog backup marker does not match")

    ids = {}
    counts = []
    for table in CATALOG_TABLES:
        table_payload = payload["tables"][table]
        if set(table_payload) != {"columns", "rows"}:
            raise CatalogImportError("catalog backup table metadata is invalid")
        column_names = [column["name"] for column in table_payload["columns"]]
        if not column_names or len(column_names) != len(set(column_names)):
            raise CatalogImportError("catalog backup column metadata is invalid")
        table_ids = [row.get("id") for row in table_payload["rows"]]
        if any(not isinstance(row_id, int) for row_id in table_ids):
            raise CatalogImportError("catalog backup row ID is invalid")
        if len(table_ids) != len(set(table_ids)):
            raise CatalogImportError("catalog backup contains duplicate row IDs")
        if any(set(row) != set(column_names) for row in table_payload["rows"]):
            raise CatalogImportError("catalog backup row shape is invalid")
        ids[table] = set(table_ids)
        counts.append(len(table_ids))

    actual_counts = ExpectedDemo(*counts)
    if expected_counts is not None and actual_counts != expected_counts:
        raise CatalogImportError("catalog backup counts do not match")
    if any(
        row["category_id"] not in ids["categories"]
        for row in payload["tables"]["products"]["rows"]
    ):
        raise CatalogImportError("catalog backup has an invalid category reference")
    if any(
        row["product_id"] not in ids["products"]
        for row in payload["tables"]["product_options"]["rows"]
    ):
        raise CatalogImportError("catalog backup has an invalid product reference")
    for sequence in payload["sequences"]:
        if (
            sequence.get("table") not in CATALOG_TABLES
            or not isinstance(sequence.get("last_value"), int)
            or not isinstance(sequence.get("is_called"), bool)
        ):
            raise CatalogImportError("catalog backup sequence metadata is invalid")
    return actual_counts


def _create_catalog_backup(
    cursor,
    identity,
    backup_dir=DEFAULT_BACKUP_DIR,
    expected_counts=None,
    expected_marker=DEMO_MARKER,
):
    payload = _catalog_backup_payload(cursor, identity)
    counts = _validate_catalog_backup(
        payload,
        expected_counts=expected_counts,
        expected_marker=expected_marker,
    )
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / (
        f"preview_catalog_backup_before_import_{timestamp}_{digest[:12]}.json"
    )
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=".preview_catalog_backup_", suffix=".tmp", dir=backup_dir
    )
    try:
        with os.fdopen(temporary_fd, "wb") as temporary_file:
            temporary_file.write(encoded)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
        stored = path.read_bytes()
        if hashlib.sha256(stored).hexdigest() != digest:
            raise CatalogImportError("catalog backup checksum verification failed")
        stored_payload = json.loads(stored.decode("utf-8"))
        _validate_catalog_backup(
            stored_payload,
            expected_counts=expected_counts,
            expected_marker=expected_marker,
        )
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    return BackupArtifact(path=path, sha256=digest, counts=counts)


def _insert_plan(cursor, plan, fail_after_products=False):
    cursor.execute("DELETE FROM public.product_options")
    cursor.execute("DELETE FROM public.products")
    cursor.execute("DELETE FROM public.categories")
    cursor.executemany(
        """
        INSERT INTO public.categories (id, name, sort_order, is_active)
        VALUES (%s, %s, %s, %s)
        """,
        [
            (row["id"], row["name"], row["sort_order"], row["is_active"])
            for row in plan["categories"]
        ],
    )
    cursor.executemany(
        """
        INSERT INTO public.products (
            id, category_id, name, price_per_kg, description, image_url,
            is_active, sort_order, stock_grams, is_out_of_stock,
            low_stock_threshold_grams, is_promotion, promotion_title,
            promotion_sort_order, pricing_mode, fixed_price, sale_unit,
            unit_weight_grams, stock_quantity
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """,
        [
            tuple(row[field] for field in (
                "id", "category_id", "name", "price_per_kg", "description",
                "image_url", "is_active", "sort_order", "stock_grams",
                "is_out_of_stock", "low_stock_threshold_grams", "is_promotion",
                "promotion_title", "promotion_sort_order", "pricing_mode",
                "fixed_price", "sale_unit", "unit_weight_grams", "stock_quantity",
            ))
            for row in plan["products"]
        ],
    )
    if fail_after_products:
        raise CatalogImportError("injected rehearsal failure")


def _configure_sequences(cursor):
    max_id_sql = {
        "categories": "SELECT COALESCE(MAX(id), 0) FROM public.categories",
        "products": "SELECT COALESCE(MAX(id), 0) FROM public.products",
        "product_options": "SELECT COALESCE(MAX(id), 0) FROM public.product_options",
    }
    for table in CATALOG_TABLES:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", (f"public.{table}",))
        sequence_name = cursor.fetchone()[0]
        if not sequence_name:
            continue
        cursor.execute(max_id_sql[table])
        maximum_id = cursor.fetchone()[0]
        cursor.execute(
            sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(
                sql.Identifier(*sequence_name.split(".", 1)),
                sql.Literal(maximum_id + 1 if maximum_id else 1),
            )
        )


def _verify_imported_catalog(cursor):
    if _catalog_counts(cursor) != ExpectedDemo(7, 17, 0):
        raise CatalogImportError("post-import catalog counts do not match")
    cursor.execute("SELECT id FROM public.products ORDER BY id")
    if [row[0] for row in cursor.fetchall()] != INCLUDED_PRODUCT_IDS:
        raise CatalogImportError("post-import product IDs do not match")
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM public.products
        WHERE pricing_mode != 'per_kg'
           OR price_per_kg <= 0
           OR fixed_price IS NOT NULL
           OR sale_unit IS NOT NULL
           OR unit_weight_grams IS NOT NULL
           OR stock_quantity IS NOT NULL
        """
    )
    if cursor.fetchone()[0] != 0:
        raise CatalogImportError("post-import pricing fields are invalid")
    cursor.execute(
        "SELECT price_per_kg FROM public.products WHERE id = 19"
    )
    if float(cursor.fetchone()[0]) != 35.0:
        raise CatalogImportError("post-import product 19 price is invalid")
    cursor.execute(
        "SELECT stock_grams, is_active FROM public.products WHERE id IN (11, 13, 17) ORDER BY id"
    )
    special_rows = cursor.fetchall()
    if special_rows != [(0, True), (0, False), (0, False)]:
        raise CatalogImportError("post-import stock or activity flags are invalid")
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM public.products product_row
        LEFT JOIN public.categories category_row ON category_row.id = product_row.category_id
        WHERE category_row.id IS NULL
        """
    )
    if cursor.fetchone()[0] != 0:
        raise CatalogImportError("post-import category references are invalid")


def run_import(
    target_url,
    expected_demo,
    apply=False,
    replace_demo=False,
    fail_after_products=False,
    fail_before_commit=False,
    backup_dir=DEFAULT_BACKUP_DIR,
):
    plan = load_and_validate_plan()
    identity = validate_target_url(target_url)
    if apply and not replace_demo:
        raise CatalogImportError("apply requires the explicit --replace-demo flag")
    if (
        fail_after_products or fail_before_commit
    ) and "disposable" not in identity.database.lower():
        raise CatalogImportError("failure injection requires a disposable database")
    connection = None
    try:
        connection = _connect(target_url, readonly=not apply)
        cursor = connection.cursor()
        preflight = _preflight(
            cursor,
            target_url,
            identity,
            expected_demo,
            require_readonly=not apply,
        )
        if not apply:
            connection.rollback()
            return {
                "mode": "dry-run",
                "database": identity.database,
                "categories": 7,
                "products": 17,
                "product_options": 0,
            }
        _lock_catalog_and_reference_tables(cursor, preflight.reference_tables)
        preflight = _preflight(
            cursor,
            target_url,
            identity,
            expected_demo,
            require_readonly=False,
        )
        backup = _create_catalog_backup(
            cursor,
            identity,
            backup_dir=backup_dir,
            expected_counts=expected_demo,
            expected_marker=preflight.marker,
        )
        _insert_plan(cursor, plan, fail_after_products=fail_after_products)
        _configure_sequences(cursor)
        _verify_imported_catalog(cursor)
        cursor.execute(
            "COMMENT ON TABLE public.categories IS 'dealmarket-imported-catalog-v1'"
        )
        if fail_before_commit:
            raise CatalogImportError("injected failure after catalog and marker writes")
        connection.commit()
        return {
            "mode": "apply",
            "database": identity.database,
            "categories": 7,
            "products": 17,
            "product_options": 0,
            "backup_path": str(backup.path),
            "backup_sha256": backup.sha256,
        }
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Safely rehearse a catalog import")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-demo", action="store_true")
    parser.add_argument("--expected-category-count", type=int)
    parser.add_argument("--expected-product-count", type=int)
    parser.add_argument("--expected-option-count", type=int)
    parser.add_argument("--test-fail-after-products", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--test-fail-before-commit", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    counts = (
        args.expected_category_count,
        args.expected_product_count,
        args.expected_option_count,
    )
    if any(value is None or value < 0 for value in counts):
        print("ERROR: all three expected demo counts are required", file=sys.stderr)
        return 2
    target_url = os.getenv(TARGET_ENV)
    try:
        identity = validate_target_url(target_url)
        if args.test_fail_after_products or args.test_fail_before_commit:
            if os.getenv("CATALOG_IMPORT_ENABLE_FAILURE_TEST") != "1":
                raise CatalogImportError("failure injection is disabled")
            if "disposable" not in identity.database.lower():
                raise CatalogImportError("failure injection requires a disposable database")
        result = run_import(
            target_url,
            ExpectedDemo(*counts),
            apply=args.apply,
            replace_demo=args.replace_demo,
            fail_after_products=args.test_fail_after_products,
            fail_before_commit=args.test_fail_before_commit,
        )
    except CatalogImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except psycopg2.Error as exc:
        print(f"ERROR: database operation failed safely ({type(exc).__name__})", file=sys.stderr)
        return 3
    print(
        f"{result['mode']} complete for local database {result['database']}: "
        f"{result['categories']}/{result['products']}/{result['product_options']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
