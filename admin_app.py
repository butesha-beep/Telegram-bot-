import os
import base64
import binascii
import logging
import json
import html
import csv
import io
import math
import re
from html import escape
import hmac
import hashlib
import secrets
import time
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
import psycopg2

try:
    from db_schema import (
        DATABASE_URL,
        catalog_schema_is_compatible,
        get_db_connection,
        ORDER_PAYMENT_STATUS_VALUES,
        ORDER_FULFILLMENT_STATUS_VALUES,
        ORDER_SOURCE_VALUES,
        ORDER_DELIVERY_METHOD_VALUES,
    )
except ValueError:
    if os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL"):
        raise
    DATABASE_URL = None

    def get_db_connection():
        raise RuntimeError("Database is unavailable")

    def catalog_schema_is_compatible():
        return False

    ORDER_PAYMENT_STATUS_VALUES = ("unpaid", "payment_reported", "paid", "refunded")
    ORDER_FULFILLMENT_STATUS_VALUES = (
        "new", "confirmed", "picking", "packed",
        "ready_to_ship", "shipped", "delivered", "cancelled",
    )
    ORDER_SOURCE_VALUES = (
        "telegram", "website", "instagram", "tiktok",
        "whatsapp", "viber", "in_person", "other",
    )
    ORDER_DELIVERY_METHOD_VALUES = ("pickup", "delivery")

from runtime_settings import env_flag_enabled, get_app_env
from csrf_security import (
    csrf_token_timestamps,
    issue_csrf_token,
    validate_csrf_configuration,
    validate_csrf_token,
)
from order_creation import OrderCreationError, insert_order, price_single_line
from shop_settings import ADMIN_PANEL_TITLE, CURRENCY_SYMBOL
from storefront import router as storefront_router, safe_image_url

logger = logging.getLogger(__name__)
app = FastAPI()
app.include_router(storefront_router)
DATABASE_READY = False
ADMIN_SESSION_COOKIE = "admin_session"
ADMIN_SESSION_MAX_AGE = 86400
MASTER_SESSION_COOKIE = "master_session"
MASTER_SESSION_MAX_AGE = 86400
ADMIN_PREAUTH_CSRF_COOKIE = "admin_preauth_csrf"
MASTER_PREAUTH_CSRF_COOKIE = "master_preauth_csrf"
PREAUTH_CSRF_MAX_AGE = 600
AUTHENTICATED_CSRF_MAX_AGE = 3600
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_TOKEN_MAX_LENGTH = 256
URLENCODED_BODY_MAX_LENGTH = 262144
URLENCODED_FORM_MAX_FIELDS = 128
UNSAFE_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CURRENT_REQUEST = ContextVar("admin_current_request", default=None)
SESSION_ACCOUNT_KEY = "primary"
SESSION_CREDENTIAL_PREFIX = "ws1."
SESSION_CREDENTIAL_RANDOM_LENGTH = 64
SESSION_CREDENTIAL_PATTERN = re.compile(r"ws1\.[A-Za-z0-9_-]{64}")
PREAUTH_NONCE_PREFIX = "pn1."
PREAUTH_NONCE_RANDOM_LENGTH = 43
PREAUTH_NONCE_PATTERN = re.compile(r"pn1\.[A-Za-z0-9_-]{43}")
SECURITY_CLEANUP_BATCH_SIZE = 100
PREVIEW_GATE_REALM = "Deal Market Preview"
LOW_STOCK_SYNC_FAILED = "low_stock_sync_failed"
ORDER_NOTIFICATION_FAILED = "order_notification_failed"
ORDER_PAYMENT_UPDATE_FAILED = "order_payment_update_failed"
ORDER_FULFILLMENT_UPDATE_FAILED = "order_fulfillment_update_failed"
MANUAL_ORDER_CREATE_FAILED = "manual_order_create_failed"
MANUAL_ORDER_FORM_FAILED = "manual_order_form_failed"
PICKING_WORKSPACE_LOAD_FAILED = "picking_workspace_load_failed"
WEIGHING_UPDATE_FAILED = "weighing_update_failed"
WEIGHING_NOTIFICATION_FAILED = "weighing_notification_failed"
ORDER_NOTE_UPDATE_FAILED = "order_note_update_failed"
CLIENT_NOTE_UPDATE_FAILED = "client_note_update_failed"
CHANNEL_POST_FAILED = "channel_post_failed"
ADMIN_ERROR_LOG_WRITE_FAILED = "admin_error_log_write_failed"
TELEGRAM_OPERATION_FAILED = "telegram_operation_failed"
INTERNAL_OPERATION_FAILED = "internal_operation_failed"
DASHBOARD_LOAD_FAILED = "dashboard_load_failed"
WEB_SESSION_LOOKUP_FAILED = "web_session_lookup_failed"
WEB_SESSION_CREATE_FAILED = "web_session_create_failed"
WEB_SESSION_REVOKE_FAILED = "web_session_revoke_failed"

SECURITY_SESSION_CLEANUP_SQL = """
    WITH expired AS (
        SELECT id
        FROM web_sessions
        WHERE expires_at <= CURRENT_TIMESTAMP
        ORDER BY expires_at, id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM web_sessions
    WHERE id IN (SELECT id FROM expired)
"""
SECURITY_NONCE_CLEANUP_SQL = """
    WITH expired AS (
        SELECT id
        FROM consumed_login_nonces
        WHERE expires_at <= CURRENT_TIMESTAMP
        ORDER BY expires_at, id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM consumed_login_nonces
    WHERE id IN (SELECT id FROM expired)
"""
SECURITY_SESSION_CREATE_SQL = """
    WITH consumed AS (
        INSERT INTO consumed_login_nonces (
            role, nonce_hash, issued_at, expires_at, consumed_at
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (role, nonce_hash) DO NOTHING
        RETURNING id
    ), revoked AS (
        UPDATE web_sessions
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE role = %s
          AND account_key = %s
          AND revoked_at IS NULL
          AND EXISTS (SELECT 1 FROM consumed)
        RETURNING id
    )
    INSERT INTO web_sessions (
        role, account_key, token_hash, issued_at, expires_at
    )
    SELECT %s, %s, %s, %s, %s
    FROM consumed
    WHERE (SELECT COUNT(*) FROM revoked) >= 0
    RETURNING id
"""
SECURITY_SESSION_LOOKUP_SQL = """
    SELECT id
    FROM web_sessions
    WHERE role = %s
      AND account_key = %s
      AND token_hash = %s
      AND revoked_at IS NULL
      AND expires_at > CURRENT_TIMESTAMP
    LIMIT 1
"""
SECURITY_SESSION_REVOKE_SQL = """
    UPDATE web_sessions
    SET revoked_at = CURRENT_TIMESTAMP
    WHERE role = %s
      AND account_key = %s
      AND token_hash = %s
      AND revoked_at IS NULL
      AND expires_at > CURRENT_TIMESTAMP
    RETURNING id
"""
ADMIN_OPERATION_ERROR_CODES = frozenset({
    LOW_STOCK_SYNC_FAILED,
    ORDER_NOTIFICATION_FAILED,
    ORDER_PAYMENT_UPDATE_FAILED,
    ORDER_FULFILLMENT_UPDATE_FAILED,
    MANUAL_ORDER_CREATE_FAILED,
    MANUAL_ORDER_FORM_FAILED,
    PICKING_WORKSPACE_LOAD_FAILED,
    WEIGHING_UPDATE_FAILED,
    WEIGHING_NOTIFICATION_FAILED,
    ORDER_NOTE_UPDATE_FAILED,
    CLIENT_NOTE_UPDATE_FAILED,
    CHANNEL_POST_FAILED,
    ADMIN_ERROR_LOG_WRITE_FAILED,
    TELEGRAM_OPERATION_FAILED,
    "broadcast_list_failed",
    "broadcast_form_failed",
    "broadcast_create_failed",
    "broadcast_send_failed",
    "channel_list_failed",
    "channel_form_failed",
    "channel_create_failed",
    "channel_delete_failed",
    DASHBOARD_LOAD_FAILED,
    INTERNAL_OPERATION_FAILED,
    "master_dashboard_load_failed",
    "master_shop_detail_load_failed",
    "master_shop_seed_failed",
    "master_snapshot_failed",
    "error_log_list_failed",
    "error_log_detail_failed",
    "order_list_failed",
    "order_detail_failed",
    "product_list_failed",
    "product_create_failed",
    "product_recommendations_load_failed",
    "product_recommendations_update_failed",
    "product_update_failed",
})


@app.on_event("startup")
async def startup_db_init():
    global DATABASE_READY
    DATABASE_READY = False
    validate_preview_gate_configuration()
    validate_csrf_configuration()
    if not DATABASE_URL:
        refresh_database_readiness()
        logger.error("Database is not configured")
        return
    if not refresh_database_readiness():
        logger.error("Database schema is unavailable or incompatible")

PAGE_STYLE = """
<style>
  body { font-family: Arial, sans-serif; background: #f4f6f8; color: #2b2b2b; margin: 0; padding: 0; }
  .container { max-width: 1100px; margin: 30px auto; padding: 24px; background: #ffffff; box-shadow: 0 10px 24px rgba(0,0,0,0.08); border-radius: 12px; }
  h1, h2 { margin: 0 0 16px; color: #1f2937; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border: 1px solid #d1d5db; padding: 12px 14px; }
  th { background: #f3f4f6; text-align: left; }
  tr:nth-child(even) td { background: #f9fafb; }
  .button, .button-link { display: inline-block; padding: 7px 12px; margin: 2px 4px 2px 0; color: #ffffff; background: #2563eb; text-decoration: none; border-radius: 6px; border: none; cursor: pointer; font-size: 13px; }
  .button.secondary { background: #6b7280; }
  .card { padding: 18px; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 10px; margin-top: 16px; }
  .field { margin: 10px 0; }
  .field strong { color: #374151; }
  .status { font-weight: 700; color: #0f172a; }
  a { color: #2563eb; }
</style>
"""

def admin_css():
    return """
<style>
  :root {
    --bg: #121212;
    --panel: #1c1c1f;
    --panel-soft: #252529;
    --text: #f7f7f7;
    --muted: #b7b7bd;
    --line: #34343a;
    --accent: #f97316;
    --accent-hover: #ea580c;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body {
    margin: 0;
    min-height: 100vh;
    font-family: Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { color: var(--accent-hover); }
  .admin-shell { min-height: 100vh; }
  .admin-topbar {
    border-bottom: 1px solid var(--line);
    background: #18181b;
  }
  .admin-nav {
    max-width: 1180px;
    margin: 0 auto;
    padding: 14px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 24px;
  }
  .admin-brand { font-weight: 700; letter-spacing: 0; color: var(--text); }
  .admin-links { display: flex; flex-wrap: wrap; gap: 14px; }
  .admin-links a { color: var(--muted); font-size: 14px; }
  .admin-links a:hover { color: var(--accent); }
  .nav-logout-form { display: inline-flex; margin: 0; padding: 0; }
  .nav-logout-button {
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 14px;
    font-weight: 400;
  }
  .nav-logout-button:hover { color: var(--accent); }
  .admin-container {
    max-width: 1180px;
    margin: 0 auto;
    padding: 34px 28px;
  }
  .admin-card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 24px;
  }
  h1, h2 { margin: 0 0 16px; color: var(--text); }
  p { color: var(--muted); line-height: 1.5; }
  table { width: 100%; border-collapse: collapse; margin-top: 16px; }
  th, td { border-bottom: 1px solid var(--line); padding: 12px 14px; text-align: left; }
  th { background: var(--panel-soft); color: var(--text); }
  .button, .button-link, button {
    display: inline-block;
    padding: 8px 12px;
    border: 0;
    border-radius: 6px;
    background: var(--accent);
    color: #111111;
    font-weight: 700;
    cursor: pointer;
  }
  .button.secondary { background: #3f3f46; color: var(--text); }
  .status {
    display: inline-block;
    padding: 4px 8px;
    border: 1px solid rgba(249, 115, 22, 0.45);
    border-radius: 999px;
    background: rgba(249, 115, 22, 0.12);
    color: var(--accent);
    font-size: 13px;
    font-weight: 700;
    white-space: nowrap;
  }
  .status.warning {
    border-color: rgba(245, 158, 11, 0.45);
    background: rgba(245, 158, 11, 0.14);
    color: #fbbf24;
  }
  .status.info {
    border-color: rgba(56, 189, 248, 0.45);
    background: rgba(56, 189, 248, 0.12);
    color: #38bdf8;
  }
  .status.success {
    border-color: rgba(34, 197, 94, 0.45);
    background: rgba(34, 197, 94, 0.12);
    color: #4ade80;
  }
  .status.danger {
    border-color: rgba(248, 113, 113, 0.45);
    background: rgba(248, 113, 113, 0.12);
    color: #f87171;
  }
  .status.neutral {
    border-color: rgba(161, 161, 170, 0.35);
    background: rgba(161, 161, 170, 0.12);
    color: #d4d4d8;
  }
  .action-group {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 5px;
    min-width: 0;
    max-width: 190px;
  }
  .action-group form { display: inline-flex !important; margin: 0 !important; padding: 0 !important; }
  .action-group .button {
    margin: 0;
    padding: 5px 7px;
    font-size: 11px;
    line-height: 1.1;
    white-space: nowrap;
  }
  .dash-hero {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 24px;
    margin-bottom: 22px;
  }
  .dash-kicker {
    margin: 0 0 8px;
    color: var(--accent);
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .dash-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 18px;
  }
  .dash-grid > * { min-width: 0; }
  .dash-card {
    display: block;
    min-width: 0;
    max-width: 100%;
    min-height: 118px;
    padding: 18px;
    border: 1px solid var(--line);
    border-top: 3px solid var(--accent);
    border-radius: 10px;
    background: var(--panel);
    color: var(--text);
  }
  .dash-card:hover { border-color: var(--accent); color: var(--text); }
  .dash-card strong { display: block; margin-bottom: 8px; font-size: 18px; }
  .dash-card span { color: var(--muted); font-size: 14px; }
  .dash-card.priority-urgent { border-top-color: #ef4444; }
  .dash-card.priority-today { border-top-color: #eab308; }
  .dash-card.priority-later { border-top-color: #3b82f6; }
  .attention-banner {
    margin: 16px 0;
    padding: 14px 16px;
    border: 1px solid rgba(245, 158, 11, 0.45);
    border-left: 4px solid var(--accent);
    border-radius: 8px;
    background: rgba(245, 158, 11, 0.10);
    color: var(--text);
    font-weight: 700;
  }
  tr.attention-row td {
    background: rgba(245, 158, 11, 0.08);
    border-bottom-color: rgba(245, 158, 11, 0.24);
  }
  .stat-value { display: block; margin-top: 10px; font-size: 28px; font-weight: 700; color: var(--text); }
  .dash-section { margin-top: 22px; }
  .dash-table-wrap {
    width: 100%;
    min-width: 0;
    max-width: 100%;
    overflow-x: auto;
    overscroll-behavior-x: contain;
  }
  .analytics-mobile-list { display: none; }
  .analytics-mobile-row {
    min-width: 0;
    padding: 11px 0;
    border-bottom: 1px solid var(--line);
  }
  .analytics-mobile-row:last-child { border-bottom: 0; }
  .analytics-mobile-row dl { display: grid; gap: 8px; margin: 0; }
  .analytics-mobile-field {
    display: grid;
    grid-template-columns: minmax(76px, 0.7fr) minmax(0, 1.3fr);
    gap: 10px;
  }
  .analytics-mobile-field dt { color: var(--muted); font-size: 12px; }
  .analytics-mobile-field dd {
    min-width: 0;
    margin: 0;
    overflow-wrap: anywhere;
    font-weight: 700;
  }
  .view-link { font-weight: 700; white-space: nowrap; }
  .detail-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }
  .detail-field {
    padding: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel-soft);
  }
  .picking-queue {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 14px;
  }
  .picking-order-card { margin: 0; }
  .picking-items {
    margin: 14px 0;
    padding-left: 18px;
  }
  .picking-items li { margin-bottom: 6px; }
  .detail-field strong {
    display: block;
    margin-bottom: 6px;
    color: var(--muted);
    font-size: 13px;
  }
  .products-table {
    min-width: 860px;
  }
  .products-table th,
  .products-table td {
    vertical-align: middle;
  }
  .products-table td:nth-child(2) {
    min-width: 180px;
    font-weight: 700;
  }
  .products-table td:nth-child(3) {
    white-space: nowrap;
  }
  .product-thumb {
    display: block;
    width: 72px;
    height: 54px;
    object-fit: cover;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-soft);
  }
  .product-thumb-wrap { display: inline-block; }
  .product-thumb-placeholder {
    display: grid;
    place-items: center;
    padding: 4px;
    color: var(--muted);
    font-size: 10px;
    font-weight: 700;
    line-height: 1.15;
    text-align: center;
    white-space: normal;
  }
  .status.active {
    border-color: rgba(34, 197, 94, 0.45);
    background: rgba(34, 197, 94, 0.12);
    color: #4ade80;
  }
  .status.inactive {
    border-color: rgba(148, 163, 184, 0.35);
    background: rgba(148, 163, 184, 0.12);
    color: #cbd5e1;
  }
  .products-table .action-group {
    max-width: 170px;
  }
  .category-row td {
    padding: 14px;
    background: rgba(249, 115, 22, 0.10);
    color: var(--text);
    font-size: 16px;
    font-weight: 700;
  }
  .quick-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 16px 0;
  }
  .quick-nav a {
    padding: 7px 10px;
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--panel-soft);
    color: var(--text);
    font-size: 13px;
    font-weight: 700;
  }
  .quick-nav a:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .mobile-quick-nav,
  .mobile-products-list { display: none; }
  .mobile-product-category { min-width: 0; }
  .mobile-product-category h2 {
    margin: 0 0 10px;
    font-size: 17px;
  }
  .mobile-product-cards { display: grid; gap: 12px; }
  .mobile-product-card {
    min-width: 0;
    padding: 16px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel-soft);
  }
  .mobile-product-head {
    display: grid;
    grid-template-columns: 72px minmax(0, 1fr);
    gap: 12px;
    align-items: start;
  }
  .mobile-product-name {
    margin: 3px 0 0;
    overflow-wrap: anywhere;
    font-size: 17px;
    line-height: 1.35;
  }
  .mobile-product-id { color: var(--muted); font-size: 12px; font-weight: 700; }
  .mobile-product-details {
    display: grid;
    gap: 0;
    margin: 14px 0;
  }
  .mobile-product-details div {
    display: grid;
    grid-template-columns: minmax(92px, 0.8fr) minmax(0, 1.2fr);
    gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid var(--line);
  }
  .mobile-product-details dt { color: var(--muted); font-size: 13px; }
  .mobile-product-details dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
  .categories-table {
    min-width: 640px;
  }
  .categories-table th,
  .categories-table td {
    vertical-align: middle;
  }
  .categories-table td:nth-child(2) {
    min-width: 180px;
    font-weight: 700;
  }
  .categories-table .action-group {
    max-width: 140px;
  }
  .admin-form {
    display: grid;
    gap: 16px;
    max-width: 720px;
  }
  .admin-form label {
    display: grid;
    gap: 7px;
    color: var(--muted);
    font-size: 14px;
    font-weight: 700;
  }
  .admin-form input {
    width: 100%;
    padding: 11px 12px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--panel-soft);
    color: var(--text);
    font: inherit;
  }
  .admin-form input[type="checkbox"] {
    width: auto;
    accent-color: var(--accent);
  }
  .form-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    margin-top: 4px;
  }
  @media (max-width: 720px) {
    body { overflow-x: hidden; }
    .admin-shell, .admin-topbar, .admin-container, .admin-card {
      width: 100%;
      min-width: 0;
      max-width: 100%;
    }
    .admin-card, .dash-card, .attention-banner, h1, h2, h3, p {
      overflow-wrap: anywhere;
    }
    .admin-nav { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
    .admin-links {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      flex-wrap: nowrap;
      gap: 16px;
      overflow-x: auto;
      padding-bottom: 6px;
      overscroll-behavior-x: contain;
    }
    .admin-links a { flex: none; }
    .admin-container { padding: 22px 18px; }
    .admin-card { padding: 18px; }
    .dash-hero { align-items: flex-start; flex-direction: column; }
    .dash-grid { width: 100%; min-width: 0; grid-template-columns: minmax(0, 1fr); }
    .dash-grid > *, .dash-card > * { min-width: 0; max-width: 100%; }
    .detail-grid { grid-template-columns: 1fr; }
    .action-group { min-width: 0; max-width: 100%; flex-direction: column; }
    .action-group form { min-width: 0; max-width: 100%; }
    .action-group .button { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
    .dash-table-wrap {
      width: 100%;
      min-width: 0;
      max-width: 100%;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
    .dash-table-wrap > table {
      width: max-content;
      min-width: 100%;
      max-width: none;
      white-space: nowrap;
    }
    .analytics-desktop-table { display: none; }
    .analytics-mobile-list { display: grid; min-width: 0; max-width: 100%; }
    .desktop-quick-nav,
    .products-desktop-table { display: none; }
    .mobile-quick-nav { display: flex; }
    .mobile-quick-nav a { min-width: 0; overflow-wrap: anywhere; }
    .mobile-products-list { display: grid; gap: 22px; }
    .mobile-product-card .action-group {
      display: grid;
      width: 100%;
      max-width: none;
      grid-template-columns: 1fr;
      gap: 8px;
    }
    .mobile-product-card .action-group a,
    .mobile-product-card .action-group form,
    .mobile-product-card .action-group button {
      width: 100%;
      min-height: 44px;
      margin: 0;
      text-align: center;
    }
  }
  @media (max-width: 380px) {
    .admin-nav { padding-inline: 12px; }
    .admin-container { padding: 18px 12px; }
    .admin-card { padding: 14px; }
  }
</style>
"""


def _csrf_hidden_input(role):
    request = _CURRENT_REQUEST.get()
    token = authenticated_csrf_token(request, role) if request is not None else ""
    return (
        f'<input type="hidden" name="{CSRF_FORM_FIELD}" '
        f'value="{html.escape(token, quote=True)}">'
    )


def _protect_post_forms(page, role):
    hidden_input = _csrf_hidden_input(role)
    post_form = re.compile(
        r"(<form\b(?=[^>]*\bmethod\s*=\s*(['\"])post\2)[^>]*>)",
        re.IGNORECASE,
    )
    return post_form.sub(lambda match: match.group(1) + hidden_input, page)


def csrf_multipart_script():
    return f"""
<script>
document.addEventListener("submit", async function (event) {{
  const form = event.target.closest('form[data-csrf-multipart="header"]');
  if (!form) return;
  event.preventDefault();
  const tokenInput = form.querySelector('input[name="{CSRF_FORM_FIELD}"]');
  if (!tokenInput || !tokenInput.value) return;
  const submitButton = form.querySelector('button[type="submit"]');
  if (submitButton) submitButton.disabled = true;
  try {{
    const response = await fetch(form.action, {{
      method: "POST",
      body: new FormData(form),
      credentials: "same-origin",
      headers: {{ "{CSRF_HEADER}": tokenInput.value }}
    }});
    if (response.redirected) {{
      window.location.assign(response.url);
      return;
    }}
    const page = await response.text();
    document.open();
    document.write(page);
    document.close();
  }} catch (_error) {{
    window.alert("Request failed. Please try again.");
    if (submitButton) submitButton.disabled = false;
  }}
}});
</script>
"""


def admin_layout(title, content, refresh_seconds=None):
    refresh_meta = ""
    if refresh_seconds:
        refresh_meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
    page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{title}</title>
  {admin_css()}
</head>
<body>
  <div class="admin-shell">
    <header class="admin-topbar">
      <nav class="admin-nav">
        <a class="admin-brand" href="/admin">🏠 Главная</a>
        <div class="admin-links">
          <a href="/orders">📦 Заказы</a>
          <a href="/picking">📦 Сборка</a>
          <a href="/products">🛒 Товары</a>
          <a href="/categories">🗂 Категории</a>
          <a href="/clients">👥 Клиенты</a>
          <a href="/broadcasts">📨 Рассылка</a>
          <a href="/channel">📢 Канал</a>
          <a href="/logs">🧾 Логи</a>
          <form class="nav-logout-form" method="post" action="/logout">
            <button class="nav-logout-button" type="submit">Выйти</button>
          </form>
        </div>
      </nav>
    </header>
    <main class="admin-container">{content}</main>
  </div>
  {csrf_multipart_script()}
</body>
</html>"""
    return _protect_post_forms(page, "admin")


def admin_error_page(title, message):
    content = f"""
    <section class="admin-card">
      <h1>{html.escape(str(title))}</h1>
      <p>{html.escape(str(message))}</p>
      <p><a class="button button-link" href="/">На главную</a></p>
    </section>
    """
    return admin_layout(title, content)


def master_layout(title, content, refresh_seconds=None):
    refresh_meta = ""
    if refresh_seconds:
        refresh_meta = f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
    page = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh_meta}
  <title>{html.escape(str(title))}</title>
  {admin_css()}
</head>
<body>
  <div class="admin-shell">
    <header class="admin-topbar">
      <nav class="admin-nav">
        <a class="admin-brand" href="/master">Master Admin</a>
        <div class="admin-links">
          <a href="/master">Shops</a>
          <form class="nav-logout-form" method="post" action="/master/logout">
            <button class="nav-logout-button" type="submit">Logout</button>
          </form>
        </div>
      </nav>
    </header>
    <main class="admin-container">{content}</main>
  </div>
</body>
</html>"""
    return _protect_post_forms(page, "master")


def master_error_page(title, message):
    content = f"""
    <section class="admin-card">
      <h1>{html.escape(str(title))}</h1>
      <p>{html.escape(str(message))}</p>
      <p><a class="button button-link" href="/master">Back to Master Dashboard</a></p>
    </section>
    """
    return master_layout(title, content)


def admin_auth_configured():
    return bool(os.getenv("ADMIN_PASSWORD") and os.getenv("ADMIN_SESSION_SECRET"))


def preview_gate_enabled():
    return env_flag_enabled("PREVIEW_GATE_ENABLED")


def master_admin_enabled():
    return env_flag_enabled("ENABLE_MASTER_ADMIN")


def telegram_actions_enabled():
    return env_flag_enabled("ENABLE_TELEGRAM_ACTIONS")


def preview_gate_required():
    app_env = get_app_env()
    gate_enabled = preview_gate_enabled()
    if app_env == "preview" and not gate_enabled:
        raise RuntimeError("Preview access gate is not configured")
    return gate_enabled


def _is_trivially_repeated(value):
    for size in range(1, len(value) // 2 + 1):
        if len(value) % size == 0 and value[:size] * (len(value) // size) == value:
            return True
    return False


def _contains_sequential_pattern(value):
    sequences = (
        "abcdefghijklmnopqrstuvwxyz",
        "zyxwvutsrqponmlkjihgfedcba",
        "0123456789",
        "9876543210",
        "qwertyuiop",
        "poiuytrewq",
        "asdfghjkl",
        "lkjhgfdsa",
        "zxcvbnm",
        "mnbvcxz",
    )
    normalized = value.casefold()
    return any(
        sequence[index:index + 6] in normalized
        for sequence in sequences
        for index in range(len(sequence) - 5)
    )


def _preview_gate_password_is_strong(username, password):
    if not 32 <= len(password) <= 256:
        return False
    if password != password.strip() or any(character.isspace() for character in password):
        return False
    if not all(33 <= ord(character) <= 126 for character in password):
        return False
    if password.casefold() == username.casefold() or _is_trivially_repeated(password):
        return False

    character_classes = (
        any(character.islower() for character in password),
        any(character.isupper() for character in password),
        any(character.isdigit() for character in password),
        any(not character.isalnum() for character in password),
    )
    if sum(character_classes) < 4 or len(set(password)) < 12:
        return False

    if _contains_sequential_pattern(password):
        return False

    normalized = password.casefold()
    forbidden_patterns = (
        "password",
        "letmein",
        "qwerty",
        "administrator",
        "dealmarket",
        "123456",
        "654321",
        "abcdef",
        "fedcba",
    )
    return not any(pattern in normalized for pattern in forbidden_patterns)


def _preview_gate_credentials():
    username = os.getenv("PREVIEW_GATE_USERNAME", "")
    password = os.getenv("PREVIEW_GATE_PASSWORD", "")
    username_valid = bool(re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username))
    password_valid = username_valid and _preview_gate_password_is_strong(
        username, password
    )
    if not username_valid or not password_valid:
        raise RuntimeError("Preview access gate is not configured")
    return username, password


def validate_preview_gate_configuration():
    if preview_gate_required():
        _preview_gate_credentials()


def _preview_basic_credentials(authorization):
    if not authorization:
        return None
    try:
        scheme, encoded = authorization.split(" ", 1)
        if scheme.lower() != "basic" or not encoded:
            return None
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return username, password
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def preview_gate_authenticated(request):
    try:
        expected_username, expected_password = _preview_gate_credentials()
    except RuntimeError:
        return False
    provided = _preview_basic_credentials(request.headers.get("authorization"))
    if provided is None:
        return False
    username, password = provided
    username_matches = hmac.compare_digest(
        username.encode("utf-8"), expected_username.encode("utf-8")
    )
    password_matches = hmac.compare_digest(
        password.encode("utf-8"), expected_password.encode("utf-8")
    )
    return username_matches and password_matches


def preview_gate_unauthorized():
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={
            "WWW-Authenticate": f'Basic realm="{PREVIEW_GATE_REALM}", charset="UTF-8"'
        },
    )


def generic_not_found():
    return PlainTextResponse("Not Found", status_code=404)


def refresh_database_readiness():
    global DATABASE_READY
    DATABASE_READY = False
    if not DATABASE_URL:
        return False
    DATABASE_READY = bool(catalog_schema_is_compatible())
    return DATABASE_READY


def telegram_write_route_disabled(request):
    if telegram_actions_enabled() or request.method in {"GET", "HEAD", "OPTIONS"}:
        return False
    return request.url.path.startswith(("/broadcasts", "/channel"))


def master_auth_configured():
    return bool(os.getenv("MASTER_ADMIN_PASSWORD") and os.getenv("MASTER_ADMIN_SESSION_SECRET"))


