import os

import psycopg2


DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

# Orders v2 schema foundation: CHECK-constraint vocabularies as named,
# testable constants (single source of truth for the SQL text below), not a
# runtime validation API. cash_on_delivery is intentionally NOT a payment
# status -- COD is represented by orders.payment_method; payment_status
# tracks money received/not received/reported/refunded only.
ORDER_PAYMENT_STATUS_VALUES = ("unpaid", "payment_reported", "paid", "refunded")
ORDER_FULFILLMENT_STATUS_VALUES = (
    "new",
    "confirmed",
    "picking",
    "packed",
    "ready_to_ship",
    "shipped",
    "delivered",
    "cancelled",
)
ORDER_SOURCE_VALUES = (
    "telegram",
    "website",
    "instagram",
    "tiktok",
    "whatsapp",
    "viber",
    "in_person",
    "other",
)
ORDER_DELIVERY_METHOD_VALUES = ("pickup", "delivery")


def _sql_string_list(values):
    return ", ".join(f"'{value}'" for value in values)


REQUIRED_CATALOG_COLUMNS = {
    "categories": {"id", "name", "sort_order", "is_active"},
    "products": {
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
        "pricing_mode",
        "fixed_price",
        "sale_unit",
        "unit_weight_grams",
        "stock_quantity",
    },
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
    "order_items": {
        "pricing_mode",
        "price_per_kg_snapshot",
    },
    "inventory_movements": {
        "quantity_units",
    },
    "orders": {
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
    "clients": {
        "last_name",
    },
}

REQUIRED_SECURITY_COLUMNS = {
    "web_sessions": {
        "id": ("bigint", "NO"),
        "role": ("text", "NO"),
        "account_key": ("text", "NO"),
        "token_hash": ("bytea", "NO"),
        "issued_at": ("timestamp with time zone", "NO"),
        "expires_at": ("timestamp with time zone", "NO"),
        "revoked_at": ("timestamp with time zone", "YES"),
    },
    "consumed_login_nonces": {
        "id": ("bigint", "NO"),
        "role": ("text", "NO"),
        "nonce_hash": ("bytea", "NO"),
        "issued_at": ("timestamp with time zone", "NO"),
        "expires_at": ("timestamp with time zone", "NO"),
        "consumed_at": ("timestamp with time zone", "NO"),
    },
}

EXPECTED_SECURITY_COLUMN_STRUCTURE = {
    "web_sessions": {
        "id": ("bigint", True, True),
        "role": ("text", True, False),
        "account_key": ("text", True, False),
        "token_hash": ("bytea", True, False),
        "issued_at": ("timestamp with time zone", True, False),
        "expires_at": ("timestamp with time zone", True, False),
        "revoked_at": ("timestamp with time zone", False, False),
    },
    "consumed_login_nonces": {
        "id": ("bigint", True, True),
        "role": ("text", True, False),
        "nonce_hash": ("bytea", True, False),
        "issued_at": ("timestamp with time zone", True, False),
        "expires_at": ("timestamp with time zone", True, False),
        "consumed_at": ("timestamp with time zone", True, False),
    },
}

REQUIRED_SECURITY_CONSTRAINTS = {
    "web_sessions": {
        "web_sessions_pkey",
        "ck_web_sessions_role",
        "ck_web_sessions_account_key",
        "ck_web_sessions_token_hash_length",
        "ck_web_sessions_expiry",
        "ck_web_sessions_revocation",
        "uq_web_sessions_token_hash",
    },
    "consumed_login_nonces": {
        "consumed_login_nonces_pkey",
        "ck_consumed_login_nonces_role",
        "ck_consumed_login_nonces_hash_length",
        "ck_consumed_login_nonces_expiry",
        "ck_consumed_login_nonces_consumption",
        "uq_consumed_login_nonces_role_hash",
    },
}

REQUIRED_SECURITY_INDEXES = {
    "web_sessions": {
        "uq_web_sessions_token_hash",
        "uq_web_sessions_active_role_account",
        "idx_web_sessions_expires_at",
    },
    "consumed_login_nonces": {
        "uq_consumed_login_nonces_role_hash",
        "idx_consumed_login_nonces_expires_at",
    },
}

EXPECTED_SECURITY_CONSTRAINT_STRUCTURE = {
    "web_sessions": {
        "web_sessions_pkey": (
            "p",
            ("id",),
            "PRIMARY KEY (id)",
        ),
        "ck_web_sessions_role": (
            "c",
            ("role",),
            "CHECK (role = ANY (ARRAY['admin'::text, 'master'::text]))",
        ),
        "ck_web_sessions_account_key": (
            "c",
            ("account_key",),
            "CHECK (length(account_key) >= 1 AND length(account_key) <= 64)",
        ),
        "ck_web_sessions_token_hash_length": (
            "c",
            ("token_hash",),
            "CHECK (octet_length(token_hash) = 32)",
        ),
        "ck_web_sessions_expiry": (
            "c",
            ("issued_at", "expires_at"),
            "CHECK (expires_at > issued_at)",
        ),
        "ck_web_sessions_revocation": (
            "c",
            ("issued_at", "revoked_at"),
            "CHECK (revoked_at IS NULL OR revoked_at >= issued_at)",
        ),
        "uq_web_sessions_token_hash": (
            "u",
            ("token_hash",),
            "UNIQUE (token_hash)",
        ),
    },
    "consumed_login_nonces": {
        "consumed_login_nonces_pkey": (
            "p",
            ("id",),
            "PRIMARY KEY (id)",
        ),
        "ck_consumed_login_nonces_role": (
            "c",
            ("role",),
            "CHECK (role = ANY (ARRAY['admin'::text, 'master'::text]))",
        ),
        "ck_consumed_login_nonces_hash_length": (
            "c",
            ("nonce_hash",),
            "CHECK (octet_length(nonce_hash) = 32)",
        ),
        "ck_consumed_login_nonces_expiry": (
            "c",
            ("issued_at", "expires_at"),
            "CHECK (expires_at > issued_at)",
        ),
        "ck_consumed_login_nonces_consumption": (
            "c",
            ("issued_at", "expires_at", "consumed_at"),
            "CHECK (consumed_at >= issued_at AND consumed_at < expires_at)",
        ),
        "uq_consumed_login_nonces_role_hash": (
            "u",
            ("role", "nonce_hash"),
            "UNIQUE (role, nonce_hash)",
        ),
    },
}

EXPECTED_SECURITY_INDEX_STRUCTURE = {
    "web_sessions": {
        "uq_web_sessions_token_hash": (
            True,
            ("token_hash",),
            "",
        ),
        "uq_web_sessions_active_role_account": (
            True,
            ("role", "account_key"),
            "revoked_at IS NULL",
        ),
        "idx_web_sessions_expires_at": (
            False,
            ("expires_at",),
            "",
        ),
    },
    "consumed_login_nonces": {
        "uq_consumed_login_nonces_role_hash": (
            True,
            ("role", "nonce_hash"),
            "",
        ),
        "idx_consumed_login_nonces_expires_at": (
            False,
            ("expires_at",),
            "",
        ),
    },
}

SECURITY_SCHEMA_STATEMENTS = (
    """
        CREATE TABLE IF NOT EXISTS web_sessions (
            id BIGSERIAL,
            role TEXT NOT NULL,
            account_key TEXT NOT NULL,
            token_hash BYTEA NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ,
            CONSTRAINT web_sessions_pkey PRIMARY KEY (id),
            CONSTRAINT ck_web_sessions_role CHECK (role IN ('admin', 'master')),
            CONSTRAINT ck_web_sessions_account_key CHECK (length(account_key) BETWEEN 1 AND 64),
            CONSTRAINT ck_web_sessions_token_hash_length CHECK (octet_length(token_hash) = 32),
            CONSTRAINT ck_web_sessions_expiry CHECK (expires_at > issued_at),
            CONSTRAINT ck_web_sessions_revocation CHECK (
                revoked_at IS NULL OR revoked_at >= issued_at
            ),
            CONSTRAINT uq_web_sessions_token_hash UNIQUE (token_hash)
        )
    """,
    """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_web_sessions_active_role_account
        ON web_sessions (role, account_key)
        WHERE revoked_at IS NULL
    """,
    """
        CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at
        ON web_sessions (expires_at)
    """,
    """
        CREATE TABLE IF NOT EXISTS consumed_login_nonces (
            id BIGSERIAL,
            role TEXT NOT NULL,
            nonce_hash BYTEA NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT consumed_login_nonces_pkey PRIMARY KEY (id),
            CONSTRAINT ck_consumed_login_nonces_role CHECK (role IN ('admin', 'master')),
            CONSTRAINT ck_consumed_login_nonces_hash_length CHECK (octet_length(nonce_hash) = 32),
            CONSTRAINT ck_consumed_login_nonces_expiry CHECK (expires_at > issued_at),
            CONSTRAINT ck_consumed_login_nonces_consumption CHECK (
                consumed_at >= issued_at AND consumed_at < expires_at
            ),
            CONSTRAINT uq_consumed_login_nonces_role_hash UNIQUE (role, nonce_hash)
        )
    """,
    """
        CREATE INDEX IF NOT EXISTS idx_consumed_login_nonces_expires_at
        ON consumed_login_nonces (expires_at)
    """,
)

if not DATABASE_URL:
    raise ValueError("DATABASE_URL is not set")


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def get_readiness_connection():
    return psycopg2.connect(DATABASE_URL, connect_timeout=3)


def _normalize_catalog_definition(value):
    return " ".join(str(value or "").split()).casefold()


def _security_catalog_is_compatible(
    schema_name,
    table_rows,
    column_rows,
    constraint_rows,
    index_rows,
    sequence_rows,
):
    expected_tables = set(EXPECTED_SECURITY_COLUMN_STRUCTURE)
    table_oids = {}
    for row_schema, table_name, table_oid, relkind in table_rows:
        if (
            row_schema != schema_name
            or table_name not in expected_tables
            or relkind != "r"
            or not isinstance(table_oid, int)
            or table_oid <= 0
            or table_name in table_oids
        ):
            return False
        table_oids[table_name] = table_oid
    if set(table_oids) != expected_tables or len(set(table_oids.values())) != len(
        table_oids
    ):
        return False

    found_columns = {table_name: {} for table_name in expected_tables}
    column_numbers = {table_name: {} for table_name in expected_tables}
    for (
        table_oid,
        table_name,
        attribute_number,
        column_name,
        data_type,
        is_not_null,
        has_default,
    ) in column_rows:
        if (
            table_oids.get(table_name) != table_oid
            or column_name in found_columns.get(table_name, {})
            or not isinstance(attribute_number, int)
            or attribute_number <= 0
        ):
            return False
        found_columns[table_name][column_name] = (
            data_type,
            bool(is_not_null),
            bool(has_default),
        )
        column_numbers[table_name][column_name] = attribute_number
    if found_columns != EXPECTED_SECURITY_COLUMN_STRUCTURE:
        return False

    found_constraints = {table_name: {} for table_name in expected_tables}
    for (
        table_oid,
        table_name,
        constraint_name,
        constraint_type,
        is_validated,
        constrained_columns,
        definition,
    ) in constraint_rows:
        if (
            table_oids.get(table_name) != table_oid
            or constraint_name in found_constraints.get(table_name, {})
            or not is_validated
        ):
            return False
        # Column order within a CHECK constraint's conkey reflects Postgres's
        # internal expression-analysis order, not a stable/semantic
        # ordering (e.g. "a > b" and "b < a" can populate conkey
        # differently for the same real constraint) -- sorted() makes this
        # an order-independent set comparison, matching PRIMARY KEY/UNIQUE
        # constraints too, where only the column set (not physical order)
        # determines the enforced invariant here.
        found_constraints[table_name][constraint_name] = (
            constraint_type,
            tuple(sorted(constrained_columns or ())),
            _normalize_catalog_definition(definition),
        )
    expected_constraints = {
        table_name: {
            constraint_name: (
                constraint_type,
                tuple(sorted(constrained_columns)),
                _normalize_catalog_definition(definition),
            )
            for constraint_name, (
                constraint_type,
                constrained_columns,
                definition,
            ) in constraints.items()
        }
        for table_name, constraints in EXPECTED_SECURITY_CONSTRAINT_STRUCTURE.items()
    }
    if found_constraints != expected_constraints:
        return False

    found_indexes = {table_name: {} for table_name in expected_tables}
    for (
        table_oid,
        table_name,
        index_name,
        is_unique,
        is_valid,
        is_ready,
        key_attribute_count,
        total_attribute_count,
        key_columns,
        has_expressions,
        predicate,
    ) in index_rows:
        if (
            table_oids.get(table_name) != table_oid
            or index_name in found_indexes.get(table_name, {})
            or not is_valid
            or not is_ready
            or has_expressions
            or key_attribute_count != total_attribute_count
            or key_attribute_count != len(tuple(key_columns or ()))
        ):
            return False
        found_indexes[table_name][index_name] = (
            bool(is_unique),
            tuple(key_columns or ()),
            _normalize_catalog_definition(predicate),
        )
    expected_indexes = {
        table_name: {
            index_name: (
                bool(is_unique),
                tuple(key_columns),
                _normalize_catalog_definition(predicate),
            )
            for index_name, (is_unique, key_columns, predicate) in indexes.items()
        }
        for table_name, indexes in EXPECTED_SECURITY_INDEX_STRUCTURE.items()
    }
    if found_indexes != expected_indexes:
        return False

    found_sequences = {}
    for (
        table_oid,
        table_name,
        id_attribute_number,
        sequence_oid,
        owned_table_oid,
        owned_attribute_number,
        dependency_type,
        has_default,
        default_matches_owned_sequence,
        default_depends_on_sequence,
    ) in sequence_rows:
        if (
            table_oids.get(table_name) != table_oid
            or table_name in found_sequences
            or not isinstance(sequence_oid, int)
            or sequence_oid <= 0
        ):
            return False
        found_sequences[table_name] = (
            id_attribute_number,
            owned_table_oid,
            owned_attribute_number,
            dependency_type,
            bool(has_default),
            bool(default_matches_owned_sequence),
            bool(default_depends_on_sequence),
        )
    expected_sequences = {
        table_name: (
            column_numbers[table_name]["id"],
            table_oids[table_name],
            column_numbers[table_name]["id"],
            "a",
            True,
            True,
            True,
        )
        for table_name in expected_tables
    }
    return found_sequences == expected_sequences


def catalog_schema_is_compatible(connection_factory=None):
    if connection_factory is None:
        if not DATABASE_URL:
            return False
        connection_factory = get_readiness_connection

    connection = None
    cursor = None
    try:
        connection = connection_factory()
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor()
        cursor.execute("BEGIN TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '3000ms'")
        cursor.execute("SET LOCAL lock_timeout = '1000ms'")
        cursor.execute("SELECT current_setting('transaction_read_only')")
        if cursor.fetchone()[0] != "on":
            return False
        cursor.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = ANY(%s)
            """,
            (sorted(REQUIRED_CATALOG_COLUMNS),),
        )
        found_columns = {table: set() for table in REQUIRED_CATALOG_COLUMNS}
        for table_name, column_name in cursor.fetchall():
            if table_name in found_columns:
                found_columns[table_name].add(column_name)
        catalog_compatible = all(
            required_columns <= found_columns[table_name]
            for table_name, required_columns in REQUIRED_CATALOG_COLUMNS.items()
        )
        if not catalog_compatible:
            return False

        cursor.execute(
            """
            SELECT current_schema()
            """,
        )
        schema_row = cursor.fetchone()
        if not schema_row or not schema_row[0]:
            return False
        schema_name = schema_row[0]

        cursor.execute(
            """
            SELECT namespace_info.nspname, table_info.relname,
                   table_info.oid, table_info.relkind
            FROM pg_class table_info
            JOIN pg_namespace namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = current_schema()
              AND table_info.relname = ANY(%s)
            """,
            (sorted(EXPECTED_SECURITY_COLUMN_STRUCTURE),),
        )
        table_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT table_info.oid, table_info.relname,
                   attribute_info.attnum, attribute_info.attname,
                   format_type(attribute_info.atttypid, attribute_info.atttypmod),
                   attribute_info.attnotnull, attribute_info.atthasdef
            FROM pg_class table_info
            JOIN pg_namespace namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute attribute_info
              ON attribute_info.attrelid = table_info.oid
            WHERE namespace_info.nspname = current_schema()
              AND table_info.relname = ANY(%s)
              AND attribute_info.attnum > 0
              AND NOT attribute_info.attisdropped
            ORDER BY table_info.relname, attribute_info.attnum
            """,
            (sorted(EXPECTED_SECURITY_COLUMN_STRUCTURE),),
        )
        column_rows = cursor.fetchall()

        constraint_names = sorted(
            constraint_name
            for constraints in EXPECTED_SECURITY_CONSTRAINT_STRUCTURE.values()
            for constraint_name in constraints
        )
        cursor.execute(
            """
            SELECT table_info.oid, table_info.relname,
                   constraint_info.conname, constraint_info.contype,
                   constraint_info.convalidated,
                   ARRAY(
                       SELECT attribute_info.attname
                       FROM unnest(constraint_info.conkey)
                            WITH ORDINALITY AS key_info(attribute_number, position)
                       JOIN pg_attribute attribute_info
                         ON attribute_info.attrelid = table_info.oid
                        AND attribute_info.attnum = key_info.attribute_number
                       ORDER BY key_info.position
                   ),
                   pg_get_constraintdef(constraint_info.oid, true)
            FROM pg_constraint constraint_info
            JOIN pg_class table_info ON table_info.oid = constraint_info.conrelid
            JOIN pg_namespace namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = current_schema()
              AND table_info.relname = ANY(%s)
              AND constraint_info.conname = ANY(%s)
            ORDER BY table_info.relname, constraint_info.conname
            """,
            (
                sorted(EXPECTED_SECURITY_CONSTRAINT_STRUCTURE),
                constraint_names,
            ),
        )
        constraint_rows = cursor.fetchall()

        index_names = sorted(
            index_name
            for indexes in EXPECTED_SECURITY_INDEX_STRUCTURE.values()
            for index_name in indexes
        )
        cursor.execute(
            """
            SELECT table_info.oid, table_info.relname, index_class.relname,
                   index_info.indisunique, index_info.indisvalid,
                   index_info.indisready, index_info.indnkeyatts,
                   index_info.indnatts,
                   ARRAY(
                       SELECT attribute_info.attname
                       FROM unnest(index_info.indkey)
                            WITH ORDINALITY AS key_info(attribute_number, position)
                       JOIN pg_attribute attribute_info
                         ON attribute_info.attrelid = table_info.oid
                        AND attribute_info.attnum = key_info.attribute_number
                       WHERE key_info.position <= index_info.indnkeyatts
                       ORDER BY key_info.position
                   ),
                   index_info.indexprs IS NOT NULL,
                   COALESCE(
                       pg_get_expr(index_info.indpred, index_info.indrelid, true),
                       ''
                   )
            FROM pg_index index_info
            JOIN pg_class table_info ON table_info.oid = index_info.indrelid
            JOIN pg_class index_class ON index_class.oid = index_info.indexrelid
            JOIN pg_namespace namespace_info
              ON namespace_info.oid = table_info.relnamespace
            WHERE namespace_info.nspname = current_schema()
              AND table_info.relname = ANY(%s)
              AND index_class.relname = ANY(%s)
            ORDER BY table_info.relname, index_class.relname
            """,
            (sorted(EXPECTED_SECURITY_INDEX_STRUCTURE), index_names),
        )
        index_rows = cursor.fetchall()

        cursor.execute(
            """
            SELECT table_info.oid, table_info.relname, id_attribute.attnum,
                   sequence_info.oid, ownership_info.refobjid,
                   ownership_info.refobjsubid, ownership_info.deptype,
                   default_info.oid IS NOT NULL,
                   pg_get_expr(default_info.adbin, default_info.adrelid, true)
                       = format(
                           'nextval(%%L::regclass)',
                           sequence_info.oid::regclass::text
                       ),
                   EXISTS (
                       SELECT 1
                       FROM pg_depend default_dependency
                       WHERE default_dependency.classid = 'pg_attrdef'::regclass
                         AND default_dependency.objid = default_info.oid
                         AND default_dependency.refclassid = 'pg_class'::regclass
                         AND default_dependency.refobjid = sequence_info.oid
                   )
            FROM pg_class table_info
            JOIN pg_namespace namespace_info
              ON namespace_info.oid = table_info.relnamespace
            JOIN pg_attribute id_attribute
              ON id_attribute.attrelid = table_info.oid
             AND id_attribute.attname = 'id'
             AND id_attribute.attnum > 0
             AND NOT id_attribute.attisdropped
            JOIN pg_depend ownership_info
              ON ownership_info.classid = 'pg_class'::regclass
             AND ownership_info.refclassid = 'pg_class'::regclass
             AND ownership_info.refobjid = table_info.oid
             AND ownership_info.refobjsubid = id_attribute.attnum
             AND ownership_info.deptype = 'a'
            JOIN pg_class sequence_info
              ON sequence_info.oid = ownership_info.objid
             AND sequence_info.relkind = 'S'
            LEFT JOIN pg_attrdef default_info
              ON default_info.adrelid = table_info.oid
             AND default_info.adnum = id_attribute.attnum
            WHERE namespace_info.nspname = current_schema()
              AND table_info.relname = ANY(%s)
            ORDER BY table_info.relname
            """,
            (sorted(EXPECTED_SECURITY_COLUMN_STRUCTURE),),
        )
        sequence_rows = cursor.fetchall()

        return _security_catalog_is_compatible(
            schema_name,
            table_rows,
            column_rows,
            constraint_rows,
            index_rows,
            sequence_rows,
        )
    except Exception:
        return False
    finally:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def create_security_schema(cursor):
    for statement in SECURITY_SCHEMA_STATEMENTS:
        cursor.execute(statement)


