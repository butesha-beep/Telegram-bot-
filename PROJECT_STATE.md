# Project State

## Current Architecture

This project is a single-shop Telegram commerce system with:

- Telegram customer bot
- FastAPI admin panel
- PostgreSQL database
- JSON-based seed catalog
- Manual payment workflow
- CRM/activity tracking
- Admin error logs
- Broadcast and channel posting tools

It is currently best described as a white-label clone base for one shop at a time, not a multi-tenant SaaS.

## File Roles

### bot.py

Customer-facing Telegram bot.

Main responsibilities:

- Starts aiogram bot polling.
- Handles `/start` and customer navigation.
- Shows categories, products, product options, cart, checkout, and payment flows.
- Creates orders.
- Sends admin notifications.
- Logs customer funnel events.
- Runs abandoned cart reminder worker.
- Runs unpaid/payment reminder worker.
- Seeds products/categories from JSON on startup.
- Performs demo catalog cleanup after seed.

### admin_app.py

Admin-facing FastAPI application.

Main responsibilities:

- Admin login/session handling.
- Dashboard with order, revenue, stock, funnel, and analytics sections.
- Order list/detail/status updates.
- Product/category management.
- Product options and stock editing.
- Client CRM pages and activity view.
- Error logs page.
- Channel posts UI.
- Broadcast draft/send UI.
- Low-stock alert sync and Telegram helper functions.

### db_schema.py

Shared database schema initialization module.

Main responsibilities:

- Resolves `DATABASE_URL` / `DATABASE_PUBLIC_URL`.
- Provides `get_db_connection()`.
- Provides `init_db()`.
- Creates/migrates tables and indexes.
- Lets both bot and admin initialize schema without importing each other.

### shop_settings.py

Small shop settings layer.

Main responsibilities:

- Loads `config.json` with UTF-8.
- Caches config values.
- Exposes brand/payment/currency/support constants.
- Currently used only partially by `admin_app.py`.

## PostgreSQL Tables

- `clients`: Telegram clients, contact data, admin note.
- `cart_items`: current cart rows per Telegram user.
- `customer_events`: bot funnel/activity events.
- `orders`: order header, status, payment state/timestamps.
- `order_items`: order line items.
- `order_events`: admin/order timeline events.
- `error_logs`: admin route error log records.
- `channel_posts`: admin-created Telegram channel posts.
- `broadcasts`: client broadcast drafts/send status.
- `broadcast_recipients`: per-user broadcast delivery status.
- `categories`: catalog categories.
- `products`: catalog products, stock fields, low-stock alert flags.
- `product_options`: sale options/weights/prices.
- `inventory_movements`: stock movement history.

## Completed Features

- Telegram customer catalog flow.
- Cart and checkout flow.
- Manual IBAN, PayPal, and cash payment choices.
- Order creation and admin notification.
- Admin dashboard.
- Admin order status management.
- Product/category CRUD-style management.
- Product options.
- Stock tracking and inventory movement history.
- Stock deduction on paid and restoration on cancelled.
- Low-stock alert flags and admin alert trigger.
- Customer event logging.
- Client activity and funnel stage display.
- Dashboard funnel today.
- Admin error logging and `/logs`.
- Channel posting UI.
- Broadcast draft/send UI.
- Broadcast target segments.
- Abandoned cart reminders hardened against retry loops.
- Payment reminders hardened against retry loops.
- Shared schema initialization in `db_schema.py`.
- Phase 1 shop settings layer.

## Partially Completed Features

- White-label settings: started, but bot/admin still have hardcoded currency and text.
- Clone readiness: workable for a developer, not yet non-developer friendly.
- Broadcasts: V1 works, but no opt-out/consent management or advanced segmentation.
- Channel posts: V1 text-only send/delete.
- Error logs: route errors logged, but no resolve/delete/filter UI.
- Images: catalog supports `image_url`, but no upload/storage workflow.
- Payment: manual only, no official payment provider integration.
- Deployment: assumed Railway setup, but no Procfile/Dockerfile/runbook enforcement.

## Known Risks

- Hardcoded EUR/euro display remains in bot and parts of admin.
- Payment methods are hardcoded.
- Delivery/minimum-order texts are hardcoded.
- Demo catalog cleanup assumes product IDs 1-8 and category IDs 1-4.
- No CSRF protection in admin forms.
- No connection pooling.
- No official backup/restore process documented in code.
- No automated tests.
- Product images depend on external URLs.
- Bot service must run to seed catalog.
- Broadcasts can message real users if a reused database is used.
- Telegram blocked users are handled per event/broadcast path, but clients table does not track global blocked status.

## Current Roadmap

1. Finish shop settings adoption.
2. Add `format_money()` and remove hardcoded currency display.
3. Make payment methods configurable.
4. Move delivery/minimum-order text to config/settings.
5. Remove hardcoded demo cleanup IDs.
6. Add deployment `.env.example`.
7. Add image upload/storage plan.
8. Add backup/restore process.
9. Add admin CSRF protection.
10. Add official payment integration later.