def _session_secret_for_role(role):
    if role == "admin":
        return os.getenv("ADMIN_SESSION_SECRET", "")
    if role == "master":
        return os.getenv("MASTER_ADMIN_SESSION_SECRET", "")
    return ""


def _security_value_hash(kind, role, value):
    secret = _session_secret_for_role(role)
    if kind not in {"session", "preauth-nonce"} or not secret or not value:
        raise ValueError("Invalid security value")
    message = (
        b"dealmarket:web-security:v1\0"
        + kind.encode("ascii")
        + b"\0"
        + role.encode("ascii")
        + b"\0"
        + value.encode("ascii")
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()


def _new_session_credential():
    random_value = secrets.token_urlsafe(48)
    if len(random_value) != SESSION_CREDENTIAL_RANDOM_LENGTH:
        raise RuntimeError("Session generation failed")
    return SESSION_CREDENTIAL_PREFIX + random_value


def sign_admin_session():
    return _new_session_credential()


def sign_master_session():
    return _new_session_credential()


def session_credential_is_well_formed(value):
    return isinstance(value, str) and bool(SESSION_CREDENTIAL_PATTERN.fullmatch(value))


def server_session_is_active(role, value, connection_factory=None):
    if role not in {"admin", "master"} or not session_credential_is_well_formed(value):
        return False
    if not _session_secret_for_role(role):
        return False
    if connection_factory is None:
        connection_factory = get_db_connection
    connection = None
    cursor = None
    try:
        token_hash = _security_value_hash("session", role, value)
        connection = connection_factory()
        connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor()
        cursor.execute("BEGIN TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '3000ms'")
        cursor.execute("SET LOCAL lock_timeout = '1000ms'")
        cursor.execute(
            SECURITY_SESSION_LOOKUP_SQL,
            (role, SESSION_ACCOUNT_KEY, token_hash),
        )
        return cursor.fetchone() is not None
    except Exception:
        logger.error(WEB_SESSION_LOOKUP_FAILED)
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


def verify_admin_session(value):
    return admin_auth_configured() and server_session_is_active("admin", value)


def verify_master_session(value):
    return master_auth_configured() and server_session_is_active("master", value)


def _request_session_value(request, role):
    checked_name = f"_{role}_session_checked"
    value_name = f"_{role}_session_value"
    if getattr(request.state, checked_name, False):
        return getattr(request.state, value_name, "")
    cookie_name = ADMIN_SESSION_COOKIE if role == "admin" else MASTER_SESSION_COOKIE
    value = request.cookies.get(cookie_name, "")
    active = server_session_is_active(role, value)
    setattr(request.state, checked_name, True)
    setattr(request.state, value_name, value if active else "")
    return value if active else ""


def is_admin_authenticated(request):
    return bool(admin_auth_configured() and _request_session_value(request, "admin"))


def is_master_authenticated(request):
    return bool(master_auth_configured() and _request_session_value(request, "master"))


def csrf_cookie_secure():
    return get_app_env() in {"preview", "production"}


def _csrf_session_value(request, role):
    if role not in {"admin", "master"}:
        return ""
    return _request_session_value(request, role)


def create_authenticated_session(
    role,
    preauth_nonce,
    preauth_issued_at,
    preauth_expires_at,
    connection_factory=None,
    current_time=None,
):
    now_timestamp = int(time.time() if current_time is None else current_time)
    if role not in {"admin", "master"}:
        return "failed", None
    if not isinstance(preauth_nonce, str) or not PREAUTH_NONCE_PATTERN.fullmatch(
        preauth_nonce
    ):
        return "failed", None
    if not (
        isinstance(preauth_issued_at, int)
        and isinstance(preauth_expires_at, int)
        and preauth_issued_at <= now_timestamp < preauth_expires_at
        and preauth_expires_at - preauth_issued_at <= PREAUTH_CSRF_MAX_AGE
    ):
        return "failed", None
    if connection_factory is None:
        connection_factory = get_db_connection

    connection = None
    cursor = None
    try:
        credential = _new_session_credential()
        nonce_hash = _security_value_hash("preauth-nonce", role, preauth_nonce)
        session_hash = _security_value_hash("session", role, credential)
        preauth_issued = datetime.fromtimestamp(preauth_issued_at, timezone.utc)
        preauth_expires = datetime.fromtimestamp(preauth_expires_at, timezone.utc)
        session_issued = datetime.fromtimestamp(now_timestamp, timezone.utc)
        session_max_age = (
            ADMIN_SESSION_MAX_AGE if role == "admin" else MASTER_SESSION_MAX_AGE
        )
        session_expires = session_issued + timedelta(seconds=session_max_age)

        connection = connection_factory()
        connection.set_session(readonly=False, autocommit=False)
        cursor = connection.cursor()
        cursor.execute("SET LOCAL statement_timeout = '3000ms'")
        cursor.execute("SET LOCAL lock_timeout = '1000ms'")
        cursor.execute(
            SECURITY_SESSION_CLEANUP_SQL,
            (SECURITY_CLEANUP_BATCH_SIZE,),
        )
        cursor.execute(
            SECURITY_NONCE_CLEANUP_SQL,
            (SECURITY_CLEANUP_BATCH_SIZE,),
        )
        cursor.execute(
            SECURITY_SESSION_CREATE_SQL,
            (
                role,
                nonce_hash,
                preauth_issued,
                preauth_expires,
                session_issued,
                role,
                SESSION_ACCOUNT_KEY,
                role,
                SESSION_ACCOUNT_KEY,
                session_hash,
                session_issued,
                session_expires,
            ),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return "replayed", None
        connection.commit()
        return "created", credential
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        logger.error(WEB_SESSION_CREATE_FAILED)
        return "failed", None
    finally:
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


def revoke_authenticated_session(role, credential, connection_factory=None):
    if role not in {"admin", "master"} or not session_credential_is_well_formed(
        credential
    ):
        return False
    if connection_factory is None:
        connection_factory = get_db_connection
    connection = None
    cursor = None
    try:
        token_hash = _security_value_hash("session", role, credential)
        connection = connection_factory()
        connection.set_session(readonly=False, autocommit=False)
        cursor = connection.cursor()
        cursor.execute("SET LOCAL statement_timeout = '3000ms'")
        cursor.execute("SET LOCAL lock_timeout = '1000ms'")
        cursor.execute(
            SECURITY_SESSION_REVOKE_SQL,
            (role, SESSION_ACCOUNT_KEY, token_hash),
        )
        if cursor.fetchone() is None:
            connection.rollback()
            return False
        connection.commit()
        return True
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
        logger.error(WEB_SESSION_REVOKE_FAILED)
        return False
    finally:
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


def authenticated_csrf_token(request, role):
    if request is None:
        return ""
    session_value = _csrf_session_value(request, role)
    if not session_value:
        return ""
    try:
        return issue_csrf_token(
            f"authenticated:{role}",
            session_value,
            AUTHENTICATED_CSRF_MAX_AGE,
        )
    except (RuntimeError, ValueError):
        return ""


def _header_csrf_token(request):
    values = request.headers.getlist(CSRF_HEADER)
    if len(values) != 1:
        return ""
    token = values[0]
    if not isinstance(token, str) or not 1 <= len(token) <= CSRF_TOKEN_MAX_LENGTH:
        return ""
    return token


async def _limited_urlencoded_body(request):
    try:
        if hasattr(request, "_body"):
            body = request._body
            return body if len(body) <= URLENCODED_BODY_MAX_LENGTH else None
        content_length = request.headers.get("content-length")
        if content_length is not None:
            declared_length = int(content_length)
            if declared_length < 0 or declared_length > URLENCODED_BODY_MAX_LENGTH:
                return None
        body_parts = []
        total_length = 0
        async for chunk in request.stream():
            total_length += len(chunk)
            if total_length > URLENCODED_BODY_MAX_LENGTH:
                return None
            body_parts.append(chunk)
        body = b"".join(body_parts)
        request._body = body
        return body
    except (RuntimeError, TypeError, ValueError):
        return None


async def _submitted_csrf_token(request):
    content_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if content_type in {"application/json", "multipart/form-data"}:
        return _header_csrf_token(request)
    if content_type != "application/x-www-form-urlencoded":
        return ""
    body = await _limited_urlencoded_body(request)
    if body is None:
        return ""
    try:
        parsed = urllib.parse.parse_qs(
            body.decode("utf-8"),
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=URLENCODED_FORM_MAX_FIELDS,
        )
    except (UnicodeDecodeError, ValueError):
        return ""
    values = parsed.get(CSRF_FORM_FIELD, [])
    if len(values) != 1:
        return ""
    token = values[0]
    if not isinstance(token, str) or not 1 <= len(token) <= CSRF_TOKEN_MAX_LENGTH:
        return ""
    return token


def _reject_csrf():
    raise HTTPException(status_code=403, detail="Forbidden")


async def _require_authenticated_csrf(request, role):
    validation_key = f"authenticated:{role}"
    if getattr(request.state, "csrf_validation_key", "") == validation_key:
        return
    session_value = _csrf_session_value(request, role)
    if not session_value:
        _reject_csrf()
    token = await _submitted_csrf_token(request)
    if not validate_csrf_token(
        token,
        f"authenticated:{role}",
        session_value,
        AUTHENTICATED_CSRF_MAX_AGE,
    ):
        _reject_csrf()
    request.state.csrf_validation_key = validation_key


async def require_admin_csrf(request: Request):
    await _require_authenticated_csrf(request, "admin")


async def require_master_csrf(request: Request):
    await _require_authenticated_csrf(request, "master")


def _preauth_cookie_name(role):
    if role == "admin":
        return ADMIN_PREAUTH_CSRF_COOKIE
    if role == "master":
        return MASTER_PREAUTH_CSRF_COOKIE
    raise ValueError("Invalid pre-authentication flow")


def _preauth_cookie_path(role):
    return "/login" if role == "admin" else "/master/login"


def _new_preauth_token(role):
    random_value = secrets.token_urlsafe(32)
    if len(random_value) != PREAUTH_NONCE_RANDOM_LENGTH:
        raise RuntimeError("Pre-authentication generation failed")
    nonce = PREAUTH_NONCE_PREFIX + random_value
    token = issue_csrf_token(
        f"preauth:{role}", nonce, PREAUTH_CSRF_MAX_AGE
    )
    return nonce, token


async def _require_preauth_csrf(request, role):
    validation_key = f"preauth:{role}"
    if getattr(request.state, "csrf_validation_key", "") == validation_key:
        return
    nonce = request.cookies.get(_preauth_cookie_name(role), "")
    if not isinstance(nonce, str) or not PREAUTH_NONCE_PATTERN.fullmatch(nonce):
        _reject_csrf()
    token = await _submitted_csrf_token(request)
    if not validate_csrf_token(
        token,
        f"preauth:{role}",
        nonce,
        PREAUTH_CSRF_MAX_AGE,
    ):
        _reject_csrf()
    timestamps = csrf_token_timestamps(token)
    if timestamps is None:
        _reject_csrf()
    request.state.preauth_role = role
    request.state.preauth_nonce = nonce
    request.state.preauth_issued_at = timestamps[0]
    request.state.preauth_expires_at = timestamps[1]
    request.state.csrf_validation_key = validation_key


async def require_admin_login_csrf(request: Request):
    await _require_preauth_csrf(request, "admin")


async def require_master_login_csrf(request: Request):
    await _require_preauth_csrf(request, "master")


def _preauth_login_response(role, message=""):
    nonce, token = _new_preauth_token(role)
    page = login_page(message, token) if role == "admin" else master_login_page(message, token)
    response = HTMLResponse(page)
    response.set_cookie(
        _preauth_cookie_name(role),
        nonce,
        max_age=PREAUTH_CSRF_MAX_AGE,
        httponly=True,
        secure=csrf_cookie_secure(),
        samesite="strict",
        path=_preauth_cookie_path(role),
    )
    return response


def _delete_preauth_cookie(response, role):
    response.delete_cookie(
        _preauth_cookie_name(role),
        path=_preauth_cookie_path(role),
        secure=csrf_cookie_secure(),
        httponly=True,
        samesite="strict",
    )


def login_page(message="", csrf_token=""):
    message_html = f"<p>{html.escape(message)}</p>" if message else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Вход</title>
  {admin_css()}
</head>
<body>
  <main class="admin-container">
    <section class="admin-card" style="max-width: 420px; margin: 8vh auto 0;">
      <h1>Вход в админ-панель</h1>
      {message_html}
      <form class="admin-form" method="post" action="/login">
        <input type="hidden" name="{CSRF_FORM_FIELD}" value="{html.escape(csrf_token, quote=True)}">
        <label>Пароль
          <input type="password" name="password" autocomplete="current-password" required>
        </label>
        <div class="form-actions">
          <button type="submit">Войти</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>"""


def master_login_page(message="", csrf_token=""):
    message_html = f"<p>{html.escape(message)}</p>" if message else ""
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Master Login</title>
  {admin_css()}
</head>
<body>
  <main class="admin-container">
    <section class="admin-card" style="max-width: 420px; margin: 8vh auto 0;">
      <h1>Master Admin Login</h1>
      {message_html}
      <form class="admin-form" method="post" action="/master/login">
        <input type="hidden" name="{CSRF_FORM_FIELD}" value="{html.escape(csrf_token, quote=True)}">
        <label>Password
          <input type="password" name="password" autocomplete="current-password" required>
        </label>
        <div class="form-actions">
          <button type="submit">Log in</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>"""


def database_service_unavailable():
    return HTMLResponse(
        """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Service unavailable</title></head>
<body><main><h1>Service temporarily unavailable</h1><p>Please try again later.</p></main></body>
</html>""",
        status_code=503,
    )


async def csrf_rejection_before_dispatch(request, role, preauth=False):
    try:
        if preauth:
            await _require_preauth_csrf(request, role)
        else:
            await _require_authenticated_csrf(request, role)
    except HTTPException:
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return None


@app.middleware("http")
async def require_admin_login(request: Request, call_next):
    context_token = _CURRENT_REQUEST.set(request)
    try:
        return await _apply_request_access_policy(request, call_next)
    finally:
        _CURRENT_REQUEST.reset(context_token)


async def _apply_request_access_policy(request, call_next):
    path = request.url.path
    if path not in {"/health", "/ready"}:
        try:
            gate_required = preview_gate_required()
            if gate_required:
                _preview_gate_credentials()
        except RuntimeError:
            return PlainTextResponse("Service unavailable", status_code=503)
        if gate_required and not preview_gate_authenticated(request):
            return preview_gate_unauthorized()

    if request.method in {"GET", "HEAD"} and path in {"/logout", "/master/logout"}:
        return await call_next(request)

    if path.startswith("/master") and not master_admin_enabled():
        return generic_not_found()
    if telegram_write_route_disabled(request):
        return generic_not_found()

    if request.method in UNSAFE_HTTP_METHODS and path == "/login":
        rejection = await csrf_rejection_before_dispatch(
            request, "admin", preauth=True
        )
        if rejection is not None:
            return rejection
    if request.method in UNSAFE_HTTP_METHODS and path == "/master/login":
        rejection = await csrf_rejection_before_dispatch(
            request, "master", preauth=True
        )
        if rejection is not None:
            return rejection

    if path in {"/", "/shop", "/shop/"}:
        return await call_next(request)

    if path.startswith("/master"):
        public_master_paths = {"/master/login", "/master/health"}
        if path in public_master_paths:
            return await call_next(request)
        if is_master_authenticated(request):
            if path != "/master/logout" and not DATABASE_READY:
                return database_service_unavailable()
            if request.method in UNSAFE_HTTP_METHODS:
                rejection = await csrf_rejection_before_dispatch(request, "master")
                if rejection is not None:
                    return rejection
            return await call_next(request)
        return RedirectResponse("/master/login", status_code=303)

    public_paths = {"/login", "/health", "/ready"}
    if path in public_paths:
        return await call_next(request)
    if is_admin_authenticated(request):
        if path != "/logout" and not DATABASE_READY:
            return database_service_unavailable()
        if request.method in UNSAFE_HTTP_METHODS:
            rejection = await csrf_rejection_before_dispatch(request, "admin")
            if rejection is not None:
                return rejection
        return await call_next(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_form():
    if not admin_auth_configured():
        return _preauth_login_response("admin", "Admin auth is not configured.")
    return _preauth_login_response("admin")


@app.post(
    "/login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_login_csrf)],
)
async def login(request: Request, password: str = Form(...)):
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if not admin_auth_configured():
        return _preauth_login_response("admin", "Admin auth is not configured.")
    if not secrets.compare_digest(password, admin_password):
        return _preauth_login_response("admin", "Неверный пароль.")

    result, credential = create_authenticated_session(
        "admin",
        getattr(request.state, "preauth_nonce", ""),
        getattr(request.state, "preauth_issued_at", None),
        getattr(request.state, "preauth_expires_at", None),
    )
    if result == "replayed":
        return PlainTextResponse("Forbidden", status_code=403)
    if result != "created" or credential is None:
        return PlainTextResponse("Service unavailable", status_code=503)

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        credential,
        max_age=ADMIN_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    _delete_preauth_cookie(response, "admin")
    return response


@app.post("/logout", dependencies=[Depends(require_admin_csrf)])
async def logout(request: Request):
    credential = _csrf_session_value(request, "admin")
    if not credential or not revoke_authenticated_session("admin", credential):
        return PlainTextResponse("Service unavailable", status_code=503)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        ADMIN_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/master/login", response_class=HTMLResponse)
async def master_login_form():
    if not master_auth_configured():
        return _preauth_login_response("master", "Master admin auth is not configured.")
    return _preauth_login_response("master")


@app.post(
    "/master/login",
    response_class=HTMLResponse,
    dependencies=[Depends(require_master_login_csrf)],
)
async def master_login(request: Request, password: str = Form(...)):
    master_password = os.getenv("MASTER_ADMIN_PASSWORD", "")
    if not master_auth_configured():
        return _preauth_login_response("master", "Master admin auth is not configured.")
    if not secrets.compare_digest(password, master_password):
        return _preauth_login_response("master", "Invalid password.")

    result, credential = create_authenticated_session(
        "master",
        getattr(request.state, "preauth_nonce", ""),
        getattr(request.state, "preauth_issued_at", None),
        getattr(request.state, "preauth_expires_at", None),
    )
    if result == "replayed":
        return PlainTextResponse("Forbidden", status_code=403)
    if result != "created" or credential is None:
        return PlainTextResponse("Service unavailable", status_code=503)

    response = RedirectResponse("/master", status_code=303)
    response.set_cookie(
        MASTER_SESSION_COOKIE,
        credential,
        max_age=MASTER_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    _delete_preauth_cookie(response, "master")
    return response


@app.post("/master/logout", dependencies=[Depends(require_master_csrf)])
async def master_logout(request: Request):
    credential = _csrf_session_value(request, "master")
    if not credential or not revoke_authenticated_session("master", credential):
        return PlainTextResponse("Service unavailable", status_code=503)
    response = RedirectResponse("/master/login", status_code=303)
    response.delete_cookie(
        MASTER_SESSION_COOKIE,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def admin_status_badge(status):
    status_key = str(status or "").strip()
    labels = {
        "pending": ("Ожидает выбора оплаты", "warning"),
        "awaiting_payment": ("Ожидает оплаты", "warning"),
        "payment_reported": ("Оплата заявлена", "info"),
        "cash_on_delivery": ("Наличными", "neutral"),
        "cancelled": ("Отменён", "danger"),
        "paid": ("Оплачен", "success"),
        "preparing": ("Готовится", "info"),
        "done": ("Готов", "success"),
    }
    label, status_class = labels.get(status_key, (status_key or "-", "neutral"))
    return f"<span class='status {status_class}'>{html.escape(label)}</span>"


def admin_status_label(status):
    labels = {
        "pending": "Ожидает выбора оплаты",
        "awaiting_payment": "Ожидает оплаты",
        "payment_reported": "Оплата заявлена",
        "cash_on_delivery": "Наличными",
        "paid": "Оплачен",
        "preparing": "Готовится",
        "done": "Готов",
        "cancelled": "Отменён",
    }
    return labels.get(str(status or ""), str(status or "-"))


# ---------------------------------------------------------------------------
# Orders v2 (Checkpoint E): payment_status/fulfillment_status are the sole
# runtime authority. Transition tables are centralized here so both the
# /orders/{id}/payment/{action} and /orders/{id}/fulfillment/{action} routes
# validate against the same source of truth. Legacy orders.status is no
# longer written or read operationally -- it remains present only as frozen
# historical data (nullable, no default; existing rows untouched).
# ---------------------------------------------------------------------------

# Checkpoint F: the practical, already-supported payment methods (matching
# the exact literal values bot.py's pay_iban/pay_paypal/pay_cash write).
ORDER_PAYMENT_METHOD_VALUES = ("IBAN", "PayPal", "Cash")

PAYMENT_TRANSITIONS = {
    "unpaid": {"payment_reported", "paid"},
    "payment_reported": {"paid"},
    "paid": {"refunded"},
    "refunded": set(),
}

FULFILLMENT_TRANSITIONS = {
    "new": {"confirmed", "cancelled"},
    "confirmed": {"picking", "cancelled"},
    "picking": {"packed", "cancelled"},
    "packed": {"ready_to_ship", "cancelled"},
    "ready_to_ship": {"shipped", "cancelled"},
    "shipped": {"delivered", "cancelled"},
    "delivered": set(),
    "cancelled": set(),
}

PAYMENT_STATUS_LABELS = {
    "unpaid": "Не оплачен",
    "payment_reported": "Оплата заявлена",
    "paid": "Оплачен",
    "refunded": "Возврат оформлен",
}

FULFILLMENT_STATUS_LABELS = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "picking": "Сборка",
    "packed": "Упакован",
    "ready_to_ship": "Готов к отправке",
    "shipped": "Отправлен",
    "delivered": "Доставлен",
    "cancelled": "Отменён",
}

ORDER_SOURCE_LABELS = {
    "telegram": "Telegram",
    "website": "Сайт",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "whatsapp": "WhatsApp",
    "viber": "Viber",
    "in_person": "Лично",
    "other": "Другое",
}


def payment_status_label(payment_status):
    return PAYMENT_STATUS_LABELS.get(str(payment_status or ""), str(payment_status or "-"))


def fulfillment_status_label(fulfillment_status):
    return FULFILLMENT_STATUS_LABELS.get(str(fulfillment_status or ""), str(fulfillment_status or "-"))


def payment_status_badge(payment_status):
    status_key = str(payment_status or "")
    status_class = {
        "unpaid": "warning",
        "payment_reported": "info",
        "paid": "success",
        "refunded": "danger",
    }.get(status_key, "neutral")
    return f"<span class='status {status_class}'>{html.escape(payment_status_label(status_key))}</span>"


def fulfillment_status_badge(fulfillment_status):
    status_key = str(fulfillment_status or "")
    status_class = {
        "new": "warning",
        "confirmed": "info",
        "picking": "info",
        "packed": "info",
        "ready_to_ship": "info",
        "shipped": "info",
        "delivered": "success",
        "cancelled": "danger",
    }.get(status_key, "neutral")
    return f"<span class='status {status_class}'>{html.escape(fulfillment_status_label(status_key))}</span>"


def order_source_badge(source):
    label = ORDER_SOURCE_LABELS.get(str(source or ""), str(source or "-"))
    return f"<span class='status neutral'>{html.escape(label)}</span>"


def payment_actions_for(payment_status):
    labels = {
        "payment_reported": "Оплата заявлена",
        "paid": "Подтвердить оплату",
        "refunded": "Оформить возврат",
    }
    targets = PAYMENT_TRANSITIONS.get(str(payment_status or ""), set())
    return [
        (action, labels[action])
        for action in ("payment_reported", "paid", "refunded")
        if action in targets
    ]


def fulfillment_actions_for(fulfillment_status):
    labels = {
        "confirmed": "Подтвердить",
        "picking": "В сборку",
        "packed": "Упаковано",
        "ready_to_ship": "Готов к отправке",
        "shipped": "Отправлен",
        "delivered": "Доставлен",
        "cancelled": "Отмена",
    }
    targets = FULFILLMENT_TRANSITIONS.get(str(fulfillment_status or ""), set())
    return [
        (action, labels[action])
        for action in (
            "confirmed", "picking", "packed", "ready_to_ship",
            "shipped", "delivered", "cancelled",
        )
        if action in targets
    ]


def _order_has_pending_weighing(cursor, order_id):
    cursor.execute(
        "SELECT 1 FROM order_items WHERE order_id = %s AND weight IS NULL "
        "AND pricing_mode = 'per_kg' LIMIT 1",
        (order_id,)
    )
    return cursor.fetchone() is not None


def format_admin_datetime(value):
    if not value:
        return "—"
    if hasattr(value, "strftime"):
        return html.escape(value.strftime("%d.%m.%Y %H:%M"))
    return html.escape(str(value))


def format_stock_grams(stock_grams):
    stock = max(int(stock_grams or 0), 0)
    if stock >= 1000:
        return f"{stock / 1000:g} кг"
    return f"{stock} г"


def format_analytics_weight(value):
    grams = int(value or 0)
    if grams >= 1000:
        return f"{grams / 1000:.1f} кг"
    return f"{grams} г"


def render_product_analytics(rows):
    if not rows:
        return "<p>Нет данных</p>"

    table_rows = []
    mobile_rows = []
    for row in rows:
        _, product_name, grams_sold, revenue, *_ = row
        product_text = html.escape(str(product_name or "-"))
        sold_text = format_analytics_weight(grams_sold)
        revenue_text = f"€{float(revenue or 0):.2f}"
        table_rows.append(
            "<tr>"
            f"<td>{product_text}</td>"
            f"<td>{sold_text}</td>"
            f"<td>{revenue_text}</td>"
            "</tr>"
        )
        mobile_rows.append(
            '<article class="analytics-mobile-row">'
            '<dl>'
            f'<div class="analytics-mobile-field"><dt>Товар</dt><dd>{product_text}</dd></div>'
            f'<div class="analytics-mobile-field"><dt>Продано</dt><dd>{sold_text}</dd></div>'
            f'<div class="analytics-mobile-field"><dt>Выручка</dt><dd>{revenue_text}</dd></div>'
            '</dl></article>'
        )
    return (
        '<div class="dash-table-wrap analytics-desktop-table">'
        '<table class="analytics-table">'
        '<tr><th>Товар</th><th>Продано</th><th>Выручка</th></tr>'
        f'{"".join(table_rows)}</table></div>'
        '<div class="analytics-mobile-list" aria-label="Аналитика товаров">'
        f'{"".join(mobile_rows)}</div>'
    )


def render_customer_analytics(rows, orders_label):
    if not rows:
        return "<p>Нет данных</p>"

    safe_orders_label = html.escape(str(orders_label))
    table_rows = []
    mobile_rows = []
    for _, username, phone, orders_count, total_spent in rows:
        customer = username if username and username != "-" else phone
        customer_text = html.escape(str(customer or "-"))
        orders_text = html.escape(str(orders_count))
        total_text = f"€{float(total_spent or 0):.2f}"
        table_rows.append(
            "<tr>"
            f"<td>{customer_text}</td>"
            f"<td>{orders_text}</td>"
            f"<td>{total_text}</td>"
            "</tr>"
        )
        mobile_rows.append(
            '<article class="analytics-mobile-row">'
            '<dl>'
            f'<div class="analytics-mobile-field"><dt>Клиент</dt><dd>{customer_text}</dd></div>'
            f'<div class="analytics-mobile-field"><dt>{safe_orders_label}</dt><dd>{orders_text}</dd></div>'
            f'<div class="analytics-mobile-field"><dt>Сумма</dt><dd>{total_text}</dd></div>'
            '</dl></article>'
        )
    return (
        '<div class="dash-table-wrap analytics-desktop-table">'
        '<table class="analytics-table">'
        f'<tr><th>Клиент</th><th>{safe_orders_label}</th><th>Сумма</th></tr>'
        f'{"".join(table_rows)}</table></div>'
        '<div class="analytics-mobile-list" aria-label="Аналитика клиентов">'
        f'{"".join(mobile_rows)}</div>'
    )


PRICING_MODES = {"fixed", "per_kg", "options"}


def parse_optional_nonnegative_number(value, field_name, integer=False):
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = int(raw_value) if integer else float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}: некорректное число") from exc
    if not integer and not math.isfinite(parsed):
        raise ValueError(f"{field_name}: требуется конечное число")
    if parsed < 0:
        raise ValueError(f"{field_name}: значение не может быть отрицательным")
    return parsed


def parse_optional_positive_number(value, field_name, integer=False):
    parsed = parse_optional_nonnegative_number(value, field_name, integer=integer)
    if parsed is not None and parsed <= 0:
        raise ValueError(f"{field_name}: значение должно быть больше нуля")
    return parsed


def normalize_product_pricing(
    pricing_mode,
    price_per_kg,
    fixed_price,
    sale_unit,
    unit_weight_grams,
    stock_quantity,
):
    mode = str(pricing_mode or "").strip()
    if mode not in PRICING_MODES:
        raise ValueError("Неизвестный режим цены")

    if mode == "fixed":
        fixed_price_value = parse_optional_positive_number(
            fixed_price, "Фиксированная цена"
        )
        stock_quantity_value = parse_optional_nonnegative_number(
            stock_quantity, "Остаток в единицах", integer=True
        )
        sale_unit_value = str(sale_unit or "").strip()
        if fixed_price_value is None:
            raise ValueError("Для fixed требуется фиксированная цена")
        if not sale_unit_value:
            raise ValueError("Для fixed требуется единица продажи")
        return mode, 0.0, fixed_price_value, sale_unit_value, None, stock_quantity_value
    if mode == "per_kg":
        per_kg_price = parse_optional_positive_number(price_per_kg, "Цена за кг")
        unit_weight_value = parse_optional_positive_number(
            unit_weight_grams, "Ориентировочный вес", integer=True
        )
        if per_kg_price is None:
            raise ValueError("Для per_kg требуется цена за кг")
        return mode, per_kg_price, None, None, unit_weight_value, None
    return mode, 0.0, None, None, None, None


def normalize_product_stock_grams(pricing_mode, stock_grams, existing_stock_grams=None):
    if pricing_mode != "per_kg":
        return int(existing_stock_grams or 0) if existing_stock_grams is not None else 0
    parsed = parse_optional_nonnegative_number(
        stock_grams, "Остаток в граммах", integer=True
    )
    return int(parsed or 0)


def normalize_product_option(label, weight, price, stock_quantity):
    label_value = str(label or "").strip()
    if not label_value:
        raise ValueError("Название варианта не может быть пустым")
    price_value = parse_optional_positive_number(price, "Цена варианта")
    if price_value is None:
        raise ValueError("Для варианта требуется цена")
    weight_value = parse_optional_positive_number(
        weight, "Вес варианта", integer=True
    )
    stock_quantity_value = parse_optional_nonnegative_number(
        stock_quantity, "Остаток варианта", integer=True
    )
    return label_value, weight_value, price_value, stock_quantity_value


def validate_weight_inventory_modes(rows):
    unsupported_ids = sorted({
        int(product_id)
        for product_id, _quantity, pricing_mode in rows
        if product_id and pricing_mode != "per_kg"
    })
    if unsupported_ids:
        ids = ", ".join(str(product_id) for product_id in unsupported_ids)
        raise ValueError(
            "Списание по весу доступно только для режима per_kg. "
            f"Проверьте товары: {ids}."
        )


def admin_options_warning(pricing_mode, options):
    if pricing_mode != "options":
        if options:
            return (
                f"Сохранено старых вариантов: {len(options)}. "
                "Они игнорируются в текущем режиме и не показываются в storefront."
            )
        return "Варианты не используются в выбранном режиме цены."

    active_options = [option for option in options if bool(option[5])]
    available_options = [
        option for option in active_options
        if not bool(option[7])
        and option[6] is not None
        and int(option[6] or 0) > 0
    ]
    if not active_options:
        return "Нет активных вариантов: товар недоступен в storefront."
    if not available_options:
        return "Нет доступных активных вариантов: товар недоступен в storefront."
    return ""


def format_admin_product_price(pricing_mode, price_per_kg, fixed_price, sale_unit):
    if pricing_mode not in PRICING_MODES:
        raise ValueError(f"Неизвестный режим цены: {pricing_mode!r}")
    if pricing_mode == "fixed":
        unit = escape(str(sale_unit or "за упаковку"), quote=True)
        return f"{float(fixed_price or 0):.2f} {escape(CURRENCY_SYMBOL)} {unit}"
    if pricing_mode == "options":
        return "Варианты"
    return f"{float(price_per_kg or 0):.2f} {escape(CURRENCY_SYMBOL)}/кг"


def render_admin_product_image(image_url, product_name):
    placeholder = (
        '<span class="product-thumb product-thumb-placeholder" '
        'role="img" aria-label="Фотография отсутствует">Фото скоро</span>'
    )
    image = safe_image_url(image_url)
    if not image:
        return placeholder
    escaped_image = escape(image, quote=True)
    escaped_name = escape(str(product_name or ""), quote=True)
    hidden_placeholder = placeholder.replace("<span ", "<span hidden ", 1)
    return (
        '<span class="product-thumb-wrap">'
        f'<a href="{escaped_image}" target="_blank" rel="noopener noreferrer">'
        f'<img class="product-thumb" src="{escaped_image}" alt="{escaped_name}" '
        'referrerpolicy="no-referrer" '
        'onerror="this.parentElement.hidden=true;this.parentElement.nextElementSibling.hidden=false">'
        f'</a>{hidden_placeholder}</span>'
    )


def admin_event_type_label(event_type):
    labels = {
        "order_created": "Заказ создан",
        "payment_selected": "Выбран способ оплаты",
        "payment_reported": "Клиент сообщил об оплате",
        "payment_confirmed": "Оплата подтверждена",
        "status_changed": "Статус изменён",
        "payment_status_changed": "Статус оплаты изменён",
        "fulfillment_status_changed": "Статус выполнения изменён",
        "inventory_deducted": "Склад списан",
        "stock_restored": "Склад восстановлен",
        "notification_sent": "Уведомление отправлено",
        "notification_failed": "Ошибка уведомления",
        "order_note_updated": "Заметка обновлена",
        "order_completed": "Заказ завершён",
        "order_cancelled": "Заказ отменён",
        "item_weighed": "Товар взвешен",
        "weighing_notification_sent": "Клиенту отправлено уведомление о взвешивании",
        "weighing_notification_failed": "Ошибка уведомления о взвешивании",
    }
    return labels.get(str(event_type or ""), str(event_type or "-"))


def log_order_event(cursor, order_id, event_type, event_text):
    cursor.execute(
        """
        INSERT INTO order_events
        (order_id, event_type, event_text)
        VALUES (%s, %s, %s)
        """,
        (order_id, event_type, event_text)
    )


def log_inventory_movement(
    cursor,
    product_id,
    movement_type,
    quantity_grams,
    stock_before=None,
    stock_after=None,
    order_id=None,
    note="",
    quantity_units=None,
):
    cursor.execute(
        """
        INSERT INTO inventory_movements
        (product_id, order_id, movement_type, quantity_grams, quantity_units, stock_before, stock_after, note)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (product_id, order_id, movement_type, quantity_grams, quantity_units, stock_before, stock_after, note),
    )


def _fetch_order_items_for_fulfillment(cursor, order_id):
    cursor.execute(
        """
        SELECT oi.product_id, oi.option_id, oi.weight, oi.pricing_mode
        FROM order_items oi
        WHERE oi.order_id = %s
        """,
        (order_id,)
    )
    return cursor.fetchall()


class InsufficientStockError(Exception):
    """Raised when one or more order lines cannot be fulfilled from current
    stock. No inventory row is modified and no movement is logged before
    this is raised for any line in the order."""

    def __init__(self, shortages):
        self.shortages = shortages
        ids = ", ".join(
            f"{kind}:{reference_id}" for kind, reference_id, _required, _available in shortages
        )
        super().__init__(f"Insufficient stock for: {ids}")


def _aggregate_required_inventory(order_items):
    per_kg_required_grams = {}
    fixed_required_units = {}
    options_required_units = {}

    for product_id, option_id, weight, pricing_mode in order_items:
        if pricing_mode == "per_kg":
            if not product_id or not weight:
                continue
            per_kg_required_grams[product_id] = (
                per_kg_required_grams.get(product_id, 0) + int(weight)
            )
        elif pricing_mode == "fixed":
            if not product_id:
                continue
            fixed_required_units[product_id] = fixed_required_units.get(product_id, 0) + 1
        elif pricing_mode == "options":
            if not option_id:
                continue
            entry = options_required_units.setdefault(option_id, [product_id, 0])
            entry[1] += 1

    return per_kg_required_grams, fixed_required_units, options_required_units


def _validate_and_lock_order_inventory(cursor, order_id):
    """Locks every product/option row this order will touch with
    SELECT ... FOR UPDATE inside the caller's existing transaction, and
    validates that every required component has sufficient stock BEFORE any
    mutation happens. Rows are locked in a fixed, sorted order (per_kg
    products, then fixed products, then options) to keep concurrent Paid
    transitions on overlapping lines from deadlocking each other.

    Raises InsufficientStockError (no rows changed, no movements logged) if
    any component is short. Returns the data deduct_order_inventory needs so
    it never has to re-query the now-locked rows."""
    order_items = _fetch_order_items_for_fulfillment(cursor, order_id)
    per_kg_required, fixed_required, options_required = _aggregate_required_inventory(
        order_items
    )

    shortages = []

    per_kg_stock = {}
    for product_id in sorted(per_kg_required):
        required = per_kg_required[product_id]
        cursor.execute(
            "SELECT stock_grams FROM products WHERE id = %s AND pricing_mode = 'per_kg' FOR UPDATE",
            (product_id,)
        )
        row = cursor.fetchone()
        available = int(row[0]) if row and row[0] is not None else 0
        per_kg_stock[product_id] = available
        if row is None or available < required:
            shortages.append(("per_kg", product_id, required, available))

    fixed_stock = {}
    for product_id in sorted(fixed_required):
        required = fixed_required[product_id]
        cursor.execute(
            "SELECT stock_quantity FROM products WHERE id = %s AND pricing_mode = 'fixed' FOR UPDATE",
            (product_id,)
        )
        row = cursor.fetchone()
        available = int(row[0]) if row and row[0] is not None else 0
        fixed_stock[product_id] = available
        if row is None or available < required:
            shortages.append(("fixed", product_id, required, available))

    options_stock = {}
    for option_id in sorted(options_required):
        _product_id, required = options_required[option_id]
        cursor.execute(
            "SELECT stock_quantity FROM product_options WHERE id = %s FOR UPDATE",
            (option_id,)
        )
        row = cursor.fetchone()
        available = int(row[0]) if row and row[0] is not None else 0
        options_stock[option_id] = available
        if row is None or available < required:
            shortages.append(("options", option_id, required, available))

    if shortages:
        raise InsufficientStockError(shortages)

    return per_kg_required, fixed_required, options_required, per_kg_stock, fixed_stock, options_stock


def deduct_order_inventory(cursor, order_id):
    """Deducts stock for every line of an order at the moment it is marked
    paid, dispatched per-line by the order line's own snapshotted
    pricing_mode (never the product's current pricing_mode). Validates
    sufficient stock for every required component first (see
    _validate_and_lock_order_inventory); if any is short, raises
    InsufficientStockError and changes nothing. Returns the list of
    affected product_ids for low-stock/out-of-stock bookkeeping."""
    (
        per_kg_required, fixed_required, options_required,
        per_kg_stock, fixed_stock, options_stock,
    ) = _validate_and_lock_order_inventory(cursor, order_id)

    affected_product_ids = []

    for product_id, required in per_kg_required.items():
        stock_before = per_kg_stock[product_id]
        stock_after = stock_before - required
        cursor.execute(
            "UPDATE products SET stock_grams = %s WHERE id = %s AND pricing_mode = 'per_kg'",
            (stock_after, product_id)
        )
        affected_product_ids.append(product_id)
        log_inventory_movement(
            cursor,
            product_id,
            "order_deducted",
            stock_after - stock_before,
            stock_before,
            stock_after,
            order_id,
            "Склад списан по заказу (per_kg)."
        )

    for product_id, required in fixed_required.items():
        stock_before = fixed_stock[product_id]
        stock_after = stock_before - required
        cursor.execute(
            "UPDATE products SET stock_quantity = %s WHERE id = %s AND pricing_mode = 'fixed'",
            (stock_after, product_id)
        )
        affected_product_ids.append(product_id)
        log_inventory_movement(
            cursor,
            product_id,
            "order_deducted",
            None,
            stock_before,
            stock_after,
            order_id,
            "Списаны штучные единицы по заказу (fixed).",
            quantity_units=stock_after - stock_before,
        )

    for option_id, (product_id, required) in options_required.items():
        stock_before = options_stock[option_id]
        stock_after = stock_before - required
        cursor.execute(
            "UPDATE product_options SET stock_quantity = %s WHERE id = %s",
            (stock_after, option_id)
        )
        affected_product_ids.append(product_id)
        log_inventory_movement(
            cursor,
            product_id,
            "order_deducted",
            None,
            stock_before,
            stock_after,
            order_id,
            "Списаны единицы варианта по заказу (options).",
            quantity_units=stock_after - stock_before,
        )

    return affected_product_ids


def restore_order_inventory(cursor, order_id):
    """Mirror of deduct_order_inventory for the cancelled-after-deducted
    path: restores exactly what was deducted, per-line, by the order line's
    own snapshotted pricing_mode."""
    order_items = _fetch_order_items_for_fulfillment(cursor, order_id)
    affected_product_ids = []

    # per_kg: grouped per product (matches the pre-existing grouped restore shape).
    per_kg_restore_grams = {}
    for product_id, _option_id, weight, pricing_mode in order_items:
        if pricing_mode != "per_kg" or not product_id or not weight:
            continue
        per_kg_restore_grams[product_id] = per_kg_restore_grams.get(product_id, 0) + int(weight)
    for product_id, restore_grams in per_kg_restore_grams.items():
        if restore_grams <= 0:
            continue
        cursor.execute(
            """
            WITH before_update AS (
                SELECT stock_grams AS stock_before
                FROM products
                WHERE id = %s
            ),
            updated AS (
                UPDATE products
                SET stock_grams = stock_grams + %s
                WHERE id = %s
                  AND pricing_mode = 'per_kg'
                RETURNING stock_grams AS stock_after
            )
            SELECT before_update.stock_before, updated.stock_after
            FROM before_update, updated
            """,
            (product_id, restore_grams, product_id)
        )
        stock_row = cursor.fetchone()
        if not stock_row:
            continue
        affected_product_ids.append(product_id)
        stock_before, stock_after = stock_row
        log_inventory_movement(
            cursor,
            product_id,
            "stock_restored",
            int(stock_after or 0) - int(stock_before or 0),
            stock_before,
            stock_after,
            order_id,
            "Остаток восстановлен после отмены заказа (per_kg)."
        )

    # fixed: grouped units per product.
    fixed_unit_counts = {}
    for product_id, _option_id, _weight, pricing_mode in order_items:
        if pricing_mode != "fixed" or not product_id:
            continue
        fixed_unit_counts[product_id] = fixed_unit_counts.get(product_id, 0) + 1
    for product_id, units in fixed_unit_counts.items():
        cursor.execute(
            """
            WITH before_update AS (
                SELECT stock_quantity AS stock_before
                FROM products
                WHERE id = %s
            ),
            updated AS (
                UPDATE products
                SET stock_quantity = COALESCE(stock_quantity, 0) + %s
                WHERE id = %s
                  AND pricing_mode = 'fixed'
                RETURNING stock_quantity AS stock_after
            )
            SELECT before_update.stock_before, updated.stock_after
            FROM before_update, updated
            """,
            (product_id, units, product_id)
        )
        stock_row = cursor.fetchone()
        if not stock_row:
            continue
        affected_product_ids.append(product_id)
        stock_before, stock_after = stock_row
        log_inventory_movement(
            cursor,
            product_id,
            "stock_restored",
            None,
            stock_before,
            stock_after,
            order_id,
            "Восстановлены штучные единицы после отмены заказа (fixed).",
            quantity_units=int(stock_after or 0) - int(stock_before or 0),
        )

    # options: grouped units per option.
    options_unit_counts = {}
    for product_id, option_id, _weight, pricing_mode in order_items:
        if pricing_mode != "options" or not option_id:
            continue
        entry = options_unit_counts.setdefault(option_id, [product_id, 0])
        entry[1] += 1
    for option_id, (product_id, units) in options_unit_counts.items():
        cursor.execute(
            """
            WITH before_update AS (
                SELECT stock_quantity AS stock_before
                FROM product_options
                WHERE id = %s
            ),
            updated AS (
                UPDATE product_options
                SET stock_quantity = COALESCE(stock_quantity, 0) + %s
                WHERE id = %s
                RETURNING stock_quantity AS stock_after
            )
            SELECT before_update.stock_before, updated.stock_after
            FROM before_update, updated
            """,
            (option_id, units, option_id)
        )
        stock_row = cursor.fetchone()
        if not stock_row:
            continue
        affected_product_ids.append(product_id)
        stock_before, stock_after = stock_row
        log_inventory_movement(
            cursor,
            product_id,
            "stock_restored",
            None,
            stock_before,
            stock_after,
            order_id,
            "Восстановлены единицы варианта после отмены заказа (options).",
            quantity_units=int(stock_after or 0) - int(stock_before or 0),
        )

    return affected_product_ids


def get_admin_chat_id():
    value = os.getenv("ADMIN_ID", "")
    if not re.fullmatch(r"-?[1-9][0-9]{0,18}", value):
        return None
    return int(value)


def send_low_stock_alert(product_name, stock_grams, threshold_grams):
    if not telegram_actions_enabled():
        return False
    bot_token = os.getenv("BOT_TOKEN")
    admin_id = get_admin_chat_id()
    if not bot_token or not admin_id:
        return False

    text = (
        "⚠️ Низкий остаток\n\n"
        f"Товар: {product_name}\n"
        f"Остаток: {stock_grams} г\n"
        f"Порог: {threshold_grams} г"
    )
    data = urllib.parse.urlencode({
        "chat_id": admin_id,
        "text": text,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()
    return True


def sync_low_stock_alert_state(cursor, product_ids):
    if not telegram_actions_enabled():
        return
    seen_product_ids = []
    for product_id in product_ids or []:
        if not product_id or product_id in seen_product_ids:
            continue
        seen_product_ids.append(product_id)

    for product_id in seen_product_ids:
        cursor.execute(
            """
            UPDATE products
            SET low_stock_alert_sent = FALSE,
                low_stock_alert_sent_at = NULL
            WHERE id = %s
              AND low_stock_alert_sent = TRUE
              AND (
                  pricing_mode != 'per_kg'
                  OR
                  low_stock_threshold_grams <= 0
                  OR stock_grams > low_stock_threshold_grams
              )
            """,
            (product_id,),
        )
        cursor.execute(
            """
            SELECT name, stock_grams, low_stock_threshold_grams
            FROM products
            WHERE id = %s
              AND pricing_mode = 'per_kg'
              AND is_active = TRUE
              AND is_out_of_stock = FALSE
              AND stock_grams > 0
              AND low_stock_threshold_grams > 0
              AND stock_grams <= low_stock_threshold_grams
              AND low_stock_alert_sent = FALSE
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            continue

        name, stock_grams, threshold_grams = row
        try:
            if not send_low_stock_alert(name, stock_grams, threshold_grams):
                continue
            cursor.execute(
                """
                UPDATE products
                SET low_stock_alert_sent = TRUE,
                    low_stock_alert_sent_at = NOW()
                WHERE id = %s
                """,
                (product_id,),
            )
        except Exception:
            print(LOW_STOCK_SYNC_FAILED)


def send_order_status_notification(telegram_id, order_id, status):
    if not telegram_actions_enabled():
        return False
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or not telegram_id:
        return False

    messages = {
        "paid": f"✅ Ваш заказ №{order_id} подтверждён и оплачен.",
        "preparing": f"👨‍🍳 Ваш заказ №{order_id} передан в работу и готовится.",
        "done": f"📦 Ваш заказ №{order_id} готов.\nСпасибо за заказ!",
        "cancelled": f"❌ Заказ №{order_id} отменён.\nЕсли произошла ошибка — свяжитесь с администратором.",
    }
    text = messages.get(status)
    if not text:
        return False

    data = urllib.parse.urlencode({
        "chat_id": telegram_id,
        "text": text,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()
    return True


def send_weighing_complete_notification(
    telegram_id,
    order_id,
    total,
    items,
    photo_bytes=None,
    photo_filename=None,
    photo_content_type=None,
):
    if not telegram_actions_enabled():
        return False
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or not telegram_id:
        return False

    item_lines = ""
    for product_name, weight, price, option_label in items:
        label_or_weight = option_label if option_label else f"{weight} г"
        item_lines += (
            f"\n• {product_name}\n"
            f"Вариант/вес: {label_or_weight}\n"
            f"Вес: {weight} г\n"
            f"Стоимость: {float(price or 0):.2f} €\n"
        )

    text = (
        f"⚖️ Ваш заказ полностью собран.\n\n"
        f"📦 Номер заказа:\n#{order_id}\n"
        f"{item_lines}\n"
        f"━━━━━━━━━━━━━━\n"
        f"💰 Итого к оплате:\n{float(total or 0):.2f} €\n\n"
        f"👇 Выберите способ оплаты:"
    )
    inline_keyboard = [
        [{"text": "🏦 Оплата IBAN", "callback_data": "pay_iban"}],
        [{"text": "💵 Оплата наличными", "callback_data": "pay_cash"}],
        [{"text": "🅿️ PayPal", "callback_data": "pay_paypal"}],
        [{"text": "🛍 Продолжить покупки", "callback_data": "back_to_menu"}],
    ]
    reply_markup_json = json.dumps({"inline_keyboard": inline_keyboard})

    if photo_bytes and len(text) <= 1024:
        boundary = secrets.token_hex(16)
        boundary_bytes = boundary.encode("utf-8")
        parts = []

        def add_field(name, value):
            parts.append(b"--" + boundary_bytes + b"\r\n")
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            parts.append(str(value).encode("utf-8") + b"\r\n")

        add_field("chat_id", telegram_id)
        add_field("caption", text)
        add_field("reply_markup", reply_markup_json)

        parts.append(b"--" + boundary_bytes + b"\r\n")
        parts.append(
            'Content-Disposition: form-data; name="photo"; filename="{}"\r\n'.format(
                photo_filename or "photo.jpg"
            ).encode("utf-8")
        )
        parts.append(
            f"Content-Type: {photo_content_type or 'application/octet-stream'}\r\n\r\n".encode("utf-8")
        )
        parts.append(photo_bytes + b"\r\n")
        parts.append(b"--" + boundary_bytes + b"--\r\n")

        body = b"".join(parts)
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(request, timeout=15).read()
        return True

    data = urllib.parse.urlencode({
        "chat_id": telegram_id,
        "text": text,
        "reply_markup": reply_markup_json,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=data,
        method="POST"
    )
    urllib.request.urlopen(request, timeout=5).read()
    return True


def record_notification_event(order_id, event_type, event_text):
    if not telegram_actions_enabled():
        return False
    connection = psycopg2.connect(DATABASE_URL)
    cursor = None
    try:
        cursor = connection.cursor()
        log_order_event(cursor, order_id, event_type, event_text)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        connection.close()
    return True


def send_order_status_notification_and_record(telegram_id, order_id, status):
    if not send_order_status_notification(telegram_id, order_id, status):
        return False
    return record_notification_event(
        order_id,
        "notification_sent",
        f"Клиенту отправлено уведомление о статусе: {admin_status_label(status)}",
    )


def send_weighing_notification_and_record(
    telegram_id,
    order_id,
    total,
    items,
    photo_bytes=None,
    photo_filename=None,
    photo_content_type=None,
):
    sent = send_weighing_complete_notification(
        telegram_id,
        order_id,
        total,
        items,
        photo_bytes=photo_bytes,
        photo_filename=photo_filename,
        photo_content_type=photo_content_type,
    )
    if not sent:
        return False
    return record_notification_event(
        order_id,
        "weighing_notification_sent",
        f"Клиенту отправлено уведомление о финальной сумме: {float(total or 0):.2f} €",
    )


def get_channel_chat_id():
    return os.getenv("TELEGRAM_CHANNEL_ID")


def _telegram_error_is_forbidden(error):
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "code", None)
    return status_code == 403


def send_channel_post(message_text):
    if not telegram_actions_enabled():
        return False, "disabled"
    try:
        bot_token = os.getenv("BOT_TOKEN")
        channel_chat_id = get_channel_chat_id()
        if not bot_token or not channel_chat_id:
            return False, CHANNEL_POST_FAILED

        data = urllib.parse.urlencode({
            "chat_id": channel_chat_id,
            "text": message_text,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST"
        )
        urllib.request.urlopen(request, timeout=5).read()
        return True, None
    except Exception:
        return False, CHANNEL_POST_FAILED


def send_broadcast_message(telegram_id, message_text):
    if not telegram_actions_enabled():
        return False, "disabled"
    try:
        bot_token = os.getenv("BOT_TOKEN")
        if not bot_token or not telegram_id:
            return False, "failed"

        data = urllib.parse.urlencode({
            "chat_id": telegram_id,
            "text": message_text,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST"
        )
        urllib.request.urlopen(request, timeout=5).read()
        return True, "sent"
    except Exception as error:
        if _telegram_error_is_forbidden(error):
            return False, "blocked"
        return False, "failed"


def channel_post_status_label(status):
    labels = {
        "draft": "Черновик",
        "sent": "Отправлено",
        "failed": "Ошибка",
    }
    return html.escape(labels.get(str(status or ""), str(status or "-")))


def broadcast_status_label(status):
    labels = {
        "draft": "Черновик",
        "sending": "Отправляется",
        "sent": "Отправлено",
        "failed": "Ошибка",
    }
    return html.escape(labels.get(str(status or ""), str(status or "-")))


def broadcast_target_label(target_type):
    labels = {
        "all_clients": "Все клиенты",
        "clients_with_orders": "Клиенты с заказами",
        "active_last_7_days": "Активные за 7 дней",
        "active_last_30_days": "Активные за 30 дней",
        "awaiting_payment": "Ожидают оплату",
    }
    return html.escape(labels.get(str(target_type or ""), str(target_type or "-")))


def stable_admin_error_code(error_code):
    if isinstance(error_code, str) and error_code in ADMIN_OPERATION_ERROR_CODES:
        return error_code
    return INTERNAL_OPERATION_FAILED


def report_read_error(error_code):
    logger.error(stable_admin_error_code(error_code))


def log_admin_error(route, action, error_code):
    stable_code = stable_admin_error_code(error_code)
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO error_logs
            (route, action, error_message, traceback)
            VALUES (%s, %s, %s, %s)
            """,
            (route, action, stable_code, None),
        )
        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(ADMIN_ERROR_LOG_WRITE_FAILED)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def log_admin_stable_error(route, action, error_code):
    log_admin_error(route, action, error_code)


def seed_default_master_shop(cursor):
    seed_rows = [
        (
            "current",
            ADMIN_PANEL_TITLE or "Current Shop",
            None,
            None,
            None,
            None,
            "live",
            "Current local shop deployment",
        ),
        (
            "deal-market-nl",
            "Deal Market NL",
            "https://admin-production-1523.up.railway.app",
            "DealMarketNL_bot",
            None,
            None,
            "live",
            "Main live Telegram shop project.",
        ),
        (
            "koptilnya-demo",
            "Koptilnya Demo",
            None,
            "zakaz_koptim_bot",
            "https://t.me/zakaz_koptim_bot",
            None,
            "demo",
            "Demo Telegram shop for smoked products.",
        ),
        (
            "angelinix-automation",
            "Angelinix Automation",
            None,
            None,
            None,
            "https://butesha-beep.github.io/AngelinixAutomation/",
            "draft",
            "Landing/demo funnel for automation services.",
        ),
        (
            "irpin-installers",
            "Irpin Installers",
            None,
            None,
            None,
            None,
            "draft",
            "Demo landing project for installer/repair services.",
        ),
    ]
    for row in seed_rows:
        cursor.execute(
            """
            INSERT INTO master_shops (
                shop_key,
                brand_name,
                admin_url,
                bot_username,
                bot_url,
                landing_url,
                status,
                notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (shop_key) DO NOTHING
            """,
            row,
        )


def create_current_master_snapshot(cursor):
    cursor.execute(
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(CASE WHEN created_at::date = CURRENT_DATE THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN payment_status IN ('unpaid', 'payment_reported') THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' AND date_trunc('month', created_at) = date_trunc('month', NOW()) THEN total ELSE 0 END), 0)
        FROM orders
        """
    )
    total_orders, today_orders, pending_orders, month_revenue = cursor.fetchone()
    cursor.execute("SELECT COUNT(*) FROM clients")
    total_clients = cursor.fetchone()[0]
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM products
        WHERE
            pricing_mode = 'per_kg'
            AND is_active = TRUE
            AND is_out_of_stock = FALSE
            AND low_stock_threshold_grams > 0
            AND stock_grams > 0
            AND stock_grams <= low_stock_threshold_grams
        """
    )
    low_stock_count = cursor.fetchone()[0]
    cursor.execute(
        """
        INSERT INTO master_shop_snapshots (
            shop_key,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """,
        (
            "current",
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
        ),
    )


def master_snapshot_value(value):
    if value is None:
        return "-"
    return html.escape(str(value))


def master_money_value(value):
    if value is None:
        return "-"
    return f"{CURRENCY_SYMBOL}{float(value):.2f}"


def master_project_status_badge(status):
    status_key = str(status or "").strip().lower()
    labels = {
        "live": ("Live", "success"),
        "demo": ("Demo", "info"),
        "paused": ("Paused", "warning"),
        "draft": ("Draft", "neutral"),
        "error": ("Error", "danger"),
    }
    if status_key not in labels:
        return html.escape(str(status or "-"))
    label, status_class = labels[status_key]
    return f"<span class='status {status_class}'>{label}</span>"


def master_shop_rows(rows):
    if not rows:
        return """
        <section class="admin-card">
          <h1>Master Dashboard</h1>
          <p>No shops are registered yet.</p>
        </section>
        <section class="admin-card dash-section">
          <h2>Maintenance actions</h2>
          <p>These operations are explicit and never run while viewing a page.</p>
          <div class="form-actions">
            <form method="post" action="/master/actions/sync-default-shops">
              <button class="button secondary" type="submit">Sync default shop registry</button>
            </form>
            <form method="post" action="/master/actions/capture-current-snapshot">
              <button class="button secondary" type="submit">Capture current snapshot</button>
            </form>
          </div>
        </section>
        """

    rendered = ""
    for row in rows:
        (
            shop_key,
            brand_name,
            admin_url,
            bot_username,
            bot_url,
            landing_url,
            status,
            notes,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at,
        ) = row
        shop_key_text = html.escape(str(shop_key or ""))
        admin_link = "<span>-</span>"
        if admin_url:
            safe_admin_url = html.escape(str(admin_url), quote=True)
            admin_link = f'<a class="button button-link" href="{safe_admin_url}">Open admin</a>'
        bot_link = ""
        if bot_url:
            safe_bot_url = html.escape(str(bot_url), quote=True)
            bot_link = f'<a class="button button-link secondary" href="{safe_bot_url}">Open bot</a>'
        landing_link = "<span>-</span>"
        if landing_url:
            safe_landing_url = html.escape(str(landing_url), quote=True)
            landing_link = f'<a class="button button-link secondary" href="{safe_landing_url}">Open landing</a>'
        bot_username_text = html.escape(str(bot_username or "-"))
        rendered += f"""
        <article class="dash-card">
          <strong><a class="view-link" href="/master/shops/{urllib.parse.quote(str(shop_key or ''), safe='')}">{html.escape(str(brand_name or '-'))}</a></strong>
          <span>Shop key: {shop_key_text}</span><br>
          <span>Status: {master_project_status_badge(status)}</span><br>
          <span>Bot username: {bot_username_text}</span>
          <div class="detail-grid" style="margin-top: 16px;">
            <div><span>Total orders</span><strong>{master_snapshot_value(total_orders)}</strong></div>
            <div><span>Today</span><strong>{master_snapshot_value(today_orders)}</strong></div>
            <div><span>Pending</span><strong>{master_snapshot_value(pending_orders)}</strong></div>
            <div><span>Month revenue</span><strong>{master_money_value(month_revenue)}</strong></div>
            <div><span>Low stock</span><strong>{master_snapshot_value(low_stock_count)}</strong></div>
            <div><span>Clients</span><strong>{master_snapshot_value(total_clients)}</strong></div>
          </div>
          <p>Last seen: {master_snapshot_value(last_seen_at)}</p>
          <p>{html.escape(str(notes or '-'))}</p>
          <div class="form-actions">
            {bot_link}
            {admin_link}
            {landing_link}
          </div>
        </article>
        """

    return f"""
    <section class="admin-card">
      <h1>Master Dashboard</h1>
      <p>Read-only overview of registered shop deployments.</p>
      <div class="dash-grid" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));">
        {rendered}
      </div>
    </section>
    <section class="admin-card dash-section">
      <h2>Maintenance actions</h2>
      <p>These operations are explicit and never run while viewing a page.</p>
      <div class="form-actions">
        <form method="post" action="/master/actions/sync-default-shops">
          <button class="button secondary" type="submit">Sync default shop registry</button>
        </form>
        <form method="post" action="/master/actions/capture-current-snapshot">
          <button class="button secondary" type="submit">Capture current snapshot</button>
        </form>
      </div>
    </section>
    """


@app.get("/master", response_class=HTMLResponse)
async def master_dashboard():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                s.shop_key,
                s.brand_name,
                s.admin_url,
                s.bot_username,
                s.bot_url,
                s.landing_url,
                s.status,
                s.notes,
                snap.total_orders,
                snap.today_orders,
                snap.pending_orders,
                snap.month_revenue,
                snap.low_stock_count,
                snap.total_clients,
                snap.last_seen_at
            FROM master_shops s
            LEFT JOIN LATERAL (
                SELECT *
                FROM master_shop_snapshots mss
                WHERE mss.shop_key = s.shop_key
                ORDER BY mss.last_seen_at DESC, mss.id DESC
                LIMIT 1
            ) snap ON TRUE
            ORDER BY s.brand_name ASC, s.shop_key ASC
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return master_layout("Master Dashboard", master_shop_rows(rows))
    except Exception:
        report_read_error("master_dashboard_load_failed")
        return master_error_page("Error", "Could not load Master Dashboard.")


@app.post(
    "/master/actions/sync-default-shops",
    dependencies=[Depends(require_master_csrf)],
)
async def sync_default_master_shops():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        seed_default_master_shop(cursor)
        conn.commit()
        return RedirectResponse("/master", status_code=303)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_error(
            "/master/actions/sync-default-shops",
            "sync_default_master_shops",
            "master_shop_seed_failed",
        )
        return master_error_page("Error", "Could not synchronize the shop registry.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post(
    "/master/actions/capture-current-snapshot",
    dependencies=[Depends(require_master_csrf)],
)
async def capture_current_master_snapshot():
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        create_current_master_snapshot(cursor)
        conn.commit()
        return RedirectResponse("/master", status_code=303)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_error(
            "/master/actions/capture-current-snapshot",
            "capture_current_master_snapshot",
            "master_snapshot_failed",
        )
        return master_error_page("Error", "Could not capture the current snapshot.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/master/shops/{shop_key}", response_class=HTMLResponse)
async def master_shop_detail(shop_key: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                s.shop_key,
                s.brand_name,
                s.admin_url,
                s.bot_username,
                s.bot_url,
                s.landing_url,
                s.status,
                s.notes,
                snap.total_orders,
                snap.today_orders,
                snap.pending_orders,
                snap.month_revenue,
                snap.low_stock_count,
                snap.total_clients,
                snap.last_seen_at
            FROM master_shops s
            LEFT JOIN LATERAL (
                SELECT *
                FROM master_shop_snapshots mss
                WHERE mss.shop_key = s.shop_key
                ORDER BY mss.last_seen_at DESC, mss.id DESC
                LIMIT 1
            ) snap ON TRUE
            WHERE s.shop_key = %s
            """,
            (shop_key,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return master_error_page("Shop not found", "This shop is not registered in Master Admin.")

        (
            shop_key_value,
            brand_name,
            admin_url,
            bot_username,
            bot_url,
            landing_url,
            status,
            notes,
            total_orders,
            today_orders,
            pending_orders,
            month_revenue,
            low_stock_count,
            total_clients,
            last_seen_at,
        ) = row
        admin_link = html.escape(str(admin_url or "-"))
        if admin_url:
            safe_admin_url = html.escape(str(admin_url), quote=True)
            admin_link = f'<a href="{safe_admin_url}">{safe_admin_url}</a>'
        bot_link = html.escape(str(bot_url or "-"))
        if bot_url:
            safe_bot_url = html.escape(str(bot_url), quote=True)
            bot_link = f'<a href="{safe_bot_url}">{safe_bot_url}</a>'
        landing_link = html.escape(str(landing_url or "-"))
        if landing_url:
            safe_landing_url = html.escape(str(landing_url), quote=True)
            landing_link = f'<a href="{safe_landing_url}">{safe_landing_url}</a>'

        content = f"""
        <section class="admin-card">
          <h1>{html.escape(str(brand_name or shop_key_value or 'Shop'))}</h1>
          <div class="detail-grid">
            <div class="detail-field"><strong>Shop key</strong>{html.escape(str(shop_key_value or '-'))}</div>
            <div class="detail-field"><strong>Status</strong>{master_project_status_badge(status)}</div>
            <div class="detail-field"><strong>Bot username</strong>{html.escape(str(bot_username or '-'))}</div>
            <div class="detail-field"><strong>Bot URL</strong>{bot_link}</div>
            <div class="detail-field"><strong>Admin URL</strong>{admin_link}</div>
            <div class="detail-field"><strong>Landing URL</strong>{landing_link}</div>
            <div class="detail-field"><strong>Notes</strong>{html.escape(str(notes or '-'))}</div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Latest Snapshot</h2>
          <div class="dash-grid">
            <div class="dash-card"><span>Total orders</span><strong class="stat-value">{master_snapshot_value(total_orders)}</strong></div>
            <div class="dash-card"><span>Today orders</span><strong class="stat-value">{master_snapshot_value(today_orders)}</strong></div>
            <div class="dash-card"><span>Pending orders</span><strong class="stat-value">{master_snapshot_value(pending_orders)}</strong></div>
            <div class="dash-card"><span>Month revenue</span><strong class="stat-value">{master_money_value(month_revenue)}</strong></div>
            <div class="dash-card"><span>Low stock</span><strong class="stat-value">{master_snapshot_value(low_stock_count)}</strong></div>
            <div class="dash-card"><span>Total clients</span><strong class="stat-value">{master_snapshot_value(total_clients)}</strong></div>
            <div class="dash-card"><span>Last seen</span><strong class="stat-value">{master_snapshot_value(last_seen_at)}</strong></div>
          </div>
        </section>
        """
        return master_layout(f"Master Shop: {brand_name or shop_key_value}", content)
    except Exception:
        report_read_error("master_shop_detail_load_failed")
        return master_error_page("Error", "Could not load shop details.")


@app.get("/")
async def public_root():
    return RedirectResponse("/shop", status_code=307)


@app.get("/admin", response_class=HTMLResponse)
async def root():
    stats = None
    latest_orders = []
    top_products = []
    worst_products = []
    best_customers = []
    repeat_customers = []
    low_stock_products = []
    low_stock_count = 0
    funnel_rows = []
    error_message = None
    cc_pending_weighing_count = 0
    cc_low_stock_count = 0
    cc_recent_errors_count = 0
    cc_no_recommendations_count = 0
    cc_no_promotion_count = 0
    cc_no_sales_count = 0
    cc_no_image_count = 0
    cc_no_description_count = 0
    cc_never_sold_count = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN payment_status = 'unpaid' AND payment_method IS NULL THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN payment_status = 'unpaid' AND payment_method IN ('IBAN', 'PayPal') THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN payment_status = 'payment_reported' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN payment_status = 'unpaid' AND payment_method = 'Cash' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN payment_status = 'paid' AND fulfillment_status IN ('new', 'confirmed') THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status IN ('picking', 'packed', 'ready_to_ship', 'shipped') THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status = 'cancelled' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN created_at::date = CURRENT_DATE THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' AND created_at::date = CURRENT_DATE THEN total ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' AND date_trunc('month', created_at) = date_trunc('month', NOW()) THEN total ELSE 0 END), 0)
            FROM orders
            """
        )
        stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT
                oi.product_id,
                COALESCE(p.name, oi.product_name) AS product_name,
                COALESCE(SUM(oi.weight), 0) AS grams_sold,
                COALESCE(SUM(oi.price), 0) AS revenue
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE o.fulfillment_status = 'delivered'
            GROUP BY oi.product_id, COALESCE(p.name, oi.product_name)
            ORDER BY revenue DESC
            LIMIT 5
            """
        )
        top_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                p.id,
                p.name,
                COALESCE(s.grams_sold, 0) AS grams_sold,
                COALESCE(s.revenue, 0) AS revenue,
                COALESCE(s.units_sold, 0) AS units_sold
            FROM products p
            LEFT JOIN (
                SELECT
                    oi.product_id,
                    COUNT(*) AS units_sold,
                    COALESCE(SUM(oi.weight), 0) AS grams_sold,
                    COALESCE(SUM(oi.price), 0) AS revenue
                FROM order_items oi
                JOIN orders o ON o.order_id = oi.order_id
                WHERE o.fulfillment_status = 'delivered'
                GROUP BY oi.product_id
            ) s ON s.product_id = p.id
            WHERE p.is_active = TRUE
            ORDER BY revenue ASC, units_sold ASC, p.name ASC
            LIMIT 5
            """
        )
        worst_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                telegram_id,
                COALESCE(MAX(username), '-') AS username,
                COALESCE(MAX(phone), '-') AS phone,
                COUNT(*) AS orders_count,
                COALESCE(SUM(total), 0) AS total_spent
            FROM orders
            WHERE fulfillment_status = 'delivered'
            GROUP BY telegram_id
            ORDER BY total_spent DESC
            LIMIT 5
            """
        )
        best_customers = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                telegram_id,
                COALESCE(MAX(username), '-') AS username,
                COALESCE(MAX(phone), '-') AS phone,
                COUNT(*) AS completed_orders,
                COALESCE(SUM(total), 0) AS total_spent
            FROM orders
            WHERE fulfillment_status = 'delivered'
            GROUP BY telegram_id
            HAVING COUNT(*) >= 2
            ORDER BY completed_orders DESC, total_spent DESC
            LIMIT 5
            """
        )
        repeat_customers = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                id,
                name,
                stock_grams,
                low_stock_threshold_grams
            FROM products
            WHERE
                pricing_mode = 'per_kg'
                AND is_active = TRUE
                AND is_out_of_stock = FALSE
                AND low_stock_threshold_grams > 0
                AND stock_grams > 0
                AND stock_grams <= low_stock_threshold_grams
            ORDER BY stock_grams ASC
            LIMIT 10
            """
        )
        low_stock_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE
                pricing_mode = 'per_kg'
                AND is_active = TRUE
                AND is_out_of_stock = FALSE
                AND low_stock_threshold_grams > 0
                AND stock_grams > 0
                AND stock_grams <= low_stock_threshold_grams
            """
        )
        low_stock_count = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT stage, label, COALESCE(users_count, 0) AS users_count
            FROM (
                VALUES
                    (1, 'start', 'Стартовали бота'),
                    (2, 'view_category', 'Открыли категорию'),
                    (3, 'view_product', 'Открыли товар'),
                    (4, 'add_to_cart', 'Добавили в корзину'),
                    (5, 'checkout_started', 'Начали оформление'),
                    (6, 'order_created', 'Создали заказ'),
                    (7, 'payment_method_selected', 'Выбрали оплату'),
                    (8, 'payment_reported', 'Сообщили об оплате')
            ) AS funnel(sort_order, stage, label)
            LEFT JOIN (
                SELECT event_type, COUNT(DISTINCT telegram_id) AS users_count
                FROM customer_events
                WHERE created_at::date = CURRENT_DATE
                GROUP BY event_type
            ) events ON events.event_type = funnel.stage
            ORDER BY sort_order
            """
        )
        funnel_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT order_id, username, phone, address, total,
                   payment_status, fulfillment_status
            FROM orders
            ORDER BY id DESC
            LIMIT 5
            """
        )
        latest_orders = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(DISTINCT o.order_id)
            FROM orders o
            WHERE o.fulfillment_status != 'cancelled'
              AND EXISTS (
                  SELECT 1
                  FROM order_items oi
                  WHERE oi.order_id = o.order_id
                    AND oi.weight IS NULL
                    AND oi.pricing_mode = 'per_kg'
              )
            """
        )
        cc_pending_weighing_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND (
                  p.is_out_of_stock = TRUE
                  OR (
                      p.pricing_mode = 'per_kg'
                      AND p.stock_grams IS NOT NULL
                      AND p.low_stock_threshold_grams IS NOT NULL
                      AND p.stock_grams <= p.low_stock_threshold_grams
                  )
              )
            """
        )
        cc_low_stock_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM error_logs
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            """
        )
        cc_recent_errors_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM product_recommendations pr
                  WHERE pr.product_id = p.id
                    AND pr.recommendation_type = 'frequently_bought_together'
                    AND pr.is_active = TRUE
              )
            """
        )
        cc_no_recommendations_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND is_promotion = FALSE
            """
        )
        cc_no_promotion_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  WHERE oi.product_id = p.id
                    AND o.fulfillment_status != 'cancelled'
                    AND o.created_at >= NOW() - INTERVAL '14 days'
              )
            """
        )
        cc_no_sales_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND (image_url IS NULL OR TRIM(image_url) = '')
            """
        )
        cc_no_image_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products
            WHERE is_active = TRUE
              AND (description IS NULL OR TRIM(description) = '')
            """
        )
        cc_no_description_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM products p
            WHERE p.is_active = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM order_items oi
                  JOIN orders o ON o.order_id = oi.order_id
                  WHERE oi.product_id = p.id
                    AND o.fulfillment_status != 'cancelled'
              )
            """
        )
        cc_never_sold_count = cursor.fetchone()[0]

        conn.close()
    except Exception:
        report_read_error(DASHBOARD_LOAD_FAILED)
        error_message = "Не удалось загрузить статистику. Попробуйте позже."

    if stats:
        (
            total_orders,
            pending_orders,
            awaiting_payment_orders,
            payment_reported_orders,
            cash_on_delivery_orders,
            paid_orders,
            preparing_orders,
            done_orders,
            cancelled_orders,
            today_orders,
            total_done_revenue,
            today_done_revenue,
            month_done_revenue,
        ) = stats
        awaiting_action = (
            pending_orders
            + awaiting_payment_orders
            + payment_reported_orders
            + cash_on_delivery_orders
        )
        stat_cards = f"""
        <h2>Обзор</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Всего заказов</span><strong class="stat-value">{total_orders}</strong></div>
          <div class="dash-card"><span>Заказов сегодня</span><strong class="stat-value">{today_orders}</strong></div>
          <div class="dash-card"><span>Требуют внимания</span><strong class="stat-value">{awaiting_action}</strong></div>
        </div>
        <h2>Выручка</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Всего завершённых</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(total_done_revenue):.2f}</strong></div>
          <div class="dash-card"><span>Сегодня</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(today_done_revenue):.2f}</strong></div>
          <div class="dash-card"><span>За месяц</span><strong class="stat-value">{CURRENCY_SYMBOL}{float(month_done_revenue):.2f}</strong></div>
        </div>
        <h2>Статусы</h2>
        <div class="dash-grid">
          <div class="dash-card"><span>Ожидает выбора оплаты</span><strong class="stat-value">{pending_orders}</strong></div>
          <div class="dash-card"><span>Ожидает оплаты</span><strong class="stat-value">{awaiting_payment_orders}</strong></div>
          <div class="dash-card"><span>Оплата заявлена</span><strong class="stat-value">{payment_reported_orders}</strong></div>
          <div class="dash-card"><span>Наличными</span><strong class="stat-value">{cash_on_delivery_orders}</strong></div>
          <div class="dash-card"><span>Оплачен</span><strong class="stat-value">{paid_orders}</strong></div>
          <div class="dash-card"><span>Готовится</span><strong class="stat-value">{preparing_orders}</strong></div>
          <div class="dash-card"><span>Готов</span><strong class="stat-value">{done_orders}</strong></div>
          <div class="dash-card"><span>Отменён</span><strong class="stat-value">{cancelled_orders}</strong></div>
        </div>
        """
    else:
        stat_cards = f"""
        <section class="admin-card">
          <h2>Статистика недоступна</h2>
          <p>{html.escape(error_message or 'Подключение к базе данных недоступно.')}</p>
        </section>
        """

    order_rows = ""
    for order_id, username, phone, address, total, payment_status, fulfillment_status in latest_orders:
        order_id_text = html.escape(str(order_id))
        order_rows += f"""
        <tr>
          <td>{order_id_text}</td>
          <td>{html.escape(str(username or '-'))}</td>
          <td>{html.escape(str(phone or '-'))}</td>
          <td>{html.escape(str(address or '-'))}</td>
          <td>EUR {float(total):.2f}</td>
          <td>{payment_status_badge(payment_status)}</td>
          <td>{fulfillment_status_badge(fulfillment_status)}</td>
          <td><a class="view-link" href="/orders/{order_id_text}">Открыть</a></td>
        </tr>
        """
    latest_section = """
        <p>Последних заказов пока нет.</p>
    """
    if order_rows:
        latest_section = f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>ID заказа</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Сумма</th><th>Оплата</th><th>Выполнение</th><th></th></tr>
            {order_rows}
          </table>
        </div>
        """

    low_stock_rows = ""
    for product_id, name, stock_grams, low_stock_threshold_grams in low_stock_products:
        low_stock_rows += f"""
        <tr>
          <td><a class="view-link" href="/products/{product_id}/edit">{html.escape(str(name or '-'))}</a></td>
          <td>{format_stock_grams(stock_grams)}</td>
          <td>{format_stock_grams(low_stock_threshold_grams)}</td>
        </tr>
        """
    low_stock_section = """
        <p>Все товары имеют достаточный остаток.</p>
    """
    if low_stock_rows:
        low_stock_section = f"""
        <div class="dash-table-wrap">
          <table>
            <tr><th>Название товара</th><th>Остаток</th><th>Порог</th></tr>
            {low_stock_rows}
          </table>
        </div>
        """

    funnel_section = """
        <section class="admin-card dash-section">
          <h2>📊 Воронка сегодня</h2>
          <p>Воронка пока недоступна.</p>
        </section>
    """
    if funnel_rows:
        funnel_table_rows = ""
        previous_count = None
        for _, label, users_count in funnel_rows:
            users_value = int(users_count or 0)
            if previous_count is None or previous_count <= 0:
                conversion_text = "-"
            else:
                conversion_text = f"{round(users_value / previous_count * 100)}%"
            funnel_table_rows += f"""
            <tr>
              <td>{html.escape(str(label or '-'))}</td>
              <td>{users_value}</td>
              <td>{conversion_text}</td>
            </tr>
            """
            previous_count = users_value
        funnel_section = f"""
        <section class="admin-card dash-section">
          <h2>📊 Воронка сегодня</h2>
          <div class="dash-table-wrap">
            <table>
              <tr><th>Этап</th><th>Клиентов</th><th>Конверсия</th></tr>
              {funnel_table_rows}
            </table>
          </div>
        </section>
        """

    analytics_section = f"""
        <section class="admin-card dash-section">
          <h2>Аналитика продаж</h2>
          <div class="dash-grid">
            <div class="dash-card">
              <strong>Топ товары</strong>
              {render_product_analytics(top_products)}
            </div>
            <div class="dash-card">
              <strong>Слабые товары</strong>
              {render_product_analytics(worst_products)}
            </div>
            <div class="dash-card">
              <strong>Лучшие клиенты</strong>
              {render_customer_analytics(best_customers, "Заказов")}
            </div>
            <div class="dash-card">
              <strong>Повторные клиенты</strong>
              {render_customer_analytics(repeat_customers, "Заказов")}
            </div>
          </div>
        </section>
    """

    control_center_section = f"""
        <section class="admin-card dash-section">
          <h2>🧠 Центр управления владельца</h2>

          <h3>🔴 Срочно</h3>
          <div class="dash-grid">
            <a class="dash-card priority-urgent" href="/orders?pending_weighing=1">
              <strong>⚖️ Ожидают взвешивания</strong>
              <span>Заказы с товарами, которые ещё нужно взвесить</span>
              <strong class="stat-value">{cc_pending_weighing_count}</strong>
            </a>
            <a class="dash-card priority-urgent" href="/products?filter=low_stock">
              <strong>⚠️ Низкий остаток</strong>
              <span>Товары заканчиваются или уже отсутствуют</span>
              <strong class="stat-value">{cc_low_stock_count}</strong>
            </a>
            <a class="dash-card priority-urgent" href="/logs">
              <strong>🛑 Ошибки за 24 часа</strong>
              <span>Системные ошибки за последние сутки</span>
              <strong class="stat-value">{cc_recent_errors_count}</strong>
            </a>
          </div>

          <h3>🟡 Стоит сделать сегодня</h3>
          <div class="dash-grid">
            <a class="dash-card priority-today" href="/products?filter=no_recommendations">
              <strong>🎯 Без рекомендаций</strong>
              <span>Товары без настроенных сопутствующих рекомендаций</span>
              <strong class="stat-value">{cc_no_recommendations_count}</strong>
            </a>
            <a class="dash-card priority-today" href="/products?filter=no_promotion">
              <strong>🔥 Без акции</strong>
              <span>Товары, не отмеченные как акционные</span>
              <strong class="stat-value">{cc_no_promotion_count}</strong>
            </a>
            <a class="dash-card priority-today" href="/products?filter=no_sales&days=14">
              <strong>📉 Без продаж 14 дней</strong>
              <span>Товары без продаж за последние 14 дней</span>
              <strong class="stat-value">{cc_no_sales_count}</strong>
            </a>
          </div>

          <h3>🔵 Можно улучшить позже</h3>
          <div class="dash-grid">
            <a class="dash-card priority-later" href="/products?filter=no_image">
              <strong>📷 Без фотографии</strong>
              <span>Товары без изображения в карточке</span>
              <strong class="stat-value">{cc_no_image_count}</strong>
            </a>
            <a class="dash-card priority-later" href="/products?filter=no_description">
              <strong>📝 Без описания</strong>
              <span>Товары без текстового описания</span>
              <strong class="stat-value">{cc_no_description_count}</strong>
            </a>
            <a class="dash-card priority-later" href="/products?filter=never_sold">
              <strong>📦 Никогда не продавались</strong>
              <span>Товары без единой продажи за всё время</span>
              <strong class="stat-value">{cc_never_sold_count}</strong>
            </a>
          </div>
        </section>
    """

    return admin_layout(
        ADMIN_PANEL_TITLE,
        f"""
        <section class="dash-hero">
          <div>
            <p class="dash-kicker">Панель управления</p>
            <h1>{html.escape(ADMIN_PANEL_TITLE)}</h1>
            <p>Быстрый доступ к заказам, каталогу, категориям и клиентам.</p>
          </div>
        </section>

        {control_center_section}

        {stat_cards}

        {funnel_section}

        <section class="admin-card dash-section">
          <h2>⚠️ Низкий остаток</h2>
          {low_stock_section}
        </section>

        <section class="admin-card dash-section">
          <h2>Последние заказы</h2>
          {latest_section}
        </section>

        {analytics_section}
        """,
        refresh_seconds=60,
    )

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    if refresh_database_readiness():
        return {"status": "ready"}
    return JSONResponse({"status": "unavailable"}, status_code=503)


@app.get("/broadcasts", response_class=HTMLResponse)
async def broadcasts():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                b.id,
                b.message_text,
                b.status,
                b.target_type,
                b.created_at,
                COUNT(br.id) AS total_recipients,
                COUNT(*) FILTER (WHERE br.status = 'sent') AS sent_count,
                COUNT(*) FILTER (WHERE br.status = 'pending') AS pending_count,
                COUNT(*) FILTER (WHERE br.status = 'blocked') AS blocked_count,
                COUNT(*) FILTER (WHERE br.status = 'failed') AS failed_count
            FROM broadcasts b
            LEFT JOIN broadcast_recipients br ON br.broadcast_id = b.id
            GROUP BY b.id
            ORDER BY b.created_at DESC
            LIMIT 50
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = """
        <section class="admin-card">
          <h1>📨 Рассылка</h1>
          <p><a class="button button-link" href="/broadcasts/new">Новая рассылка</a></p>
        """
        if not rows:
            html_content += "<p>Рассылок пока нет.</p>"
        else:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Статус</th><th>Кому</th><th>Текст</th><th>Получателей</th><th>Отправлено</th><th>Ожидает</th><th>Заблокировано</th><th>Ошибки</th><th>Действия</th></tr>"
            for broadcast_id, message_text, status, target_type, created_at, total_count, sent_count, pending_count, blocked_count, failed_count in rows:
                message_html = html.escape(str(message_text or "-")).replace("\n", "<br>")
                pending_value = int(pending_count or 0)
                action_html = ""
                if pending_value > 0:
                    button_label = ""
                    if str(status or "") == "draft":
                        button_label = "Отправить"
                    elif str(status or "") == "sending":
                        button_label = "Продолжить"
                    elif str(status or "") == "failed":
                        button_label = "Повторить"
                    if button_label:
                        action_html = (
                            f'<form method="post" action="/broadcasts/{int(broadcast_id)}/send" style="display:inline; margin:0; padding:0;">'
                            f'<button class="button secondary" type="submit">{button_label}</button>'
                            '</form>'
                        )
                html_content += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{broadcast_status_label(status)}</td>
                  <td>{broadcast_target_label(target_type)}</td>
                  <td>{message_html}</td>
                  <td>{int(total_count or 0)}</td>
                  <td>{int(sent_count or 0)}</td>
                  <td>{pending_value}</td>
                  <td>{int(blocked_count or 0)}</td>
                  <td>{int(failed_count or 0)}</td>
                  <td>{action_html or '-'}</td>
                </tr>
                """
            html_content += "</table></div>"
        html_content += "</section>"
        return admin_layout("📨 Рассылка", html_content)
    except Exception:
        report_read_error("broadcast_list_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/broadcasts/new", response_class=HTMLResponse)
