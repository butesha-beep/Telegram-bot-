import asyncio
import ast
import base64
import contextlib
import copy
import hashlib
import inspect
import io
import logging
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Request
from fastapi.responses import PlainTextResponse


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/preview-foundation")
os.environ.setdefault(
    "BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
)
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-session-secret")

import admin_app
import bot
import db_schema
from runtime_settings import (
    PREVIEW_PUBLIC_EXPOSURE_APPROVED,
    PREVIEW_PUBLIC_EXPOSURE_REQUIREMENTS,
    env_flag_enabled,
    get_app_env,
)


DUMMY_GATE_USERNAME = "preview_tester"
DUMMY_GATE_PASSWORD = "R7!vK2@qM9#tL4$zN8%wC5&xP3*eH6^sJ1"
SECRET_EXCEPTION_MARKER = "B11A_SECRET_EXCEPTION_MARKER_7f29"


def generated_gate_password(length):
    material = DUMMY_GATE_PASSWORD
    counter = 0
    while len(material) < length:
        digest = hashlib.sha256(
            f"preview-gate-boundary-{counter}".encode("ascii")
        ).hexdigest()
        material += digest
        counter += 1
    return material[:length]


def capture_observables(callback):
    stdout = io.StringIO()
    stderr = io.StringIO()
    logs = io.StringIO()
    handler = logging.StreamHandler(logs)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = callback()
    finally:
        root_logger.removeHandler(handler)
    return result, stdout.getvalue() + stderr.getvalue() + logs.getvalue()


def response_observable(response):
    if isinstance(response, str):
        return response
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    headers = getattr(response, "headers", {})
    return str(body) + str(dict(headers))


def make_request(
    path,
    method="GET",
    authorization=None,
    query_string=b"",
    raw_path=None,
):
    headers = []
    if authorization is not None:
        header_value = (
            authorization
            if isinstance(authorization, bytes)
            else authorization.encode("latin-1")
        )
        headers.append((b"authorization", header_value))
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": raw_path or path.encode("ascii"),
        "query_string": query_string,
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    })


def basic_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8"))
    return f"Basic {token.decode('ascii')}"


SECURITY_TABLE_OIDS = {
    "web_sessions": 1001,
    "consumed_login_nonces": 1002,
}


def compatible_security_catalog_rows():
    table_rows = [
        ("public", table_name, table_oid, "r")
        for table_name, table_oid in SECURITY_TABLE_OIDS.items()
    ]
    column_rows = []
    column_numbers = {}
    for table_name, expected_columns in db_schema.EXPECTED_SECURITY_COLUMN_STRUCTURE.items():
        column_numbers[table_name] = {}
        for attribute_number, (column_name, specification) in enumerate(
            expected_columns.items(), start=1
        ):
            column_numbers[table_name][column_name] = attribute_number
            column_rows.append(
                (
                    SECURITY_TABLE_OIDS[table_name],
                    table_name,
                    attribute_number,
                    column_name,
                    *specification,
                )
            )
    constraint_rows = [
        (
            SECURITY_TABLE_OIDS[table_name],
            table_name,
            constraint_name,
            constraint_type,
            True,
            list(constrained_columns),
            definition,
        )
        for table_name, constraints in db_schema.EXPECTED_SECURITY_CONSTRAINT_STRUCTURE.items()
        for constraint_name, (
            constraint_type,
            constrained_columns,
            definition,
        ) in constraints.items()
    ]
    index_rows = [
        (
            SECURITY_TABLE_OIDS[table_name],
            table_name,
            index_name,
            is_unique,
            True,
            True,
            len(key_columns),
            len(key_columns),
            list(key_columns),
            False,
            predicate,
        )
        for table_name, indexes in db_schema.EXPECTED_SECURITY_INDEX_STRUCTURE.items()
        for index_name, (is_unique, key_columns, predicate) in indexes.items()
    ]
    sequence_rows = [
        (
            SECURITY_TABLE_OIDS[table_name],
            table_name,
            column_numbers[table_name]["id"],
            SECURITY_TABLE_OIDS[table_name] + 100,
            SECURITY_TABLE_OIDS[table_name],
            column_numbers[table_name]["id"],
            "a",
            True,
            True,
            True,
        )
        for table_name in SECURITY_TABLE_OIDS
    ]
    return table_rows, column_rows, constraint_rows, index_rows, sequence_rows


class SchemaCursor:
    def __init__(
        self,
        columns=None,
        tables=None,
        security_columns=None,
        constraints=None,
        indexes=None,
        sequences=None,
        schema_name="public",
        read_only="on",
        fail=False,
    ):
        self.columns = columns
        self.tables = tables
        self.security_columns = security_columns
        self.constraints = constraints
        self.indexes = indexes
        self.sequences = sequences
        self.schema_name = schema_name
        self.read_only = read_only
        self.fail = fail
        self.queries = []
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))
        if self.fail:
            raise RuntimeError("credential-like internal detail")

    def fetchone(self):
        if "current_schema()" in self.queries[-1][0]:
            return (self.schema_name,)
        return (self.read_only,)

    def fetchall(self):
        query = self.queries[-1][0]
        if "information_schema.columns" in query and "data_type" not in query:
            if self.columns is not None:
                return self.columns
            return [
                (table_name, column_name)
                for table_name, required in db_schema.REQUIRED_CATALOG_COLUMNS.items()
                for column_name in required
            ]
        valid_rows = compatible_security_catalog_rows()
        if "table_info.relkind" in query:
            return valid_rows[0] if self.tables is None else self.tables
        if "format_type" in query:
            if self.security_columns is not None:
                return self.security_columns
            return valid_rows[1]
        if "FROM pg_constraint" in query:
            if self.constraints is not None:
                return self.constraints
            return valid_rows[2]
        if "FROM pg_index" in query:
            if self.indexes is not None:
                return self.indexes
            return valid_rows[3]
        if "ownership_info" in query:
            return valid_rows[4] if self.sequences is None else self.sequences
        return []

    def close(self):
        self.closed = True


class SchemaConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.session_args = None
        self.rolled_back = False
        self.closed = False
        self.committed = False

    def set_session(self, **kwargs):
        self.session_args = kwargs

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class RecordingCursor:
    def __init__(self, fetchone_values=(), fetchall_values=()):
        self.fetchone_values = list(fetchone_values)
        self.fetchall_values = list(fetchall_values)
        self.queries = []
        self.rowcount = 1
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))

    def fetchone(self):
        if not self.fetchone_values:
            return None
        return self.fetchone_values.pop(0)

    def fetchall(self):
        if not self.fetchall_values:
            return []
        return self.fetchall_values.pop(0)

    def close(self):
        self.closed = True


class RecordingConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


class FailureCursor(RecordingCursor):
    def __init__(
        self,
        fetchone_values=(),
        fetchall_values=(),
        fail_execute_at=None,
        fail_close=False,
    ):
        super().__init__(fetchone_values, fetchall_values)
        self.fail_execute_at = fail_execute_at
        self.fail_close = fail_close

    def execute(self, query, params=None):
        super().execute(query, params)
        if self.fail_execute_at == len(self.queries):
            raise RuntimeError(SECRET_EXCEPTION_MARKER)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError(SECRET_EXCEPTION_MARKER)


class FailureConnection(RecordingConnection):
    def __init__(
        self,
        cursor,
        fail_cursor=False,
        fail_commit_at=None,
        fail_rollback=False,
        fail_close=False,
    ):
        super().__init__(cursor)
        self.fail_cursor = fail_cursor
        self.fail_commit_at = fail_commit_at
        self.fail_rollback = fail_rollback
        self.fail_close = fail_close

    def cursor(self):
        if self.fail_cursor:
            raise RuntimeError(SECRET_EXCEPTION_MARKER)
        return super().cursor()

    def commit(self):
        super().commit()
        if self.fail_commit_at == self.commit_count:
            raise RuntimeError(SECRET_EXCEPTION_MARKER)

    def rollback(self):
        super().rollback()
        if self.fail_rollback:
            raise RuntimeError(SECRET_EXCEPTION_MARKER)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError(SECRET_EXCEPTION_MARKER)


