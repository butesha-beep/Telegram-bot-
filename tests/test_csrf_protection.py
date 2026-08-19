import asyncio
import ast
import contextlib
import io
import inspect
import logging
import os
import re
import time
import unittest
import urllib.parse
import threading
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException, Request


os.environ.setdefault("DATABASE_URL", "postgresql://unit-test.invalid/csrf")
os.environ.setdefault("ADMIN_PASSWORD", "unit-test-admin-password")
os.environ.setdefault("ADMIN_SESSION_SECRET", "unit-test-admin-session-secret")

import admin_app
import csrf_security
import db_schema
import storefront


VALID_CSRF_SECRET = "R7!mQ2#vL9@kT4$zN8&cW5*pH3^sD6%yF1xJ"
VALID_GATE_PASSWORD = "N4!rV8#qM2@tK7$zC5&wH9*pL3^sD6%y"
SECRET_MARKER = "B11B1_CSRF_SECRET_MARKER_8d52f1"
UNSAFE_ROUTE_DEPENDENCIES = {
    ("POST", "/login"): "require_admin_login_csrf",
    ("POST", "/logout"): "require_admin_csrf",
    ("POST", "/master/login"): "require_master_login_csrf",
    ("POST", "/master/logout"): "require_master_csrf",
    ("POST", "/master/actions/sync-default-shops"): "require_master_csrf",
    ("POST", "/master/actions/capture-current-snapshot"): "require_master_csrf",
    ("POST", "/broadcasts/new"): "require_admin_csrf",
    ("POST", "/broadcasts/{broadcast_id}/send"): "require_admin_csrf",
    ("POST", "/channel/new"): "require_admin_csrf",
    ("POST", "/channel/{post_id}/send"): "require_admin_csrf",
    ("POST", "/channel/{post_id}/delete"): "require_admin_csrf",
    ("POST", "/orders/new"): "require_admin_csrf",
    ("POST", "/orders/{order_id}/payment/{action}"): "require_admin_csrf",
    ("POST", "/orders/{order_id}/fulfillment/{action}"): "require_admin_csrf",
    ("POST", "/orders/{order_id}/note"): "require_admin_csrf",
    ("POST", "/orders/{order_id}/items/{item_id}/weigh"): "require_admin_csrf",
    ("POST", "/picking/{order_id}/start"): "require_admin_csrf",
    ("POST", "/picking/{order_id}/pack"): "require_admin_csrf",
    ("POST", "/picking/{order_id}/items/{item_id}/weigh"): "require_admin_csrf",
    ("POST", "/clients/{telegram_id}/note"): "require_admin_csrf",
    ("POST", "/products/new"): "require_admin_csrf",
    ("POST", "/products/{product_id}/recommendations"): "require_admin_csrf",
    ("POST", "/products/{product_id}/options/new"): "require_admin_csrf",
    ("POST", "/options/{option_id}/edit"): "require_admin_csrf",
    ("POST", "/options/{option_id}/toggle"): "require_admin_csrf",
    ("POST", "/products/{product_id}/edit"): "require_admin_csrf",
    ("POST", "/products/{product_id}/deactivate"): "require_admin_csrf",
    ("POST", "/products/{product_id}/activate"): "require_admin_csrf",
    ("POST", "/categories/new"): "require_admin_csrf",
    ("POST", "/categories/{category_id}/edit"): "require_admin_csrf",
}


@contextlib.contextmanager
def test_environment(mock_sessions=True, **overrides):
    environment = {
        "APP_ENV": "test",
        "CSRF_SECRET": VALID_CSRF_SECRET,
        "ADMIN_PASSWORD": "unit-test-admin-password",
        "ADMIN_SESSION_SECRET": "unit-test-admin-session-secret",
        "MASTER_ADMIN_PASSWORD": "unit-test-master-password",
        "MASTER_ADMIN_SESSION_SECRET": "unit-test-master-session-secret",
        "ENABLE_MASTER_ADMIN": "true",
        "ENABLE_TELEGRAM_ACTIONS": "true",
        "PREVIEW_GATE_ENABLED": "",
    }
    environment.update(overrides)
    with patch.dict(os.environ, environment, clear=False):
        if mock_sessions:
            with patch.object(
                admin_app, "server_session_is_active", return_value=True
            ):
                yield
        else:
            yield


def make_request(
    path,
    method="POST",
    form=None,
    cookies=None,
    headers=None,
    query_string="",
    json_body=None,
    raw_body=None,
    content_type=None,
):
    if raw_body is not None:
        body = raw_body
        content_type = content_type or "application/octet-stream"
    elif json_body is not None:
        body = json_body.encode("utf-8")
        content_type = "application/json"
    else:
        body = urllib.parse.urlencode(form or {}).encode("utf-8")
        content_type = content_type or "application/x-www-form-urlencoded"
    raw_headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if cookies:
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        raw_headers.append((b"cookie", cookie_value.encode("latin-1")))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("ascii"), value.encode("latin-1")))
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 443),
        },
        receive,
    )


def run_asgi_request(
    method,
    path,
    form=None,
    cookies=None,
    headers=None,
    query_string="",
    raw_body=None,
    content_type="application/x-www-form-urlencoded",
    observation=None,
):
    body = raw_body if raw_body is not None else urllib.parse.urlencode(form or {}).encode("utf-8")
    raw_headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if cookies:
        cookie_value = "; ".join(f"{name}={value}" for name, value in cookies.items())
        raw_headers.append((b"cookie", cookie_value.encode("latin-1")))
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("ascii"), value.encode("latin-1")))
    messages = []
    sent = False

    async def receive():
        nonlocal sent
        if observation is not None:
            observation["receive_calls"] = observation.get("receive_calls", 0) + 1
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query_string.encode("ascii"),
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
    }
    asyncio.run(admin_app.app(scope, receive, send))
    start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return start["status"], start["headers"], response_body


def response_cookies(headers):
    cookies = SimpleCookie()
    for name, value in headers:
        if name.lower() == b"set-cookie":
            cookies.load(value.decode("latin-1"))
    return cookies


def hidden_csrf_token(body):
    match = re.search(
        rb'<input[^>]+name="csrf_token"[^>]+value="([^"]*)"', body
    )
    if not match:
        raise AssertionError("CSRF hidden field is missing")
    return match.group(1).decode("ascii")


def assert_csrf_rejected(test_case, callback):
    with test_case.assertRaises(HTTPException) as raised:
        asyncio.run(callback())
    test_case.assertEqual(raised.exception.status_code, 403)
    test_case.assertEqual(raised.exception.detail, "Forbidden")


class SecurityCursor:
    def __init__(self, fetchone_values=(), fail_at=None):
        self.fetchone_values = list(fetchone_values)
        self.fail_at = fail_at
        self.queries = []
        self.closed = False

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))
        if self.fail_at == len(self.queries):
            raise RuntimeError(SECRET_MARKER)

    def fetchone(self):
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    def close(self):
        self.closed = True


class SecurityConnection:
    def __init__(self, cursor, fail_commit=False):
        self.cursor_instance = cursor
        self.fail_commit = fail_commit
        self.session_args = None
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def set_session(self, **kwargs):
        self.session_args = kwargs

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commit_count += 1
        if self.fail_commit:
            raise RuntimeError(SECRET_MARKER)

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


class ConcurrentSecurityStore:
    def __init__(self):
        self.condition = threading.Condition()
        self.lock = self.condition
        self.consumed = set()
        self.sessions = {}
        self.nonce_reservations = {}
        self.next_id = 1

    def connection(self, fail_stage=None):
        return ConcurrentSecurityConnection(self, fail_stage=fail_stage)


class ConcurrentSecurityConnection:
    def __init__(self, store, fail_stage=None):
        self.store = store
        self.fail_stages = (
            set(fail_stage)
            if isinstance(fail_stage, (set, tuple, list, frozenset))
            else ({fail_stage} if fail_stage else set())
        )
        self.cursor_instance = ConcurrentSecurityCursor(self)
        self.session_args = None
        self.closed = False
        self.rollback_count = 0
        self.commit_count = 0
        self.pending_nonce = None
        self.pending_session = None
        self.pending_revoke = None
        self.committed = False

    def set_session(self, **kwargs):
        self.session_args = kwargs

    def cursor(self):
        if self.fails("cursor"):
            raise RuntimeError(SECRET_MARKER)
        return self.cursor_instance

    def commit(self):
        self.commit_count += 1
        if self.fails("commit"):
            raise RuntimeError(SECRET_MARKER)
        with self.store.lock:
            if self.pending_nonce is not None:
                self.store.consumed.add(self.pending_nonce)
            if self.pending_session is not None:
                role, session_id, session_hash = self.pending_session
                self.store.sessions[role] = (session_id, session_hash)
            if self.pending_revoke is not None:
                role, token_hash = self.pending_revoke
                current = self.store.sessions.get(role)
                if current and current[1] == token_hash:
                    del self.store.sessions[role]
            self._release_nonce_reservation_locked()
        self.committed = True

    def rollback(self):
        self.rollback_count += 1
        self._discard_pending()
        if self.fails("rollback"):
            raise RuntimeError(SECRET_MARKER)

    def close(self):
        if not self.committed:
            self._discard_pending()
        self.closed = True
        if self.fails("connection_close"):
            raise RuntimeError(SECRET_MARKER)

    def fails(self, stage):
        return stage in self.fail_stages

    def _release_nonce_reservation_locked(self):
        if (
            self.pending_nonce is not None
            and self.store.nonce_reservations.get(self.pending_nonce) is self
        ):
            del self.store.nonce_reservations[self.pending_nonce]
            self.store.condition.notify_all()

    def _discard_pending(self):
        with self.store.lock:
            self._release_nonce_reservation_locked()
        self.pending_nonce = None
        self.pending_session = None
        self.pending_revoke = None