async def new_broadcast_form():
    try:
        content = """
        <section class="admin-card">
          <h1>Новая рассылка</h1>
          <p><a class="button button-link secondary" href="/broadcasts">← К рассылкам</a></p>
          <form class="admin-form" method="post" action="/broadcasts/new">
            <label>Кому
              <select name="target_type">
                <option value="all_clients">Все клиенты</option>
                <option value="clients_with_orders">Клиенты с заказами</option>
                <option value="active_last_7_days">Активные за 7 дней</option>
                <option value="active_last_30_days">Активные за 30 дней</option>
                <option value="awaiting_payment">Ожидают оплату</option>
              </select>
            </label>
            <label>Текст сообщения
              <textarea name="message_text" rows="8"></textarea>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Создать рассылку</button>
            </div>
          </form>
        </section>
        """
        return admin_layout("Новая рассылка", content)
    except Exception:
        report_read_error("broadcast_form_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/broadcasts/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_broadcast(message_text: str = Form(""), target_type: str = Form("all_clients")):
    if not telegram_actions_enabled():
        return generic_not_found()
    try:
        message_text = (message_text or "").strip()
        if not message_text:
            return admin_error_page("Ошибка", "Текст сообщения не может быть пустым.")
        target_type = str(target_type or "all_clients")
        target_queries = {
            "all_clients": """
                SELECT DISTINCT telegram_id
                FROM clients
                WHERE telegram_id IS NOT NULL
            """,
            "clients_with_orders": """
                SELECT DISTINCT telegram_id
                FROM orders
                WHERE telegram_id IS NOT NULL
            """,
            "active_last_7_days": """
                SELECT DISTINCT telegram_id
                FROM customer_events
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """,
            "active_last_30_days": """
                SELECT DISTINCT telegram_id
                FROM customer_events
                WHERE created_at >= NOW() - INTERVAL '30 days'
            """,
            "awaiting_payment": """
                SELECT DISTINCT telegram_id
                FROM orders
                WHERE telegram_id IS NOT NULL
                  AND payment_status = 'unpaid'
                  AND payment_method IN ('IBAN', 'PayPal')
            """,
        }
        recipients_query = target_queries.get(target_type)
        if not recipients_query:
            return admin_error_page("Ошибка", "Неверный сегмент рассылки.")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO broadcasts (message_text, status, target_type)
            VALUES (%s, 'draft', %s)
            RETURNING id
            """,
            (message_text, target_type),
        )
        broadcast_id = cursor.fetchone()[0]
        cursor.execute(
            f"""
            INSERT INTO broadcast_recipients (broadcast_id, telegram_id)
            SELECT %s, telegram_id
            FROM ({recipients_query}) recipients
            ON CONFLICT (broadcast_id, telegram_id) DO NOTHING
            """,
            (broadcast_id,),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/broadcasts", status_code=303)
    except Exception:
        log_admin_stable_error(
            "/broadcasts/new", "create_broadcast", "broadcast_create_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/broadcasts/{broadcast_id}/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def send_broadcast_route(broadcast_id: int):
    if not telegram_actions_enabled():
        return generic_not_found()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, message_text, status
            FROM broadcasts
            WHERE id = %s
            """,
            (broadcast_id,),
        )
        broadcast = cursor.fetchone()
        if not broadcast:
            return admin_error_page("Ошибка", "Рассылка не найдена.")

        _, message_text, status = broadcast
        if str(status or "") == "sent":
            return RedirectResponse("/broadcasts", status_code=303)

        cursor.execute(
            "UPDATE broadcasts SET status = 'sending', error_message = NULL WHERE id = %s",
            (broadcast_id,),
        )
        cursor.execute(
            """
            SELECT id, telegram_id
            FROM broadcast_recipients
            WHERE broadcast_id = %s
              AND status = 'pending'
            ORDER BY id
            LIMIT 20
            """,
            (broadcast_id,),
        )
        recipients = cursor.fetchall()
        conn.commit()

        for recipient_id, telegram_id in recipients:
            success, result_status = send_broadcast_message(telegram_id, message_text)
            if success:
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'sent',
                        error_message = NULL,
                        sent_at = NOW()
                    WHERE id = %s
                    """,
                    (recipient_id,),
                )
            elif result_status == "blocked":
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'blocked',
                        error_message = %s
                    WHERE id = %s
                    """,
                    ("blocked", recipient_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE broadcast_recipients
                    SET status = 'failed',
                        error_message = %s
                    WHERE id = %s
                    """,
                    ("failed", recipient_id),
                )
            conn.commit()

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM broadcast_recipients
            WHERE broadcast_id = %s
              AND status = 'pending'
            """,
            (broadcast_id,),
        )
        remaining_pending = cursor.fetchone()[0]
        if int(remaining_pending or 0) == 0:
            cursor.execute(
                """
                UPDATE broadcasts
                SET status = 'sent',
                    sent_at = NOW(),
                    error_message = NULL
                WHERE id = %s
                """,
                (broadcast_id,),
            )

        conn.commit()
        return RedirectResponse("/broadcasts", status_code=303)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_stable_error(
            "/broadcasts/{id}/send", "send_broadcast", "broadcast_send_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/channel", response_class=HTMLResponse)