class PreviewFoundationTests(unittest.TestCase):
    def setUp(self):
        self.original_database_ready = admin_app.DATABASE_READY

    def tearDown(self):
        admin_app.DATABASE_READY = self.original_database_ready

    def test_strict_environment_flags_accept_only_complete_true_values(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"B11A_TEST_FLAG": value}):
                    self.assertTrue(env_flag_enabled("B11A_TEST_FLAG"))
        for value in ("", "0", "false", "1x", "yesplease", "true!", "unknown"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"B11A_TEST_FLAG": value}):
                    self.assertFalse(env_flag_enabled("B11A_TEST_FLAG"))

    def test_imports_never_open_database_connections_even_with_write_flags(self):
        repository_root = Path(__file__).resolve().parents[1]
        code = "\n".join([
            "import os",
            "os.environ['DATABASE_URL'] = 'postgresql://unit-test.invalid/import-check'",
            "os.environ['BOT_TOKEN'] = '123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'",
            "os.environ['RUN_DB_INIT'] = 'true'",
            "os.environ['RUN_DB_SEED'] = 'true'",
            "import psycopg2",
            "def forbidden(*args, **kwargs):",
            "    raise AssertionError('import attempted a database connection')",
            "psycopg2.connect = forbidden",
            "import db_schema, storefront, admin_app, bot",
            "print('imports-ok')",
        ])
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repository_root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "imports-ok")

    def test_catalog_schema_probe_is_read_only_and_always_rolls_back(self):
        cursor = SchemaCursor()
        connection = SchemaConnection(cursor)

        compatible = db_schema.catalog_schema_is_compatible(lambda: connection)

        self.assertTrue(compatible)
        self.assertEqual(
            connection.session_args, {"readonly": True, "autocommit": False}
        )
        statements = [query for query, _ in cursor.queries]
        self.assertEqual(statements[0], "BEGIN TRANSACTION READ ONLY")
        self.assertIn("SET LOCAL statement_timeout", statements[1])
        self.assertIn("SET LOCAL lock_timeout", statements[2])
        self.assertIn("transaction_read_only", statements[3])
        self.assertIn("information_schema.columns", statements[4])
        self.assertIn("current_schema", statements[5])
        self.assertIn("relkind", statements[6])
        self.assertIn("format_type", statements[7])
        self.assertIn("pg_constraint", statements[8])
        self.assertIn("pg_index", statements[9])
        self.assertIn("pg_depend", statements[10])
        executed_sql = "\n".join(statements).upper()
        for write_keyword in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "CREATE ",
            "ALTER ",
            "DROP ",
            "TRUNCATE ",
            "COMMIT",
        ):
            self.assertNotIn(write_keyword, executed_sql)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(connection.committed)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)

    def test_catalog_schema_probe_rejects_incompatible_or_non_read_only_state(self):
        complete_columns = [
            (table_name, column_name)
            for table_name, required in db_schema.REQUIRED_CATALOG_COLUMNS.items()
            for column_name in required
        ]
        incomplete = complete_columns[:-1]
        for cursor in (
            SchemaCursor(columns=incomplete),
            SchemaCursor(tables=[]),
            SchemaCursor(security_columns=[]),
            SchemaCursor(constraints=[]),
            SchemaCursor(indexes=[]),
            SchemaCursor(sequences=[]),
            SchemaCursor(schema_name=""),
            SchemaCursor(read_only="off"),
            SchemaCursor(fail=True),
        ):
            with self.subTest(cursor=cursor):
                connection = SchemaConnection(cursor)
                self.assertFalse(
                    db_schema.catalog_schema_is_compatible(lambda: connection)
                )
                self.assertTrue(connection.rolled_back)
                self.assertFalse(connection.committed)
                self.assertTrue(connection.closed)

    def test_security_catalog_structure_rejects_same_name_wrong_objects(self):
        valid_rows = compatible_security_catalog_rows()

        def rejected(rows):
            self.assertFalse(
                db_schema._security_catalog_is_compatible("public", *rows)
            )

        self.assertTrue(
            db_schema._security_catalog_is_compatible("public", *valid_rows)
        )

        for table_index, replacement in (
            (0, ("other", "web_sessions", 1001, "r")),
            (0, ("public", "web_sessions", 1001, "v")),
            (1, ("public", "consumed_login_nonces", 1001, "r")),
        ):
            rows = copy.deepcopy(valid_rows)
            rows[0][table_index] = replacement
            rejected(rows)

        for column_index, changed_value in (
            (0, "text"),
            (1, False),
            (2, False),
        ):
            rows = copy.deepcopy(valid_rows)
            target = list(rows[1][0])
            target[4 + column_index] = changed_value
            rows[1][0] = tuple(target)
            rejected(rows)
        rows = copy.deepcopy(valid_rows)
        rows[1].append((1001, "web_sessions", 99, "unexpected", "text", False, False))
        rejected(rows)

        for constraint_index in range(len(valid_rows[2])):
            rows = copy.deepcopy(valid_rows)
            target = list(rows[2][constraint_index])
            target[6] = str(target[6]) + " AND FALSE"
            rows[2][constraint_index] = tuple(target)
            rejected(rows)
        for field_index, changed_value in (
            (0, 9999),
            (3, "x"),
            (4, False),
            (5, ["role"]),
        ):
            rows = copy.deepcopy(valid_rows)
            target = list(rows[2][0])
            target[field_index] = changed_value
            rows[2][0] = tuple(target)
            rejected(rows)

        for index_index in range(len(valid_rows[3])):
            rows = copy.deepcopy(valid_rows)
            target = list(rows[3][index_index])
            target[8] = ["id"]
            target[6] = 1
            target[7] = 1
            rows[3][index_index] = tuple(target)
            rejected(rows)
        active_index = next(
            index
            for index, row in enumerate(valid_rows[3])
            if row[2] == "uq_web_sessions_active_role_account"
        )
        for field_index, changed_value in (
            (0, 9999),
            (3, False),
            (4, False),
            (5, False),
            (7, 3),
            (8, ["account_key", "role"]),
            (9, True),
            (10, ""),
            (10, "revoked_at IS NOT NULL"),
        ):
            rows = copy.deepcopy(valid_rows)
            target = list(rows[3][active_index])
            target[field_index] = changed_value
            rows[3][active_index] = tuple(target)
            rejected(rows)

        for sequence_index in range(len(valid_rows[4])):
            for field_index, changed_value in (
                (2, 99),
                (3, 0),
                (4, 9999),
                (5, 99),
                (6, "i"),
                (7, False),
                (8, False),
                (9, False),
            ):
                rows = copy.deepcopy(valid_rows)
                target = list(rows[4][sequence_index])
                target[field_index] = changed_value
                rows[4][sequence_index] = tuple(target)
                rejected(rows)

    def test_security_catalog_constraint_column_order_is_not_significant(self):
        # Corrective fix: Postgres populates a CHECK constraint's conkey in
        # expression-analysis order (an implementation detail), not a
        # stable/semantic order -- the same real constraint can legitimately
        # report its columns in a different order than
        # EXPECTED_SECURITY_CONSTRAINT_STRUCTURE declares them. Only the
        # column *set* must match; reversing a multi-column constraint's
        # reported order must not be treated as an incompatibility.
        valid_rows = compatible_security_catalog_rows()
        expiry_index = next(
            index
            for index, row in enumerate(valid_rows[2])
            if row[2] == "ck_web_sessions_expiry"
        )
        self.assertEqual(valid_rows[2][expiry_index][5], ["issued_at", "expires_at"])

        rows = copy.deepcopy(valid_rows)
        target = list(rows[2][expiry_index])
        target[5] = ["expires_at", "issued_at"]  # same columns, reversed order
        rows[2][expiry_index] = tuple(target)

        self.assertTrue(db_schema._security_catalog_is_compatible("public", *rows))

    def test_security_catalog_sequence_query_escapes_postgres_format_percent(self):
        # Corrective fix: the sequence-ownership query calls Postgres's own
        # format('nextval(%L::regclass)', ...) -- since this query is also
        # executed with a psycopg2 params tuple, a literal %L must be
        # escaped as %%L or psycopg2 misreads it as an extra placeholder
        # and raises IndexError before any row is ever returned. This is a
        # static guard: the compatible-fixture-driven tests above use a
        # SchemaCursor mock that never re-parses the query as psycopg2
        # would, so they cannot catch this class of bug themselves.
        source = inspect.getsource(db_schema.catalog_schema_is_compatible)
        self.assertIn("nextval(%%L::regclass)", source)
        self.assertNotIn("nextval(%L::regclass)", source)

    def test_ready_returns_only_generic_success_or_failure(self):
        with patch.object(admin_app, "DATABASE_URL", "postgresql://configured/test"):
            with patch.object(
                admin_app, "catalog_schema_is_compatible", return_value=True
            ):
                success = asyncio.run(admin_app.ready())
            with patch.object(
                admin_app, "catalog_schema_is_compatible", return_value=False
            ):
                failure = asyncio.run(admin_app.ready())

        self.assertEqual(success, {"status": "ready"})
        self.assertEqual(failure.status_code, 503)
        body = failure.body.decode("utf-8")
        self.assertEqual(body, '{"status":"unavailable"}')
        self.assertNotIn("postgresql://", body)
        self.assertNotIn("schema", body.lower())

    def test_app_environment_is_strict_and_defaults_to_local(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(get_app_env(), "local")
        for value in ("local", "test", "preview", "production"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"APP_ENV": value}):
                    self.assertEqual(get_app_env(), value)
        for value in ("", "Preview", " preview", "staging", "unknown"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"APP_ENV": value}):
                    with self.assertRaisesRegex(RuntimeError, "APP_ENV is invalid"):
                        get_app_env()

    def test_preview_public_exposure_remains_blocked_until_b11b(self):
        self.assertFalse(PREVIEW_PUBLIC_EXPOSURE_APPROVED)
        self.assertEqual(
            set(PREVIEW_PUBLIC_EXPOSURE_REQUIREMENTS),
            {"trusted_proxy_handling", "https_enforcement", "hsts"},
        )

    def test_preview_environment_requires_enabled_valid_gate(self):
        for enabled in ("", "0", "false", "invalid"):
            with self.subTest(enabled=enabled):
                environment = {
                    "APP_ENV": "preview",
                    "PREVIEW_GATE_ENABLED": enabled,
                    "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
                    "PREVIEW_GATE_PASSWORD": DUMMY_GATE_PASSWORD,
                }
                with patch.dict(os.environ, environment):
                    with self.assertRaisesRegex(
                        RuntimeError, "Preview access gate is not configured"
                    ):
                        asyncio.run(admin_app.startup_db_init())

        with patch.dict(
            os.environ,
            {
                "APP_ENV": "preview",
                "PREVIEW_GATE_ENABLED": "true",
                "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
                "PREVIEW_GATE_PASSWORD": "weak-password",
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Preview access gate is not configured"
            ):
                asyncio.run(admin_app.startup_db_init())

    def test_unknown_environment_fails_admin_and_bot_startup_closed(self):
        with patch.dict(os.environ, {"APP_ENV": "staging"}):
            with self.assertRaisesRegex(RuntimeError, "APP_ENV is invalid"):
                asyncio.run(admin_app.startup_db_init())
            with self.assertRaisesRegex(RuntimeError, "APP_ENV is invalid"):
                asyncio.run(bot.main())

    def test_weak_preview_gate_passwords_are_rejected(self):
        weak_passwords = (
            " " * 32,
            "A" * 32,
            "Aa1!" * 8,
            "Password123456!Password123456!Xy",
            "QWERTY123456!qwerty123456!Qwerty",
            "Abcdefghijklmnopqrstuvwxyz123456!",
            " R7!vK2@qM9#tL4$zN8%wC5&xP3*eH6^s",
        )
        for password in weak_passwords:
            with self.subTest(password_length=len(password)):
                environment = {
                    "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
                    "PREVIEW_GATE_PASSWORD": password,
                }
                with patch.dict(os.environ, environment):
                    with self.assertRaisesRegex(
                        RuntimeError, "Preview access gate is not configured"
                    ):
                        admin_app._preview_gate_credentials()

        username_equivalent = "Abcd_Efgh_1234_Ijkl_Mnop_5678_Qrst"
        with patch.dict(
            os.environ,
            {
                "PREVIEW_GATE_USERNAME": username_equivalent,
                "PREVIEW_GATE_PASSWORD": username_equivalent,
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Preview access gate is not configured"
            ):
                admin_app._preview_gate_credentials()

    def test_preview_gate_rejects_otherwise_valid_31_character_password(self):
        password = generated_gate_password(31)

        self.assertEqual(len(password), 31)
        self.assertTrue(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, generated_gate_password(32)
            )
        )
        self.assertFalse(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, password
            )
        )

    def test_preview_gate_accepts_valid_32_character_password(self):
        password = generated_gate_password(32)

        self.assertEqual(len(password), 32)
        self.assertTrue(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, password
            )
        )

    def test_preview_gate_accepts_valid_256_character_password(self):
        password = generated_gate_password(256)

        self.assertEqual(len(password), 256)
        self.assertTrue(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, password
            )
        )

    def test_preview_gate_rejects_otherwise_valid_257_character_password(self):
        password = generated_gate_password(257)

        self.assertEqual(len(password), 257)
        self.assertTrue(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, password[:256]
            )
        )
        self.assertFalse(
            admin_app._preview_gate_password_is_strong(
                DUMMY_GATE_USERNAME, password
            )
        )

    def test_preview_gate_rejects_missing_or_empty_configured_username(self):
        password = generated_gate_password(32)
        with patch.dict(
            os.environ,
            {"PREVIEW_GATE_PASSWORD": password},
            clear=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Preview access gate is not configured"
            ):
                admin_app._preview_gate_credentials()

        with patch.dict(
            os.environ,
            {
                "PREVIEW_GATE_USERNAME": "",
                "PREVIEW_GATE_PASSWORD": password,
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Preview access gate is not configured"
            ):
                admin_app._preview_gate_credentials()

    def test_preview_gate_rejects_malformed_configured_usernames(self):
        password = generated_gate_password(32)
        invalid_usernames = (
            "preview user",
            " preview_user",
            "preview_user ",
            "preview:user",
            "preview\nuser",
            "a" * 65,
        )
        for username in invalid_usernames:
            with self.subTest(username_length=len(username)):
                with patch.dict(
                    os.environ,
                    {
                        "PREVIEW_GATE_USERNAME": username,
                        "PREVIEW_GATE_PASSWORD": password,
                    },
                ):
                    with self.assertRaisesRegex(
                        RuntimeError, "Preview access gate is not configured"
                    ):
                        admin_app._preview_gate_credentials()

    def test_valid_configured_preview_gate_credentials_are_accepted(self):
        password = generated_gate_password(32)
        with patch.dict(
            os.environ,
            {
                "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
                "PREVIEW_GATE_PASSWORD": password,
            },
        ):
            self.assertEqual(
                admin_app._preview_gate_credentials(),
                (DUMMY_GATE_USERNAME, password),
            )

    def test_invalid_preview_username_fails_before_requests_are_served(self):
        environment = {
            "APP_ENV": "preview",
            "PREVIEW_GATE_ENABLED": "true",
            "PREVIEW_GATE_USERNAME": "invalid:user",
            "PREVIEW_GATE_PASSWORD": generated_gate_password(32),
        }
        call_next = AsyncMock(return_value=PlainTextResponse("unexpected"))
        with patch.dict(os.environ, environment):
            with patch.object(admin_app, "catalog_schema_is_compatible") as probe:
                with self.assertRaisesRegex(
                    RuntimeError, "Preview access gate is not configured"
                ):
                    asyncio.run(admin_app.startup_db_init())
            response = asyncio.run(
                admin_app.require_admin_login(make_request("/shop"), call_next)
            )

        probe.assert_not_called()
        call_next.assert_not_awaited()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("invalid:user", response.body.decode("utf-8"))

    def test_preview_gate_protects_every_non_exempt_route_and_method(self):
        environment = {
            "APP_ENV": "preview",
            "PREVIEW_GATE_ENABLED": "true",
            "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
            "PREVIEW_GATE_PASSWORD": DUMMY_GATE_PASSWORD,
        }
        cases = (
            ("/docs", "GET", b""),
            ("/redoc", "GET", b""),
            ("/openapi.json", "GET", b""),
            ("/static/app.css", "GET", b""),
            ("/shop", "GET", b""),
            ("/shop", "GET", b"view=grid"),
            ("/login", "GET", b""),
            ("/admin", "GET", b""),
            ("/master", "GET", b""),
            ("/shop", "HEAD", b""),
            ("/shop", "OPTIONS", b""),
            ("/health/", "GET", b""),
            ("/ready/", "GET", b""),
            ("//health", "GET", b""),
            ("/%61dmin", "GET", b""),
        )
        calls = []

        async def call_next(_request):
            calls.append(True)
            return PlainTextResponse("unexpected")

        with patch.dict(os.environ, environment):
            for path, method, query_string in cases:
                with self.subTest(path=path, method=method, query=query_string):
                    response = asyncio.run(
                        admin_app.require_admin_login(
                            make_request(
                                path, method=method, query_string=query_string
                            ),
                            call_next,
                        )
                    )
                    self.assertEqual(response.status_code, 401)
            encoded_response = asyncio.run(
                admin_app.require_admin_login(
                    make_request("/admin", raw_path=b"/%61dmin"), call_next
                )
            )
            self.assertEqual(encoded_response.status_code, 401)
        self.assertEqual(calls, [])

    def test_preview_gate_rejects_malformed_or_wrong_basic_headers(self):
        environment = {
            "APP_ENV": "preview",
            "PREVIEW_GATE_ENABLED": "true",
            "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
            "PREVIEW_GATE_PASSWORD": DUMMY_GATE_PASSWORD,
        }
        invalid_headers = (
            None,
            "",
            "Bearer token",
            "Basic",
            "Basic ",
            "Basic !!!not-base64!!!",
            "Basic " + base64.b64encode(b"missing-colon").decode("ascii"),
            basic_header("", DUMMY_GATE_PASSWORD),
            basic_header(DUMMY_GATE_USERNAME, ""),
            basic_header("wrong_user", DUMMY_GATE_PASSWORD),
            basic_header(DUMMY_GATE_USERNAME, "wrong-password"),
        )

        async def call_next(_request):
            return PlainTextResponse("unexpected")

        with patch.dict(os.environ, environment):
            for authorization in invalid_headers:
                with self.subTest(authorization=authorization):
                    response = asyncio.run(
                        admin_app.require_admin_login(
                            make_request("/shop", authorization=authorization),
                            call_next,
                        )
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertNotIn(
                        DUMMY_GATE_PASSWORD, response.body.decode("utf-8")
                    )

    def test_valid_basic_allows_request_and_exact_health_paths_are_exempt(self):
        environment = {
            "APP_ENV": "preview",
            "PREVIEW_GATE_ENABLED": "true",
            "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
            "PREVIEW_GATE_PASSWORD": DUMMY_GATE_PASSWORD,
        }
        calls = []

        async def call_next(_request):
            calls.append(True)
            return PlainTextResponse("allowed")

        with patch.dict(os.environ, environment):
            allowed = asyncio.run(
                admin_app.require_admin_login(
                    make_request(
                        "/shop",
                        authorization=basic_header(
                            DUMMY_GATE_USERNAME, DUMMY_GATE_PASSWORD
                        ),
                    ),
                    call_next,
                )
            )
            for path, query_string in (
                ("/health", b""),
                ("/health", b"probe=1"),
                ("/ready", b""),
            ):
                response = asyncio.run(
                    admin_app.require_admin_login(
                        make_request(path, query_string=query_string), call_next
                    )
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(calls, [True, True, True, True])

    def test_preview_gate_compares_both_credentials_without_short_circuit(self):
        environment = {
            "APP_ENV": "preview",
            "PREVIEW_GATE_ENABLED": "true",
            "PREVIEW_GATE_USERNAME": DUMMY_GATE_USERNAME,
            "PREVIEW_GATE_PASSWORD": DUMMY_GATE_PASSWORD,
        }
        request = make_request(
            "/shop", authorization=basic_header("wrong_user", "wrong-password")
        )
        with patch.dict(os.environ, environment):
            with patch.object(
                admin_app.hmac, "compare_digest", side_effect=(False, False)
            ) as compare:
                self.assertFalse(admin_app.preview_gate_authenticated(request))
        self.assertEqual(compare.call_count, 2)

    def test_disabled_master_and_telegram_write_routes_stop_before_handler(self):
        calls = []

        async def call_next(_request):
            calls.append(True)
            return PlainTextResponse("unexpected")

        environment = {
            "APP_ENV": "test",
            "PREVIEW_GATE_ENABLED": "",
            "ENABLE_MASTER_ADMIN": "",
            "ENABLE_TELEGRAM_ACTIONS": "",
        }
        with patch.dict(os.environ, environment):
            master = asyncio.run(
                admin_app.require_admin_login(make_request("/master"), call_next)
            )
            channel = asyncio.run(admin_app.require_admin_login(
                make_request("/channel/new", method="POST"), call_next
            ))
            broadcast = asyncio.run(admin_app.require_admin_login(
                make_request("/broadcasts/1/send", method="POST"), call_next
            ))

        self.assertEqual(
            [master.status_code, channel.status_code, broadcast.status_code],
            [404, 404, 404],
        )
        self.assertEqual(calls, [])

    def test_disabled_telegram_routes_also_reject_direct_invocation(self):
        environment = {"APP_ENV": "test", "ENABLE_TELEGRAM_ACTIONS": ""}
        with patch.dict(os.environ, environment):
            with patch.object(admin_app, "get_db_connection") as connection:
                responses = (
                    asyncio.run(admin_app.create_broadcast("message", "all_clients")),
                    asyncio.run(admin_app.send_broadcast_route(1)),
                    asyncio.run(admin_app.create_channel_post("message")),
                    asyncio.run(admin_app.send_channel_post_route(1)),
                    asyncio.run(admin_app.delete_channel_post(1)),
                )
        self.assertEqual([response.status_code for response in responses], [404] * 5)
        connection.assert_not_called()

    def test_disabled_admin_telegram_helpers_never_call_network_or_write_state(self):
        environment = {
            "ENABLE_TELEGRAM_ACTIONS": "",
            "ADMIN_ID": "not-a-recipient",
            "BOT_TOKEN": "dummy-not-real",
        }
        with patch.dict(os.environ, environment):
            with patch.object(admin_app.urllib.request, "urlopen") as urlopen:
                with patch.object(admin_app.psycopg2, "connect") as connect:
                    cursor = SchemaCursor()
                    self.assertIsNone(admin_app.get_admin_chat_id())
                    self.assertFalse(admin_app.send_low_stock_alert("Test", 1, 2))
                    admin_app.sync_low_stock_alert_state(cursor, [1])
                    self.assertFalse(
                        admin_app.send_order_status_notification_and_record(
                            123, "order-1", "paid"
                        )
                    )
                    self.assertFalse(
                        admin_app.send_weighing_notification_and_record(
                            123, "order-1", 10, []
                        )
                    )
                    self.assertFalse(
                        admin_app.record_notification_event(
                            "order-1", "notification_sent", "message"
                        )
                    )
                    self.assertEqual(
                        admin_app.send_channel_post("Test"), (False, "disabled")
                    )
                    self.assertEqual(
                        admin_app.send_broadcast_message(1, "Test"),
                        (False, "disabled"),
                    )

        urlopen.assert_not_called()
        connect.assert_not_called()
        self.assertEqual(cursor.queries, [])

    def test_false_notification_results_never_record_success(self):
        environment = {"ENABLE_TELEGRAM_ACTIONS": "true"}
        with patch.dict(os.environ, environment):
            with patch.object(
                admin_app, "send_order_status_notification", return_value=False
            ):
                with patch.object(admin_app.psycopg2, "connect") as connect:
                    result = admin_app.send_order_status_notification_and_record(
                        123, "order-1", "paid"
                    )
            self.assertFalse(result)
            connect.assert_not_called()

    def test_telegram_helper_exception_marker_is_fully_redacted(self):
        environment = {
            "ENABLE_TELEGRAM_ACTIONS": "true",
            "ADMIN_ID": "123456789",
            "BOT_TOKEN": "dummy-not-real",
            "TELEGRAM_CHANNEL_ID": "-1001234567890",
        }
        low_stock_cursor = RecordingCursor(
            fetchone_values=[("Product", 1, 2)]
        )
        with patch.dict(os.environ, environment):
            with patch.object(
                admin_app,
                "send_low_stock_alert",
                side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
            ):
                result, observed = capture_observables(
                    lambda: admin_app.sync_low_stock_alert_state(
                        low_stock_cursor, [19]
                    )
                )
            self.assertIsNone(result)
            self.assertIn(admin_app.LOW_STOCK_SYNC_FAILED, observed)
            self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
            self.assertNotIn(
                SECRET_EXCEPTION_MARKER, str(low_stock_cursor.queries)
            )
            self.assertFalse(
                any(
                    "SET low_stock_alert_sent = TRUE" in query
                    for query, _params in low_stock_cursor.queries
                )
            )

            with patch.object(
                admin_app.urllib.request,
                "urlopen",
                side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
            ):
                channel_result, channel_observed = capture_observables(
                    lambda: admin_app.send_channel_post("message")
                )
                broadcast_result, broadcast_observed = capture_observables(
                    lambda: admin_app.send_broadcast_message(123, "message")
                )

        self.assertEqual(
            channel_result, (False, admin_app.CHANNEL_POST_FAILED)
        )
        self.assertEqual(broadcast_result, (False, "failed"))
        for observable in (
            channel_observed,
            broadcast_observed,
            str(channel_result),
            str(broadcast_result),
        ):
            self.assertNotIn(SECRET_EXCEPTION_MARKER, observable)

    def test_channel_route_allowlists_stored_error_code(self):
        cursor = RecordingCursor(
            fetchone_values=[(1, "message", "draft")]
        )
        connection = RecordingConnection(cursor)
        environment = {"ENABLE_TELEGRAM_ACTIONS": "true"}
        with patch.dict(os.environ, environment):
            with patch.object(
                admin_app, "get_db_connection", return_value=connection
            ):
                with patch.object(
                    admin_app,
                    "send_channel_post",
                    return_value=(False, SECRET_EXCEPTION_MARKER),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.send_channel_post_route(1)
                        )
                    )

        stored = str(cursor.queries)
        response_text = (
            response.body.decode("utf-8", errors="replace")
            + str(dict(response.headers))
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(connection.commit_count, 1)
        self.assertIn(admin_app.CHANNEL_POST_FAILED, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, response_text)

    def test_stable_admin_error_writer_rejects_raw_error_values(self):
        cursor = RecordingCursor()
        connection = RecordingConnection(cursor)
        with patch.object(
            admin_app, "get_db_connection", return_value=connection
        ):
            result, observed = capture_observables(
                lambda: admin_app.log_admin_stable_error(
                    "/channel", "send_channel_post", SECRET_EXCEPTION_MARKER
                )
            )

        stored = str(cursor.queries)
        self.assertIsNone(result)
        self.assertEqual(connection.commit_count, 1)
        self.assertIn(admin_app.INTERNAL_OPERATION_FAILED, stored)
        self.assertTrue(
            any(params and params[3] is None for _query, params in cursor.queries)
        )
        self.assertNotIn(SECRET_EXCEPTION_MARKER, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)

    def test_dashboard_and_admin_error_logger_never_expose_raw_marker(self):
        with patch.object(
            admin_app.psycopg2,
            "connect",
            side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
        ):
            with patch.object(admin_app, "get_db_connection") as get_connection:
                with patch.object(admin_app.logger, "error") as read_error:
                    response, observed = capture_observables(
                        lambda: asyncio.run(admin_app.root())
                    )

        response_text = response_observable(response)
        read_error.assert_called_once_with(admin_app.DASHBOARD_LOAD_FAILED)
        get_connection.assert_not_called()
        self.assertNotIn(SECRET_EXCEPTION_MARKER, response_text)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)

        with patch.object(
            admin_app,
            "get_db_connection",
            side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
        ):
            result, failure_observed = capture_observables(
                lambda: admin_app.log_admin_error(
                    "/admin", "dashboard", SECRET_EXCEPTION_MARKER
                )
            )

        self.assertIsNone(result)
        self.assertIn(admin_app.ADMIN_ERROR_LOG_WRITE_FAILED, failure_observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, failure_observed)

    def test_historical_error_values_are_normalized_before_rendering(self):
        rows = [
            (1, "/admin", "dashboard", SECRET_EXCEPTION_MARKER, None),
        ]
        cursor = RecordingCursor(fetchall_values=[rows])
        connection = RecordingConnection(cursor)
        with patch.object(
            admin_app, "get_db_connection", return_value=connection
        ):
            page = asyncio.run(admin_app.logs())

        self.assertIn(admin_app.INTERNAL_OPERATION_FAILED, page)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, page)

    def test_admin_error_calls_use_only_classified_stable_codes(self):
        source = Path(admin_app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed_names = {"DASHBOARD_LOAD_FAILED"}
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id not in {
                "log_admin_error",
                "report_read_error",
            }:
                continue
            error_index = 2 if node.func.id == "log_admin_error" else 0
            if len(node.args) <= error_index:
                continue
            if node.func.id == "log_admin_error" and not isinstance(
                node.args[0], ast.Constant
            ):
                continue
            calls.append(node)
            error_code = node.args[error_index]
            is_literal = (
                isinstance(error_code, ast.Constant)
                and isinstance(error_code.value, str)
                and error_code.value in admin_app.ADMIN_OPERATION_ERROR_CODES
            )
            is_allowed_name = (
                isinstance(error_code, ast.Name)
                and error_code.id in allowed_names
            )
            self.assertTrue(is_literal or is_allowed_name)

        self.assertGreaterEqual(len(calls), 12)
        logger_source = inspect.getsource(
            admin_app.log_admin_error
        ) + inspect.getsource(admin_app.report_read_error)
        for fragment in (
            "str(error",
            "repr(",
            "traceback.",
            "logger.exception",
        ):
            self.assertNotIn(fragment, logger_source)

    def test_runbook_has_no_obsolete_startup_or_admin_recipient_claims(self):
        runbook = (
            Path(__file__).resolve().parents[1] / "CLONE_RUNBOOK.md"
        ).read_text(encoding="utf-8")
        obsolete_claims = (
            "Bot startup runs catalog seed",
            "Start/restart bot service",
            "config.json.admin_id as fallback",
            "runs `init_db()` on startup",
        )
        for claim in obsolete_claims:
            self.assertNotIn(claim, runbook)
        self.assertIn("no supported maintenance command currently exposes", runbook)
        self.assertIn("`RUN_DB_INIT` and `RUN_DB_SEED` do not enable", runbook)

    def test_customer_event_writer_cleans_up_every_failure_stage(self):
        cases = (
            ("connection", None),
            (
                "cursor",
                FailureConnection(RecordingCursor(), fail_cursor=True),
            ),
            (
                "execute",
                FailureConnection(FailureCursor(fail_execute_at=1)),
            ),
            (
                "commit",
                FailureConnection(RecordingCursor(), fail_commit_at=1),
            ),
            (
                "cleanup",
                FailureConnection(
                    FailureCursor(fail_execute_at=1, fail_close=True),
                    fail_rollback=True,
                    fail_close=True,
                ),
            ),
        )
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            for stage, connection in cases:
                with self.subTest(stage=stage):
                    factory_result = (
                        RuntimeError(SECRET_EXCEPTION_MARKER)
                        if connection is None
                        else connection
                    )
                    with patch.object(
                        bot,
                        "get_db_connection",
                        side_effect=[factory_result],
                    ):
                        result, observed = capture_observables(
                            lambda: bot.log_customer_event(
                                123,
                                "pending_order_reminder_failed",
                                {"reason": "failed"},
                            )
                        )

                    self.assertFalse(result)
                    self.assertIn("customer_event_log_failed", observed)
                    self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
                    if connection is not None:
                        self.assertEqual(connection.rollback_count, 1)
                        self.assertTrue(connection.closed)
                        if not connection.fail_cursor:
                            self.assertTrue(connection.cursor_instance.closed)

    def test_order_fulfillment_status_route_cleans_up_database_and_send_failures(self):
        # Migrated from the retired update_order_status/'done' path
        # (Checkpoint D): the same generic connection/cursor/execute/
        # cleanup/commit failure handling is now exercised through
        # update_order_fulfillment_status's 'delivered' action instead.
        def run_stage(stage):
            log_cursor = RecordingCursor()
            log_connection = RecordingConnection(log_cursor)
            if stage == "connection":
                connection = None
                connect_effect = RuntimeError(SECRET_EXCEPTION_MARKER)
            elif stage == "cursor":
                connection = FailureConnection(
                    RecordingCursor(), fail_cursor=True
                )
                connect_effect = connection
            elif stage == "execute":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1)
                )
                connect_effect = connection
            elif stage == "cleanup":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1, fail_close=True),
                    fail_rollback=True,
                    fail_close=True,
                )
                connect_effect = connection
            else:
                connection = FailureConnection(
                    RecordingCursor(
                        fetchone_values=[
                            ("shipped", True, False, 123)
                        ]
                    ),
                    fail_commit_at=1,
                )
                connect_effect = connection
            with patch.object(
                admin_app.psycopg2, "connect", side_effect=[connect_effect]
            ):
                with patch.object(
                    admin_app, "get_db_connection", return_value=log_connection
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.update_order_fulfillment_status(
                                "order-1", "delivered"
                            )
                        )
                    )
            return connection, log_cursor, response, observed

        for stage in ("connection", "cursor", "execute", "commit", "cleanup"):
            with self.subTest(stage=stage):
                connection, log_cursor, response, observed = run_stage(stage)
                combined = observed + response_observable(response) + str(log_cursor.queries)
                self.assertNotIn(SECRET_EXCEPTION_MARKER, combined)
                self.assertIn(admin_app.ORDER_FULFILLMENT_UPDATE_FAILED, str(log_cursor.queries))
                if connection is not None:
                    self.assertEqual(connection.rollback_count, 1)
                    self.assertTrue(connection.closed)
                    if not connection.fail_cursor:
                        self.assertTrue(connection.cursor_instance.closed)

        main_cursor = RecordingCursor(
            fetchone_values=[("shipped", True, False, 123)]
        )
        main_connection = RecordingConnection(main_cursor)
        with patch.object(
            admin_app.psycopg2, "connect", return_value=main_connection
        ):
            with patch.object(
                admin_app,
                "send_order_status_notification_and_record",
                side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
            ):
                with patch.object(
                    admin_app, "record_notification_event", return_value=True
                ) as record_event:
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.update_order_fulfillment_status(
                                "order-1", "delivered"
                            )
                        )
                    )
        self.assertTrue(main_cursor.closed)
        self.assertTrue(main_connection.closed)
        self.assertEqual(main_connection.commit_count, 1)
        self.assertIn(admin_app.ORDER_NOTIFICATION_FAILED, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed + response_observable(response))
        self.assertEqual(record_event.call_args.args[1], "notification_failed")
        self.assertNotIn("notification_sent", str(record_event.call_args))

        primary = FailureConnection(FailureCursor(fail_execute_at=1))
        with patch.object(admin_app.psycopg2, "connect", return_value=primary):
            with patch.object(
                admin_app,
                "get_db_connection",
                side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
            ):
                response, observed = capture_observables(
                    lambda: asyncio.run(
                        admin_app.update_order_fulfillment_status(
                            "order-1", "delivered"
                        )
                    )
                )
        self.assertEqual(primary.rollback_count, 1)
        self.assertTrue(primary.closed)
        self.assertIn(admin_app.ADMIN_ERROR_LOG_WRITE_FAILED, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed + response_observable(response))

    def test_weighing_route_cleans_up_database_and_send_failures(self):
        cases = (
            ("connection", None),
            ("cursor", FailureConnection(RecordingCursor(), fail_cursor=True)),
            ("execute", FailureConnection(FailureCursor(fail_execute_at=1))),
            (
                "cleanup",
                FailureConnection(
                    FailureCursor(fail_execute_at=1, fail_close=True),
                    fail_rollback=True,
                    fail_close=True,
                ),
            ),
            (
                "commit",
                FailureConnection(
                    RecordingCursor(
                        fetchone_values=[(35.0, "per_kg", 19, None), (1,)]
                    ),
                    fail_commit_at=1,
                ),
            ),
        )
        for stage, connection in cases:
            with self.subTest(stage=stage):
                connect_effect = (
                    RuntimeError(SECRET_EXCEPTION_MARKER)
                    if connection is None
                    else connection
                )
                with patch.object(
                    admin_app.psycopg2,
                    "connect",
                    side_effect=[connect_effect],
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.weigh_order_item(
                                "order-1", 1, 1000, None
                            )
                        )
                    )
                self.assertIn(admin_app.WEIGHING_UPDATE_FAILED, observed)
                self.assertNotIn(
                    SECRET_EXCEPTION_MARKER,
                    observed + response_observable(response),
                )
                if connection is not None:
                    self.assertEqual(connection.rollback_count, 1)
                    self.assertTrue(connection.closed)
                    if not connection.fail_cursor:
                        self.assertTrue(connection.cursor_instance.closed)

        main_cursor = RecordingCursor(
            fetchone_values=[
                (35.0, "per_kg", 19, None),
                None,
                (123, 35.0),
            ],
            fetchall_values=[[('Product', 1000, 35.0, None)]],
        )
        main_connection = RecordingConnection(main_cursor)
        with patch.object(
            admin_app.psycopg2, "connect", return_value=main_connection
        ):
            with patch.object(
                admin_app,
                "send_weighing_notification_and_record",
                side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
            ):
                with patch.object(
                    admin_app, "record_notification_event", return_value=True
                ) as record_event:
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.weigh_order_item(
                                "order-1", 1, 1000, None
                            )
                        )
                    )
        self.assertEqual(main_connection.commit_count, 1)
        self.assertTrue(main_cursor.closed)
        self.assertTrue(main_connection.closed)
        self.assertIn(admin_app.WEIGHING_NOTIFICATION_FAILED, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed + response_observable(response))
        self.assertEqual(
            record_event.call_args.args[1], "weighing_notification_failed"
        )
        self.assertNotIn("weighing_notification_sent", str(record_event.call_args))

    def test_broadcast_route_cleans_up_database_send_and_logging_failures(self):
        def run_stage(stage):
            log_cursor = RecordingCursor()
            log_connection = RecordingConnection(log_cursor)
            if stage == "connection":
                connection = None
                first_result = RuntimeError(SECRET_EXCEPTION_MARKER)
            elif stage == "cursor":
                connection = FailureConnection(
                    RecordingCursor(), fail_cursor=True
                )
                first_result = connection
            elif stage == "execute":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1)
                )
                first_result = connection
            elif stage == "cleanup":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1, fail_close=True),
                    fail_rollback=True,
                    fail_close=True,
                )
                first_result = connection
            else:
                connection = FailureConnection(
                    RecordingCursor(
                        fetchone_values=[(1, "message", "draft")],
                        fetchall_values=[[]],
                    ),
                    fail_commit_at=1,
                )
                first_result = connection
            with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
                with patch.object(
                    admin_app,
                    "get_db_connection",
                    side_effect=(first_result, log_connection),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(admin_app.send_broadcast_route(1))
                    )
            return connection, log_cursor, response, observed

        for stage in ("connection", "cursor", "execute", "commit", "cleanup"):
            with self.subTest(stage=stage):
                connection, log_cursor, response, observed = run_stage(stage)
                combined = observed + response_observable(response) + str(log_cursor.queries)
                self.assertNotIn(SECRET_EXCEPTION_MARKER, combined)
                self.assertIn("broadcast_send_failed", str(log_cursor.queries))
                if connection is not None:
                    self.assertEqual(connection.rollback_count, 1)
                    self.assertTrue(connection.closed)
                    if not connection.fail_cursor:
                        self.assertTrue(connection.cursor_instance.closed)

        main_cursor = RecordingCursor(
            fetchone_values=[(1, "message", "draft")],
            fetchall_values=[[(10, 123)]],
        )
        main_connection = RecordingConnection(main_cursor)
        log_connection = RecordingConnection(RecordingCursor())
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app,
                "get_db_connection",
                side_effect=(main_connection, log_connection),
            ):
                with patch.object(
                    admin_app,
                    "send_broadcast_message",
                    side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(admin_app.send_broadcast_route(1))
                    )
        self.assertEqual(main_connection.commit_count, 1)
        self.assertEqual(main_connection.rollback_count, 1)
        self.assertTrue(main_cursor.closed)
        self.assertTrue(main_connection.closed)
        self.assertNotIn("status = 'sent'", str(main_cursor.queries))
        self.assertNotIn(
            SECRET_EXCEPTION_MARKER,
            observed + response_observable(response) + str(main_cursor.queries),
        )

        primary = FailureConnection(FailureCursor(fail_execute_at=1))
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app,
                "get_db_connection",
                side_effect=(primary, RuntimeError(SECRET_EXCEPTION_MARKER)),
            ):
                response, observed = capture_observables(
                    lambda: asyncio.run(admin_app.send_broadcast_route(1))
                )
        self.assertEqual(primary.rollback_count, 1)
        self.assertTrue(primary.closed)
        self.assertIn(admin_app.ADMIN_ERROR_LOG_WRITE_FAILED, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed + response_observable(response))

    def test_channel_route_cleans_up_database_send_and_logging_failures(self):
        def run_stage(stage):
            log_cursor = RecordingCursor()
            log_connection = RecordingConnection(log_cursor)
            if stage == "connection":
                connection = None
                first_result = RuntimeError(SECRET_EXCEPTION_MARKER)
            elif stage == "cursor":
                connection = FailureConnection(
                    RecordingCursor(), fail_cursor=True
                )
                first_result = connection
            elif stage == "execute":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1)
                )
                first_result = connection
            elif stage == "cleanup":
                connection = FailureConnection(
                    FailureCursor(fail_execute_at=1, fail_close=True),
                    fail_rollback=True,
                    fail_close=True,
                )
                first_result = connection
            else:
                connection = FailureConnection(
                    RecordingCursor(
                        fetchone_values=[(1, "message", "draft")]
                    ),
                    fail_commit_at=1,
                )
                first_result = connection
            with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
                with patch.object(
                    admin_app,
                    "get_db_connection",
                    side_effect=(first_result, log_connection),
                ):
                    with patch.object(
                        admin_app,
                        "send_channel_post",
                        return_value=(True, None),
                    ):
                        response, observed = capture_observables(
                            lambda: asyncio.run(
                                admin_app.send_channel_post_route(1)
                            )
                        )
            return connection, log_cursor, response, observed

        for stage in ("connection", "cursor", "execute", "commit", "cleanup"):
            with self.subTest(stage=stage):
                connection, log_cursor, response, observed = run_stage(stage)
                combined = observed + response_observable(response) + str(log_cursor.queries)
                self.assertNotIn(SECRET_EXCEPTION_MARKER, combined)
                self.assertIn(admin_app.CHANNEL_POST_FAILED, str(log_cursor.queries))
                if connection is not None:
                    expected_rollbacks = 2 if stage == "commit" else 1
                    self.assertEqual(
                        connection.rollback_count, expected_rollbacks
                    )
                    self.assertTrue(connection.closed)
                    if not connection.fail_cursor:
                        self.assertTrue(connection.cursor_instance.closed)

        main_cursor = RecordingCursor(
            fetchone_values=[(1, "message", "draft")]
        )
        main_connection = RecordingConnection(main_cursor)
        log_connection = RecordingConnection(RecordingCursor())
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app,
                "get_db_connection",
                side_effect=(main_connection, log_connection),
            ):
                with patch.object(
                    admin_app,
                    "send_channel_post",
                    side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(admin_app.send_channel_post_route(1))
                    )
        self.assertEqual(main_connection.commit_count, 0)
        self.assertEqual(main_connection.rollback_count, 2)
        self.assertTrue(main_cursor.closed)
        self.assertTrue(main_connection.closed)
        self.assertFalse(
            any("UPDATE channel_posts" in query for query, _params in main_cursor.queries)
        )
        self.assertNotIn(
            SECRET_EXCEPTION_MARKER,
            observed + response_observable(response) + str(main_cursor.queries),
        )

        primary = FailureConnection(FailureCursor(fail_execute_at=1))
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app,
                "get_db_connection",
                side_effect=(primary, RuntimeError(SECRET_EXCEPTION_MARKER)),
            ):
                response, observed = capture_observables(
                    lambda: asyncio.run(admin_app.send_channel_post_route(1))
                )
        self.assertEqual(primary.rollback_count, 1)
        self.assertTrue(primary.closed)
        self.assertIn(admin_app.ADMIN_ERROR_LOG_WRITE_FAILED, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed + response_observable(response))

    def test_order_notification_exception_marker_is_redacted(self):
        # Migrated from the retired update_order_status/'preparing' path
        # (Checkpoint E): the fulfillment 'picking' action always attempts a
        # customer notification (fixed vocabulary), so its failure is still
        # exercised here.
        main_cursor = RecordingCursor(
            fetchone_values=[("confirmed", False, False, 123456789)]
        )
        event_cursor = RecordingCursor()
        main_connection = RecordingConnection(main_cursor)
        event_connection = RecordingConnection(event_cursor)
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app.psycopg2,
                "connect",
                side_effect=(main_connection, event_connection),
            ):
                with patch.object(
                    admin_app,
                    "send_order_status_notification_and_record",
                    side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.update_order_fulfillment_status(
                                "order-test", "picking"
                            )
                        )
                    )

        stored = str(main_cursor.queries + event_cursor.queries)
        response_text = response_observable(response)
        self.assertIsInstance(response, str)
        self.assertEqual(main_connection.commit_count, 1)
        self.assertEqual(event_connection.commit_count, 1)
        self.assertIn(admin_app.ORDER_NOTIFICATION_FAILED, observed)
        self.assertIn("notification_failed", stored)
        self.assertNotIn("notification_sent", stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, response_text)

    def test_weighing_notification_exception_marker_is_redacted(self):
        main_cursor = RecordingCursor(
            fetchone_values=[
                (35.0, "per_kg", 19, None),
                None,
                (123456789, 35.0),
            ],
            fetchall_values=[[("Product", 1000, 35.0, None)]],
        )
        event_cursor = RecordingCursor()
        main_connection = RecordingConnection(main_cursor)
        event_connection = RecordingConnection(event_cursor)
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                admin_app.psycopg2,
                "connect",
                side_effect=(main_connection, event_connection),
            ):
                with patch.object(
                    admin_app,
                    "send_weighing_notification_and_record",
                    side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
                ):
                    response, observed = capture_observables(
                        lambda: asyncio.run(
                            admin_app.weigh_order_item(
                                "order-test", 1, 1000, None
                            )
                        )
                    )

        stored = str(main_cursor.queries + event_cursor.queries)
        response_text = (
            response.body.decode("utf-8", errors="replace")
            + str(dict(response.headers))
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(main_connection.commit_count, 1)
        self.assertEqual(event_connection.commit_count, 1)
        self.assertIn(admin_app.WEIGHING_NOTIFICATION_FAILED, observed)
        self.assertIn("weighing_notification_failed", stored)
        self.assertNotIn("weighing_notification_sent", stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, response_text)

    def test_order_and_weighing_update_errors_do_not_expose_marker(self):
        log_cursor = RecordingCursor()
        log_connection = RecordingConnection(log_cursor)
        with patch.object(
            admin_app.psycopg2,
            "connect",
            side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
        ):
            with patch.object(
                admin_app, "get_db_connection", return_value=log_connection
            ):
                order_response, order_observed = capture_observables(
                    lambda: asyncio.run(
                        admin_app.update_order_payment_status("order-test", "paid")
                    )
                )
                weighing_response, weighing_observed = capture_observables(
                    lambda: asyncio.run(
                        admin_app.weigh_order_item(
                            "order-test", 1, 1000, None
                        )
                    )
                )

        stored = str(log_cursor.queries)
        self.assertIn(admin_app.ORDER_PAYMENT_UPDATE_FAILED, stored)
        self.assertIn(admin_app.WEIGHING_UPDATE_FAILED, weighing_observed)
        for observable in (
            order_observed,
            weighing_observed,
            stored,
            response_observable(order_response),
            response_observable(weighing_response),
        ):
            self.assertNotIn(SECRET_EXCEPTION_MARKER, observable)

    def test_bot_reminder_exception_marker_is_redacted(self):
        initial_cursor = RecordingCursor(fetchall_values=[[(123456789,)]])
        update_cursor = RecordingCursor()
        initial_connection = RecordingConnection(initial_cursor)
        update_connection = RecordingConnection(update_cursor)
        with patch.dict(os.environ, {"ENABLE_TELEGRAM_ACTIONS": "true"}):
            with patch.object(
                bot.psycopg2,
                "connect",
                side_effect=(initial_connection, update_connection),
            ):
                with patch.object(
                    bot.bot,
                    "send_message",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError(SECRET_EXCEPTION_MARKER),
                ):
                    with patch.object(bot, "log_customer_event") as log_event:
                        result, observed = capture_observables(
                            lambda: asyncio.run(
                                bot.send_pending_order_reminders()
                            )
                        )

        stored = str(initial_cursor.queries + update_cursor.queries)
        self.assertIsNone(result)
        self.assertEqual(update_connection.commit_count, 1)
        log_event.assert_called_once_with(
            123456789,
            "pending_order_reminder_failed",
            {"reason": "failed"},
        )
        self.assertIn("pending_order_reminder_failed", observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, observed)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, stored)
        self.assertNotIn(SECRET_EXCEPTION_MARKER, str(log_event.call_args))

    def test_reviewed_telegram_paths_do_not_render_raw_exceptions(self):
        reviewed_functions = (
            admin_app.send_low_stock_alert,
            admin_app.sync_low_stock_alert_state,
            admin_app.send_order_status_notification,
            admin_app.send_weighing_complete_notification,
            admin_app.record_notification_event,
            admin_app.send_order_status_notification_and_record,
            admin_app.send_weighing_notification_and_record,
            admin_app.send_channel_post,
            admin_app.send_broadcast_message,
            admin_app.broadcasts,
            admin_app.new_broadcast_form,
            admin_app.create_broadcast,
            admin_app.send_broadcast_route,
            admin_app.channel_posts,
            admin_app.new_channel_post_form,
            admin_app.create_channel_post,
            admin_app.send_channel_post_route,
            admin_app.delete_channel_post,
            admin_app.update_order_payment_status,
            admin_app.update_order_fulfillment_status,
            admin_app.weigh_order_item,
            admin_app.log_admin_stable_error,
            bot.send_admin_message,
            bot.is_telegram_blocked_error,
            bot.log_customer_event,
            bot.send_abandoned_cart_reminders,
            bot.clear_expired_abandoned_carts,
            bot.abandoned_cart_worker,
            bot.send_pending_order_reminders,
            bot.cancel_expired_pending_orders,
            bot.send_awaiting_payment_reminders,
            bot.cancel_expired_awaiting_payment_orders,
            bot.unpaid_order_worker,
        )
        forbidden_fragments = (
            "str(error)",
            "str(e)",
            "str(exc)",
            "repr(",
            "traceback.",
            "format_exc(",
            "logger.exception",
            "type(e).__name__",
            "type(error).__name__",
        )
        for function in reviewed_functions:
            source = inspect.getsource(function)
            with self.subTest(function=function.__name__):
                for fragment in forbidden_fragments:
                    self.assertNotIn(fragment, source)

            class LowStockCursor:
                def __init__(self):
                    self.queries = []

                def execute(self, query, params=None):
                    self.queries.append(" ".join(query.split()))

                def fetchone(self):
                    return ("Product", 1, 2)

            cursor = LowStockCursor()
            with patch.object(
                admin_app, "send_low_stock_alert", return_value=False
            ):
                admin_app.sync_low_stock_alert_state(cursor, [1])
            self.assertFalse(
                any("SET low_stock_alert_sent = TRUE" in query for query in cursor.queries)
            )

            with patch.object(
                admin_app, "send_weighing_complete_notification", return_value=False
            ):
                with patch.object(admin_app.psycopg2, "connect") as connect:
                    result = admin_app.send_weighing_notification_and_record(
                        123, "order-1", 10, []
                    )
            self.assertFalse(result)
            connect.assert_not_called()

    def test_bot_handlers_workers_and_helpers_are_guarded_at_their_boundaries(self):
        guarded_names = (
            "send_abandoned_cart_reminders",
            "clear_expired_abandoned_carts",
            "abandoned_cart_worker",
            "send_pending_order_reminders",
            "cancel_expired_pending_orders",
            "send_awaiting_payment_reminders",
            "cancel_expired_awaiting_payment_orders",
            "unpaid_order_worker",
            "render_category_products",
            "show_category",
            "render_promotions",
            "show_promotions",
            "render_product",
            "render_product_suggestions",
            "show_product",
            "choose_option",
            "choose_weight",
            "add_option_to_cart",
            "add_to_cart",
            "create_order",
            "support",
            "show_cart",
            "cart_plus_option",
            "cart_plus_weight",
            "remove_item",
            "clear_cart",
            "start",
            "show_orders",
            "show_clients",
            "handle_free_text_fallback",
            "handle_order_data",
            "checkout",
            "resume_payment",
            "use_saved_data",
            "enter_new_data",
            "pay_iban",
            "pay_paypal",
            "pay_cash",
            "back_to_menu",
            "payment_done_for_order",
            "payment_done",
        )
        for name in guarded_names:
            with self.subTest(name=name):
                self.assertTrue(
                    getattr(getattr(bot, name), "telegram_actions_guarded", False)
                )

    def test_direct_bot_invocation_does_not_call_api_or_database_when_disabled(self):
        environment = {
            "ENABLE_TELEGRAM_ACTIONS": "",
            "ADMIN_ID": "123456789",
        }
        direct_functions = (
            bot.send_abandoned_cart_reminders,
            bot.clear_expired_abandoned_carts,
            bot.abandoned_cart_worker,
            bot.send_pending_order_reminders,
            bot.cancel_expired_pending_orders,
            bot.send_awaiting_payment_reminders,
            bot.cancel_expired_awaiting_payment_orders,
            bot.unpaid_order_worker,
        )
        with patch.dict(os.environ, environment):
            with patch.object(bot.psycopg2, "connect") as connect:
                with patch.object(bot, "get_db_connection") as get_connection:
                    with patch.object(
                        bot.bot, "send_message", new_callable=AsyncMock
                    ) as send_message:
                        for function in direct_functions:
                            asyncio.run(function())
                        asyncio.run(bot.start(object()))
                        self.assertFalse(
                            asyncio.run(bot.send_admin_message("message"))
                        )
                        self.assertFalse(bot.log_customer_event(1, "event"))
                        self.assertFalse(bot.mark_cart_active(1))

        connect.assert_not_called()
        get_connection.assert_not_called()
        send_message.assert_not_awaited()

    def test_tracked_admin_id_is_never_used_as_recipient(self):
        source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('config["admin_id"]', source)
        with patch.dict(
            os.environ,
            {"ENABLE_TELEGRAM_ACTIONS": "true", "ADMIN_ID": ""},
        ):
            with patch.object(bot, "load_json") as load_json:
                with patch.object(
                    bot.bot, "send_message", new_callable=AsyncMock
                ) as send_message:
                    self.assertFalse(asyncio.run(bot.send_admin_message("message")))
        load_json.assert_not_called()
        send_message.assert_not_awaited()

        for value in ("", "0", "+123", "123.0", "admin", "1" * 20):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"ADMIN_ID": value}):
                    self.assertIsNone(bot.get_admin_recipient_id())
                    self.assertIsNone(admin_app.get_admin_chat_id())
        for value in ("123456789", "-1001234567890"):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"ADMIN_ID": value}):
                    self.assertEqual(bot.get_admin_recipient_id(), int(value))
                    self.assertEqual(admin_app.get_admin_chat_id(), int(value))

    def test_startup_write_flags_are_ignored_by_bot_database_preparation(self):
        environment = {
            "APP_ENV": "test",
            "RUN_DB_INIT": "true",
            "RUN_DB_SEED": "true",
        }
        with patch.dict(os.environ, environment):
            with patch.object(db_schema, "init_db") as init_db:
                with patch.object(bot, "seed_products_from_json") as seed:
                    with patch.object(
                        bot, "catalog_schema_is_compatible", return_value=True
                    ) as probe:
                        bot.prepare_bot_database()

        probe.assert_called_once_with()
        init_db.assert_not_called()
        seed.assert_not_called()

    def test_bot_incompatible_schema_fails_without_write_fallback(self):
        environment = {
            "APP_ENV": "test",
            "RUN_DB_INIT": "true",
            "RUN_DB_SEED": "true",
        }
        with patch.dict(os.environ, environment):
            with patch.object(db_schema, "init_db") as init_db:
                with patch.object(bot, "seed_products_from_json") as seed:
                    with patch.object(
                        bot, "catalog_schema_is_compatible", return_value=False
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError, "Database schema is unavailable"
                        ):
                            bot.prepare_bot_database()

        init_db.assert_not_called()
        seed.assert_not_called()

    def test_ordinary_bot_startup_fails_closed_without_init_or_seed(self):
        environment = {
            "APP_ENV": "test",
            "ENABLE_TELEGRAM_ACTIONS": "",
            "RUN_DB_INIT": "true",
            "RUN_DB_SEED": "true",
        }
        with patch.dict(os.environ, environment):
            with patch.object(db_schema, "init_db") as init_db:
                with patch.object(bot, "seed_products_from_json") as seed:
                    with self.assertRaisesRegex(
                        RuntimeError, "Telegram actions are disabled"
                    ):
                        asyncio.run(bot.main())

        init_db.assert_not_called()
        seed.assert_not_called()

    def test_login_redirects_to_admin_and_keeps_secure_cookie(self):
        environment = {
            "ADMIN_PASSWORD": "dummy-admin-password",
            "ADMIN_SESSION_SECRET": "dummy-admin-session-secret",
        }
        with patch.dict(os.environ, environment):
            request = make_request("/login", method="POST")
            request.state.preauth_nonce = "test-nonce"
            request.state.preauth_issued_at = 1
            request.state.preauth_expires_at = 2
            credential = admin_app.sign_admin_session()
            with patch.object(
                admin_app,
                "create_authenticated_session",
                return_value=("created", credential),
            ):
                response = asyncio.run(
                    admin_app.login(request, "dummy-admin-password")
                )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/admin")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)


if __name__ == "__main__":
    unittest.main()