class ConcurrentSecurityCursor:
    def __init__(self, connection):
        self.connection = connection
        self.store = connection.store
        self.result = None
        self.closed = False
        self.execute_count = 0

    def execute(self, query, params=None):
        self.execute_count += 1
        normalized = " ".join(query.split())
        if self.connection.fails(f"execute_{self.execute_count}"):
            raise RuntimeError(SECRET_MARKER)
        if (
            self.connection.fails("session_cleanup")
            and "DELETE FROM web_sessions" in normalized
        ):
            raise RuntimeError(SECRET_MARKER)
        if (
            self.connection.fails("nonce_cleanup")
            and "DELETE FROM consumed_login_nonces" in normalized
        ):
            raise RuntimeError(SECRET_MARKER)
        if "INSERT INTO consumed_login_nonces" in normalized:
            role, nonce_hash = params[0], bytes(params[1])
            session_hash = bytes(params[9])
            with self.store.lock:
                key = (role, nonce_hash)
                reservation = self.store.nonce_reservations.get(key)
                while reservation is not None and reservation is not self.connection:
                    self.store.condition.wait(timeout=1)
                    reservation = self.store.nonce_reservations.get(key)
                if key in self.store.consumed:
                    self.result = None
                    return
                self.store.nonce_reservations[key] = self.connection
                self.connection.pending_nonce = key
                if self.connection.fails("nonce_consumption"):
                    raise RuntimeError(SECRET_MARKER)
                if self.connection.fails("previous_session_revocation"):
                    raise RuntimeError(SECRET_MARKER)
                session_id = self.store.next_id
                self.store.next_id += 1
                self.connection.pending_session = (role, session_id, session_hash)
                if self.connection.fails("session_insertion"):
                    raise RuntimeError(SECRET_MARKER)
                self.result = (session_id,)
        elif normalized.startswith("SELECT id FROM web_sessions"):
            if self.connection.fails("lookup"):
                raise RuntimeError(SECRET_MARKER)
            role, _account_key, token_hash = params
            with self.store.lock:
                current = self.store.sessions.get(role)
                self.result = current if current and current[1] == bytes(token_hash) else None
        elif normalized.startswith("UPDATE web_sessions"):
            if self.connection.fails("logout_revocation"):
                raise RuntimeError(SECRET_MARKER)
            role, _account_key, token_hash = params
            with self.store.lock:
                current = self.store.sessions.get(role)
                if current and current[1] == bytes(token_hash):
                    self.result = (current[0],)
                    self.connection.pending_revoke = (role, bytes(token_hash))
                else:
                    self.result = None

    def fetchone(self):
        return self.result

    def close(self):
        self.closed = True
        if self.connection.fails("cursor_close"):
            raise RuntimeError(SECRET_MARKER)