async def channel_posts():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, message_text, status, error_message, created_at, sent_at
            FROM channel_posts
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = """
        <section class="admin-card">
          <h1>📢 Канал</h1>
          <p><a class="button button-link" href="/channel/new">Новый пост</a></p>
        """
        if not rows:
            html_content += "<p>Постов пока нет.</p>"
        else:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Статус</th><th>Текст</th><th>Отправлено</th><th>Ошибка</th><th>Действия</th></tr>"
            for post_id, message_text, status, error_message, created_at, sent_at in rows:
                post_id_int = int(post_id)
                message_html = html.escape(str(message_text or "-")).replace("\n", "<br>")
                error_code = (
                    CHANNEL_POST_FAILED
                    if error_message == CHANNEL_POST_FAILED
                    else INTERNAL_OPERATION_FAILED
                )
                error_html = "-" if not error_message else html.escape(error_code)
                actions_html = "Отправлено"
                if str(status or "") in {"draft", "failed"}:
                    actions_html = (
                        f'<form method="post" action="/channel/{post_id_int}/send" style="display:inline; margin:0; padding:0;">'
                        '<button class="button secondary" type="submit">Отправить</button>'
                        '</form>'
                    )
                actions_html += (
                    f' <form method="post" action="/channel/{post_id_int}/delete" style="display:inline; margin:0; padding:0;">'
                    '<button class="button secondary" type="submit">Удалить</button>'
                    '</form>'
                )
                html_content += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{channel_post_status_label(status)}</td>
                  <td>{message_html}</td>
                  <td>{format_admin_datetime(sent_at)}</td>
                  <td>{error_html}</td>
                  <td>{actions_html}</td>
                </tr>
                """
            html_content += "</table></div>"
        html_content += "</section>"
        return admin_layout("📢 Канал", html_content)
    except Exception:
        report_read_error("channel_list_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/channel/new", response_class=HTMLResponse)
async def new_channel_post_form():
    try:
        content = """
        <section class="admin-card">
          <h1>Новый пост в канал</h1>
          <p><a class="button button-link secondary" href="/channel">← К каналу</a></p>
          <form class="admin-form" method="post" action="/channel/new">
            <label>Текст сообщения
              <textarea name="message_text" rows="8"></textarea>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить черновик</button>
            </div>
          </form>
        </section>
        """
        return admin_layout("Новый пост в канал", content)
    except Exception:
        report_read_error("channel_form_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/channel/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_channel_post(message_text: str = Form("")):
    if not telegram_actions_enabled():
        return generic_not_found()
    try:
        message_text = (message_text or "").strip()
        if not message_text:
            return admin_error_page("Ошибка", "Текст сообщения не может быть пустым.")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO channel_posts (message_text, status)
            VALUES (%s, 'draft')
            RETURNING id
            """,
            (message_text,),
        )
        cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/channel", status_code=303)
    except Exception:
        log_admin_stable_error(
            "/channel/new", "create_channel_post", "channel_create_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/channel/{post_id}/send",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def send_channel_post_route(post_id: int):
    if not telegram_actions_enabled():
        return generic_not_found()
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, message_text, status FROM channel_posts WHERE id = %s",
            (post_id,),
        )
        row = cursor.fetchone()
        if not row:
            return admin_error_page("Ошибка", "Пост не найден.")

        _, message_text, status = row
        if str(status or "") == "sent":
            return RedirectResponse("/channel", status_code=303)

        conn.rollback()
        success, error_message = send_channel_post(message_text)
        if success:
            cursor.execute(
                """
                UPDATE channel_posts
                SET status = 'sent', error_message = NULL, sent_at = NOW()
                WHERE id = %s
                """,
                (post_id,),
            )
        else:
            error_message = (
                error_message
                if error_message == CHANNEL_POST_FAILED
                else CHANNEL_POST_FAILED
            )
            cursor.execute(
                """
                UPDATE channel_posts
                SET status = 'failed', error_message = %s
                WHERE id = %s
                """,
                (error_message, post_id),
            )
        conn.commit()
        return RedirectResponse("/channel", status_code=303)
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_stable_error(
            "/channel/{id}/send", "send_channel_post", CHANNEL_POST_FAILED
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post(
    "/channel/{post_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def delete_channel_post(post_id: int):
    if not telegram_actions_enabled():
        return generic_not_found()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM channel_posts WHERE id = %s",
            (post_id,),
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return admin_error_page("Ошибка", "Пост не найден.")

        cursor.execute("DELETE FROM channel_posts WHERE id = %s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return RedirectResponse("/channel", status_code=303)
    except Exception:
        log_admin_stable_error(
            "/channel/{id}/delete", "delete_channel_post", "channel_delete_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/logs", response_class=HTMLResponse)
async def logs():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                route,
                action,
                error_message,
                created_at
            FROM error_logs
            ORDER BY created_at DESC
            LIMIT 100
            """
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        html_content = "<section class='admin-card'><h1>🧾 Логи ошибок</h1>"
        if rows:
            html_content += "<div class='dash-table-wrap'><table>"
            html_content += "<tr><th>Дата</th><th>Route</th><th>Action</th><th>Ошибка</th><th></th></tr>"
            for log_id, route, action, error_message, created_at in rows:
                error_code = stable_admin_error_code(error_message)
                html_content += (
                    "<tr>"
                    f"<td>{format_admin_datetime(created_at)}</td>"
                    f"<td>{html.escape(str(route or '-'))}</td>"
                    f"<td>{html.escape(str(action or '-'))}</td>"
                    f"<td>{html.escape(error_code)}</td>"
                    f"<td><a class='button button-link' href='/logs/{int(log_id)}'>Открыть</a></td>"
                    "</tr>"
                )
            html_content += "</table></div>"
        else:
            html_content += "<p>Логов пока нет.</p>"
        html_content += "</section>"
        return admin_layout("🧾 Логи ошибок", html_content)
    except Exception:
        report_read_error("error_log_list_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/logs/{log_id}", response_class=HTMLResponse)
async def log_detail(log_id: int):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                route,
                action,
                error_message,
                created_at
            FROM error_logs
            WHERE id = %s
            """,
            (log_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if not row:
            return admin_layout(
                "Лог не найден",
                "<section class='admin-card'><h1>Лог не найден</h1><p><a class='button button-link' href='/logs'>← К логам</a></p></section>",
            )

        _, route, action, error_message, created_at = row
        error_code = stable_admin_error_code(error_message)
        content = f"""
        <section class="admin-card">
          <h1>🧾 Лог ошибки</h1>
          <p><a class="button button-link" href="/logs">← К логам</a></p>
          <div class="detail-grid">
            <div class="detail-field"><strong>Дата</strong>{format_admin_datetime(created_at)}</div>
            <div class="detail-field"><strong>Route</strong>{html.escape(str(route or '-'))}</div>
            <div class="detail-field"><strong>Action</strong>{html.escape(str(action or '-'))}</div>
            <div class="detail-field"><strong>Ошибка</strong>{html.escape(error_code)}</div>
          </div>
        </section>
        """
        return admin_layout("🧾 Лог ошибки", content)
    except Exception:
        report_read_error("error_log_detail_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/orders", response_class=HTMLResponse)
async def orders(
    payment_status_filter: str = "all",
    fulfillment_status_filter: str = "all",
    q: str = "",
    pending_weighing: int = 0,
):
    try:
        if payment_status_filter not in {"all", *ORDER_PAYMENT_STATUS_VALUES}:
            payment_status_filter = "all"
        if fulfillment_status_filter not in {"all", *ORDER_FULFILLMENT_STATUS_VALUES}:
            fulfillment_status_filter = "all"
        is_pending_weighing_filter = pending_weighing == 1
        search_query = q.strip()
        where_clauses = []
        params = []
        if payment_status_filter != "all":
            where_clauses.append("payment_status = %s")
            params.append(payment_status_filter)
        if fulfillment_status_filter != "all":
            where_clauses.append("fulfillment_status = %s")
            params.append(fulfillment_status_filter)
        if search_query:
            search_value = f"%{search_query}%"
            where_clauses.append(
                """
                (
                    CAST(order_id AS TEXT) ILIKE %s
                    OR username ILIKE %s
                    OR phone ILIKE %s
                    OR address ILIKE %s
                )
                """
            )
            params.extend([search_value, search_value, search_value, search_value])
        if is_pending_weighing_filter:
            where_clauses.append("fulfillment_status != 'cancelled'")
            where_clauses.append(
                """
                EXISTS (
                    SELECT 1
                    FROM order_items oi
                    WHERE oi.order_id = orders.order_id
                      AND oi.weight IS NULL
                      AND oi.pricing_mode = 'per_kg'
                )
                """
            )
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT id, order_id, username, phone, address, total, payment_method,
                   payment_status, fulfillment_status, source, created_at
            FROM orders
            {where_sql}
            ORDER BY id DESC
            LIMIT 50
        """, params)
        rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM orders
            WHERE payment_status IN ('unpaid', 'payment_reported')
            """
        )
        attention_orders_count = cursor.fetchone()[0]
        conn.close()

        payment_status_options = {"all": "Все", **PAYMENT_STATUS_LABELS}
        payment_status_options_html = ""
        for value, label in payment_status_options.items():
            selected = "selected" if value == payment_status_filter else ""
            payment_status_options_html += f"<option value=\"{value}\" {selected}>{label}</option>"

        fulfillment_status_options = {"all": "Все", **FULFILLMENT_STATUS_LABELS}
        fulfillment_status_options_html = ""
        for value, label in fulfillment_status_options.items():
            selected = "selected" if value == fulfillment_status_filter else ""
            fulfillment_status_options_html += f"<option value=\"{value}\" {selected}>{label}</option>"

        pending_weighing_banner_html = ""
        if is_pending_weighing_filter:
            pending_weighing_banner_html = (
                "<div class='attention-banner'>⚖️ Показаны только заказы, ожидающие взвешивания. "
                "<a class='button button-link secondary' href='/orders'>Сбросить фильтр</a></div>"
            )

        html = f"""
        <section class='admin-card'>
          <h1>📦 Заказы</h1>
          <div class="form-actions">
            <a class="button" href="/orders/new">➕ Новый заказ</a>
          </div>
          <form class="admin-form" method="get" action="/orders">
            <label>Поиск <input name="q" value="{escape(search_query, quote=True)}" placeholder="№ заказа, клиент, телефон, адрес"/></label>
            <label>Оплата
              <select name="payment_status_filter">{payment_status_options_html}</select>
            </label>
            <label>Выполнение
              <select name="fulfillment_status_filter">{fulfillment_status_options_html}</select>
            </label>
            <div class="form-actions">
              <button type="submit">Показать</button>
              <a class="button button-link secondary" href="/orders">Сбросить</a>
              <a class="button button-link secondary" href="/orders/export.csv?payment_status_filter={escape(payment_status_filter, quote=True)}&fulfillment_status_filter={escape(fulfillment_status_filter, quote=True)}&q={escape(search_query, quote=True)}">Экспорт CSV</a>
            </div>
          </form>
          <div class="attention-banner">⚠️ Требуют внимания: {attention_orders_count} заказов</div>
          {pending_weighing_banner_html}
        """

        if is_pending_weighing_filter and not rows:
            html += "<p>Нет заказов, ожидающих взвешивания.</p></section>"
            return admin_layout("📦 Заказы", html, refresh_seconds=60)

        html += "<div class='dash-table-wrap'><table>"
        html += "<tr><th>ID</th><th>№ заказа</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Сумма</th><th>Оплата</th><th>Выполнение</th><th>Источник</th><th>Способ оплаты</th><th>Создан</th><th>Действия</th></tr>"
        for row in rows:
            (
                id_, order_id, username, phone, address, total, payment_method,
                payment_status, fulfillment_status, source, created_at,
            ) = row
            order_id_text = escape(str(order_id), quote=True)
            order_id_path = urllib.parse.quote(str(order_id), safe="")
            username_text = escape(str(username or "-"), quote=True)
            phone_text = escape(str(phone or "-"), quote=True)
            address_text = escape(str(address or "-"), quote=True)
            payment_method_text = escape(str(payment_method or "-"), quote=True)
            actions = [f"<a class=\"button\" href=\"/orders/{order_id_path}\">Открыть</a>"]
            for action, button_label in payment_actions_for(payment_status):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id_path}/payment/{action}\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">{button_label}</button></form>")
            for action, button_label in fulfillment_actions_for(fulfillment_status):
                actions.append(f"<form method=\"post\" action=\"/orders/{order_id_path}/fulfillment/{action}\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">{button_label}</button></form>")
            actions_html = f"<div class=\"action-group\">{' '.join(actions)}</div>"
            row_class = (
                " class=\"attention-row\""
                if str(payment_status or "") in {"unpaid", "payment_reported"}
                else ""
            )
            html += f"<tr{row_class}><td>{id_}</td><td>{order_id_text}</td><td>{username_text}</td><td>{phone_text}</td><td>{address_text}</td><td>{total:.2f}</td><td>{payment_status_badge(payment_status)}</td><td>{fulfillment_status_badge(fulfillment_status)}</td><td>{order_source_badge(source)}</td><td>{payment_method_text}</td><td>{format_admin_datetime(created_at)}</td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("📦 Заказы", html, refresh_seconds=60)
    except Exception:
        report_read_error("order_list_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/orders/export.csv")
async def orders_export_csv(
    payment_status_filter: str = "all",
    fulfillment_status_filter: str = "all",
    q: str = "",
):
    if payment_status_filter not in {"all", *ORDER_PAYMENT_STATUS_VALUES}:
        payment_status_filter = "all"
    if fulfillment_status_filter not in {"all", *ORDER_FULFILLMENT_STATUS_VALUES}:
        fulfillment_status_filter = "all"
    search_query = q.strip()
    where_clauses = []
    params = []
    if payment_status_filter != "all":
        where_clauses.append("payment_status = %s")
        params.append(payment_status_filter)
    if fulfillment_status_filter != "all":
        where_clauses.append("fulfillment_status = %s")
        params.append(fulfillment_status_filter)
    if search_query:
        search_value = f"%{search_query}%"
        where_clauses.append(
            """
            (
                CAST(order_id AS TEXT) ILIKE %s
                OR username ILIKE %s
                OR phone ILIKE %s
                OR address ILIKE %s
            )
            """
        )
        params.extend([search_value, search_value, search_value, search_value])
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT
            id,
            order_id,
            username,
            phone,
            address,
            total,
            payment_status,
            fulfillment_status,
            source,
            payment_method,
            created_at,
            updated_at,
            payment_selected_at,
            payment_reported_at,
            inventory_deducted,
            status
        FROM orders
        {where_sql}
        ORDER BY id DESC
    """, params)
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=';')
    writer.writerow([
        "id",
        "order_id",
        "username",
        "phone",
        "address",
        "total",
        "payment_status",
        "fulfillment_status",
        "source",
        "payment_method",
        "created_at",
        "updated_at",
        "payment_selected_at",
        "payment_reported_at",
        "inventory_deducted",
        "legacy_status",
    ])

    def excel_text(value):
        if value is None:
            return ""
        return f'="{str(value)}"'

    for row in rows:
        row = list(row)
        row[1] = excel_text(row[1])
        row[3] = excel_text(row[3])
        writer.writerow(row)

    output.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="orders.csv"'}
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers=headers
    )


# ---------------------------------------------------------------------------
# Checkpoint F: admin manual order creation. Uses the SAME Orders v2 model
# and the SAME per-mode pricing rules as Telegram orders (order_creation.
# price_single_line / insert_order) -- this is deliberately not a second
# order system, just a second caller of the shared core.
# ---------------------------------------------------------------------------

def format_order_number(value):
    """Friendly display form for a manually-created order's number, e.g.
    DM-000127. Only used where this checkpoint explicitly asks for it (the
    manual-order form/confirmation) -- existing order number display
    elsewhere is untouched, since Telegram order_id values are a
    completely different, much larger scheme."""
    try:
        return f"DM-{int(value):06d}"
    except (TypeError, ValueError):
        return str(value)


def _manual_order_catalog(cursor):
    cursor.execute(
        """
        SELECT id, name, pricing_mode, price_per_kg, fixed_price, sale_unit
        FROM products
        WHERE is_active = TRUE
        ORDER BY sort_order, name
        """
    )
    products = cursor.fetchall()
    cursor.execute(
        """
        SELECT po.id, po.product_id, po.label, po.price
        FROM product_options po
        JOIN products p ON p.id = po.product_id
        WHERE po.is_active = TRUE AND p.is_active = TRUE
        ORDER BY po.sort_order
        """
    )
    options = cursor.fetchall()
    return products, options


def _search_clients_for_manual_order(cursor, query):
    query = (query or "").strip()[:100]
    if query:
        search_value = f"%{query}%"
        cursor.execute(
            """
            SELECT id, first_name, last_name, phone, telegram_id
            FROM clients
            WHERE first_name ILIKE %s OR last_name ILIKE %s OR phone ILIKE %s
            ORDER BY id DESC
            LIMIT 10
            """,
            (search_value, search_value, search_value),
        )
    else:
        cursor.execute(
            """
            SELECT id, first_name, last_name, phone, telegram_id
            FROM clients
            ORDER BY id DESC
            LIMIT 10
            """
        )
    return cursor.fetchall()


def _manual_order_client_options_html(matching_clients, selected_client_id):
    if not matching_clients:
        return "<p>Клиенты не найдены. Уточните поиск или создайте нового клиента.</p>"
    rows_html = ""
    for client_id, first_name, last_name, phone, telegram_id in matching_clients:
        checked = "checked" if str(selected_client_id or "") == str(client_id) else ""
        full_name = " ".join(part for part in (first_name, last_name) if part) or "Без имени"
        telegram_text = f"Telegram ID: {telegram_id}" if telegram_id else "Telegram ID: —"
        rows_html += (
            '<label class="manual-order-client-option" style="display:block;">'
            f'<input type="radio" name="client_id" value="{client_id}" {checked}/> '
            f'{escape(full_name, quote=True)} · {escape(str(phone or "-"), quote=True)} · {telegram_text}'
            '</label>'
        )
    return rows_html


def _manual_order_product_rows_html(products, options, form_values):
    options_by_product = {}
    for option_id, product_id, label, price in options:
        options_by_product.setdefault(product_id, []).append((option_id, label, price))

    rows = ""
    for product_id, name, pricing_mode, price_per_kg, fixed_price, sale_unit in products:
        name_text = escape(str(name), quote=True)
        if pricing_mode == "per_kg":
            field_name = f"weight_{product_id}"
            existing_value = escape(str(form_values.get(field_name, "")), quote=True)
            input_html = (
                f'<input type="number" name="{field_name}" min="1" step="1" '
                f'value="{existing_value}" placeholder="г"/>'
            )
            price_text = f"{float(price_per_kg or 0):.2f} {CURRENCY_SYMBOL}/кг"
        elif pricing_mode == "fixed":
            field_name = f"qty_{product_id}"
            existing_value = escape(str(form_values.get(field_name, "0") or "0"), quote=True)
            unit_text = escape(str(sale_unit or "шт"), quote=True)
            input_html = (
                f'<input type="number" name="{field_name}" min="0" step="1" '
                f'value="{existing_value}"/> {unit_text}'
            )
            price_text = f"{float(fixed_price or 0):.2f} {CURRENCY_SYMBOL}"
        else:
            product_options = options_by_product.get(product_id, [])
            if not product_options:
                continue
            option_inputs = ""
            for option_id, label, price in product_options:
                field_name = f"optqty_{option_id}"
                existing_value = escape(str(form_values.get(field_name, "0") or "0"), quote=True)
                option_inputs += (
                    f'<div>{escape(str(label), quote=True)} '
                    f'({float(price):.2f} {CURRENCY_SYMBOL}) '
                    f'<input type="number" name="{field_name}" min="0" step="1" '
                    f'value="{existing_value}"/></div>'
                )
            input_html = option_inputs
            price_text = "варианты"
        rows += f"<tr><td>{name_text}</td><td>{price_text}</td><td>{input_html}</td></tr>"
    return rows


def _render_new_order_form(products, options, matching_clients, client_query,
                            form_values=None, errors=None):
    form_values = form_values or {}
    errors = errors or []

    def fv(name, default=""):
        return escape(str(form_values.get(name, default) or default), quote=True)

    error_html = ""
    if errors:
        items_html = "".join(f"<li>{escape(str(e), quote=True)}</li>" for e in errors)
        error_html = (
            f'<div class="attention-banner">⚠️ Проверьте данные заказа:<ul>{items_html}</ul></div>'
        )

    source_options_html = ""
    for value in ORDER_SOURCE_VALUES:
        selected = "selected" if form_values.get("source") == value else ""
        source_options_html += (
            f'<option value="{value}" {selected}>{ORDER_SOURCE_LABELS.get(value, value)}</option>'
        )

    customer_mode = form_values.get("customer_mode") or "existing"
    existing_checked = "checked" if customer_mode != "new" else ""
    new_checked = "checked" if customer_mode == "new" else ""

    delivery_method = form_values.get("delivery_method") or "pickup"
    pickup_checked = "checked" if delivery_method != "delivery" else ""
    delivery_checked = "checked" if delivery_method == "delivery" else ""

    payment_method_options_html = "".join(
        f'<option value="{value}" {"selected" if form_values.get("payment_method") == value else ""}>{value}</option>'
        for value in ORDER_PAYMENT_METHOD_VALUES
    )
    payment_status_options_html = "".join(
        f'<option value="{value}" {"selected" if (form_values.get("payment_status") or "unpaid") == value else ""}>'
        f'{PAYMENT_STATUS_LABELS.get(value, value)}</option>'
        for value in ("unpaid", "paid")
    )

    client_options_html = _manual_order_client_options_html(
        matching_clients, form_values.get("client_id")
    )
    product_rows_html = _manual_order_product_rows_html(products, options, form_values)

    return f"""
    <section class="admin-card">
      <h1>➕ Новый заказ</h1>
      <p><a href="/orders">← К заказам</a></p>
      {error_html}
      <form class="admin-form" method="get">
        <label>Поиск клиента (имя или телефон)
          <input name="client_query" value="{escape(client_query, quote=True)}"/>
        </label>
        <div class="form-actions"><button class="button secondary" type="submit">Искать</button></div>
      </form>
      <form class="admin-form" method="post" action="/orders/new">
        <h2>Источник заказа</h2>
        <label>Канал <select name="source">{source_options_html}</select></label>
        <label>Референс источника
          <input name="source_reference" value="{fv('source_reference')}" placeholder="@instagram, номер WhatsApp..."/>
        </label>

        <h2>Клиент</h2>
        <label><input type="radio" name="customer_mode" value="existing" {existing_checked}/> Существующий клиент</label>
        <label><input type="radio" name="customer_mode" value="new" {new_checked}/> Новый клиент</label>
        <div class="manual-order-existing-client">
          {client_options_html}
        </div>
        <div class="manual-order-new-client">
          <label>Имя <input name="new_first_name" value="{fv('new_first_name')}"/></label>
          <label>Фамилия (необязательно) <input name="new_last_name" value="{fv('new_last_name')}"/></label>
          <label>Телефон <input name="new_phone" value="{fv('new_phone')}"/></label>
          <label>Telegram ID (необязательно) <input name="new_telegram_id" value="{fv('new_telegram_id')}"/></label>
        </div>

        <h2>Доставка</h2>
        <label><input type="radio" name="delivery_method" value="pickup" {pickup_checked}/> Самовывоз</label>
        <label><input type="radio" name="delivery_method" value="delivery" {delivery_checked}/> Доставка</label>
        <div class="manual-order-delivery-fields">
          <label>Улица <input name="delivery_street" value="{fv('delivery_street')}"/></label>
          <label>Дом/корпус <input name="delivery_house_number" value="{fv('delivery_house_number')}"/></label>
          <label>Индекс <input name="delivery_postcode" value="{fv('delivery_postcode')}"/></label>
          <label>Город <input name="delivery_city" value="{fv('delivery_city')}"/></label>
          <label>Страна <input name="delivery_country" value="{fv('delivery_country')}"/></label>
          <label>Комментарий <input name="delivery_notes" value="{fv('delivery_notes')}"/></label>
        </div>

        <h2>Товары</h2>
        <div class="dash-table-wrap"><table>
          <tr><th>Товар</th><th>Цена</th><th>Количество / вес</th></tr>
          {product_rows_html}
        </table></div>

        <h2>Оплата</h2>
        <label>Способ оплаты <select name="payment_method">{payment_method_options_html}</select></label>
        <label>Статус оплаты <select name="payment_status">{payment_status_options_html}</select></label>

        <div class="form-actions">
          <button class="button" type="submit">Создать заказ</button>
          <a class="button button-link secondary" href="/orders">Отмена</a>
        </div>
      </form>
    </section>
    """


@app.get("/orders/new", response_class=HTMLResponse)
async def new_order_form(client_query: str = ""):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        products, options = _manual_order_catalog(cursor)
        matching_clients = _search_clients_for_manual_order(cursor, client_query)
        conn.close()
        page = _render_new_order_form(
            products, options, matching_clients, client_query
        )
        return admin_layout("➕ Новый заказ", page)
    except Exception:
        report_read_error("manual_order_form_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/orders/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_manual_order(request: Request):
    form = await request.form()
    form_values = {key: form.get(key, "") for key in form.keys()}
    errors = []
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        products, options = _manual_order_catalog(cursor)

        source = str(form.get("source") or "")
        if source not in ORDER_SOURCE_VALUES:
            errors.append("Выберите корректный источник заказа.")
        source_reference = (str(form.get("source_reference") or "")).strip() or None

        customer_mode = form.get("customer_mode") or "existing"
        client_id = None
        customer_name = None
        phone = None
        telegram_id = None
        username = None

        if customer_mode == "new":
            first_name = str(form.get("new_first_name") or "").strip()
            last_name = str(form.get("new_last_name") or "").strip()
            new_phone = str(form.get("new_phone") or "").strip()
            raw_telegram_id = str(form.get("new_telegram_id") or "").strip()
            if not first_name:
                errors.append("Укажите имя нового клиента.")
            if not new_phone:
                errors.append("Укажите телефон нового клиента.")
            new_telegram_id_value = None
            if raw_telegram_id:
                try:
                    new_telegram_id_value = int(raw_telegram_id)
                except ValueError:
                    errors.append("Telegram ID должен быть числом.")
                else:
                    cursor.execute(
                        "SELECT id FROM clients WHERE telegram_id = %s",
                        (new_telegram_id_value,),
                    )
                    if cursor.fetchone():
                        errors.append(
                            "Клиент с этим Telegram ID уже существует. "
                            "Выберите его через поиск существующих клиентов."
                        )
            if not errors:
                cursor.execute(
                    """
                    INSERT INTO clients (telegram_id, first_name, last_name, phone)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (new_telegram_id_value, first_name, last_name or None, new_phone),
                )
                client_id = cursor.fetchone()[0]
                customer_name = " ".join(part for part in (first_name, last_name) if part)
                phone = new_phone
                telegram_id = new_telegram_id_value
        else:
            raw_client_id = form.get("client_id")
            if not raw_client_id:
                errors.append("Выберите существующего клиента через поиск.")
            else:
                try:
                    client_id = int(raw_client_id)
                except (TypeError, ValueError):
                    errors.append("Некорректный клиент.")
                    client_id = None
                else:
                    cursor.execute(
                        "SELECT first_name, last_name, phone, telegram_id, username "
                        "FROM clients WHERE id = %s",
                        (client_id,),
                    )
                    client_row = cursor.fetchone()
                    if not client_row:
                        errors.append("Выбранный клиент не найден.")
                        client_id = None
                    else:
                        existing_first_name, existing_last_name, phone, telegram_id, username = client_row
                        customer_name = " ".join(
                            part for part in (existing_first_name, existing_last_name) if part
                        ) or None

        delivery_method = form.get("delivery_method") or "pickup"
        if delivery_method not in ORDER_DELIVERY_METHOD_VALUES:
            errors.append("Выберите способ доставки.")
        delivery_street = delivery_house_number = delivery_postcode = None
        delivery_city = delivery_country = delivery_notes = None
        if delivery_method == "delivery":
            delivery_street = str(form.get("delivery_street") or "").strip() or None
            delivery_house_number = str(form.get("delivery_house_number") or "").strip() or None
            delivery_postcode = str(form.get("delivery_postcode") or "").strip() or None
            delivery_city = str(form.get("delivery_city") or "").strip() or None
            delivery_country = str(form.get("delivery_country") or "").strip() or None
            delivery_notes = str(form.get("delivery_notes") or "").strip() or None
            if not (delivery_street and delivery_house_number and delivery_postcode and delivery_city):
                errors.append(
                    "Для доставки укажите улицу, дом/корпус, индекс и город."
                )

        payment_method = form.get("payment_method") or None
        if payment_method not in ORDER_PAYMENT_METHOD_VALUES:
            errors.append("Выберите способ оплаты.")
        payment_status = form.get("payment_status") or "unpaid"
        if payment_status not in ("unpaid", "paid"):
            errors.append("Недопустимый статус оплаты.")

        priced_items = []
        products_by_id = {row[0]: row for row in products}

        for product_id, name, pricing_mode, price_per_kg, fixed_price, sale_unit in products:
            product_dict = {
                "pricing_mode": pricing_mode,
                "price_per_kg": price_per_kg,
                "fixed_price": fixed_price,
            }
            if pricing_mode == "per_kg":
                raw_weight = str(form.get(f"weight_{product_id}") or "").strip()
                if not raw_weight:
                    continue
                try:
                    weight = int(raw_weight)
                except ValueError:
                    errors.append(f"Некорректный вес для товара «{name}».")
                    continue
                if weight <= 0:
                    continue
                price, mode, snapshot = price_single_line(product_dict, weight, None, None)
                priced_items.append({
                    "product_id": product_id, "product_name": name, "weight": weight,
                    "option_id": None, "price": price, "pricing_mode": mode,
                    "price_per_kg_snapshot": snapshot,
                })
            elif pricing_mode == "fixed":
                raw_qty = str(form.get(f"qty_{product_id}") or "0").strip()
                try:
                    qty = int(raw_qty or 0)
                except ValueError:
                    errors.append(f"Некорректное количество для товара «{name}».")
                    continue
                for _ in range(max(qty, 0)):
                    price, mode, snapshot = price_single_line(product_dict, None, None, None)
                    priced_items.append({
                        "product_id": product_id, "product_name": name, "weight": None,
                        "option_id": None, "price": price, "pricing_mode": mode,
                        "price_per_kg_snapshot": snapshot,
                    })

        for option_id, product_id, label, option_price in options:
            raw_qty = str(form.get(f"optqty_{option_id}") or "0").strip()
            try:
                qty = int(raw_qty or 0)
            except ValueError:
                errors.append(f"Некорректное количество для варианта «{label}».")
                continue
            if qty <= 0:
                continue
            product_row = products_by_id.get(product_id)
            product_name = product_row[1] if product_row else label
            product_dict = {"pricing_mode": "options"}
            for _ in range(qty):
                price, mode, snapshot = price_single_line(
                    product_dict, None, option_id, option_price
                )
                priced_items.append({
                    "product_id": product_id, "product_name": product_name, "weight": None,
                    "option_id": option_id, "price": price, "pricing_mode": mode,
                    "price_per_kg_snapshot": snapshot,
                })

        if not priced_items and not errors:
            errors.append("Добавьте хотя бы один товар в заказ.")

        if errors:
            conn.rollback()
            matching_clients = _search_clients_for_manual_order(
                cursor, str(form.get("client_query") or "")
            )
            page = _render_new_order_form(
                products, options, matching_clients, str(form.get("client_query") or ""),
                form_values=form_values, errors=errors,
            )
            return admin_layout("➕ Новый заказ", page)

        order_id, _total = insert_order(
            cursor,
            source=source,
            source_reference=source_reference,
            priced_items=priced_items,
            client_id=client_id,
            telegram_id=telegram_id,
            username=username,
            customer_name=customer_name,
            phone=phone,
            address=None,
            payment_method=payment_method,
            payment_status=payment_status,
            delivery_method=delivery_method,
            delivery_street=delivery_street,
            delivery_house_number=delivery_house_number,
            delivery_postcode=delivery_postcode,
            delivery_city=delivery_city,
            delivery_country=delivery_country,
            delivery_notes=delivery_notes,
        )

        log_order_event(
            cursor,
            order_id,
            "order_created",
            f"Заказ создан вручную администратором. Источник: {source}. "
            f"Номер: {format_order_number(order_id)}.",
        )
        conn.commit()
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    except OrderCreationError:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return admin_error_page(
            "Ошибка", "Не удалось создать заказ: некорректные данные заказа."
        )
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_stable_error(
            "/orders/new", "create_manual_order", MANUAL_ORDER_CREATE_FAILED,
        )
        return admin_error_page(
            "Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже."
        )
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post(
    "/orders/{order_id}/payment/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def update_order_payment_status(order_id: str, action: str):
    """Checkpoint E: payment_status is the sole runtime authority here --
    unpaid -> payment_reported -> paid -> refunded (direct unpaid -> paid is
    also allowed for manual admin confirmation). Never touches inventory --
    stock is only ever moved by the fulfillment 'packed'/'cancelled' actions
    below. Legacy orders.status is no longer written (frozen historical
    data only)."""
    if action not in PAYMENT_STATUS_LABELS:
        return admin_error_page("Недопустимое действие", "Операция не может быть выполнена для этого заказа.")
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payment_status, telegram_id "
            "FROM orders WHERE order_id = %s",
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            return RedirectResponse("/orders", status_code=303)

        current_payment_status = str(row[0] or "unpaid")
        telegram_id = row[1]

        if action not in PAYMENT_TRANSITIONS.get(current_payment_status, set()):
            print("order_payment_transition_rejected")
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        cursor.execute(
            "UPDATE orders SET payment_status = %s, updated_at = NOW() WHERE order_id = %s",
            (action, order_id)
        )
        log_order_event(
            cursor,
            order_id,
            "payment_status_changed",
            f"Оплата: {payment_status_label(current_payment_status)} → {payment_status_label(action)}"
        )
        conn.commit()

        # Customer-facing notification: only 'paid' has an established
        # message (see send_order_status_notification's fixed vocabulary).
        # payment_reported/refunded have never had a customer notification
        # and none is introduced here.
        if action == "paid":
            try:
                send_order_status_notification_and_record(telegram_id, order_id, "paid")
            except Exception:
                print(ORDER_NOTIFICATION_FAILED)
                try:
                    record_notification_event(
                        order_id,
                        "notification_failed",
                        f"Не удалось отправить уведомление о статусе: {admin_status_label('paid')}"
                    )
                except Exception:
                    print(ORDER_NOTIFICATION_FAILED)

        return admin_layout(
            "✅ Статус оплаты обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Статус оплаты обновлён</h1>
              <p>Статус оплаты заказа успешно изменён.</p>
              <div class="form-actions">
                <a class="button button-link" href="/orders">← К заказам</a>
                <a class="button button-link secondary" href="/orders/{order_id}">Открыть заказ</a>
              </div>
            </section>
            """,
        )
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_stable_error(
            "/orders/{order_id}/payment/{action}",
            "update_order_payment_status",
            ORDER_PAYMENT_UPDATE_FAILED,
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post(
    "/orders/{order_id}/fulfillment/{action}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def update_order_fulfillment_status(order_id: str, action: str):
    """Checkpoint E: fulfillment_status is the sole runtime authority here
    -- new -> confirmed -> picking -> packed -> ready_to_ship -> shipped ->
    delivered, with 'cancelled' reachable from any non-terminal state.
    Never gated by payment_status -- a cash/COD order must be able to
    progress through fulfillment while payment_status stays 'unpaid'.
    Legacy orders.status is no longer written (frozen historical data
    only).

    Inventory is deducted exactly once, atomically, when this transition
    reaches 'packed' (not on any payment change), after confirming every
    per_kg line that requires weighing already has a real weight. Inventory
    is restored exactly once when a 'cancelled' transition is applied to an
    order that had already been deducted."""
    if action not in FULFILLMENT_STATUS_LABELS:
        return admin_error_page("Недопустимое действие", "Операция не может быть выполнена для этого заказа.")
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT fulfillment_status, inventory_deducted, "
            "inventory_restored, telegram_id FROM orders WHERE order_id = %s",
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            return RedirectResponse("/orders", status_code=303)

        current_fulfillment_status = str(row[0] or "new")
        inventory_deducted = bool(row[1])
        inventory_restored = bool(row[2])
        telegram_id = row[3]

        if action not in FULFILLMENT_TRANSITIONS.get(current_fulfillment_status, set()):
            print("order_fulfillment_transition_rejected")
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        if action == "packed" and not inventory_deducted:
            if _order_has_pending_weighing(cursor, order_id):
                conn.rollback()
                return admin_error_page(
                    "Требуется взвешивание",
                    "Перед упаковкой взвесьте все весовые товары в заказе.",
                )
            try:
                affected_product_ids = deduct_order_inventory(cursor, order_id)
            except InsufficientStockError:
                conn.rollback()
                return admin_error_page(
                    "Недостаточно товара на складе",
                    "Пополните остаток или измените состав заказа и повторите попытку.",
                )
            if affected_product_ids:
                cursor.execute(
                    """
                    UPDATE products
                    SET is_out_of_stock = TRUE
                    WHERE pricing_mode = 'per_kg'
                      AND id = ANY(%s)
                      AND stock_grams <= 0
                    """,
                    (affected_product_ids,),
                )
            sync_low_stock_alert_state(cursor, affected_product_ids)
            cursor.execute(
                """
                UPDATE orders
                SET inventory_deducted = TRUE,
                    inventory_deducted_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,)
            )
            log_order_event(
                cursor,
                order_id,
                "inventory_deducted",
                "Склад списан по заказу."
            )

        if action == "cancelled" and inventory_deducted and not inventory_restored:
            affected_product_ids = restore_order_inventory(cursor, order_id)
            if affected_product_ids:
                cursor.execute(
                    """
                    UPDATE products
                    SET is_out_of_stock = FALSE
                    WHERE pricing_mode = 'per_kg'
                      AND id = ANY(%s)
                      AND stock_grams > 0
                    """,
                    (affected_product_ids,),
                )
            sync_low_stock_alert_state(cursor, affected_product_ids)
            cursor.execute(
                """
                UPDATE orders
                SET inventory_restored = TRUE,
                    inventory_restored_at = NOW()
                WHERE order_id = %s
                """,
                (order_id,)
            )
            log_order_event(
                cursor,
                order_id,
                "stock_restored",
                "Остаток восстановлен после отмены заказа."
            )

        cursor.execute(
            "UPDATE orders SET fulfillment_status = %s, updated_at = NOW() WHERE order_id = %s",
            (action, order_id)
        )
        log_order_event(
            cursor,
            order_id,
            "fulfillment_status_changed",
            f"Статус выполнения: {fulfillment_status_label(current_fulfillment_status)} → {fulfillment_status_label(action)}"
        )
        conn.commit()

        # Customer-facing notification: only the three fulfillment actions
        # that have an established message fire one (see
        # send_order_status_notification's fixed vocabulary) -- 'picking'
        # is the first point a customer is told their order is being
        # prepared, matching the single notification the old
        # picking/packed/ready_to_ship/shipped -> 'preparing' mapping used
        # to send exactly once. confirmed/packed/ready_to_ship/shipped
        # never notify, same as before.
        notification_key = {
            "picking": "preparing",
            "delivered": "done",
            "cancelled": "cancelled",
        }.get(action)
        if notification_key is not None:
            try:
                send_order_status_notification_and_record(telegram_id, order_id, notification_key)
            except Exception:
                print(ORDER_NOTIFICATION_FAILED)
                try:
                    record_notification_event(
                        order_id,
                        "notification_failed",
                        f"Не удалось отправить уведомление о статусе: {admin_status_label(notification_key)}"
                    )
                except Exception:
                    print(ORDER_NOTIFICATION_FAILED)

        return admin_layout(
            "✅ Статус выполнения обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Статус выполнения обновлён</h1>
              <p>Статус выполнения заказа успешно изменён.</p>
              <div class="form-actions">
                <a class="button button-link" href="/orders">← К заказам</a>
                <a class="button button-link secondary" href="/orders/{order_id}">Открыть заказ</a>
              </div>
            </section>
            """,
        )
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        log_admin_stable_error(
            "/orders/{order_id}/fulfillment/{action}",
            "update_order_fulfillment_status",
            ORDER_FULFILLMENT_UPDATE_FAILED,
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(order_id: str):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                id,
                order_id,
                username,
                phone,
                address,
                total,
                status,
                payment_method,
                created_at,
                updated_at,
                payment_selected_at,
                payment_reminded_at,
                payment_reported_at,
                inventory_deducted,
                inventory_deducted_at,
                order_note,
                payment_status,
                fulfillment_status,
                source
            FROM orders
            WHERE order_id = %s
            """,
            (order_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Заказ не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Заказ не найден</h1>
                  <p>Такой заказ не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/orders">← К заказам</a>
                  </div>
                </section>
                """,
            )

        (
            id_,
            order_id,
            username,
            phone,
            address,
            total,
            status,
            payment_method,
            created_at,
            updated_at,
            payment_selected_at,
            payment_reminded_at,
            payment_reported_at,
            inventory_deducted,
            inventory_deducted_at,
            order_note,
            payment_status,
            fulfillment_status,
            source,
        ) = row
        try:
            cursor.execute(
                """
                SELECT oi.id, oi.product_name, oi.weight, oi.price, po.label,
                       p.price_per_kg, oi.pricing_mode, oi.price_per_kg_snapshot
                FROM order_items oi
                LEFT JOIN product_options po ON po.id = oi.option_id
                LEFT JOIN products p ON p.id = oi.product_id
                WHERE oi.order_id = %s
                ORDER BY oi.id
                """,
                (order_id,),
            )
            items = cursor.fetchall()
        except Exception:
            cursor.execute(
                """
                SELECT id, product_name, weight, price
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
                """,
                (order_id,),
            )
            items = [(item_id, product_name, weight, price, None, None) for item_id, product_name, weight, price in cursor.fetchall()]
        cursor.execute(
            """
            SELECT event_type, event_text, created_at
            FROM order_events
            WHERE order_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (order_id,),
        )
        order_events = cursor.fetchall()
        conn.close()

        order_id_text = escape(str(order_id), quote=True)
        order_id_path = urllib.parse.quote(str(order_id), safe="")
        username_text = escape(str(username or "-"), quote=True)
        phone_text = escape(str(phone or "-"), quote=True)
        address_text = escape(str(address or "-"), quote=True)
        payment_method_text = escape(str(payment_method or "-"), quote=True)

        html = f"<section class='admin-card'><h1>Заказ {order_id_text}</h1><div class='detail-grid'>"
        html += f"<div class='detail-field'><strong>№ заказа</strong>{order_id_text}</div>"
        html += f"<div class='detail-field'><strong>Клиент</strong>{username_text}</div>"
        html += f"<div class='detail-field'><strong>Телефон</strong>{phone_text}</div>"
        html += f"<div class='detail-field'><strong>Адрес</strong>{address_text}</div>"
        html += f"<div class='detail-field'><strong>Оплата</strong>{payment_status_badge(payment_status)}</div>"
        html += f"<div class='detail-field'><strong>Выполнение</strong>{fulfillment_status_badge(fulfillment_status)}</div>"
        html += f"<div class='detail-field'><strong>Источник</strong>{order_source_badge(source)}</div>"
        html += f"<div class='detail-field'><strong>Способ оплаты</strong>{payment_method_text}</div>"
        html += f"<div class='detail-field'><strong>Статус (устар.)</strong>{admin_status_badge(status)}</div>"
        html += "</div></section>"

        payment_action_buttons = ""
        for action, button_label in payment_actions_for(payment_status):
            payment_action_buttons += (
                f'<form method="post" action="/orders/{order_id_path}/payment/{action}" '
                'style="display:inline; margin:0; padding:0;">'
                f'<button class="button secondary" type="submit">{button_label}</button>'
                '</form>'
            )
        fulfillment_action_buttons = ""
        for action, button_label in fulfillment_actions_for(fulfillment_status):
            fulfillment_action_buttons += (
                f'<form method="post" action="/orders/{order_id_path}/fulfillment/{action}" '
                'style="display:inline; margin:0; padding:0;">'
                f'<button class="button secondary" type="submit">{button_label}</button>'
                '</form>'
            )
        payment_actions_html = (
            f'<div class="form-actions">{payment_action_buttons}</div>' if payment_action_buttons else ""
        )
        fulfillment_actions_html = (
            f'<div class="form-actions">{fulfillment_action_buttons}</div>' if fulfillment_action_buttons else ""
        )
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Оплата</h2>
          <div class='detail-grid'>
            <div class='detail-field'><strong>Способ оплаты</strong>{payment_method_text}</div>
            <div class='detail-field'><strong>Оплата выбрана</strong>{format_admin_datetime(payment_selected_at)}</div>
            <div class='detail-field'><strong>Клиент сообщил об оплате</strong>{format_admin_datetime(payment_reported_at)}</div>
            <div class='detail-field'><strong>Текущий статус оплаты</strong>{payment_status_badge(payment_status)}</div>
          </div>
          {payment_actions_html}
        </section>
        <section class='admin-card dash-section'>
          <h2>Выполнение</h2>
          <div class='detail-grid'>
            <div class='detail-field'><strong>Текущий статус выполнения</strong>{fulfillment_status_badge(fulfillment_status)}</div>
            <div class='detail-field'><strong>Источник заказа</strong>{order_source_badge(source)}</div>
          </div>
          {fulfillment_actions_html}
        </section>
        """
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Заметка администратора</h2>
          <form class="admin-form" method="post" action="/orders/{order_id_path}/note">
            <label>Заметка
              <textarea name="order_note" rows="4">{escape(str(order_note or ""), quote=True)}</textarea>
              <small>Видно только в админке.</small>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить заметку</button>
            </div>
          </form>
        </section>
        """
        timeline_events = [
            ("Заказ создан", created_at),
            ("Выбран способ оплаты", payment_selected_at),
            ("Отправлено напоминание об оплате", payment_reminded_at),
            ("Клиент сообщил об оплате", payment_reported_at),
            ("Склад списан", inventory_deducted_at if inventory_deducted else None),
            ("Последнее обновление", updated_at),
        ]
        timeline_events = sorted(
            [(label, value) for label, value in timeline_events if value],
            key=lambda item: item[1],
        )
        timeline_rows = ""
        for label, value in timeline_events:
            timeline_rows += f"""
            <tr>
              <td>{escape(str(label), quote=True)}</td>
              <td>{format_admin_datetime(value)}</td>
            </tr>
            """
        if not timeline_rows:
            timeline_rows = "<tr><td colspan='2'>Нет данных</td></tr>"
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Таймлайн заказа</h2>
          <p>Показаны системные отметки времени. Полный журнал действий будет добавлен позже.</p>
          <div class='dash-table-wrap'>
            <table>
              <tr><th>Событие</th><th>Время</th></tr>
              {timeline_rows}
            </table>
          </div>
        </section>
        """
        event_rows = ""
        for event_type, event_text, event_created_at in order_events:
            event_rows += f"""
            <tr>
              <td>{format_admin_datetime(event_created_at)}</td>
              <td>{escape(str(event_text or '-'), quote=True)}<br><span class="muted">{escape(admin_event_type_label(event_type), quote=True)}</span></td>
            </tr>
            """
        if not event_rows:
            event_rows = "<tr><td colspan='2'>Пока нет записей журнала.</td></tr>"
        html += f"""
        <section class='admin-card dash-section'>
          <h2>Журнал событий</h2>
          <div class='dash-table-wrap'>
            <table>
              <tr><th>Время</th><th>Событие</th></tr>
              {event_rows}
            </table>
          </div>
        </section>
        """
        if items:
            html += "<section class='admin-card dash-section'><h2>Товары</h2><div class='dash-table-wrap'><table>"
            html += "<tr><th>Товар</th><th>Вариант / вес</th><th>Итого</th></tr>"
            for (
                item_id, product_name, weight, price, option_label, price_per_kg,
                pricing_mode, price_per_kg_snapshot,
            ) in items:
                item_label = option_label if option_label else f"{weight} г"
                product_name_text = escape(str(product_name or "-"), quote=True)
                item_label_text = escape(str(item_label or "-"), quote=True)
                if weight is None and pricing_mode == "per_kg":
                    # Preview must match what weigh_order_item will actually
                    # charge: the order line's own snapshot, never the
                    # product's current price_per_kg.
                    effective_price_per_kg = (
                        price_per_kg_snapshot
                        if price_per_kg_snapshot is not None
                        else price_per_kg
                    )
                    price_per_kg_value = (
                        float(effective_price_per_kg)
                        if effective_price_per_kg is not None
                        else 0
                    )
                    preview_id = f"price_preview_{item_id}"
                    photo_preview_id = f"photo_preview_{item_id}"
                    html += (
                        f"<tr><td>{product_name_text}</td><td>{item_label_text}</td><td>"
                        f'<form method="post" action="/orders/{order_id_path}/items/{item_id}/weigh" '
                        'enctype="multipart/form-data" data-csrf-multipart="header" '
                        'style="display:flex; gap:4px; align-items:center; flex-wrap:wrap; margin:0;">'
                        f'<input type="number" name="final_weight_grams" placeholder="г" required style="width:70px;" '
                        f'data-price-per-kg="{price_per_kg_value}" '
                        f'oninput="document.getElementById(\'{preview_id}\').innerText = \'≈ \' + ((parseFloat(this.value || 0) / 1000) * parseFloat(this.dataset.pricePerKg || 0)).toFixed(2) + \' €\'"/>'
                        f'<span id="{preview_id}" style="min-width:70px; color:#6b7280;">≈ 0.00 €</span>'
                        '<input type="file" name="photo" accept="image/*" capture="environment" style="max-width:110px;" '
                        f'onchange="var img = document.getElementById(\'{photo_preview_id}\'); '
                        'if (this.files && this.files[0]) { img.src = URL.createObjectURL(this.files[0]); img.style.display = \'inline-block\'; } '
                        'else { img.style.display = \'none\'; }"/>'
                        f'<img id="{photo_preview_id}" style="display:none; max-width:40px; max-height:40px; border-radius:4px; object-fit:cover;"/>'
                        '<button class="button secondary" type="submit">Подтвердить вес</button>'
                        '</form>'
                        '<noscript>'
                        f'<form method="post" action="/orders/{order_id_path}/items/{item_id}/weigh" '
                        'style="display:flex; gap:4px; align-items:center; flex-wrap:wrap; margin-top:8px;">'
                        '<input type="number" name="final_weight_grams" placeholder="г" required style="width:70px;"/>'
                        '<button class="button secondary" type="submit">Подтвердить без фото</button>'
                        '</form>'
                        '</noscript>'
                        '</td></tr>'
                    )
                elif weight is None:
                    html += (
                        f"<tr><td>{product_name_text}</td><td>{item_label_text}</td>"
                        "<td>Взвешивание доступно только для товаров per_kg.</td></tr>"
                    )
                else:
                    html += f"<tr><td>{product_name_text}</td><td>{item_label_text}</td><td>{price:.2f} €</td></tr>"
            html += "</table></div>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        else:
            html += "<section class='admin-card dash-section'><p>Товары не найдены.</p>"
            html += f"<p><strong>Итого: {total:.2f} €</strong></p></section>"
        html += f"<p><a class='button button-link' href=\"/orders\">← К заказам</a></p>"
        return admin_layout(f"Заказ {order_id}", html)
    except Exception:
        report_read_error("order_detail_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/orders/{order_id}/note",
    dependencies=[Depends(require_admin_csrf)],
)
async def update_order_note(order_id: str, order_note: str = Form("")):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE orders
            SET order_note = %s,
                updated_at = NOW()
            WHERE order_id = %s
            """,
            (order_note, order_id),
        )
        log_order_event(
            cursor,
            order_id,
            "order_note_updated",
            "Заметка администратора обновлена."
        )
        conn.commit()
        conn.close()
    except Exception:
        print(ORDER_NOTE_UPDATE_FAILED)
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


@app.post(
    "/orders/{order_id}/items/{item_id}/weigh",
    dependencies=[Depends(require_admin_csrf)],
)
async def weigh_order_item(
    order_id: str,
    item_id: int,
    final_weight_grams: int = Form(...),
    photo: UploadFile = File(None),
):
    if final_weight_grams <= 0:
        return admin_error_page(
            "Некорректный вес", "Финальный вес должен быть больше нуля."
        )
    has_pending_weighing = True
    telegram_id = None
    order_total = None
    order_items_summary = []
    photo_bytes = None
    photo_filename = None
    photo_content_type = None
    if photo is not None and photo.filename:
        photo_bytes = await photo.read()
        photo_filename = photo.filename
        photo_content_type = photo.content_type
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT p.price_per_kg, oi.pricing_mode, p.id, oi.price_per_kg_snapshot
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.id = %s
              AND oi.order_id = %s
              AND oi.weight IS NULL
            """,
            (item_id, order_id),
        )
        product_row = cursor.fetchone()
        if not product_row:
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        price_per_kg, pricing_mode, product_id, price_per_kg_snapshot = product_row
        try:
            validate_weight_inventory_modes(
                [(product_id, final_weight_grams, pricing_mode)]
            )
        except ValueError:
            conn.rollback()
            return admin_error_page(
                "Взвешивание не выполнено",
                "Проверьте вес товара и повторите попытку.",
            )
        # Never use the product's current price_per_kg for an existing
        # order line -- only historical rows created before this snapshot
        # column existed fall back to it.
        effective_price_per_kg = (
            price_per_kg_snapshot if price_per_kg_snapshot is not None else price_per_kg
        )
        final_price = round(final_weight_grams / 1000 * effective_price_per_kg, 2)

        cursor.execute(
            """
            UPDATE order_items
            SET weight = %s,
                price = %s
            WHERE id = %s
              AND order_id = %s
              AND weight IS NULL
            """,
            (final_weight_grams, final_price, item_id, order_id),
        )
        if cursor.rowcount == 0:
            return RedirectResponse(f"/orders/{order_id}", status_code=303)

        cursor.execute(
            """
            UPDATE orders
            SET total = (SELECT COALESCE(SUM(price), 0) FROM order_items WHERE order_id = %s),
                updated_at = NOW()
            WHERE order_id = %s
            """,
            (order_id, order_id),
        )
        log_order_event(
            cursor,
            order_id,
            "item_weighed",
            f"Товар взвешен: {final_weight_grams} г, {final_price:.2f} €"
        )

        cursor.execute(
            "SELECT 1 FROM order_items WHERE order_id = %s AND weight IS NULL LIMIT 1",
            (order_id,),
        )
        has_pending_weighing = cursor.fetchone() is not None

        if not has_pending_weighing:
            cursor.execute(
                "SELECT telegram_id, total FROM orders WHERE order_id = %s",
                (order_id,),
            )
            order_row = cursor.fetchone()
            if order_row:
                telegram_id, order_total = order_row

            cursor.execute(
                """
                SELECT oi.product_name, oi.weight, oi.price, po.label
                FROM order_items oi
                LEFT JOIN product_options po ON po.id = oi.option_id
                WHERE oi.order_id = %s
                ORDER BY oi.id
                """,
                (order_id,),
            )
            order_items_summary = cursor.fetchall()

        conn.commit()
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(WEIGHING_UPDATE_FAILED)
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if not has_pending_weighing and telegram_id:
        try:
            send_weighing_notification_and_record(
                telegram_id,
                order_id,
                order_total,
                order_items_summary,
                photo_bytes=photo_bytes,
                photo_filename=photo_filename,
                photo_content_type=photo_content_type,
            )
        except Exception:
            print(WEIGHING_NOTIFICATION_FAILED)
            try:
                record_notification_event(
                    order_id,
                    "weighing_notification_failed",
                    "Не удалось отправить клиенту уведомление о финальной сумме."
                )
            except Exception:
                print(WEIGHING_NOTIFICATION_FAILED)

    return RedirectResponse(f"/orders/{order_id}", status_code=303)


# ---------------------------------------------------------------------------
# Warehouse/Picking Workspace V1: a work screen inside the existing admin,
# not a separate app. Reads Orders v2 data (fulfillment_status='confirmed'/
# 'picking'/'packed') only. Every mutation on this page is a thin wrapper
# around the existing, already-tested functions above --
# update_order_fulfillment_status (confirmed->picking, picking->packed,
# including its pending-weighing check, atomic stock validation/deduction,
# row locking and rollback-on-shortage) and weigh_order_item -- called
# directly as plain Python functions, never duplicated. Each wrapper's only
# job is deciding where to redirect: back to /picking on success, or the
# existing function's own result (already a clear operational message) on
# rejection/failure.
# ---------------------------------------------------------------------------

PICKING_QUEUE_SECTIONS = (
    ("confirmed", "К сборке"),
    ("picking", "В сборке"),
    ("packed", "Недавно собрано"),
)


def _picking_order_lines(item_rows):
    """Groups one order's raw order_items rows into display-ready
    components. per_kg lines are shown individually (each carries its own
    weight/needs_weighing); fixed/options lines are grouped into a
    quantity per (product, option) since order_items has one row per unit,
    not a quantity column. Returns small dicts rather than raw DB rows so
    a future "boxes" component type can be added here without reshaping
    this function's callers -- no schema change, presentation only."""
    per_kg_lines = []
    grouped = {}
    grouped_order = []
    for item_id, product_name, weight, option_id, option_label, pricing_mode in item_rows:
        if pricing_mode == "per_kg":
            per_kg_lines.append({
                "type": "per_kg",
                "item_id": item_id,
                "product_name": product_name,
                "weight": weight,
                "needs_weighing": weight is None,
            })
        else:
            key = (pricing_mode, product_name, option_id)
            if key not in grouped:
                grouped[key] = {
                    "type": pricing_mode,
                    "product_name": product_name,
                    "option_label": option_label,
                    "quantity": 0,
                }
                grouped_order.append(key)
            grouped[key]["quantity"] += 1
    return per_kg_lines + [grouped[key] for key in grouped_order]


def _picking_item_line_html(order_id_path, line):
    name_text = escape(str(line["product_name"] or "-"), quote=True)
    if line["type"] == "per_kg":
        if line["needs_weighing"]:
            return (
                f"<li>{name_text} — <span class='status warning'>требует взвешивания</span>"
                f'<form method="post" action="/picking/{order_id_path}/items/{line["item_id"]}/weigh" '
                'style="display:flex; gap:4px; align-items:center; margin-top:4px;">'
                '<input type="number" name="final_weight_grams" min="1" step="1" placeholder="г" required style="width:70px;"/>'
                '<button class="button secondary" type="submit">Подтвердить вес</button>'
                '</form></li>'
            )
        return f"<li>{name_text} — {line['weight']} г</li>"
    if line["type"] == "fixed":
        return f"<li>{name_text} × {line['quantity']}</li>"
    option_text = escape(str(line.get("option_label") or "-"), quote=True)
    return f"<li>{name_text}: {option_text} × {line['quantity']}</li>"


def _picking_order_card_html(order_row, items_for_order):
    (
        id_, order_id, customer_name, username, total, payment_status,
        payment_method, fulfillment_status, source, created_at,
        delivery_method, delivery_street, delivery_city,
    ) = order_row
    order_id_path = urllib.parse.quote(str(order_id), safe="")
    customer_text = escape(str(customer_name or username or "-"), quote=True)
    payment_method_text = escape(str(payment_method or "-"), quote=True)

    if delivery_method == "delivery":
        location_bits = [bit for bit in (delivery_city, delivery_street) if bit]
        delivery_text = "🚚 Доставка"
        if location_bits:
            delivery_text += " · " + escape(", ".join(location_bits), quote=True)
    else:
        delivery_text = "🏬 Самовывоз"

    lines = _picking_order_lines(items_for_order)
    items_html = "".join(
        _picking_item_line_html(order_id_path, line) for line in lines
    ) or "<li>Нет товаров</li>"
    has_pending_weighing = any(
        line["type"] == "per_kg" and line["needs_weighing"] for line in lines
    )

    action_html = ""
    if fulfillment_status == "confirmed":
        action_html = (
            f'<form method="post" action="/picking/{order_id_path}/start" style="display:inline; margin:0;">'
            '<button class="button" type="submit">▶️ Начать сборку</button></form>'
        )
    elif fulfillment_status == "picking":
        pack_button = (
            f'<form method="post" action="/picking/{order_id_path}/pack" style="display:inline; margin:0;">'
            '<button class="button" type="submit">✅ Собрано</button></form>'
        )
        action_html = pack_button
        if has_pending_weighing:
            action_html = "<p class='muted'>Взвесьте весовые товары, чтобы упаковать заказ.</p>" + pack_button

    return f"""
    <div class="admin-card picking-order-card">
      <div class="detail-grid">
        <div class="detail-field"><strong>№</strong>{escape(format_order_number(id_), quote=True)}</div>
        <div class="detail-field"><strong>Клиент</strong>{customer_text}</div>
        <div class="detail-field"><strong>Источник</strong>{order_source_badge(source)}</div>
        <div class="detail-field"><strong>Доставка</strong>{delivery_text}</div>
        <div class="detail-field"><strong>Сумма</strong>{total:.2f} {CURRENCY_SYMBOL}</div>
        <div class="detail-field"><strong>Создан</strong>{format_admin_datetime(created_at)}</div>
        <div class="detail-field"><strong>Оплата</strong>{payment_status_badge(payment_status)} <span class="muted">{payment_method_text}</span></div>
        <div class="detail-field"><strong>Выполнение</strong>{fulfillment_status_badge(fulfillment_status)}</div>
      </div>
      <ul class="picking-items">{items_html}</ul>
      <div class="form-actions">
        {action_html}
        <a class="button button-link secondary" href="/orders/{order_id_path}">Заказ →</a>
      </div>
    </div>
    """


@app.get("/picking", response_class=HTMLResponse)
async def picking_workspace():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()

        orders_by_section = {}
        all_order_ids = []
        for status_value, _label in PICKING_QUEUE_SECTIONS:
            if status_value == "packed":
                cursor.execute(
                    """
                    SELECT id, order_id, customer_name, username, total, payment_status,
                           payment_method, fulfillment_status, source, created_at,
                           delivery_method, delivery_street, delivery_city
                    FROM orders
                    WHERE fulfillment_status = %s
                    ORDER BY updated_at DESC
                    LIMIT 15
                    """,
                    (status_value,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, order_id, customer_name, username, total, payment_status,
                           payment_method, fulfillment_status, source, created_at,
                           delivery_method, delivery_street, delivery_city
                    FROM orders
                    WHERE fulfillment_status = %s
                    ORDER BY created_at ASC
                    """,
                    (status_value,),
                )
            rows = cursor.fetchall()
            orders_by_section[status_value] = rows
            all_order_ids.extend(row[1] for row in rows)

        items_by_order = {}
        if all_order_ids:
            cursor.execute(
                """
                SELECT oi.order_id, oi.id, oi.product_name, oi.weight, oi.option_id,
                       po.label, oi.pricing_mode
                FROM order_items oi
                LEFT JOIN product_options po ON po.id = oi.option_id
                WHERE oi.order_id = ANY(%s)
                ORDER BY oi.order_id, oi.id
                """,
                (all_order_ids,),
            )
            for (
                order_id, item_id, product_name, weight, option_id,
                option_label, pricing_mode,
            ) in cursor.fetchall():
                items_by_order.setdefault(order_id, []).append(
                    (item_id, product_name, weight, option_id, option_label, pricing_mode)
                )
        conn.close()

        html_content = "<section class='admin-card'><h1>📦 Сборка</h1></section>"
        for status_value, label in PICKING_QUEUE_SECTIONS:
            rows = orders_by_section[status_value]
            html_content += f"<section class='admin-card dash-section'><h2>{label} ({len(rows)})</h2>"
            if not rows:
                html_content += "<p>Пусто.</p></section>"
                continue
            html_content += "<div class='picking-queue'>"
            for row in rows:
                html_content += _picking_order_card_html(row, items_by_order.get(row[1], []))
            html_content += "</div></section>"

        return admin_layout("📦 Сборка", html_content, refresh_seconds=30)
    except Exception:
        report_read_error("picking_workspace_load_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


def _order_reached_fulfillment_status(order_id, target_status):
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT fulfillment_status FROM orders WHERE order_id = %s",
            (order_id,),
        )
        row = cursor.fetchone()
        return row is not None and str(row[0] or "") == target_status
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@app.post("/picking/{order_id}/start", dependencies=[Depends(require_admin_csrf)])
async def picking_start_order(order_id: str):
    """Performs no transition logic of its own -- calls the existing
    update_order_fulfillment_status('picking') unchanged and, only once
    that transition has actually taken effect, redirects back to the
    picking workspace instead of its normal destination. Any
    rejection/error from the existing function is returned as-is."""
    result = await update_order_fulfillment_status(order_id, "picking")
    if _order_reached_fulfillment_status(order_id, "picking"):
        return RedirectResponse("/picking", status_code=303)
    return result


@app.post("/picking/{order_id}/pack", dependencies=[Depends(require_admin_csrf)])
async def picking_pack_order(order_id: str):
    """Same wrapper pattern as picking_start_order, for picking -> packed.
    update_order_fulfillment_status already performs the pending-weighing
    check, atomic stock validation/deduction, row locking and
    rollback-on-shortage -- this route never touches inventory or
    order_items itself."""
    result = await update_order_fulfillment_status(order_id, "packed")
    if _order_reached_fulfillment_status(order_id, "packed"):
        return RedirectResponse("/picking", status_code=303)
    return result


@app.post(
    "/picking/{order_id}/items/{item_id}/weigh",
    dependencies=[Depends(require_admin_csrf)],
)
async def picking_weigh_order_item(
    order_id: str,
    item_id: int,
    final_weight_grams: int = Form(...),
    photo: UploadFile = File(None),
):
    """Reuses weigh_order_item unchanged -- same validation, same
    price_per_kg_snapshot rule, same customer notification behavior. Its
    success path always returns a redirect, so on success this redirects
    back to the picking workspace instead; a validation error page
    (weigh_order_item's own) is returned as-is."""
    result = await weigh_order_item(order_id, item_id, final_weight_grams, photo)
    if isinstance(result, RedirectResponse):
        return RedirectResponse("/picking", status_code=303)
    return result


@app.get("/clients", response_class=HTMLResponse)
async def clients(q: str = ""):
    try:
        search_query = q.strip()[:100]
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        if search_query:
            search_param = f"%{search_query}%"
            cursor.execute(
                """
                SELECT telegram_id, username, phone, address
                FROM clients
                WHERE
                CAST(telegram_id AS TEXT) ILIKE %s
                OR username ILIKE %s
                OR phone ILIKE %s
                ORDER BY telegram_id DESC
                LIMIT 20
                """,
                (search_param, search_param, search_param),
            )
        else:
            cursor.execute("""
                SELECT telegram_id, username, phone, address
                FROM clients
                ORDER BY telegram_id DESC
                LIMIT 20
            """)
        rows = cursor.fetchall()
        conn.close()
        
        html = f"""
        <section class='admin-card'>
        <h1>👥 Клиенты</h1>
        <form class="admin-form" method="get" action="/clients">
          <label>Поиск <input name="q" value="{escape(search_query, quote=True)}" placeholder="Поиск по Telegram ID, username или телефону"/></label>
          <div class="form-actions">
            <button class="button" type="submit">Показать</button>
            <a class="button button-link secondary" href="/clients">Сбросить</a>
          </div>
        </form>
        <div class='dash-table-wrap'><table>
        """
        html += "<tr><th>Telegram ID</th><th>Клиент</th><th>Телефон</th><th>Адрес</th><th>Действия</th></tr>"
        for row in rows:
            telegram_id, username, phone, address = row
            telegram_id_text = escape(str(telegram_id), quote=True)
            html += (
                "<tr>"
                f"<td>{telegram_id_text}</td>"
                f"<td>{escape(str(username or '-'), quote=True)}</td>"
                f"<td>{escape(str(phone or '-'), quote=True)}</td>"
                f"<td>{escape(str(address or '-'), quote=True)}</td>"
                f"<td><a class='button button-link' href='/clients/{telegram_id_text}'>Открыть</a></td>"
                "</tr>"
            )
        html += "</table></div></section>"
        return admin_layout("👥 Клиенты", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/clients/{telegram_id}", response_class=HTMLResponse)
async def client_detail(telegram_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT telegram_id, username, first_name, phone, address, client_note
            FROM clients
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        )
        client = cursor.fetchone()
        if not client:
            conn.close()
            return admin_layout(
                "Клиент не найден",
                """
                <section class="admin-card">
                  <h1>Клиент не найден</h1>
                  <p>Такой клиент не найден.</p>
                  <p><a class="button button-link" href="/clients">Назад к клиентам</a></p>
                </section>
                """,
            )

        cursor.execute(
            """
            SELECT
              COUNT(*) AS total_orders,
              COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' THEN 1 ELSE 0 END), 0) AS completed_orders,
              COALESCE(SUM(CASE WHEN fulfillment_status = 'cancelled' THEN 1 ELSE 0 END), 0) AS cancelled_orders,
              COALESCE(SUM(CASE WHEN fulfillment_status = 'delivered' THEN total ELSE 0 END), 0) AS total_spent,
              COALESCE(AVG(CASE WHEN fulfillment_status = 'delivered' THEN total ELSE NULL END), 0) AS average_order_value,
              MIN(created_at) AS first_order_date,
              MAX(created_at) AS last_order_date
            FROM orders
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        )
        stats = cursor.fetchone()
        cursor.execute(
            """
            SELECT order_id, total, payment_status, fulfillment_status, payment_method, created_at
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 20
            """,
            (telegram_id,),
        )
        order_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                oi.product_id,
                COALESCE(p.name, oi.product_name) AS product_name,
                COUNT(*) AS purchase_count,
                COALESCE(SUM(oi.weight), 0) AS grams_purchased,
                COALESCE(SUM(oi.price), 0) AS revenue
            FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            LEFT JOIN products p ON p.id = oi.product_id
            WHERE o.telegram_id = %s
              AND o.fulfillment_status = 'delivered'
            GROUP BY
                oi.product_id,
                COALESCE(p.name, oi.product_name)
            ORDER BY
                purchase_count DESC,
                revenue DESC
            LIMIT 5
            """,
            (telegram_id,),
        )
        favorite_products = cursor.fetchall()
        cursor.execute(
            """
            SELECT event_type, metadata, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (telegram_id,),
        )
        activity_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT event_type, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        latest_funnel_event = cursor.fetchone()
        cursor.execute(
            """
            SELECT event_type, created_at
            FROM customer_events
            WHERE telegram_id = %s
            ORDER BY
            CASE event_type
                WHEN 'payment_reported' THEN 9
                WHEN 'payment_method_selected' THEN 8
                WHEN 'order_created' THEN 7
                WHEN 'checkout_started' THEN 6
                WHEN 'open_cart' THEN 5
                WHEN 'add_to_cart' THEN 4
                WHEN 'view_product' THEN 3
                WHEN 'view_category' THEN 2
                WHEN 'start' THEN 1
                ELSE 0
            END DESC,
            created_at DESC,
            id DESC
            LIMIT 1
            """,
            (telegram_id,),
        )
        highest_funnel_event = cursor.fetchone()
        conn.close()

        client_id, username, first_name, phone, address, client_note = client
        (
            total_orders,
            completed_orders,
            cancelled_orders,
            total_spent,
            average_order_value,
            first_order_date,
            last_order_date,
        ) = stats

        orders_html = "<p>Заказов пока нет.</p>"
        if order_rows:
            rows_html = ""
            for order_id, total, payment_status, fulfillment_status, payment_method, created_at in order_rows:
                order_id_text = html.escape(str(order_id))
                rows_html += f"""
                <tr>
                  <td><a class="view-link" href="/orders/{order_id_text}">{order_id_text}</a></td>
                  <td>€{float(total or 0):.2f}</td>
                  <td>{payment_status_badge(payment_status)}</td>
                  <td>{fulfillment_status_badge(fulfillment_status)}</td>
                  <td>{html.escape(str(payment_method or '-'))}</td>
                  <td>{format_admin_datetime(created_at)}</td>
                </tr>
                """
            orders_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Заказ</th><th>Сумма</th><th>Оплата</th><th>Выполнение</th><th>Способ оплаты</th><th>Создан</th></tr>
                {rows_html}
              </table>
            </div>
            """

        def format_client_weight(value):
            grams = int(value or 0)
            if grams >= 1000:
                return f"{grams / 1000:.1f} кг"
            return f"{grams} г"

        favorite_products_html = "<p>Нет данных</p>"
        if favorite_products:
            favorite_rows = ""
            for _, product_name, purchase_count, grams_purchased, revenue in favorite_products:
                favorite_rows += f"""
                <tr>
                  <td>{html.escape(str(product_name or '-'))}</td>
                  <td>{purchase_count}</td>
                  <td>{format_client_weight(grams_purchased)}</td>
                  <td>€{float(revenue or 0):.2f}</td>
                </tr>
                """
            favorite_products_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Товар</th><th>Покупок</th><th>Вес</th><th>Выручка</th></tr>
                {favorite_rows}
              </table>
            </div>
            """

        event_labels = {
            "start": "Открыл бота",
            "view_category": "Открыл категорию",
            "view_product": "Открыл товар",
            "add_to_cart": "Добавил в корзину",
            "open_cart": "Открыл корзину",
            "checkout_started": "Начал оформление",
            "order_created": "Создал заказ",
            "payment_method_selected": "Выбрал оплату",
            "payment_reported": "Сообщил об оплате",
        }
        funnel_stage_labels = {
            "start": "Только открыл бота",
            "view_category": "Смотрел категории",
            "view_product": "Смотрел товары",
            "add_to_cart": "Добавил в корзину",
            "open_cart": "Открыл корзину",
            "checkout_started": "Начал оформление",
            "order_created": "Создал заказ",
            "payment_method_selected": "Выбрал оплату",
            "payment_reported": "Сообщил об оплате",
        }

        def format_funnel_event(row):
            if not row:
                return None
            event_type, created_at = row
            label = funnel_stage_labels.get(str(event_type or ""), html.escape(str(event_type or "-")))
            return f"{html.escape(str(label))} / {format_admin_datetime(created_at)}"

        def format_activity_metadata(metadata):
            if not isinstance(metadata, dict):
                return "-"
            details = []
            if metadata.get("order_id") is not None:
                details.append(f"#{html.escape(str(metadata.get('order_id')))}")
            if metadata.get("product_id") is not None:
                details.append(f"product_id: {html.escape(str(metadata.get('product_id')))}")
            if metadata.get("category_id") is not None:
                details.append(f"category_id: {html.escape(str(metadata.get('category_id')))}")
            if metadata.get("payment_method") is not None:
                details.append(html.escape(str(metadata.get("payment_method"))))
            if metadata.get("option_id") is not None:
                details.append(f"option_id: {html.escape(str(metadata.get('option_id')))}")
            if metadata.get("weight") is not None:
                details.append(f"{html.escape(str(metadata.get('weight')))} г")
            return ", ".join(details) if details else "-"

        activity_html = "<p>Пока нет активности.</p>"
        if activity_rows:
            activity_table_rows = ""
            for event_type, metadata, created_at in activity_rows:
                label = event_labels.get(str(event_type or ""), html.escape(str(event_type or "-")))
                activity_table_rows += f"""
                <tr>
                  <td>{format_admin_datetime(created_at)}</td>
                  <td>{html.escape(str(label))}</td>
                  <td>{format_activity_metadata(metadata)}</td>
                </tr>
                """
            activity_html = f"""
            <div class="dash-table-wrap">
              <table>
                <tr><th>Дата</th><th>Событие</th><th>Детали</th></tr>
                {activity_table_rows}
              </table>
            </div>
            """

        latest_funnel_text = format_funnel_event(latest_funnel_event)
        highest_funnel_text = format_funnel_event(highest_funnel_event)
        if latest_funnel_text or highest_funnel_text:
            funnel_path_html = f"""
            <div class="detail-grid">
              <div class="detail-field"><strong>Последнее действие</strong>{latest_funnel_text or '-'}</div>
              <div class="detail-field"><strong>Самый дальний этап</strong>{highest_funnel_text or '-'}</div>
            </div>
            """
        else:
            funnel_path_html = "<p>Пока нет данных по действиям клиента.</p>"

        content = f"""
        <p><a class="button button-link" href="/clients">Назад к клиентам</a></p>
        <section class="admin-card">
          <h1>Клиент</h1>
          <div class="detail-grid">
            <div class="detail-field"><strong>Telegram ID</strong>{html.escape(str(client_id))}</div>
            <div class="detail-field"><strong>Username</strong>{html.escape(str(username or '-'))}</div>
            <div class="detail-field"><strong>Имя</strong>{html.escape(str(first_name or '-'))}</div>
            <div class="detail-field"><strong>Телефон</strong>{html.escape(str(phone or '-'))}</div>
            <div class="detail-field"><strong>Адрес</strong>{html.escape(str(address or '-'))}</div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Заметка администратора</h2>
          <form class="admin-form" method="post" action="/clients/{telegram_id}/note">
            <label>Заметка
              <textarea name="client_note" rows="4">{html.escape(str(client_note or ""))}</textarea>
              <small>Видно только в админке.</small>
            </label>
            <div class="form-actions">
              <button class="button" type="submit">Сохранить заметку</button>
            </div>
          </form>
        </section>
        <section class="admin-card dash-section">
          <h2>CRM статистика</h2>
          <div class="dash-grid">
            <div class="dash-card"><span>Всего заказов</span><strong class="stat-value">{total_orders}</strong></div>
            <div class="dash-card"><span>Завершённых</span><strong class="stat-value">{completed_orders}</strong></div>
            <div class="dash-card"><span>Отменённых</span><strong class="stat-value">{cancelled_orders}</strong></div>
            <div class="dash-card"><span>Потрачено</span><strong class="stat-value">€{float(total_spent or 0):.2f}</strong></div>
            <div class="dash-card"><span>Средний чек</span><strong class="stat-value">€{float(average_order_value or 0):.2f}</strong></div>
            <div class="dash-card"><span>Первый заказ</span><strong>{format_admin_datetime(first_order_date)}</strong></div>
            <div class="dash-card"><span>Последний заказ</span><strong>{format_admin_datetime(last_order_date)}</strong></div>
          </div>
        </section>
        <section class="admin-card dash-section">
          <h2>Любимые товары</h2>
          {favorite_products_html}
        </section>
        <section class="admin-card dash-section">
          <h2>Путь клиента</h2>
          {funnel_path_html}
        </section>
        <section class="admin-card dash-section">
          <h2>Активность клиента</h2>
          {activity_html}
        </section>
        <section class="admin-card dash-section">
          <h2>История заказов</h2>
          {orders_html}
        </section>
        """
        return admin_layout("Клиент", content)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/clients/{telegram_id}/note",
    dependencies=[Depends(require_admin_csrf)],
)
async def update_client_note(telegram_id: int, client_note: str = Form("")):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE clients
            SET client_note = %s
            WHERE telegram_id = %s
            """,
            (client_note, telegram_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        print(CLIENT_NOTE_UPDATE_FAILED)
    return RedirectResponse(f"/clients/{telegram_id}", status_code=303)


@app.get("/products", response_class=HTMLResponse)
async def products(filter: str = "", days: int = 14):
    try:
        allowed_filters = {
            "low_stock",
            "no_recommendations",
            "no_promotion",
            "no_sales",
            "no_image",
            "no_description",
            "never_sold",
        }
        active_filter = filter if filter in allowed_filters else ""

        try:
            days_value = int(days)
        except (TypeError, ValueError):
            days_value = 14
        days_value = max(1, min(days_value, 365))

        where_sql = ""
        params = []

        if active_filter == "low_stock":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND (
                    p.is_out_of_stock = TRUE
                    OR (
                        p.pricing_mode = 'per_kg'
                        AND p.stock_grams IS NOT NULL
                        AND p.low_stock_threshold_grams IS NOT NULL
                        AND p.stock_grams <= p.low_stock_threshold_grams
                    )
                  )
            """
        elif active_filter == "no_recommendations":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM product_recommendations pr
                    WHERE pr.product_id = p.id
                      AND pr.recommendation_type = 'frequently_bought_together'
                      AND pr.is_active = TRUE
                  )
            """
        elif active_filter == "no_promotion":
            where_sql = "WHERE p.is_active = TRUE AND p.is_promotion = FALSE"
        elif active_filter == "no_sales":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1
                    FROM order_items oi
                    JOIN orders o ON o.order_id = oi.order_id
                    WHERE oi.product_id = p.id
                      AND o.fulfillment_status != 'cancelled'
                      AND o.created_at >= NOW() - (%s * INTERVAL '1 day')
                  )
            """
            params.append(days_value)
        elif active_filter == "no_image":
            where_sql = "WHERE p.is_active = TRUE AND (p.image_url IS NULL OR TRIM(p.image_url) = '')"
        elif active_filter == "no_description":
            where_sql = "WHERE p.is_active = TRUE AND (p.description IS NULL OR TRIM(p.description) = '')"
        elif active_filter == "never_sold":
            where_sql = """
                WHERE p.is_active = TRUE
                  AND NOT EXISTS (
                    SELECT 1
                    FROM order_items oi
                    JOIN orders o ON o.order_id = oi.order_id
                    WHERE oi.product_id = p.id
                      AND o.fulfillment_status != 'cancelled'
                  )
            """

        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT p.id, p.name, p.price_per_kg, p.image_url, p.is_active, c.name,
                   p.stock_grams, p.is_out_of_stock, p.low_stock_threshold_grams,
                   p.is_promotion, p.pricing_mode, p.fixed_price, p.sale_unit,
                   p.stock_quantity,
                   (
                       SELECT COUNT(*)
                       FROM product_options po
                       WHERE po.product_id = p.id
                         AND po.is_active = TRUE
                   ) AS active_option_count,
                   (
                       SELECT COUNT(*)
                       FROM product_options po
                       WHERE po.product_id = p.id
                          AND po.is_active = TRUE
                          AND po.is_out_of_stock = FALSE
                          AND po.stock_quantity > 0
                   ) AS available_option_count
            FROM products p
            LEFT JOIN categories c ON c.id = p.category_id
            {where_sql}
            ORDER BY p.sort_order, p.id
        """, params)
        rows = cursor.fetchall()
        conn.close()

        grouped_products = {}
        category_order = []
        for row in rows:
            category_label = row[5] or "Без категории"
            if category_label not in grouped_products:
                grouped_products[category_label] = []
                category_order.append(category_label)
            grouped_products[category_label].append(row)

        category_anchors = {}
        for index, category_label in enumerate(category_order, 1):
            anchor = "-".join("".join(ch.lower() if ch.isalnum() else "-" for ch in category_label).split("-"))
            category_anchors[category_label] = f"category-{index}-{anchor or 'items'}"

        filter_labels = {
            "low_stock": "Фильтр: товары с низким остатком",
            "no_recommendations": "Фильтр: товары без рекомендаций",
            "no_promotion": "Фильтр: товары без акций",
            "no_sales": f"Фильтр: без продаж за {days_value} дн.",
            "no_image": "Фильтр: товары без фото",
            "no_description": "Фильтр: товары без описания",
            "never_sold": "Фильтр: товары, которые ни разу не продавались",
        }

        html = "<section class='admin-card' id='all-products'><h1>🛒 Товары</h1><p><a class='button button-link' href='/products/new'>➕ Новый товар</a></p><p>Скрытые товары не показываются в каталоге, но остаются в системе.</p>"
        if active_filter:
            filter_label_text = escape(filter_labels.get(active_filter, ""), quote=True)
            html += (
                f"<div class='attention-banner'>{filter_label_text} "
                f"<a class='button button-link secondary' href='/products'>Сбросить фильтр</a></div>"
            )
        if not rows:
            html += "<p>Нет товаров по выбранному фильтру.</p></section>"
            return admin_layout("🛒 Товары", html)

        html += "<div class='quick-nav desktop-quick-nav'><a href='#all-products'>Все товары</a>"
        for category_label in category_order:
            category_text = escape(category_label, quote=True)
            html += f"<a href='#{category_anchors[category_label]}-desktop'>📁 {category_text}</a>"
        html += "</div><div class='quick-nav mobile-quick-nav'><a href='#all-products'>Все товары</a>"
        for category_label in category_order:
            category_text = escape(category_label, quote=True)
            html += f"<a href='#{category_anchors[category_label]}-mobile'>📁 {category_text}</a>"
        html += "</div><div class='dash-table-wrap products-desktop-table'><table class='products-table'>"
        html += "<tr><th>ID</th><th>Название</th><th>Цена</th><th>Фото</th><th>Статус</th><th>Категория</th><th>Остаток</th><th>Наличие</th><th>Действия</th></tr>"
        mobile_sections = []

        for category_label in category_order:
            category_text = escape(category_label, quote=True)
            html += f"<tr class='category-row' id='{category_anchors[category_label]}-desktop'><td colspan='9'>📁 {category_text}</td></tr>"
            mobile_cards = []
            for row in grouped_products[category_label]:
                (
                    pid, name, price, image_url, is_active, category_name,
                    stock_grams, is_out_of_stock, low_stock_threshold_grams,
                    is_promotion, pricing_mode, fixed_price, sale_unit, stock_quantity,
                    active_option_count, available_option_count,
                ) = row
                img_html = render_admin_product_image(image_url, name)
                active_text = "Активен" if is_active else "Выключен"
                status_class = "active" if is_active else "inactive"
                actions_html = f"<a class=\"button\" href=\"/products/{pid}/edit\">Редактировать</a>"
                if is_active:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/deactivate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Отключить</button></form>"
                else:
                    actions_html += f" <form method=\"post\" action=\"/products/{pid}/activate\" style=\"display:inline; margin:0; padding:0;\"><button class=\"button secondary\" type=\"submit\">Включить</button></form>"
                actions_html = f"<div class=\"action-group\">{actions_html}</div>"
                if pricing_mode == "fixed":
                    inventory_empty = stock_quantity is None or int(stock_quantity or 0) <= 0
                    stock_text = f"{max(int(stock_quantity or 0), 0)} шт." if stock_quantity is not None else "—"
                elif pricing_mode == "options":
                    inventory_empty = int(available_option_count or 0) <= 0
                    stock_text = (
                        f"Доступно вариантов: {int(available_option_count or 0)}"
                        f"/{int(active_option_count or 0)}"
                    )
                else:
                    inventory_empty = int(stock_grams or 0) <= 0
                    stock_text = format_stock_grams(stock_grams)
                availability_text = "Нет в наличии" if is_out_of_stock or inventory_empty else "В наличии"
                availability_class = "inactive" if availability_text == "Нет в наличии" else "active"
                safe_name = escape(str(name or ""), quote=True)
                name_text = f"🔥 {safe_name}" if is_promotion else safe_name
                price_text = format_admin_product_price(
                    pricing_mode, price, fixed_price, sale_unit
                )
                html += f"<tr><td>{pid}</td><td>{name_text}</td><td>{price_text}</td><td>{img_html}</td><td><span class='status {status_class}'>{active_text}</span></td><td>{category_text}</td><td>{stock_text}</td><td><span class='status {availability_class}'>{availability_text}</span></td><td>{actions_html}</td></tr>"
                mobile_cards.append(
                    "<article class='mobile-product-card'>"
                    "<div class='mobile-product-head'>"
                    f"{img_html}<div><span class='mobile-product-id'>ID {pid}</span>"
                    f"<h3 class='mobile-product-name'>{name_text}</h3></div></div>"
                    "<dl class='mobile-product-details'>"
                    f"<div><dt>Цена</dt><dd>{price_text}</dd></div>"
                    f"<div><dt>Статус</dt><dd><span class='status {status_class}'>{active_text}</span></dd></div>"
                    f"<div><dt>Категория</dt><dd>{category_text}</dd></div>"
                    f"<div><dt>Остаток</dt><dd>{stock_text}</dd></div>"
                    f"<div><dt>Наличие</dt><dd><span class='status {availability_class}'>{availability_text}</span></dd></div>"
                    f"</dl>{actions_html}</article>"
                )
            mobile_sections.append(
                f"<section class='mobile-product-category' id='{category_anchors[category_label]}-mobile'>"
                f"<h2>📁 {category_text}</h2><div class='mobile-product-cards'>"
                f"{''.join(mobile_cards)}</div></section>"
            )
        html += f"</table></div><div class='mobile-products-list'>{''.join(mobile_sections)}</div></section>"
        return admin_layout("🛒 Товары", html)
    except Exception:
        report_read_error("product_list_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")
@app.get("/products/new", response_class=HTMLResponse)
async def new_product_form():
    html = """
    <section class="admin-card">
      <h1>➕ Новый товар</h1>
      <p><a href="/products">← Назад к товарам</a></p>
      <form class="admin-form" method="post" action="/products/new">
        <label>Категория <input name="category_id"/></label>
        <label>Название товара <input name="name"/></label>
        <label>Режим цены
          <select name="pricing_mode">
            <option value="fixed">Фиксированная цена</option>
            <option value="per_kg" selected>Цена за килограмм</option>
            <option value="options">Готовые варианты</option>
          </select>
        </label>
        <label>Цена за кг (€) <input name="price_per_kg" type="number" min="0.01" step="0.01"/></label>
        <label>Фиксированная цена (€) <input name="fixed_price" type="number" min="0.01" step="0.01"/></label>
        <label>Единица продажи <input name="sale_unit" placeholder="за упаковку / за штуку"/></label>
        <label>Ориентировочный вес, г <input name="unit_weight_grams" type="number" min="1"/></label>
        <label>Остаток в единицах <input name="stock_quantity" type="number" min="0"/></label>
        <label>Описание <input name="description"/></label>
        <label>Ссылка на фото <input name="image_url"/></label>
        <label>Остаток, г <input name="stock_grams" type="number" min="0" value="0"/></label>
        <label>Минимальный остаток, г <input name="low_stock_threshold_grams" type="number" min="0" value="500"/></label>
        <label>Порядок сортировки <input name="sort_order" value="0"/><small>Меньше число = выше в списке</small></label>
        <label>Товар активен <input type="checkbox" name="is_active" value="1"/></label>
        <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать товар"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новый товар", html)


@app.post(
    "/products/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_product(
    category_id: int = Form(...),
    name: str = Form(...),
    pricing_mode: str = Form("per_kg"),
    price_per_kg: str = Form(""),
    fixed_price: str = Form(""),
    sale_unit: str = Form(""),
    unit_weight_grams: str = Form(""),
    stock_quantity: str = Form(""),
    description: str = Form(''),
    image_url: str = Form(''),
    stock_grams: str = Form("0"),
    low_stock_threshold_grams: str = Form("500"),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    is_out_of_stock: bool = Form(False),
):
    active = True if is_active else False
    try:
        (
            pricing_mode_value,
            price_per_kg_value,
            fixed_price_value,
            sale_unit_value,
            unit_weight_value,
            stock_quantity_value,
        ) = normalize_product_pricing(
            pricing_mode,
            price_per_kg,
            fixed_price,
            sale_unit,
            unit_weight_grams,
            stock_quantity,
        )
        stock_value = normalize_product_stock_grams(
            pricing_mode_value, stock_grams
        )
    except ValueError:
        return admin_error_page(
            "Некорректная цена", "Проверьте значения цены и единицы продажи."
        )
    try:
        low_stock_threshold_value = max(int(low_stock_threshold_grams or 0), 0)
    except (TypeError, ValueError):
        low_stock_threshold_value = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM products")
        new_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO products (
    id,
    category_id,
    name,
    price_per_kg,
    description,
    image_url,
    stock_grams,
    low_stock_threshold_grams,
    is_out_of_stock,
    is_active,
    sort_order,
    pricing_mode,
    fixed_price,
    sale_unit,
    unit_weight_grams,
    stock_quantity
)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
    new_id,
    category_id,
    name,
    price_per_kg_value,
    description,
    image_url,
    stock_value,
    low_stock_threshold_value,
    is_out_of_stock,
    active,
    sort_order,
    pricing_mode_value,
    fixed_price_value,
    sale_unit_value,
    unit_weight_value,
    stock_quantity_value
),
        )
        sync_low_stock_alert_state(cursor, [new_id])
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар создан",
            """
            <section class="admin-card">
              <h1>✅ Товар создан</h1>
              <p>Товар успешно добавлен.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
                <a class="button button-link secondary" href="/products/new">➕ Добавить ещё товар</a>
              </div>
            </section>
            """,
        )
    except Exception:
        log_admin_error(
            "/products/new", "create_product", "product_create_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def edit_product_form(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT category_id, name, price_per_kg, description, image_url, sort_order,
                   is_active, stock_grams, is_out_of_stock, low_stock_threshold_grams,
                   is_promotion, promotion_title, promotion_sort_order, pricing_mode,
                   fixed_price, sale_unit, unit_weight_grams, stock_quantity
            FROM products
            WHERE id = %s
            """,
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Товар не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Товар не найден</h1>
                  <p>Такой товар не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        cursor.execute(
            """
            SELECT id, label, weight, price, sort_order, is_active,
                   stock_quantity, is_out_of_stock
            FROM product_options
            WHERE product_id = %s
            ORDER BY sort_order, id
            """,
            (product_id,),
        )
        options = cursor.fetchall()
        cursor.execute(
            """
            SELECT
                movement_type,
                quantity_grams,
                stock_before,
                stock_after,
                order_id,
                note,
                created_at
            FROM inventory_movements
            WHERE product_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (product_id,),
        )
        inventory_movements = cursor.fetchall()
        conn.close()

        (
            category_id, name, price_per_kg, description, image_url, sort_order,
            is_active, stock_grams, is_out_of_stock, low_stock_threshold_grams,
            is_promotion, promotion_title, promotion_sort_order, pricing_mode,
            fixed_price, sale_unit, unit_weight_grams, stock_quantity,
        ) = row
        if pricing_mode not in PRICING_MODES:
            raise ValueError(f"Неизвестный режим цены: {pricing_mode!r}")
        checked = "checked" if is_active else ""
        out_of_stock_checked = "checked" if is_out_of_stock else ""
        image_url_value = escape(str(image_url or ""), quote=True)
        promotion_checked = "checked" if is_promotion else ""
        promotion_title_value = escape(str(promotion_title or ""), quote=True)
        fixed_selected = "selected" if pricing_mode == "fixed" else ""
        per_kg_selected = "selected" if pricing_mode == "per_kg" else ""
        options_selected = "selected" if pricing_mode == "options" else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать товар</h1>
          <p><a href="/products">← Назад к товарам</a></p>
          <form class="admin-form" method="post" action="/products/{product_id}/edit">
            <label>Категория <input name="category_id" value="{category_id}"/></label>
            <label>Название товара <input name="name" value="{name}"/></label>
            <label>Режим цены
              <select name="pricing_mode">
                <option value="fixed" {fixed_selected}>Фиксированная цена</option>
                <option value="per_kg" {per_kg_selected}>Цена за килограмм</option>
                <option value="options" {options_selected}>Готовые варианты</option>
              </select>
            </label>
            <label>Цена за кг (€) <input name="price_per_kg" type="number" min="0.01" step="0.01" value="{price_per_kg}"/></label>
            <label>Фиксированная цена (€) <input name="fixed_price" type="number" min="0.01" step="0.01" value="{fixed_price if fixed_price is not None else ''}"/></label>
            <label>Единица продажи <input name="sale_unit" value="{escape(str(sale_unit or ''), quote=True)}"/></label>
            <label>Ориентировочный вес, г <input name="unit_weight_grams" type="number" min="1" value="{unit_weight_grams if unit_weight_grams is not None else ''}"/></label>
            <label>Остаток в единицах <input name="stock_quantity" type="number" min="0" value="{stock_quantity if stock_quantity is not None else ''}"/></label>
            <label>Описание <input name="description" value="{description or ''}"/></label>
            <label>Ссылка на фото <input name="image_url" value="{image_url_value}"/></label>
            <label>Остаток, г <input name="stock_grams" type="number" min="0" value="{max(int(stock_grams or 0), 0)}"/></label>
            <label>Минимальный остаток, г <input name="low_stock_threshold_grams" type="number" min="0" value="{max(int(low_stock_threshold_grams or 0), 0)}"/></label>
            <label>Порядок сортировки <input name="sort_order" value="{sort_order}"/><small>Меньше число = выше в списке</small></label>
            <label>Товар активен <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true" {out_of_stock_checked}/></label>
            <fieldset>
              <legend>🔥 Продвижение товара</legend>
              <label>☑️ Показывать в разделе "Акции" <input type="checkbox" name="is_promotion" value="1" {promotion_checked}/></label>
              <label>Название акции
                <input name="promotion_title" value="{promotion_title_value}" placeholder="🔥 Хит недели / 🔥 Новинка / 🔥 Скидка 10% / 🔥 Лучший выбор"/>
              </label>
              <label>Порядок показа <input name="promotion_sort_order" type="number" value="{promotion_sort_order}"/></label>
            </fieldset>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        movement_rows = ""
        for movement_type, quantity_grams, stock_before, stock_after, order_id, note, created_at in inventory_movements:
            quantity_value = int(quantity_grams or 0)
            if quantity_value > 0:
                quantity_text = f"+{format_stock_grams(quantity_value)}"
            elif quantity_value < 0:
                quantity_text = f"-{format_stock_grams(abs(quantity_value))}"
            else:
                quantity_text = "0 г"
            order_link = "-"
            if order_id:
                order_id_text = escape(str(order_id), quote=True)
                order_link = f"<a class='view-link' href='/orders/{order_id_text}'>{order_id_text}</a>"
            movement_rows += f"""
            <tr>
              <td>{format_admin_datetime(created_at)}</td>
              <td>{escape(str(movement_type or '-'), quote=True)}</td>
              <td>{quantity_text}</td>
              <td>{format_stock_grams(stock_before)}</td>
              <td>{format_stock_grams(stock_after)}</td>
              <td>{order_link}</td>
              <td>{escape(str(note or '-'), quote=True)}</td>
            </tr>
            """
        if movement_rows:
            html += f"""
            <section class="admin-card dash-section">
              <h2>История остатков</h2>
              <div class="dash-table-wrap">
                <table>
                  <tr><th>Дата</th><th>Тип</th><th>Изменение</th><th>Было</th><th>Стало</th><th>Заказ</th><th>Комментарий</th></tr>
                  {movement_rows}
                </table>
              </div>
            </section>
            """
        else:
            html += """
            <section class="admin-card dash-section">
              <h2>История остатков</h2>
              <p>Пока нет истории остатков.</p>
            </section>
            """
        html += """
        <section class="admin-card dash-section">
          <h2>⚖️ Варианты продажи</h2>
        """
        options_warning = admin_options_warning(pricing_mode, options)
        if options_warning:
            html += (
                "<div class='attention-banner'>"
                f"{escape(options_warning, quote=True)}"
                "</div>"
            )
        if pricing_mode == "options":
            html += f"<p><a class='button button-link' href='/products/{product_id}/options/new'>➕ Добавить вариант</a></p>"
        if pricing_mode == "options" and options:
            html += """
          <div class="dash-table-wrap">
            <table>
              <tr><th>ID</th><th>Вариант</th><th>Вес</th><th>Цена</th><th>Остаток</th><th>Наличие</th><th>Сортировка</th><th>Статус</th><th>Действия</th></tr>
            """
            for (
                option_id, label, weight, price, option_sort_order, option_is_active,
                option_stock_quantity, option_is_out_of_stock,
            ) in options:
                option_status = "Активен" if option_is_active else "Скрыт"
                option_status_class = "active" if option_is_active else "inactive"
                weight_text = weight if weight is not None else "-"
                option_stock_text = option_stock_quantity if option_stock_quantity is not None else "—"
                option_availability = (
                    "Нет в наличии"
                    if option_is_out_of_stock
                    or option_stock_quantity is None
                    or int(option_stock_quantity or 0) <= 0
                    else "В наличии"
                )
                toggle_text = "👁 Скрыть" if option_is_active else "♻️ Включить"
                actions_html = f"<a class='button' href='/options/{option_id}/edit'>✏️ Редактировать</a>"
                actions_html += f" <form method='post' action='/options/{option_id}/toggle' style='display:inline; margin:0; padding:0;'><button class='button secondary' type='submit'>{toggle_text}</button></form>"
                html += f"<tr><td>{option_id}</td><td>{label}</td><td>{weight_text}</td><td>{price:.2f}</td><td>{option_stock_text}</td><td>{option_availability}</td><td>{option_sort_order}</td><td><span class='status {option_status_class}'>{option_status}</span></td><td><div class='action-group'>{actions_html}</div></td></tr>"
            html += """
            </table>
          </div>
            """
        elif pricing_mode == "options":
            html += "<p>Варианты ещё не добавлены.</p>"
        html += """
        </section>
        """
        html += f"<p><a class='button button-link' href='/products/{product_id}/recommendations'>🎯 Рекомендации продаж</a></p>"
        return admin_layout("✏️ Редактировать товар", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/products/{product_id}/recommendations", response_class=HTMLResponse)
async def product_recommendations_form(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM products WHERE id = %s",
            (product_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Товар не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Товар не найден</h1>
                  <p>Такой товар не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_name = row[0]

        cursor.execute(
            """
            SELECT id, name
            FROM products
            WHERE is_active = TRUE AND id != %s
            ORDER BY sort_order, id
            """,
            (product_id,),
        )
        other_products = cursor.fetchall()

        cursor.execute(
            """
            SELECT recommended_product_id
            FROM product_recommendations
            WHERE product_id = %s
              AND recommendation_type = 'frequently_bought_together'
              AND is_active = TRUE
            """,
            (product_id,),
        )
        recommended_ids = {recommended_row[0] for recommended_row in cursor.fetchall()}
        conn.close()

        product_name_text = escape(str(product_name or "-"), quote=True)
        checkbox_rows = ""
        for other_id, other_name in other_products:
            checked = "checked" if other_id in recommended_ids else ""
            other_name_text = escape(str(other_name or "-"), quote=True)
            checkbox_rows += (
                "<label style='display:block; margin:4px 0;'>"
                f"<input type='checkbox' name='recommended_product_id' value='{other_id}' {checked}/> "
                f"{other_name_text}"
                "</label>"
            )
        if not checkbox_rows:
            checkbox_rows = "<p>Нет других активных товаров для рекомендаций.</p>"

        html = f"""
        <section class="admin-card">
          <h1>🎯 Рекомендации продаж</h1>
          <p>Товар: <strong>{product_name_text}</strong></p>
          <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
          <form class="admin-form" method="post" action="/products/{product_id}/recommendations">
            <h2>Часто покупают вместе</h2>
            {checkbox_rows}
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить"/>
              <a class="button button-link secondary" href="/products/{product_id}/edit">← К товару</a>
            </div>
          </form>
        </section>
        """
        return admin_layout("🎯 Рекомендации продаж", html)
    except Exception:
        report_read_error("product_recommendations_load_failed")
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/products/{product_id}/recommendations",
    dependencies=[Depends(require_admin_csrf)],
)
async def update_product_recommendations(
    product_id: int,
    recommended_product_id: list[int] = Form([]),
):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM products WHERE id = %s",
            (product_id,),
        )
        if not cursor.fetchone():
            conn.close()
            return RedirectResponse("/products", status_code=303)

        cursor.execute(
            "SELECT id FROM products WHERE is_active = TRUE AND id != %s",
            (product_id,),
        )
        valid_ids = {valid_row[0] for valid_row in cursor.fetchall()}

        selected_ids = []
        seen_ids = set()
        for submitted_id in recommended_product_id:
            if submitted_id in valid_ids and submitted_id not in seen_ids:
                seen_ids.add(submitted_id)
                selected_ids.append(submitted_id)

        cursor.execute(
            """
            DELETE FROM product_recommendations
            WHERE product_id = %s
              AND recommendation_type = 'frequently_bought_together'
            """,
            (product_id,),
        )

        for sort_order, recommended_id in enumerate(selected_ids):
            cursor.execute(
                """
                INSERT INTO product_recommendations (
                    product_id,
                    recommended_product_id,
                    recommendation_type,
                    sort_order,
                    is_active
                )
                VALUES (%s, %s, 'frequently_bought_together', %s, TRUE)
                """,
                (product_id, recommended_id, sort_order),
            )

        conn.commit()
        conn.close()
    except Exception:
        log_admin_error(
            "/products/{product_id}/recommendations",
            "update_product_recommendations",
            "product_recommendations_update_failed",
        )
    return RedirectResponse(f"/products/{product_id}/edit", status_code=303)


@app.get("/products/{product_id}/options/new", response_class=HTMLResponse)
async def new_product_option_form(product_id: int):
    html = f"""
    <section class="admin-card">
      <h1>➕ Новый вариант продажи</h1>
      <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
      <form class="admin-form" method="post" action="/products/{product_id}/options/new">
        <label>Название варианта <input name="label"/></label>
        <label>Вес в граммах <input name="weight" type="number" min="1"/></label>
        <label>Цена <input name="price" type="number" min="0.01" step="0.01"/></label>
        <label>Остаток в упаковках <input name="stock_quantity" type="number" min="0"/></label>
        <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true"/></label>
        <label>Сортировка <input name="sort_order"/></label>
        <label>Активен <input type="checkbox" name="is_active" value="1"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать вариант"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новый вариант продажи", html)


@app.post(
    "/products/{product_id}/options/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_product_option(
    product_id: int,
    label: str = Form(""),
    weight: str = Form(None),
    price: str = Form(""),
    stock_quantity: str = Form(""),
    is_out_of_stock: bool = Form(False),
    sort_order: str = Form("0"),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        sort_value = int(sort_order) if sort_order else 0
        (
            label_value,
            weight_value,
            price_value,
            stock_quantity_value,
        ) = normalize_product_option(
            label, weight, price, stock_quantity
        )
    except (TypeError, ValueError):
        return admin_error_page(
            "Некорректный вариант", "Проверьте название, цену, вес и остаток."
        )
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT pricing_mode FROM products WHERE id = %s", (product_id,))
        product_row = cursor.fetchone()
        if not product_row or product_row[0] != "options":
            conn.close()
            return admin_error_page(
                "Вариант не создан",
                "Варианты можно добавлять только товарам в режиме options.",
            )
        cursor.execute(
            """
            INSERT INTO product_options (
                product_id,
                label,
                weight,
                price,
                sort_order,
                is_active,
                stock_quantity,
                is_out_of_stock
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                product_id, label_value, weight_value, price_value, sort_value, active,
                stock_quantity_value, is_out_of_stock,
            ),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Вариант создан",
            f"""
            <section class="admin-card">
              <h1>✅ Вариант создан</h1>
              <p>Вариант продажи успешно добавлен.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
                <a class="button button-link secondary" href="/products/{product_id}/options/new">➕ Добавить ещё вариант</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/options/{option_id}/edit", response_class=HTMLResponse)
async def edit_product_option_form(option_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT product_id, label, weight, price, sort_order, is_active,
                   stock_quantity, is_out_of_stock
            FROM product_options
            WHERE id = %s
            """,
            (option_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )

        (
            product_id, label, weight, price, sort_order, is_active,
            stock_quantity, is_out_of_stock,
        ) = row
        checked = "checked" if is_active else ""
        out_of_stock_checked = "checked" if is_out_of_stock else ""
        weight_value = weight if weight is not None else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать вариант</h1>
          <p><a href="/products/{product_id}/edit">← Назад к товару</a></p>
          <form class="admin-form" method="post" action="/options/{option_id}/edit">
            <label>Название варианта <input name="label" value="{label}"/></label>
            <label>Вес в граммах <input name="weight" type="number" min="1" value="{weight_value}"/></label>
            <label>Цена <input name="price" type="number" min="0.01" step="0.01" value="{price}"/></label>
            <label>Остаток в упаковках <input name="stock_quantity" type="number" min="0" value="{stock_quantity if stock_quantity is not None else ''}"/></label>
            <label>Нет в наличии <input type="checkbox" name="is_out_of_stock" value="true" {out_of_stock_checked}/></label>
            <label>Сортировка <input name="sort_order" value="{sort_order}"/></label>
            <label>Активен <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        return admin_layout("✏️ Редактировать вариант", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/options/{option_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def update_product_option(
    option_id: int,
    label: str = Form(""),
    weight: str = Form(None),
    price: str = Form(""),
    stock_quantity: str = Form(""),
    is_out_of_stock: bool = Form(False),
    sort_order: str = Form("0"),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        sort_value = int(sort_order) if sort_order else 0
        (
            label_value,
            weight_value,
            price_value,
            stock_quantity_value,
        ) = normalize_product_option(
            label, weight, price, stock_quantity
        )
    except (TypeError, ValueError):
        return admin_error_page(
            "Некорректный вариант", "Проверьте название, цену, вес и остаток."
        )
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT product_id FROM product_options WHERE id = %s", (option_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_id = row[0]
        cursor.execute(
            """
            UPDATE product_options
            SET label = %s,
                weight = %s,
                price = %s,
                stock_quantity = %s,
                is_out_of_stock = %s,
                sort_order = %s,
                is_active = %s
            WHERE id = %s
            """,
            (
                label_value, weight_value, price_value, stock_quantity_value,
                is_out_of_stock, sort_value, active, option_id,
            ),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Вариант обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Вариант обновлён</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
                <a class="button button-link secondary" href="/options/{option_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/options/{option_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def toggle_product_option(option_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE product_options SET is_active = NOT is_active WHERE id = %s RETURNING product_id, is_active",
            (option_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Вариант не найден",
                """
                <section class="admin-card">
                  <h1>⚠️ Вариант не найден</h1>
                  <p>Такой вариант продажи не найден.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/products">← К товарам</a>
                  </div>
                </section>
                """,
            )
        product_id, is_active = row
        title = "✅ Вариант включён" if is_active else "✅ Вариант скрыт"
        message = "Вариант снова доступен для выбора." if is_active else "Вариант скрыт из выбора."
        return admin_layout(
            title,
            f"""
            <section class="admin-card">
              <h1>{title}</h1>
              <p>{message}</p>
              <div class="form-actions">
                <a class="button button-link" href="/products/{product_id}/edit">← К товару</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/products/{product_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def update_product(
    product_id: int,
    category_id: int = Form(...),
    name: str = Form(...),
    pricing_mode: str = Form("per_kg"),
    price_per_kg: str = Form(""),
    fixed_price: str = Form(""),
    sale_unit: str = Form(""),
    unit_weight_grams: str = Form(""),
    stock_quantity: str = Form(""),
    description: str = Form(''),
    image_url: str = Form(''),
    stock_grams: str = Form("0"),
    low_stock_threshold_grams: str = Form("0"),
    sort_order: int = Form(0),
    is_active: str = Form(None),
    is_out_of_stock: bool = Form(False),
    is_promotion: bool = Form(False),
    promotion_title: str = Form(''),
    promotion_sort_order: int = Form(0),
):
    active = True if is_active else False
    try:
        (
            pricing_mode_value,
            price_per_kg_value,
            fixed_price_value,
            sale_unit_value,
            unit_weight_value,
            stock_quantity_value,
        ) = normalize_product_pricing(
            pricing_mode,
            price_per_kg,
            fixed_price,
            sale_unit,
            unit_weight_grams,
            stock_quantity,
        )
        submitted_stock_value = (
            normalize_product_stock_grams(pricing_mode_value, stock_grams)
            if pricing_mode_value == "per_kg"
            else None
        )
    except ValueError:
        return admin_error_page(
            "Некорректная цена", "Проверьте значения цены и единицы продажи."
        )
    try:
        low_stock_threshold_value = max(int(low_stock_threshold_grams or 0), 0)
    except (TypeError, ValueError):
        low_stock_threshold_value = 0
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT image_url, stock_grams FROM products WHERE id = %s",
            (product_id,),
        )
        row = cursor.fetchone()
        existing_image_url = row[0] if row else ""
        old_stock = int(row[1] or 0) if row else 0
        stock_value = (
            submitted_stock_value
            if submitted_stock_value is not None
            else normalize_product_stock_grams(
                pricing_mode_value, stock_grams, existing_stock_grams=old_stock
            )
        )
        submitted_image_url = image_url.strip()
        saved_image_url = submitted_image_url if submitted_image_url else existing_image_url
        cursor.execute(
            """
            UPDATE products
            SET category_id = %s,
                name = %s,
                price_per_kg = %s,
                pricing_mode = %s,
                fixed_price = %s,
                sale_unit = %s,
                unit_weight_grams = %s,
                stock_quantity = %s,
                description = %s,
                image_url = %s,
                stock_grams = %s,
                low_stock_threshold_grams = %s,
                is_out_of_stock = %s,
                sort_order = %s,
                is_active = %s,
                is_promotion = %s,
                promotion_title = %s,
                promotion_sort_order = %s
            WHERE id = %s
            """,
            (
                category_id, name, price_per_kg_value, pricing_mode_value,
                fixed_price_value, sale_unit_value, unit_weight_value,
                stock_quantity_value, description, saved_image_url, stock_value,
                low_stock_threshold_value, is_out_of_stock, sort_order, active,
                is_promotion, promotion_title or None, promotion_sort_order, product_id,
            ),
        )
        if old_stock != stock_value:
            log_inventory_movement(
                cursor,
                product_id,
                "manual_set",
                stock_value - old_stock,
                old_stock,
                stock_value,
                None,
                "Остаток изменён вручную в админке."
            )
        sync_low_stock_alert_state(cursor, [product_id])
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар обновлён",
            f"""
            <section class="admin-card">
              <h1>✅ Товар обновлён</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
                <a class="button button-link secondary" href="/products/{product_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception:
        log_admin_error(
            "/products/{product_id}/edit", "update_product", "product_update_failed"
        )
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/products/{product_id}/deactivate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def deactivate_product(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET is_active = FALSE WHERE id = %s", (product_id,))
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар скрыт",
            """
            <section class="admin-card">
              <h1>✅ Товар скрыт</h1>
              <p>Товар скрыт из каталога.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/products/{product_id}/activate",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def activate_product(product_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE products SET is_active = TRUE WHERE id = %s", (product_id,))
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Товар возвращён",
            """
            <section class="admin-card">
              <h1>✅ Товар возвращён</h1>
              <p>Товар снова отображается в каталоге.</p>
              <div class="form-actions">
                <a class="button button-link" href="/products">← К товарам</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories", response_class=HTMLResponse)
async def categories():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, sort_order, is_active
            FROM categories
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        html = "<section class='admin-card'><h1>🗂 Категории</h1><p><a class=\"button button-link\" href=\"/categories/new\">➕ Новая категория</a></p><div class='dash-table-wrap'><table class='categories-table'>"
        html += "<tr><th>ID</th><th>Название</th><th>Порядок</th><th>Статус</th><th>Действия</th></tr>"
        for row in rows:
            cid, name, sort_order, is_active = row
            active_text = "Активна" if is_active else "Скрыта"
            status_class = "active" if is_active else "inactive"
            actions_html = f"<div class=\"action-group\"><a class=\"button\" href=\"/categories/{cid}/edit\">Редактировать</a></div>"
            html += f"<tr><td>{cid}</td><td>{name}</td><td>{sort_order}</td><td><span class='status {status_class}'>{active_text}</span></td><td>{actions_html}</td></tr>"
        html += "</table></div></section>"
        return admin_layout("🗂 Категории", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories/new", response_class=HTMLResponse)
async def new_category_form():
    html = """
    <section class="admin-card">
      <h1>➕ Новая категория</h1>
      <p><a href="/categories">← Назад к категориям</a></p>
      <form class="admin-form" method="post" action="/categories/new">
        <label>Название категории <input name="name"/></label>
        <label>Порядок сортировки <input name="sort_order"/></label>
        <label>Активна <input type="checkbox" name="is_active" value="1"/></label>
        <div class="form-actions">
          <input class="button" type="submit" value="Создать категорию"/>
        </div>
      </form>
    </section>
    """
    return admin_layout("➕ Новая категория", html)


@app.post(
    "/categories/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def create_category(
    name: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO categories (name, sort_order, is_active)
            VALUES (%s, %s, %s)
            """,
            (name, sort_order, active),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Категория создана",
            """
            <section class="admin-card">
              <h1>✅ Категория создана</h1>
              <p>Категория успешно добавлена.</p>
              <div class="form-actions">
                <a class="button button-link" href="/categories">← К категориям</a>
                <a class="button button-link secondary" href="/categories/new">➕ Добавить ещё</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.get("/categories/{category_id}/edit", response_class=HTMLResponse)
async def edit_category_form(category_id: int):
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT name, sort_order, is_active
            FROM categories
            WHERE id = %s
            """,
            (category_id,),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return admin_layout(
                "⚠️ Категория не найдена",
                """
                <section class="admin-card">
                  <h1>⚠️ Категория не найдена</h1>
                  <p>Такая категория не найдена.</p>
                  <div class="form-actions">
                    <a class="button button-link" href="/categories">← К категориям</a>
                  </div>
                </section>
                """,
            )

        name, sort_order, is_active = row
        checked = "checked" if is_active else ""
        html = f"""
        <section class="admin-card">
          <h1>✏️ Редактировать категорию</h1>
          <p><a href="/categories">← Назад к категориям</a></p>
          <form class="admin-form" method="post" action="/categories/{category_id}/edit">
            <label>Название категории <input name="name" value="{name}"/></label>
            <label>Порядок сортировки <input name="sort_order" value="{sort_order}"/></label>
            <label>Активна <input type="checkbox" name="is_active" value="1" {checked}/></label>
            <div class="form-actions">
              <input class="button" type="submit" value="Сохранить изменения"/>
            </div>
          </form>
        </section>
        """
        return admin_layout("✏️ Редактировать категорию", html)
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


@app.post(
    "/categories/{category_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_csrf)],
)
async def update_category(
    category_id: int,
    name: str = Form(...),
    sort_order: int = Form(0),
    is_active: str = Form(None),
):
    active = True if is_active else False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE categories
            SET name = %s,
                sort_order = %s,
                is_active = %s
            WHERE id = %s
            """,
            (name, sort_order, active, category_id),
        )
        conn.commit()
        conn.close()
        return admin_layout(
            "✅ Категория обновлена",
            f"""
            <section class="admin-card">
              <h1>✅ Категория обновлена</h1>
              <p>Изменения успешно сохранены.</p>
              <div class="form-actions">
                <a class="button button-link" href="/categories">← К категориям</a>
                <a class="button button-link secondary" href="/categories/{category_id}/edit">✏️ Продолжить редактирование</a>
              </div>
            </section>
            """,
        )
    except Exception as e:
        return admin_error_page("Ошибка", "Не удалось выполнить операцию. Проверьте журнал или попробуйте позже.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