def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE,
            username TEXT,
            first_name TEXT
        )
    """)
    try:
        cursor.execute(
            "ALTER TABLE clients ADD COLUMN phone TEXT"
        )
    except:
        conn.rollback()

    try:
        cursor.execute(
            "ALTER TABLE clients ADD COLUMN address TEXT"
        )
    except:
        conn.rollback()
    cursor.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS client_note TEXT")
    # Orders v2 foundation: clients.id becomes the channel-independent
    # customer identity; last_name is needed for non-Telegram (manual,
    # future website) customer records. telegram_id stays as-is and
    # remains nullable, unchanged.
    cursor.execute("ALTER TABLE clients ADD COLUMN IF NOT EXISTS last_name TEXT")
    create_security_schema(cursor)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart_items (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT,
            product_id INTEGER,
            weight INTEGER
        )
    """)
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS option_id INTEGER")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ")
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_cart_items_telegram_updated
        ON cart_items (telegram_id, updated_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customer_events (
            id SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            metadata JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_events_telegram_created
        ON customer_events (telegram_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_customer_events_type_created
        ON customer_events (event_type, created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_id BIGINT,
            telegram_id BIGINT,
            username TEXT,
            phone TEXT,
            address TEXT,
            total REAL,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL,
            product_id INTEGER,
            product_name TEXT,
            weight INTEGER,
            price REAL
        )
    """)
    cursor.execute("ALTER TABLE order_items ADD COLUMN IF NOT EXISTS option_id INTEGER")
    cursor.execute(
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS "
        "pricing_mode TEXT NOT NULL DEFAULT 'per_kg' "
        "CHECK (pricing_mode IN ('fixed', 'per_kg', 'options'))"
    )
    # Nullable: only meaningful for per_kg lines. NULL on historical rows
    # predating this column (no reliable value to backfill) and on
    # fixed/options lines, whose selected commercial price is already
    # captured directly in order_items.price.
    cursor.execute(
        "ALTER TABLE order_items ADD COLUMN IF NOT EXISTS "
        "price_per_kg_snapshot REAL"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_events (
            id SERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL,
            event_type TEXT NOT NULL,
            event_text TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_order_events_order_id_created_at
        ON order_events (order_id, created_at)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS error_logs (
            id SERIAL PRIMARY KEY,
            route TEXT,
            action TEXT,
            error_message TEXT,
            traceback TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_error_logs_created_at
        ON error_logs (created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_posts (
            id SERIAL PRIMARY KEY,
            message_text TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            error_message TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_at TIMESTAMPTZ
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_channel_posts_created_at
        ON channel_posts (created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id SERIAL PRIMARY KEY,
            message_text TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            error_message TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            sent_at TIMESTAMPTZ
        )
    """)
    cursor.execute("ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS target_type TEXT DEFAULT 'all_clients'")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_recipients (
            id SERIAL PRIMARY KEY,
            broadcast_id INTEGER REFERENCES broadcasts(id),
            telegram_id BIGINT NOT NULL,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            sent_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (broadcast_id, telegram_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_broadcast
        ON broadcast_recipients (broadcast_id)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY,
            category_id INTEGER REFERENCES categories(id),
            name TEXT NOT NULL,
            price_per_kg REAL NOT NULL,
            description TEXT,
            image_url TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            sort_order INTEGER DEFAULT 0
        )
    """)
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_grams INTEGER DEFAULT 0")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_out_of_stock BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_threshold_grams INTEGER DEFAULT 500")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_alert_sent BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS low_stock_alert_sent_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_promotion BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS promotion_title TEXT")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS promotion_sort_order INTEGER NOT NULL DEFAULT 0")
    cursor.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
        "pricing_mode TEXT NOT NULL DEFAULT 'per_kg' "
        "CHECK (pricing_mode IN ('fixed', 'per_kg', 'options'))"
    )
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS fixed_price REAL")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS sale_unit TEXT")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS unit_weight_grams INTEGER")
    cursor.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock_quantity INTEGER")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_options (
            id SERIAL PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            label TEXT NOT NULL,
            weight INTEGER,
            price REAL NOT NULL,
            sort_order INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    cursor.execute("ALTER TABLE product_options ADD COLUMN IF NOT EXISTS stock_quantity INTEGER")
    cursor.execute(
        "ALTER TABLE product_options ADD COLUMN IF NOT EXISTS "
        "is_out_of_stock BOOLEAN DEFAULT FALSE"
    )
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory_movements (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id),
            order_id BIGINT,
            movement_type TEXT NOT NULL,
            quantity_grams INTEGER,
            stock_before INTEGER,
            stock_after INTEGER,
            note TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    # quantity_grams predates fixed/options-mode movements and was NOT NULL;
    # a units-based movement (fixed/options) has no gram value, so the
    # column must accept NULL there. per_kg movements keep writing a real
    # gram value, same as before.
    cursor.execute(
        "ALTER TABLE inventory_movements ALTER COLUMN quantity_grams DROP NOT NULL"
    )
    cursor.execute(
        "ALTER TABLE inventory_movements ADD COLUMN IF NOT EXISTS "
        "quantity_units INTEGER"
    )
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_inventory_movements_product_created
        ON inventory_movements (product_id, created_at DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_shops (
            id SERIAL PRIMARY KEY,
            shop_key TEXT UNIQUE NOT NULL,
            brand_name TEXT NOT NULL,
            admin_url TEXT,
            bot_username TEXT,
            bot_url TEXT,
            landing_url TEXT,
            status TEXT DEFAULT 'demo',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("ALTER TABLE master_shops ADD COLUMN IF NOT EXISTS bot_url TEXT")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS master_shop_snapshots (
            id SERIAL PRIMARY KEY,
            shop_key TEXT NOT NULL,
            total_orders INTEGER DEFAULT 0,
            today_orders INTEGER DEFAULT 0,
            pending_orders INTEGER DEFAULT 0,
            month_revenue NUMERIC DEFAULT 0,
            low_stock_count INTEGER DEFAULT 0,
            total_clients INTEGER DEFAULT 0,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_master_shop_snapshots_shop_seen
        ON master_shop_snapshots (shop_key, last_seen_at DESC, id DESC)
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_recommendations (
            id SERIAL PRIMARY KEY,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            recommended_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            recommendation_type TEXT NOT NULL DEFAULT 'frequently_bought_together',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_product_recommendations_unique
        ON product_recommendations (product_id, recommended_product_id, recommendation_type)
    """)
    try:
        cursor.execute("ALTER TABLE orders ALTER COLUMN order_id TYPE BIGINT")
        cursor.execute("ALTER TABLE orders ALTER COLUMN telegram_id TYPE BIGINT")
        conn.commit()
    except Exception as e:
        print("ORDERS BIGINT MIGRATION ERROR:", e)
        conn.rollback()

    try:
        cursor.execute(
            "ALTER TABLE orders ADD COLUMN payment_method TEXT"
        )
    except:
        conn.rollback()

    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_selected_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reminded_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_reported_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_deducted BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_deducted_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_restored BOOLEAN DEFAULT FALSE")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS inventory_restored_at TIMESTAMPTZ")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_note TEXT")

    # Orders v2 schema (Checkpoint A, columns added additively; Checkpoint E,
    # cutover complete). payment_status/fulfillment_status are now the sole
    # runtime authority everywhere. The legacy `status` column is no longer
    # read or written by any runtime code path -- it remains present only as
    # frozen historical data for orders created before the cutover.
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "payment_status TEXT NOT NULL DEFAULT 'unpaid' "
        f"CHECK (payment_status IN ({_sql_string_list(ORDER_PAYMENT_STATUS_VALUES)}))"
    )
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "fulfillment_status TEXT NOT NULL DEFAULT 'new' "
        f"CHECK (fulfillment_status IN ({_sql_string_list(ORDER_FULFILLMENT_STATUS_VALUES)}))"
    )
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "source TEXT NOT NULL DEFAULT 'telegram' "
        f"CHECK (source IN ({_sql_string_list(ORDER_SOURCE_VALUES)}))"
    )
    # Free-text hint for the chosen source (e.g. an Instagram username or a
    # manually-entered reference). No identity resolution is implemented.
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS source_reference TEXT")
    # Channel-independent customer identity. Nullable: existing rows are not
    # backfilled here (Checkpoint B). No ON DELETE behavior is specified,
    # matching the existing products.category_id -> categories(id) FK style
    # in this schema; client rows are never hard-deleted by this app.
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "client_id INTEGER REFERENCES clients(id)"
    )
    # Order-level historical snapshots, mirroring the existing
    # orders.phone/orders.address pattern: copied at order-creation time,
    # never live-joined back to clients. orders.phone/orders.address remain
    # untouched for backward compatibility.
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_name TEXT")
    cursor.execute(
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS "
        "delivery_method TEXT "
        f"CHECK (delivery_method IN ({_sql_string_list(ORDER_DELIVERY_METHOD_VALUES)}))"
    )
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_street TEXT")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_house_number TEXT")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_postcode TEXT")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_city TEXT")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_country TEXT")
    cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_notes TEXT")

    conn.commit()
    conn.close()