class CsrfProtectionTests(unittest.TestCase):
    def setUp(self):
        self.original_database_ready = admin_app.DATABASE_READY
        admin_app.DATABASE_READY = True

    def tearDown(self):
        admin_app.DATABASE_READY = self.original_database_ready

    def test_registered_route_and_csrf_inventory_is_complete(self):
        self.assertEqual(len(admin_app.app.routes), 66)
        actual = {}
        for route in admin_app.app.routes:
            unsafe_methods = set(getattr(route, "methods", set())) & admin_app.UNSAFE_HTTP_METHODS
            for method in unsafe_methods:
                dependencies = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }
                actual[(method, route.path)] = dependencies

        self.assertEqual(set(actual), set(UNSAFE_ROUTE_DEPENDENCIES))
        for route_key, expected_dependency in UNSAFE_ROUTE_DEPENDENCIES.items():
            self.assertIn(expected_dependency, actual[route_key])

    def test_get_and_head_handlers_have_no_mutating_operations(self):
        source = Path(admin_app.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        forbidden_calls = {
            "commit",
            "set_cookie",
            "delete_cookie",
            "seed_default_master_shop",
            "create_current_master_snapshot",
            "urlopen",
            "log_admin_error",
            "log_admin_stable_error",
        }
        findings = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            safe_route = False
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                safe_route = safe_route or decorator.func.attr in {"get", "head"}
            if not safe_route:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                call_name = ""
                if isinstance(child.func, ast.Attribute):
                    call_name = child.func.attr
                elif isinstance(child.func, ast.Name):
                    call_name = child.func.id
                if call_name in forbidden_calls:
                    findings.append((node.name, child.lineno, call_name))
                if call_name == "execute" and child.args and isinstance(child.args[0], ast.Constant):
                    sql = str(child.args[0].value).strip().upper()
                    if re.match(r"(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", sql):
                        findings.append((node.name, child.lineno, "mutating_sql"))
        self.assertEqual(findings, [])

    def test_logout_and_master_actions_reject_get_and_head_without_side_effects(self):
        with test_environment():
            admin_session = admin_app.sign_admin_session()
            master_session = admin_app.sign_master_session()
            with patch.object(admin_app.psycopg2, "connect") as connect:
                with patch.object(admin_app, "get_db_connection") as get_connection:
                    with patch.object(
                        admin_app, "revoke_authenticated_session"
                    ) as revoke:
                        for method, path, cookies in (
                            ("GET", "/logout", {}),
                            ("HEAD", "/logout", {}),
                            ("GET", "/logout", {admin_app.ADMIN_SESSION_COOKIE: admin_session}),
                            ("HEAD", "/logout", {admin_app.ADMIN_SESSION_COOKIE: admin_session}),
                            ("GET", "/master/logout", {}),
                            ("HEAD", "/master/logout", {}),
                            ("GET", "/master/logout", {admin_app.MASTER_SESSION_COOKIE: master_session}),
                            ("HEAD", "/master/logout", {admin_app.MASTER_SESSION_COOKIE: master_session}),
                            ("GET", "/master/actions/sync-default-shops", {admin_app.MASTER_SESSION_COOKIE: master_session}),
                            ("HEAD", "/master/actions/capture-current-snapshot", {admin_app.MASTER_SESSION_COOKIE: master_session}),
                        ):
                            status, headers, _body = run_asgi_request(method, path, cookies=cookies)
                            self.assertEqual(status, 405)
                            self.assertFalse(any(name.lower() == b"set-cookie" for name, _ in headers))
            connect.assert_not_called()
            get_connection.assert_not_called()
            revoke.assert_not_called()

    def test_csrf_secret_is_fail_closed_and_separate(self):
        boundary_secret = VALID_CSRF_SECRET[:32]
        self.assertFalse(csrf_security.csrf_secret_is_strong(boundary_secret[:-1]))
        self.assertTrue(csrf_security.csrf_secret_is_strong(boundary_secret))
        for app_env in ("preview", "production"):
            environment = {
                "APP_ENV": app_env,
                "CSRF_SECRET": "",
                "PREVIEW_GATE_ENABLED": "true" if app_env == "preview" else "",
                "PREVIEW_GATE_USERNAME": "reviewer",
                "PREVIEW_GATE_PASSWORD": VALID_GATE_PASSWORD,
            }
            with self.subTest(app_env=app_env), patch.dict(os.environ, environment, clear=False):
                with self.assertRaisesRegex(RuntimeError, "CSRF protection"):
                    csrf_security.validate_csrf_configuration()

        with test_environment(CSRF_SECRET="too-short"):
            with self.assertRaisesRegex(RuntimeError, "CSRF protection"):
                csrf_security.validate_csrf_configuration()
        with test_environment(
            CSRF_SECRET=VALID_CSRF_SECRET,
            ADMIN_SESSION_SECRET=VALID_CSRF_SECRET,
        ):
            with self.assertRaisesRegex(RuntimeError, "CSRF protection"):
                csrf_security.validate_csrf_configuration()

    def test_startup_rejects_missing_csrf_before_database_probe(self):
        for app_env, extra in (
            ("production", {}),
            (
                "preview",
                {
                    "PREVIEW_GATE_ENABLED": "true",
                    "PREVIEW_GATE_USERNAME": "reviewer",
                    "PREVIEW_GATE_PASSWORD": VALID_GATE_PASSWORD,
                },
            ),
        ):
            environment = {"APP_ENV": app_env, "CSRF_SECRET": "", **extra}
            with self.subTest(app_env=app_env), patch.dict(os.environ, environment, clear=False):
                with patch.object(admin_app, "refresh_database_readiness") as readiness:
                    with self.assertRaisesRegex(RuntimeError, "CSRF protection"):
                        asyncio.run(admin_app.startup_db_init())
                readiness.assert_not_called()

    def test_local_and_test_use_only_process_ephemeral_fallback(self):
        for app_env in ("local", "test"):
            with self.subTest(app_env=app_env), patch.dict(
                os.environ, {"APP_ENV": app_env, "CSRF_SECRET": ""}, clear=False
            ):
                secret = csrf_security.get_csrf_secret()
                self.assertTrue(csrf_security.csrf_secret_is_strong(secret))
                self.assertIs(secret, csrf_security._LOCAL_TEST_CSRF_SECRET)

    def test_token_rejects_malformed_wrong_expired_cross_role_and_cross_session(self):
        with test_environment():
            first_session = admin_app.sign_admin_session()
            second_session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin", first_session, 600, now=1000
            )
            self.assertTrue(
                csrf_security.validate_csrf_token(
                    token, "authenticated:admin", first_session, 600, now=1001
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    token, "authenticated:admin", second_session, 600, now=1001
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    token, "authenticated:master", first_session, 600, now=1001
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    token, "authenticated:admin", first_session, 600, now=1700
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    "malformed", "authenticated:admin", first_session, 600
                )
            )

    def test_token_validation_uses_constant_time_signature_and_binding_checks(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin", session, 600
            )
            original_compare = csrf_security.hmac.compare_digest
            with patch.object(
                csrf_security, "get_csrf_secret", return_value=VALID_CSRF_SECRET
            ):
                with patch.object(
                    csrf_security.hmac,
                    "compare_digest",
                    wraps=original_compare,
                ) as compare:
                    self.assertFalse(
                        csrf_security.validate_csrf_token(
                            token,
                            "authenticated:admin",
                            admin_app.sign_admin_session(),
                            600,
                        )
                    )
            self.assertEqual(compare.call_count, 2)

    def test_token_time_boundaries_and_canonical_components_are_strict(self):
        with test_environment():
            purpose = "authenticated:admin"
            binding = "session-binding"
            token = csrf_security.issue_csrf_token(
                purpose, binding, 600, now=1000
            )
            self.assertTrue(
                csrf_security.validate_csrf_token(
                    token, purpose, binding, 600, now=1599
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    token, purpose, binding, 600, now=1600
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    token, purpose, binding, 600, now=1601
                )
            )
            future = csrf_security.issue_csrf_token(
                purpose, binding, 600, now=1001
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    future, purpose, binding, 600, now=1000
                )
            )

            secret = csrf_security.get_csrf_secret().encode("utf-8")

            def signed(parts):
                payload = ".".join(parts[:5])
                signature = csrf_security._urlsafe_encode(
                    csrf_security._token_signature(
                        secret, purpose, payload.encode("ascii")
                    )
                )
                return payload + "." + signature

            base = token.split(".")
            malformed_parts = []
            for timestamp in (
                "01000",
                "+1000",
                "-1000",
                " 1000",
                "99999999999",
            ):
                for timestamp_index in (1, 2):
                    parts = base.copy()
                    parts[timestamp_index] = timestamp
                    malformed_parts.append(parts)
            for nonce in (base[3][:-1], base[3] + "A", base[3][:-1] + "+"):
                parts = base.copy()
                parts[3] = nonce
                malformed_parts.append(parts)
            for token_binding in (
                base[4][:-1],
                base[4] + "A",
                base[4][:-1] + "+",
                base[4] + "=",
                base[4].swapcase(),
            ):
                parts = base.copy()
                parts[4] = token_binding
                malformed_parts.append(parts)
            for parts in malformed_parts:
                with self.subTest(parts=parts):
                    self.assertFalse(
                        csrf_security.validate_csrf_token(
                            signed(parts), purpose, binding, 600, now=1001
                        )
                    )

            for signature in (
                base[5][:-1],
                base[5] + "A",
                base[5][:-1] + "+",
                base[5] + "=",
                base[5].swapcase(),
            ):
                candidate = ".".join(base[:5] + [signature])
                with self.subTest(signature=signature):
                    self.assertFalse(
                        csrf_security.validate_csrf_token(
                            candidate, purpose, binding, 600, now=1001
                        )
                    )

    def test_strict_token_adversarial_matrix_uses_valid_signatures(self):
        with test_environment():
            purpose = "authenticated:admin"
            binding = "review-session-binding"
            secret = csrf_security.get_csrf_secret().encode("utf-8")
            base = csrf_security.issue_csrf_token(
                purpose, binding, 600, now=1000
            ).split(".")

            def signed(parts, encoding="ascii"):
                payload = ".".join(parts[:5])
                signature = csrf_security._urlsafe_encode(
                    csrf_security._token_signature(
                        secret, purpose, payload.encode(encoding)
                    )
                )
                return payload + "." + signature

            signed_invalid_cases = []
            for issued_at, expires_at in (
                ("1000", "999"),
                ("1000", "1000"),
                ("1001", "1601"),
                ("1000", "1601"),
                ("01000", "1600"),
                ("+1000", "1600"),
                ("-1000", "1600"),
                (" 1000", "1600"),
                ("1000 ", "1600"),
            ):
                parts = base.copy()
                parts[1], parts[2] = issued_at, expires_at
                signed_invalid_cases.append(signed(parts))

            unicode_parts = base.copy()
            unicode_parts[1] = "\u0661\u0660\u0660\u0660"
            signed_invalid_cases.append(signed(unicode_parts, encoding="utf-8"))

            alphabet_parts = base.copy()
            alphabet_parts[4] = alphabet_parts[4][:-1] + "+"
            signed_invalid_cases.append(signed(alphabet_parts))
            padding_parts = base.copy()
            padding_parts[4] += "="
            signed_invalid_cases.append(signed(padding_parts))

            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            binding_index = alphabet.index(base[4][-1])
            self.assertEqual(binding_index % 4, 0)
            noncanonical_parts = base.copy()
            noncanonical_parts[4] = base[4][:-1] + alphabet[binding_index + 1]
            signed_invalid_cases.append(signed(noncanonical_parts))

            for candidate in signed_invalid_cases:
                with self.subTest(candidate_length=len(candidate)):
                    self.assertFalse(
                        csrf_security.validate_csrf_token(
                            candidate, purpose, binding, 600, now=1000
                        )
                    )

            for candidate in (
                ".".join(base[:-1]),
                ".".join(base[:3] + [""] + base[4:]),
                ".".join(base) + ".extra",
            ):
                self.assertFalse(
                    csrf_security.validate_csrf_token(
                        candidate, purpose, binding, 600, now=1000
                    )
                )

            valid_token = ".".join(base)
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    valid_token, "authenticated:master", binding, 600, now=1001
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    valid_token, "preauth:admin", binding, 600, now=1001
                )
            )
            self.assertFalse(
                csrf_security.validate_csrf_token(
                    valid_token, purpose, "other-session", 600, now=1001
                )
            )
            with patch.object(
                csrf_security,
                "get_csrf_secret",
                return_value="Z9!pR4#vN7@kT2$xM8&cW5*hL3^sD6%yQ1jF",
            ):
                self.assertFalse(
                    csrf_security.validate_csrf_token(
                        valid_token, purpose, binding, 600, now=1001
                    )
                )

            oversized = "x" * (csrf_security._MAX_TOKEN_LENGTH + 1)
            with patch.object(
                csrf_security, "_urlsafe_decode_canonical"
            ) as decode, patch.object(csrf_security, "get_csrf_secret") as secret_read:
                self.assertFalse(
                    csrf_security.validate_csrf_token(
                        oversized, purpose, binding, 600, now=1001
                    )
                )
            decode.assert_not_called()
            secret_read.assert_not_called()

    def test_security_schema_defines_required_constraints_and_indexes(self):
        ddl = " ".join(db_schema.SECURITY_SCHEMA_STATEMENTS)
        for table_name in db_schema.REQUIRED_SECURITY_COLUMNS:
            self.assertIn(table_name, ddl)
        for names in db_schema.REQUIRED_SECURITY_CONSTRAINTS.values():
            for name in names:
                self.assertIn(name, ddl)
        for names in db_schema.REQUIRED_SECURITY_INDEXES.values():
            for name in names:
                self.assertIn(name, ddl)
        self.assertIn("BYTEA NOT NULL", ddl)
        self.assertIn("UNIQUE (token_hash)", ddl)
        self.assertIn("UNIQUE (role, nonce_hash)", ddl)
        self.assertIn("ON web_sessions (role, account_key)", ddl)
        self.assertIn("WHERE revoked_at IS NULL", ddl)
        self.assertIn("ON web_sessions (expires_at)", ddl)
        self.assertIn("ON consumed_login_nonces (expires_at)", ddl)
        self.assertIn("consumed_at >= issued_at AND consumed_at < expires_at", ddl)
        self.assertNotIn("CREATE TABLE", inspect.getsource(admin_app.startup_db_init))
        with test_environment():
            with patch.object(admin_app, "refresh_database_readiness", return_value=False):
                with patch.object(db_schema, "create_security_schema") as create_schema:
                    asyncio.run(admin_app.startup_db_init())
        create_schema.assert_not_called()

    def test_server_session_lookup_is_strict_read_only_and_hashes_cookie(self):
        with test_environment(mock_sessions=False):
            credential = admin_app.sign_admin_session()
            cursor = SecurityCursor(fetchone_values=[(1,)])
            connection = SecurityConnection(cursor)
            self.assertTrue(
                admin_app.server_session_is_active(
                    "admin", credential, lambda: connection
                )
            )
            self.assertEqual(
                connection.session_args, {"readonly": True, "autocommit": False}
            )
            self.assertEqual(connection.rollback_count, 1)
            self.assertTrue(cursor.closed)
            self.assertTrue(connection.closed)
            query, params = cursor.queries[-1]
            self.assertIn("expires_at > CURRENT_TIMESTAMP", query)
            self.assertNotIn(credential, repr(params))
            self.assertIsInstance(params[2], bytes)
            self.assertEqual(len(params[2]), 32)

            factory = Mock()
            self.assertFalse(
                admin_app.server_session_is_active("admin", "invalid", factory)
            )
            factory.assert_not_called()

    def test_session_create_consumes_only_hashed_nonce_and_closes_transaction(self):
        with test_environment(mock_sessions=False):
            nonce, token = admin_app._new_preauth_token("admin")
            issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
            cursor = SecurityCursor(fetchone_values=[(1,)])
            connection = SecurityConnection(cursor)
            result, credential = admin_app.create_authenticated_session(
                "admin",
                nonce,
                issued_at,
                expires_at,
                connection_factory=lambda: connection,
                current_time=issued_at,
            )
        self.assertEqual(result, "created")
        self.assertTrue(admin_app.session_credential_is_well_formed(credential))
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        self.assertEqual(
            connection.session_args, {"readonly": False, "autocommit": False}
        )
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        stored_arguments = repr([params for _query, params in cursor.queries])
        self.assertNotIn(nonce, stored_arguments)
        self.assertNotIn(token, stored_arguments)
        self.assertNotIn(credential, stored_arguments)
        create_params = next(
            params
            for query, params in cursor.queries
            if "INSERT INTO consumed_login_nonces" in query
        )
        self.assertEqual(len(create_params[1]), 32)
        self.assertEqual(len(create_params[9]), 32)

    def test_sequential_and_concurrent_preauth_replay_allow_one_session(self):
        with test_environment(mock_sessions=False):
            nonce, token = admin_app._new_preauth_token("admin")
            issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
            store = ConcurrentSecurityStore()
            first = admin_app.create_authenticated_session(
                "admin", nonce, issued_at, expires_at, store.connection
            )
            second = admin_app.create_authenticated_session(
                "admin", nonce, issued_at, expires_at, store.connection
            )
            self.assertEqual(first[0], "created")
            self.assertEqual(second, ("replayed", None))

            concurrent_store = ConcurrentSecurityStore()

            def attempt():
                return admin_app.create_authenticated_session(
                    "admin",
                    nonce,
                    issued_at,
                    expires_at,
                    concurrent_store.connection,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: attempt(), range(2)))
            self.assertEqual(
                sorted(status for status, _credential in results),
                ["created", "replayed"],
            )
            winner = next(
                credential
                for status, credential in results
                if status == "created"
            )
            self.assertTrue(
                admin_app.server_session_is_active(
                    "admin", winner, concurrent_store.connection
                )
            )
            self.assertEqual(len(concurrent_store.consumed), 1)
            self.assertEqual(len(concurrent_store.sessions), 1)
            self.assertEqual(
                [credential for status, credential in results if status == "replayed"],
                [None],
            )

            master_nonce = nonce
            master_token = csrf_security.issue_csrf_token(
                "preauth:master", master_nonce, admin_app.PREAUTH_CSRF_MAX_AGE
            )
            master_issued, master_expires = csrf_security.csrf_token_timestamps(
                master_token
            )
            master_result = admin_app.create_authenticated_session(
                "master",
                master_nonce,
                master_issued,
                master_expires,
                concurrent_store.connection,
            )
            self.assertEqual(master_result[0], "created")

    def test_concurrent_http_replay_preserves_only_winning_session(self):
        with test_environment(mock_sessions=False):
            login_page_response = asyncio.run(admin_app.login_form())
            preauth_cookies = response_cookies(login_page_response.raw_headers)
            nonce = preauth_cookies[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
            token = hidden_csrf_token(login_page_response.body)
            store = ConcurrentSecurityStore()

            def attempt(_index):
                return run_asgi_request(
                    "POST",
                    "/login",
                    form={
                        "password": "unit-test-admin-password",
                        "csrf_token": token,
                    },
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: nonce},
                )

            with patch.object(
                admin_app, "get_db_connection", side_effect=store.connection
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    responses = list(executor.map(attempt, range(2)))
                winner_response = next(
                    response for response in responses if response[0] == 303
                )
                loser_response = next(
                    response for response in responses if response[0] == 403
                )
                winner_cookie = response_cookies(winner_response[1])[
                    admin_app.ADMIN_SESSION_COOKIE
                ].value
                protected_status, _protected_headers, _protected_body = (
                    run_asgi_request(
                        "GET",
                        "/protected-route-that-does-not-exist",
                        cookies={admin_app.ADMIN_SESSION_COOKIE: winner_cookie},
                    )
                )
                replay_status, replay_headers, _replay_body = run_asgi_request(
                    "POST",
                    "/login",
                    form={
                        "password": "unit-test-admin-password",
                        "csrf_token": token,
                    },
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: nonce},
                )

        self.assertEqual(sorted(response[0] for response in responses), [303, 403])
        self.assertNotIn(
            admin_app.ADMIN_SESSION_COOKIE,
            response_cookies(loser_response[1]),
        )
        self.assertEqual(protected_status, 404)
        self.assertEqual(replay_status, 403)
        self.assertNotIn(
            admin_app.ADMIN_SESSION_COOKIE,
            response_cookies(replay_headers),
        )
        self.assertEqual(len(store.consumed), 1)
        self.assertEqual(len(store.sessions), 1)

    def test_nonce_uniqueness_conflict_is_replay_without_state_change(self):
        with test_environment(mock_sessions=False):
            store = ConcurrentSecurityStore()
            nonce, token = admin_app._new_preauth_token("admin")
            issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
            created, credential = admin_app.create_authenticated_session(
                "admin", nonce, issued_at, expires_at, store.connection
            )
            self.assertEqual(created, "created")
            committed_consumed = set(store.consumed)
            committed_sessions = dict(store.sessions)

            replay_connection = store.connection()
            replay = admin_app.create_authenticated_session(
                "admin",
                nonce,
                issued_at,
                expires_at,
                lambda: replay_connection,
            )

            self.assertEqual(replay, ("replayed", None))
            self.assertEqual(store.consumed, committed_consumed)
            self.assertEqual(store.sessions, committed_sessions)
            self.assertEqual(replay_connection.commit_count, 0)
            self.assertEqual(replay_connection.rollback_count, 1)
            self.assertTrue(replay_connection.cursor_instance.closed)
            self.assertTrue(replay_connection.closed)
            self.assertTrue(
                admin_app.server_session_is_active(
                    "admin", credential, store.connection
                )
            )

    def test_same_preauth_cookie_and_token_cannot_complete_login_twice(self):
        with test_environment(mock_sessions=False):
            login_page_response = asyncio.run(admin_app.login_form())
            preauth_cookies = response_cookies(login_page_response.raw_headers)
            nonce = preauth_cookies[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
            token = hidden_csrf_token(login_page_response.body)
            store = ConcurrentSecurityStore()
            request_data = {
                "form": {
                    "password": "unit-test-admin-password",
                    "csrf_token": token,
                },
                "cookies": {admin_app.ADMIN_PREAUTH_CSRF_COOKIE: nonce},
            }
            with patch.object(
                admin_app, "get_db_connection", side_effect=store.connection
            ):
                first_status, first_headers, _first_body = run_asgi_request(
                    "POST", "/login", **request_data
                )
                second_status, second_headers, second_body = run_asgi_request(
                    "POST", "/login", **request_data
                )
        self.assertEqual(first_status, 303)
        self.assertIn(
            admin_app.ADMIN_SESSION_COOKIE,
            response_cookies(first_headers),
        )
        self.assertEqual(second_status, 403)
        self.assertEqual(second_body, b"Forbidden")
        self.assertFalse(
            any(name.lower() == b"set-cookie" for name, _value in second_headers)
        )

    def test_relogin_and_logout_revoke_restored_session_cookie_and_token(self):
        with test_environment(mock_sessions=False):
            store = ConcurrentSecurityStore()
            nonce_one, token_one = admin_app._new_preauth_token("admin")
            issued_one, expires_one = csrf_security.csrf_token_timestamps(token_one)
            status_one, credential_one = admin_app.create_authenticated_session(
                "admin", nonce_one, issued_one, expires_one, store.connection
            )
            old_csrf = csrf_security.issue_csrf_token(
                "authenticated:admin",
                credential_one,
                admin_app.AUTHENTICATED_CSRF_MAX_AGE,
            )

            nonce_two, token_two = admin_app._new_preauth_token("admin")
            issued_two, expires_two = csrf_security.csrf_token_timestamps(token_two)
            status_two, credential_two = admin_app.create_authenticated_session(
                "admin", nonce_two, issued_two, expires_two, store.connection
            )
            self.assertEqual((status_one, status_two), ("created", "created"))
            self.assertFalse(
                admin_app.server_session_is_active(
                    "admin", credential_one, store.connection
                )
            )
            self.assertTrue(
                admin_app.server_session_is_active(
                    "admin", credential_two, store.connection
                )
            )

            old_request = make_request(
                "/products/new",
                form={"csrf_token": old_csrf},
                cookies={admin_app.ADMIN_SESSION_COOKIE: credential_one},
            )
            with patch.object(
                admin_app, "get_db_connection", side_effect=store.connection
            ):
                assert_csrf_rejected(
                    self, lambda: admin_app.require_admin_csrf(old_request)
                )

            self.assertTrue(
                admin_app.revoke_authenticated_session(
                    "admin", credential_two, store.connection
                )
            )
            self.assertFalse(
                admin_app.server_session_is_active(
                    "admin", credential_two, store.connection
                )
            )

    def test_session_transaction_failure_is_redacted_and_fails_closed(self):
        with test_environment(mock_sessions=False):
            nonce, token = admin_app._new_preauth_token("admin")
            issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
            cursor = SecurityCursor(fetchone_values=[(1,)])
            connection = SecurityConnection(cursor, fail_commit=True)
            with self.assertLogs(admin_app.logger) as captured:
                result = admin_app.create_authenticated_session(
                    "admin",
                    nonce,
                    issued_at,
                    expires_at,
                    lambda: connection,
                    current_time=issued_at,
                )
        self.assertEqual(result, ("failed", None))
        self.assertEqual(connection.rollback_count, 1)
        self.assertTrue(cursor.closed)
        self.assertTrue(connection.closed)
        observed = "\n".join(captured.output) + repr(cursor.queries)
        self.assertNotIn(SECRET_MARKER, observed)
        self.assertNotIn(nonce, observed)
        self.assertIn(admin_app.WEB_SESSION_CREATE_FAILED, observed)

    def test_session_failure_matrix_preserves_committed_state(self):
        failure_cases = (
            "connect",
            "cursor",
            "execute_1",
            "execute_2",
            "session_cleanup",
            "nonce_cleanup",
            "nonce_consumption",
            "previous_session_revocation",
            "session_insertion",
            "commit",
            ("session_insertion", "rollback"),
        )

        for failure_case in failure_cases:
            with self.subTest(failure_case=failure_case), test_environment(
                mock_sessions=False
            ):
                store = ConcurrentSecurityStore()
                original_nonce, original_token = admin_app._new_preauth_token("admin")
                original_issued, original_expires = csrf_security.csrf_token_timestamps(
                    original_token
                )
                original_status, original_credential = (
                    admin_app.create_authenticated_session(
                        "admin",
                        original_nonce,
                        original_issued,
                        original_expires,
                        store.connection,
                    )
                )
                self.assertEqual(original_status, "created")
                committed_consumed = set(store.consumed)
                committed_sessions = dict(store.sessions)

                nonce, token = admin_app._new_preauth_token("admin")
                issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
                connections = []

                def connection_factory():
                    if failure_case == "connect":
                        raise RuntimeError(SECRET_MARKER)
                    connection = store.connection(failure_case)
                    connections.append(connection)
                    return connection

                with self.assertLogs(admin_app.logger) as captured:
                    result = admin_app.create_authenticated_session(
                        "admin",
                        nonce,
                        issued_at,
                        expires_at,
                        connection_factory,
                    )

                self.assertEqual(result, ("failed", None))
                self.assertEqual(store.consumed, committed_consumed)
                self.assertEqual(store.sessions, committed_sessions)
                self.assertTrue(
                    admin_app.server_session_is_active(
                        "admin", original_credential, store.connection
                    )
                )
                observed = "\n".join(captured.output)
                self.assertEqual(observed.count(admin_app.WEB_SESSION_CREATE_FAILED), 1)
                self.assertNotIn(SECRET_MARKER, observed)
                self.assertNotIn(nonce, observed)
                self.assertNotIn(original_credential, observed)
                for connection in connections:
                    self.assertGreaterEqual(connection.rollback_count, 1)
                    if failure_case != "cursor":
                        self.assertTrue(connection.cursor_instance.closed)
                    self.assertTrue(connection.closed)

    def test_session_cleanup_failures_after_commit_do_not_change_result(self):
        for failure_case in ("cursor_close", "connection_close"):
            with self.subTest(failure_case=failure_case), test_environment(
                mock_sessions=False
            ):
                store = ConcurrentSecurityStore()
                nonce, token = admin_app._new_preauth_token("admin")
                issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
                connection = store.connection(failure_case)
                result, credential = admin_app.create_authenticated_session(
                    "admin",
                    nonce,
                    issued_at,
                    expires_at,
                    lambda: connection,
                )
                self.assertEqual(result, "created")
                self.assertTrue(connection.committed)
                self.assertTrue(connection.cursor_instance.closed)
                self.assertTrue(connection.closed)
                self.assertTrue(
                    admin_app.server_session_is_active(
                        "admin", credential, store.connection
                    )
                )

    def test_lookup_logout_and_failed_http_login_preserve_state(self):
        with test_environment(mock_sessions=False):
            store = ConcurrentSecurityStore()
            nonce, token = admin_app._new_preauth_token("admin")
            issued_at, expires_at = csrf_security.csrf_token_timestamps(token)
            status, credential = admin_app.create_authenticated_session(
                "admin", nonce, issued_at, expires_at, store.connection
            )
            self.assertEqual(status, "created")
            committed_consumed = set(store.consumed)
            committed_sessions = dict(store.sessions)

            lookup_connection = store.connection("lookup")
            with self.assertLogs(admin_app.logger) as lookup_logs:
                self.assertFalse(
                    admin_app.server_session_is_active(
                        "admin", credential, lambda: lookup_connection
                    )
                )
            revoke_connection = store.connection("logout_revocation")
            with self.assertLogs(admin_app.logger) as revoke_logs:
                self.assertFalse(
                    admin_app.revoke_authenticated_session(
                        "admin",
                        credential,
                        lambda: revoke_connection,
                    )
                )
            self.assertEqual(store.consumed, committed_consumed)
            self.assertEqual(store.sessions, committed_sessions)
            self.assertTrue(
                admin_app.server_session_is_active(
                    "admin", credential, store.connection
                )
            )
            for failed_connection in (lookup_connection, revoke_connection):
                self.assertEqual(failed_connection.commit_count, 0)
                self.assertGreaterEqual(failed_connection.rollback_count, 1)
                self.assertTrue(failed_connection.cursor_instance.closed)
                self.assertTrue(failed_connection.closed)

            login_response = asyncio.run(admin_app.login_form())
            login_cookies = response_cookies(login_response.raw_headers)
            login_nonce = login_cookies[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
            login_token = hidden_csrf_token(login_response.body)
            failing_connections = []

            def failing_connection():
                connection = store.connection("commit")
                failing_connections.append(connection)
                return connection

            with patch.object(
                admin_app, "get_db_connection", side_effect=failing_connection
            ):
                with self.assertLogs(admin_app.logger) as create_logs:
                    response_status, response_headers, response_body = (
                        run_asgi_request(
                            "POST",
                            "/login",
                            form={
                                "password": "unit-test-admin-password",
                                "csrf_token": login_token,
                            },
                            cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: login_nonce},
                        )
                    )
            self.assertEqual(response_status, 503)
            self.assertEqual(response_body, b"Service unavailable")
            self.assertNotIn(
                admin_app.ADMIN_SESSION_COOKIE,
                response_cookies(response_headers),
            )
            self.assertEqual(store.consumed, committed_consumed)
            self.assertEqual(store.sessions, committed_sessions)
            self.assertTrue(all(connection.closed for connection in failing_connections))
            self.assertTrue(
                all(
                    connection.rollback_count >= 1
                    and connection.cursor_instance.closed
                    for connection in failing_connections
                )
            )
            observed_logs = "\n".join(
                lookup_logs.output + revoke_logs.output + create_logs.output
            )
            for stable_code in (
                admin_app.WEB_SESSION_LOOKUP_FAILED,
                admin_app.WEB_SESSION_REVOKE_FAILED,
                admin_app.WEB_SESSION_CREATE_FAILED,
            ):
                self.assertIn(stable_code, observed_logs)
            self.assertNotIn(SECRET_MARKER, observed_logs)
            self.assertNotIn(credential, observed_logs)
            self.assertNotIn(login_nonce, observed_logs)

    def test_lookup_and_revoke_failures_are_redacted_and_store_only_hashes(self):
        with test_environment(mock_sessions=False):
            credential = admin_app.sign_admin_session()

            lookup_cursor = SecurityCursor(fail_at=4)
            lookup_connection = SecurityConnection(lookup_cursor)
            with self.assertLogs(admin_app.logger) as lookup_logs:
                self.assertFalse(
                    admin_app.server_session_is_active(
                        "admin", credential, lambda: lookup_connection
                    )
                )
            lookup_observed = "\n".join(lookup_logs.output) + repr(
                lookup_cursor.queries
            )
            self.assertNotIn(credential, lookup_observed)
            self.assertNotIn(SECRET_MARKER, lookup_observed)
            self.assertIn(admin_app.WEB_SESSION_LOOKUP_FAILED, lookup_observed)
            self.assertEqual(lookup_connection.rollback_count, 1)
            self.assertTrue(lookup_cursor.closed)
            self.assertTrue(lookup_connection.closed)

            revoke_cursor = SecurityCursor(fetchone_values=[(1,)])
            revoke_connection = SecurityConnection(revoke_cursor)
            self.assertTrue(
                admin_app.revoke_authenticated_session(
                    "admin", credential, lambda: revoke_connection
                )
            )
            self.assertEqual(
                revoke_connection.session_args,
                {"readonly": False, "autocommit": False},
            )
            query, params = revoke_cursor.queries[-1]
            self.assertIn("UPDATE web_sessions", query)
            self.assertNotIn(credential, repr(params))
            self.assertIsInstance(params[2], bytes)
            self.assertEqual(len(params[2]), 32)
            self.assertEqual(revoke_connection.commit_count, 1)
            self.assertTrue(revoke_cursor.closed)
            self.assertTrue(revoke_connection.closed)

            failed_cursor = SecurityCursor(fail_at=1)
            failed_connection = SecurityConnection(failed_cursor)
            with self.assertLogs(admin_app.logger) as revoke_logs:
                self.assertFalse(
                    admin_app.revoke_authenticated_session(
                        "admin", credential, lambda: failed_connection
                    )
                )
            revoke_observed = "\n".join(revoke_logs.output) + repr(
                failed_cursor.queries
            )
            self.assertNotIn(credential, revoke_observed)
            self.assertNotIn(SECRET_MARKER, revoke_observed)
            self.assertIn(admin_app.WEB_SESSION_REVOKE_FAILED, revoke_observed)
            self.assertEqual(failed_connection.rollback_count, 1)
            self.assertTrue(failed_cursor.closed)
            self.assertTrue(failed_connection.closed)

    def test_multipart_csrf_rejects_before_parser_or_spool(self):
        boundary = "B11B1Boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="photo"; filename="probe.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\nbytes\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii")
        with test_environment():
            session = admin_app.sign_admin_session()
            with patch("starlette.formparsers.MultiPartParser.parse") as parser:
                with patch("starlette.formparsers.SpooledTemporaryFile") as spool:
                    for headers in ({}, {admin_app.CSRF_HEADER: "invalid"}):
                        status, _response_headers, response_body = run_asgi_request(
                            "POST",
                            "/orders/1/items/1/weigh",
                            cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                            headers=headers,
                            raw_body=body,
                            content_type=f"multipart/form-data; boundary={boundary}",
                        )
                        self.assertEqual(status, 403)
                        self.assertEqual(response_body, b'{"detail":"Forbidden"}')
            parser.assert_not_called()
            spool.assert_not_called()

    def test_multipart_rejection_never_receives_or_constructs_body_objects(self):
        boundary = "B11B1Fix2Boundary"

        def multipart_body(token_value, padding=b""):
            return (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
                f"{token_value}\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="photo"; filename="probe.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("ascii") + padding + f"\r\n--{boundary}--\r\n".encode("ascii")

        with test_environment():
            session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin",
                session,
                admin_app.AUTHENTICATED_CSRF_MAX_AGE,
            )
            cases = (
                (
                    {"case": "hidden"},
                    {},
                    "",
                    multipart_body(token),
                    {admin_app.ADMIN_SESSION_COOKIE: session},
                    403,
                ),
                (
                    {"case": "query"},
                    {},
                    "csrf_token=" + urllib.parse.quote(token),
                    multipart_body("wrong"),
                    {admin_app.ADMIN_SESSION_COOKIE: session},
                    403,
                ),
                (
                    {"case": "invalid-header"},
                    {admin_app.CSRF_HEADER: "invalid"},
                    "",
                    multipart_body("wrong"),
                    {admin_app.ADMIN_SESSION_COOKIE: session},
                    403,
                ),
                (
                    {"case": "large"},
                    {},
                    "",
                    multipart_body("wrong", b"x" * (2 * 1024 * 1024)),
                    {admin_app.ADMIN_SESSION_COOKIE: session},
                    403,
                ),
                (
                    {"case": "malformed-session"},
                    {},
                    "",
                    multipart_body("wrong"),
                    {admin_app.ADMIN_SESSION_COOKIE: "malformed"},
                    303,
                ),
            )
            with patch("starlette.requests.Request.form") as request_form, patch(
                "starlette.formparsers.MultiPartParser.parse"
            ) as parser, patch(
                "starlette.formparsers.SpooledTemporaryFile"
            ) as spool, patch(
                "starlette.formparsers.UploadFile"
            ) as upload, patch.object(
                admin_app.psycopg2, "connect"
            ) as connect, patch.object(
                admin_app, "get_db_connection"
            ) as get_connection, patch.object(
                admin_app.urllib.request, "urlopen"
            ) as urlopen, patch(
                "builtins.open"
            ) as open_file, patch.object(
                admin_app,
                "server_session_is_active",
                side_effect=lambda _role, value: (
                    admin_app.session_credential_is_well_formed(value)
                ),
            ):
                for observation, headers, query_string, body, cookies, expected in cases:
                    status, _response_headers, _response_body = run_asgi_request(
                        "POST",
                        "/orders/1/items/1/weigh",
                        cookies=cookies,
                        headers=headers,
                        query_string=query_string,
                        raw_body=body,
                        content_type=f"multipart/form-data; boundary={boundary}",
                        observation=observation,
                    )
                    self.assertEqual(status, expected)
                    self.assertEqual(observation.get("receive_calls", 0), 0)
            request_form.assert_not_called()
            parser.assert_not_called()
            spool.assert_not_called()
            upload.assert_not_called()
            connect.assert_not_called()
            get_connection.assert_not_called()
            urlopen.assert_not_called()
            open_file.assert_not_called()

    def test_malformed_preview_basic_header_rejects_multipart_before_body(self):
        boundary = "B11B1Fix2BasicBoundary"
        observation = {}
        with test_environment(
            APP_ENV="preview",
            PREVIEW_GATE_ENABLED="true",
            PREVIEW_GATE_USERNAME="reviewer",
            PREVIEW_GATE_PASSWORD=VALID_GATE_PASSWORD,
        ):
            session = admin_app.sign_admin_session()
            with patch("starlette.requests.Request.form") as request_form, patch(
                "starlette.formparsers.MultiPartParser.parse"
            ) as parser, patch(
                "starlette.formparsers.SpooledTemporaryFile"
            ) as spool, patch(
                "starlette.formparsers.UploadFile"
            ) as upload:
                status, _headers, _body = run_asgi_request(
                    "POST",
                    "/orders/1/items/1/weigh",
                    cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                    headers={"Authorization": "Basic malformed"},
                    raw_body=b"multipart-body-must-not-be-read",
                    content_type=f"multipart/form-data; boundary={boundary}",
                    observation=observation,
                )
        self.assertEqual(status, 401)
        self.assertEqual(observation.get("receive_calls", 0), 0)
        request_form.assert_not_called()
        parser.assert_not_called()
        spool.assert_not_called()
        upload.assert_not_called()

    def test_multipart_header_is_authoritative_and_noscript_fallback_executes(self):
        boundary = "B11B1Fix2AcceptedBoundary"
        multipart_body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="final_weight_grams"\r\n\r\n'
            "250\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
            "wrong-hidden-token\r\n"
            f"--{boundary}--\r\n"
        ).encode("ascii")
        with test_environment():
            session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin",
                session,
                admin_app.AUTHENTICATED_CSRF_MAX_AGE,
            )
            cursor = Mock()
            cursor.fetchone.return_value = None
            connection = Mock()
            connection.cursor.return_value = cursor
            with patch.object(
                admin_app.psycopg2, "connect", return_value=connection
            ), patch("starlette.formparsers.SpooledTemporaryFile") as spool, patch(
                "starlette.formparsers.UploadFile"
            ) as upload:
                multipart_status, multipart_headers, _multipart_response = (
                    run_asgi_request(
                        "POST",
                        "/orders/1/items/1/weigh",
                        cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                        headers={admin_app.CSRF_HEADER: token},
                        query_string="csrf_token=wrong-query-token",
                        raw_body=multipart_body,
                        content_type=f"multipart/form-data; boundary={boundary}",
                    )
                )
                fallback_status, fallback_headers, _fallback_response = (
                    run_asgi_request(
                        "POST",
                        "/orders/1/items/1/weigh",
                        form={
                            "final_weight_grams": "250",
                            "csrf_token": token,
                        },
                        cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                    )
                )
        self.assertEqual(multipart_status, 303)
        self.assertEqual(fallback_status, 303)
        self.assertEqual(dict(multipart_headers).get(b"location"), b"/orders/1")
        self.assertEqual(dict(fallback_headers).get(b"location"), b"/orders/1")
        spool.assert_not_called()
        upload.assert_not_called()

    def test_oversized_or_unsupported_request_is_rejected_before_form_parser(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            oversized_body = (
                b"csrf_token=x&padding="
                + b"a" * admin_app.URLENCODED_BODY_MAX_LENGTH
            )
            with patch("starlette.formparsers.FormParser.parse") as parser:
                status, _headers, body = run_asgi_request(
                    "POST",
                    "/products/new",
                    cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                    raw_body=oversized_body,
                    content_type="application/x-www-form-urlencoded",
                )
                unsupported_status, _unsupported_headers, unsupported_body = (
                    run_asgi_request(
                        "POST",
                        "/products/new",
                        cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                        headers={admin_app.CSRF_HEADER: "x"},
                        raw_body=b"csrf_token=x",
                        content_type="text/plain",
                    )
                )
            self.assertEqual(status, 403)
            self.assertEqual(body, b'{"detail":"Forbidden"}')
            self.assertEqual(unsupported_status, 403)
            self.assertEqual(unsupported_body, b'{"detail":"Forbidden"}')
            parser.assert_not_called()

    def test_logout_failure_does_not_delete_browser_cookie(self):
        with test_environment():
            credential = admin_app.sign_admin_session()
            request = make_request(
                "/logout",
                cookies={admin_app.ADMIN_SESSION_COOKIE: credential},
            )
            request.state._admin_session_checked = True
            request.state._admin_session_value = credential
            with patch.object(
                admin_app, "revoke_authenticated_session", return_value=False
            ):
                response = asyncio.run(admin_app.logout(request))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(
            any(name.lower() == b"set-cookie" for name, _value in response.raw_headers)
        )

    def test_login_get_issues_role_specific_secure_preauth_cookie_and_hidden_token(self):
        with test_environment(APP_ENV="preview"):
            admin_response = asyncio.run(admin_app.login_form())
            master_response = asyncio.run(admin_app.master_login_form())
            for role, response, cookie_name, path in (
                ("admin", admin_response, admin_app.ADMIN_PREAUTH_CSRF_COOKIE, "/login"),
                ("master", master_response, admin_app.MASTER_PREAUTH_CSRF_COOKIE, "/master/login"),
            ):
                cookies = response_cookies(response.raw_headers)
                self.assertIn(cookie_name, cookies)
                cookie = cookies[cookie_name]
                self.assertTrue(cookie["httponly"])
                self.assertTrue(cookie["secure"])
                self.assertEqual(cookie["samesite"].lower(), "strict")
                self.assertEqual(cookie["path"], path)
                token = hidden_csrf_token(response.body)
                self.assertTrue(
                    csrf_security.validate_csrf_token(
                        token,
                        f"preauth:{role}",
                        cookie.value,
                        admin_app.PREAUTH_CSRF_MAX_AGE,
                    )
                )

    def test_login_csrf_missing_wrong_and_cross_flow_are_rejected(self):
        with test_environment():
            admin_nonce, admin_token = admin_app._new_preauth_token("admin")
            master_nonce, master_token = admin_app._new_preauth_token("master")
            cases = (
                make_request("/login", form={"password": "x"}),
                make_request(
                    "/login",
                    form={admin_app.CSRF_FORM_FIELD: "wrong"},
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: admin_nonce},
                ),
                make_request(
                    "/login",
                    form={admin_app.CSRF_FORM_FIELD: master_token},
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: master_nonce},
                ),
            )
            for request in cases:
                assert_csrf_rejected(
                    self, lambda request=request: admin_app.require_admin_login_csrf(request)
                )
            valid_request = make_request(
                "/login",
                form={admin_app.CSRF_FORM_FIELD: admin_token},
                cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: admin_nonce},
            )
            asyncio.run(admin_app.require_admin_login_csrf(valid_request))

    def test_csrf_rejection_precedes_password_and_all_side_effects(self):
        with test_environment():
            stdout = io.StringIO()
            stderr = io.StringIO()
            logs = io.StringIO()
            handler = logging.StreamHandler(logs)
            logging.getLogger().addHandler(handler)
            try:
                with patch.object(admin_app.secrets, "compare_digest") as password_compare:
                    with patch.object(admin_app.psycopg2, "connect") as connect:
                        with patch.object(admin_app, "get_db_connection") as get_connection:
                            with patch.object(admin_app.urllib.request, "urlopen") as urlopen:
                                with patch("builtins.open") as open_file:
                                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                                        status, headers, body = run_asgi_request(
                                            "POST",
                                            "/login",
                                            form={"password": SECRET_MARKER},
                                        )
            finally:
                logging.getLogger().removeHandler(handler)
        self.assertEqual(status, 403)
        self.assertEqual(body, b'{"detail":"Forbidden"}')
        self.assertFalse(any(name.lower() == b"set-cookie" for name, _ in headers))
        password_compare.assert_not_called()
        connect.assert_not_called()
        get_connection.assert_not_called()
        urlopen.assert_not_called()
        open_file.assert_not_called()
        observed = stdout.getvalue() + stderr.getvalue() + logs.getvalue() + body.decode()
        self.assertNotIn(SECRET_MARKER, observed)

    def test_valid_login_rotates_session_and_invalidates_preauth_cookie(self):
        with test_environment():
            first_get = asyncio.run(admin_app.login_form())
            cookies = response_cookies(first_get.raw_headers)
            nonce = cookies[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
            token = hidden_csrf_token(first_get.body)
            credential = admin_app.sign_admin_session()
            with patch.object(
                admin_app,
                "create_authenticated_session",
                return_value=("created", credential),
            ):
                status, headers, _body = run_asgi_request(
                    "POST",
                    "/login",
                    form={"password": "unit-test-admin-password", "csrf_token": token},
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: nonce},
                )
        self.assertEqual(status, 303)
        response_cookie_jar = response_cookies(headers)
        self.assertIn(admin_app.ADMIN_SESSION_COOKIE, response_cookie_jar)
        self.assertTrue(response_cookie_jar[admin_app.ADMIN_SESSION_COOKIE].value)
        self.assertIn(admin_app.ADMIN_PREAUTH_CSRF_COOKIE, response_cookie_jar)
        self.assertEqual(response_cookie_jar[admin_app.ADMIN_PREAUTH_CSRF_COOKIE]["max-age"], "0")

    def test_failed_password_rotates_preauth_token(self):
        with test_environment():
            first_get = asyncio.run(admin_app.login_form())
            first_cookies = response_cookies(first_get.raw_headers)
            first_nonce = first_cookies[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
            first_token = hidden_csrf_token(first_get.body)
            with patch.object(admin_app, "create_authenticated_session") as create:
                status, headers, body = run_asgi_request(
                    "POST",
                    "/login",
                    form={"password": "wrong-password", "csrf_token": first_token},
                    cookies={admin_app.ADMIN_PREAUTH_CSRF_COOKIE: first_nonce},
                )
            create.assert_not_called()
        self.assertEqual(status, 200)
        rotated = response_cookies(headers)[admin_app.ADMIN_PREAUTH_CSRF_COOKIE].value
        self.assertNotEqual(rotated, first_nonce)
        self.assertNotEqual(hidden_csrf_token(body), first_token)

    def test_authenticated_tokens_reject_missing_wrong_query_other_session_and_role(self):
        with test_environment():
            admin_session = admin_app.sign_admin_session()
            other_admin_session = admin_app.sign_admin_session()
            master_session = admin_app.sign_master_session()
            admin_token = csrf_security.issue_csrf_token(
                "authenticated:admin",
                admin_session,
                admin_app.AUTHENTICATED_CSRF_MAX_AGE,
            )
            master_token = csrf_security.issue_csrf_token(
                "authenticated:master",
                master_session,
                admin_app.AUTHENTICATED_CSRF_MAX_AGE,
            )
            cases = (
                make_request(
                    "/products/new",
                    cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
                ),
                make_request(
                    "/products/new",
                    form={"csrf_token": "wrong"},
                    cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
                ),
                make_request(
                    "/products/new",
                    form={"csrf_token": admin_token},
                    cookies={admin_app.ADMIN_SESSION_COOKIE: other_admin_session},
                ),
                make_request(
                    "/products/new",
                    form={"csrf_token": master_token},
                    cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
                ),
                make_request(
                    "/products/new",
                    cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
                    query_string="csrf_token=" + urllib.parse.quote(admin_token),
                ),
            )
            for request in cases:
                assert_csrf_rejected(
                    self, lambda request=request: admin_app.require_admin_csrf(request)
                )

            valid_request = make_request(
                "/products/new",
                form={"csrf_token": admin_token},
                cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
            )
            asyncio.run(admin_app.require_admin_csrf(valid_request))

    def test_json_requires_dedicated_header_and_form_does_not_accept_it(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin", session, admin_app.AUTHENTICATED_CSRF_MAX_AGE
            )
            json_request = make_request(
                "/products/new",
                json_body="{}",
                cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                headers={admin_app.CSRF_HEADER: token},
            )
            asyncio.run(admin_app.require_admin_csrf(json_request))
            form_request = make_request(
                "/products/new",
                form={},
                cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                headers={admin_app.CSRF_HEADER: token},
            )
            assert_csrf_rejected(
                self, lambda: admin_app.require_admin_csrf(form_request)
            )

    def test_logout_invalidates_browser_session_and_old_token_without_cookie(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            token = csrf_security.issue_csrf_token(
                "authenticated:admin", session, admin_app.AUTHENTICATED_CSRF_MAX_AGE
            )
            with patch.object(
                admin_app, "revoke_authenticated_session", return_value=True
            ):
                status, headers, _body = run_asgi_request(
                    "POST",
                    "/logout",
                    form={"csrf_token": token},
                    cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                )
            self.assertEqual(status, 303)
            deleted = response_cookies(headers)[admin_app.ADMIN_SESSION_COOKIE]
            self.assertEqual(deleted["max-age"], "0")
            after_logout = make_request(
                "/products/new", form={"csrf_token": token}, cookies={}
            )
            assert_csrf_rejected(
                self, lambda: admin_app.require_admin_csrf(after_logout)
            )

    def test_every_rendered_post_form_gets_session_bound_hidden_token(self):
        with test_environment():
            rendered_pages = []

            for role, path, cookie_name in (
                ("admin", "/login", admin_app.ADMIN_PREAUTH_CSRF_COOKIE),
                ("master", "/master/login", admin_app.MASTER_PREAUTH_CSRF_COOKIE),
            ):
                status, headers, body = run_asgi_request("GET", path)
                self.assertEqual(status, 200)
                nonce = response_cookies(headers)[cookie_name].value
                rendered_pages.append(
                    (f"preauth:{role}", nonce, body.decode("utf-8"))
                )

            def render_with_cursor(handler, cursor, *args, connector="psycopg2"):
                connection = Mock()
                connection.cursor.return_value = cursor
                target = admin_app.psycopg2 if connector == "psycopg2" else admin_app
                attribute = "connect" if connector == "psycopg2" else "get_db_connection"
                with patch.object(target, attribute, return_value=connection):
                    return asyncio.run(handler(*args))

            admin_session = admin_app.sign_admin_session()
            admin_request = make_request(
                "/products/new",
                method="GET",
                cookies={admin_app.ADMIN_SESSION_COOKIE: admin_session},
            )
            context_token = admin_app._CURRENT_REQUEST.set(admin_request)
            try:
                rendered_pages.extend(
                    ("authenticated:admin", admin_session, page)
                    for page in (
                        asyncio.run(admin_app.new_broadcast_form()),
                        asyncio.run(admin_app.new_channel_post_form()),
                        asyncio.run(admin_app.new_product_form()),
                        asyncio.run(admin_app.new_product_option_form(1)),
                        asyncio.run(admin_app.new_category_form()),
                    )
                )

                broadcast_cursor = Mock()
                broadcast_cursor.fetchall.return_value = [
                    (1, "Draft", "draft", "all_clients", None, 1, 0, 1, 0, 0)
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.broadcasts,
                            broadcast_cursor,
                            connector="get_db_connection",
                        ),
                    )
                )

                channel_cursor = Mock()
                channel_cursor.fetchall.return_value = [
                    (1, "Draft", "draft", None, None, None)
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.channel_posts,
                            channel_cursor,
                            connector="get_db_connection",
                        ),
                    )
                )

                orders_cursor = Mock()
                orders_cursor.fetchall.return_value = [
                    (
                        1, "42", "customer", "phone", "address", 10.0,
                        "pending", "cash", "unpaid", "new", "telegram", None,
                    )
                ]
                orders_cursor.fetchone.return_value = (1,)
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(admin_app.orders, orders_cursor),
                    )
                )

                new_order_cursor = Mock()
                new_order_cursor.fetchall.side_effect = [
                    [(1, "Product", "per_kg", 24.0, None, None, 1)],
                    [],
                    [],
                    [],
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(admin_app.new_order_form, new_order_cursor),
                    )
                )

                picking_cursor = Mock()
                picking_cursor.fetchall.side_effect = [
                    [(
                        1, "42", "Customer", "customer", 12.5, "unpaid",
                        "Cash", "confirmed", "telegram", None,
                        "pickup", None, None,
                    )],
                    [(
                        2, "43", "Customer2", "customer2", 9.0, "paid",
                        "IBAN", "picking", "website", None,
                        "delivery", "Main st", "Amsterdam",
                    )],
                    [],
                    [
                        ("42", 10, "Weighted product", None, None, None, "per_kg"),
                        ("43", 11, "Bagged product", None, None, None, "fixed"),
                    ],
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(admin_app.picking_workspace, picking_cursor),
                    )
                )

                order_cursor = Mock()
                order_cursor.fetchone.return_value = (
                    1,
                    "42",
                    "customer",
                    "phone",
                    "address",
                    0,
                    "paid",
                    "cash",
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    None,
                    "",
                    "paid",
                    "new",
                    "telegram",
                )
                order_cursor.fetchall.side_effect = [
                    [(10, "Weighted product", None, 0, None, 35.0, "per_kg", None)],
                    [],
                ]
                order_page = render_with_cursor(
                    admin_app.order_detail, order_cursor, "42"
                )
                rendered_pages.append(
                    ("authenticated:admin", admin_session, order_page)
                )
                self.assertEqual(
                    order_page.count('enctype="multipart/form-data"'), 1
                )
                self.assertIn('data-csrf-multipart="header"', order_page)
                noscript = re.search(r"<noscript>(.*?)</noscript>", order_page, re.S)
                self.assertIsNotNone(noscript)
                self.assertIn('method="post"', noscript.group(1))
                self.assertNotIn("multipart/form-data", noscript.group(1))

                client_cursor = Mock()
                client_cursor.fetchone.side_effect = [
                    (1, "customer", "Customer", "phone", "address", "note"),
                    (0, 0, 0, 0, 0, None, None),
                    None,
                    None,
                ]
                client_cursor.fetchall.side_effect = [[], [], []]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.client_detail, client_cursor, 1
                        ),
                    )
                )

                products_cursor = Mock()
                products_cursor.fetchall.return_value = [
                    (
                        1, "Active", 10.0, None, True, "Category", 1000,
                        False, 100, False, "per_kg", None, None, None, 0, 0,
                    ),
                    (
                        2, "Inactive", 10.0, None, False, "Category", 1000,
                        False, 100, False, "per_kg", None, None, None, 0, 0,
                    ),
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(admin_app.products, products_cursor),
                    )
                )

                edit_product_cursor = Mock()
                edit_product_cursor.fetchone.return_value = (
                    1, "Options product", 0, "", None, 0, True, 0, False, 0,
                    False, "", 0, "options", None, None, None, None,
                )
                edit_product_cursor.fetchall.side_effect = [
                    [(9, "Pack", 500, 5.0, 0, True, 2, False)],
                    [],
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.edit_product_form,
                            edit_product_cursor,
                            1,
                        ),
                    )
                )

                recommendations_cursor = Mock()
                recommendations_cursor.fetchone.return_value = ("Product",)
                recommendations_cursor.fetchall.side_effect = [
                    [(2, "Other")],
                    [(2,)],
                ]
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.product_recommendations_form,
                            recommendations_cursor,
                            1,
                        ),
                    )
                )

                option_cursor = Mock()
                option_cursor.fetchone.return_value = (
                    1, "Pack", 500, 5.0, 0, True, 2, False
                )
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.edit_product_option_form,
                            option_cursor,
                            9,
                        ),
                    )
                )

                category_cursor = Mock()
                category_cursor.fetchone.return_value = ("Category", 0, True)
                rendered_pages.append(
                    (
                        "authenticated:admin",
                        admin_session,
                        render_with_cursor(
                            admin_app.edit_category_form,
                            category_cursor,
                            1,
                        ),
                    )
                )
            finally:
                admin_app._CURRENT_REQUEST.reset(context_token)

            master_session = admin_app.sign_master_session()
            master_request = make_request(
                "/master",
                method="GET",
                cookies={admin_app.MASTER_SESSION_COOKIE: master_session},
            )
            context_token = admin_app._CURRENT_REQUEST.set(master_request)
            try:
                master_cursor = Mock()
                master_cursor.fetchall.return_value = []
                rendered_pages.append(
                    (
                        "authenticated:master",
                        master_session,
                        render_with_cursor(
                            admin_app.master_dashboard,
                            master_cursor,
                            connector="get_db_connection",
                        ),
                    )
                )
            finally:
                admin_app._CURRENT_REQUEST.reset(context_token)

            expected_actions = {path for _method, path in UNSAFE_ROUTE_DEPENDENCIES}
            rendered_actions = set()
            for purpose, session, page in rendered_pages:
                forms = re.findall(r"<form\b.*?</form>", page, re.I | re.S)
                for rendered_form in forms:
                    action_match = re.search(
                        r'action=["\']([^"\']+)', rendered_form, re.I
                    )
                    if action_match is None:
                        continue
                    action = action_match.group(1)
                    unsafe_action = any(
                        re.fullmatch(
                            re.sub(
                                r"\\\{[^}]+\\\}",
                                r"[^/]+",
                                re.escape(template),
                            ),
                            action,
                        )
                        for template in expected_actions
                    )
                    if unsafe_action:
                        self.assertRegex(
                            rendered_form, r"(?i)method=['\"]post['\"]"
                        )
                post_forms = [
                    form
                    for form in forms
                    if re.search(r"method=['\"]post['\"]", form, re.I)
                ]
                self.assertGreaterEqual(len(post_forms), 1)
                for form in post_forms:
                    self.assertEqual(form.count('name="csrf_token"'), 1)
                    token_match = re.search(
                        r'name="csrf_token" value="([^"]+)"', form
                    )
                    self.assertIsNotNone(token_match)
                    self.assertTrue(
                        csrf_security.validate_csrf_token(
                            token_match.group(1),
                            purpose,
                            session,
                            (
                                admin_app.PREAUTH_CSRF_MAX_AGE
                                if purpose.startswith("preauth:")
                                else admin_app.AUTHENTICATED_CSRF_MAX_AGE
                            ),
                        )
                    )
                    action_match = re.search(
                        r'action=["\']([^"\']+)', form, re.I
                    )
                    self.assertIsNotNone(action_match)
                    action = action_match.group(1)
                    self.assertNotIn("?", action)
                    self.assertNotIn("csrf", action.casefold())
                    rendered_actions.add(action)

            matched_templates = set()
            for action in rendered_actions:
                matching = []
                for template in expected_actions:
                    pattern = re.sub(
                        r"\\\{[^}]+\\\}", r"[^/]+", re.escape(template)
                    )
                    if re.fullmatch(pattern, action):
                        matching.append(template)
                self.assertEqual(len(matching), 1, action)
                matched_templates.update(matching)
            self.assertEqual(matched_templates, expected_actions)

            all_rendered_html = "".join(page for _purpose, _session, page in rendered_pages)
            self.assertEqual(all_rendered_html.count('enctype="multipart/form-data"'), 1)
            self.assertIn('data-csrf-multipart="header"', all_rendered_html)
            self.assertIn("<noscript>", all_rendered_html)
            self.assertNotIn('href="/logout"', all_rendered_html)
            self.assertNotIn('href="/master/logout"', all_rendered_html)

    def test_storefront_contains_no_admin_or_csrf_material(self):
        with test_environment():
            with patch.object(storefront, "fetch_catalog", return_value=[]):
                response = asyncio.run(storefront.shop_page())
        self.assertEqual(response.status_code, 200)
        page = response.body.decode("utf-8")
        for private_name in (
            admin_app.ADMIN_SESSION_COOKIE,
            admin_app.MASTER_SESSION_COOKIE,
            admin_app.ADMIN_PREAUTH_CSRF_COOKIE,
            admin_app.MASTER_PREAUTH_CSRF_COOKIE,
            admin_app.CSRF_FORM_FIELD,
            admin_app.CSRF_HEADER,
        ):
            self.assertNotIn(private_name, page)

    def test_unsafe_route_rejection_has_zero_database_file_api_or_session_writes(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            with patch.object(admin_app.psycopg2, "connect") as connect:
                with patch.object(admin_app, "get_db_connection") as get_connection:
                    with patch.object(admin_app.urllib.request, "urlopen") as urlopen:
                        with patch("builtins.open") as open_file:
                            status, headers, body = run_asgi_request(
                                "POST",
                                "/products/new",
                                form={"name": SECRET_MARKER},
                                cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                            )
            self.assertEqual(status, 403)
            self.assertEqual(body, b'{"detail":"Forbidden"}')
            self.assertFalse(any(name.lower() == b"set-cookie" for name, _ in headers))
            connect.assert_not_called()
            get_connection.assert_not_called()
            urlopen.assert_not_called()
            open_file.assert_not_called()
            self.assertNotIn(SECRET_MARKER.encode(), body)

    def test_disabled_telegram_route_remains_404_before_csrf_or_work(self):
        with test_environment(ENABLE_TELEGRAM_ACTIONS=""):
            session = admin_app.sign_admin_session()
            with patch.object(admin_app, "get_db_connection") as get_connection:
                with patch.object(admin_app.urllib.request, "urlopen") as urlopen:
                    status, _headers, body = run_asgi_request(
                        "POST",
                        "/channel/1/send",
                        form={},
                        cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                    )
            self.assertEqual(status, 404)
            self.assertEqual(body, b"Not Found")
            get_connection.assert_not_called()
            urlopen.assert_not_called()

    def test_marker_and_token_never_enter_logs_url_or_generic_rejection(self):
        with test_environment():
            session = admin_app.sign_admin_session()
            stdout = io.StringIO()
            stderr = io.StringIO()
            logs = io.StringIO()
            handler = logging.StreamHandler(logs)
            logging.getLogger().addHandler(handler)
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status, headers, body = run_asgi_request(
                        "POST",
                        "/products/new",
                        form={},
                        cookies={admin_app.ADMIN_SESSION_COOKIE: session},
                        query_string="csrf_token=" + SECRET_MARKER,
                    )
            finally:
                logging.getLogger().removeHandler(handler)
        self.assertEqual(status, 403)
        observed = stdout.getvalue() + stderr.getvalue() + logs.getvalue() + body.decode()
        self.assertNotIn(SECRET_MARKER, observed)
        self.assertFalse(any(SECRET_MARKER.encode() in value for _name, value in headers))


if __name__ == "__main__":
    unittest.main()
