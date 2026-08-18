# Clone Runbook

## Goal

Clone this project for a new single-shop demo or MVP.

This runbook assumes:

- One shop per Railway project/database.
- Separate Telegram bot per shop.
- Separate Telegram channel per shop.
- PostgreSQL is separate per shop.
- Manual payments stay enabled.

## Files To Change

### config.json

Update:

- `brand_name`
- `admin_panel_title`
- `support_username`
- `iban`
- `receiver_name`
- `paypal`
- `currency`
- `currency_symbol`

For EUR shops, keep:

```json
"currency": "EUR",
"currency_symbol": "EUR"
```

Use `EUR` in docs if the euro symbol displays incorrectly in the terminal. The app can still use the euro symbol in `config.json` when the file is valid UTF-8.

### products.json

Update product catalog.

Recommended for fastest clone:

- Keep product IDs `1` through `8`.
- Keep valid UTF-8 JSON.
- Use `image_url`, not `photo`.
- Use `is_active`, not `available`.
- Prefer direct image URLs ending in `.jpg`, `.png`, or `.webp`.

### categories.json

Update categories.

Recommended for fastest clone:

- Keep category IDs `1` through `4`.
- Keep valid UTF-8 JSON.
- Use `is_active`.

## Railway Services

Create a new Railway project with:

1. PostgreSQL service.
2. Bot service.
3. Admin service.

### Bot Service

Start command:

```bash
python bot.py
```

Required env:

- `BOT_TOKEN`
- `DATABASE_URL` or `DATABASE_PUBLIC_URL`

### Admin Service

Start command:

```bash
uvicorn admin_app:app --host 0.0.0.0 --port $PORT
```

Required env:

- `DATABASE_URL` or `DATABASE_PUBLIC_URL`
- `BOT_TOKEN`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`
- `CSRF_SECRET`
- `ADMIN_ID`
- `TELEGRAM_CHANNEL_ID`

Copyable env block:

```env
BOT_TOKEN=
DATABASE_URL=
ADMIN_PASSWORD=
ADMIN_SESSION_SECRET=
CSRF_SECRET=
ADMIN_ID=
TELEGRAM_CHANNEL_ID=
```

## Telegram Bot Setup

1. Open BotFather.
2. Create a new bot.
3. Save the token.
4. Set Railway `BOT_TOKEN`.
5. Send `/start` to the bot after deployment.

## Telegram Channel Setup

1. Create or choose a channel.
2. Add the bot as channel admin.
3. Give permission to post messages.
4. Set Railway `TELEGRAM_CHANNEL_ID`.

Examples:

- Public channel: `@channel_username`
- Private channel: numeric channel ID

## Admin Setup

1. Choose a strong `ADMIN_PASSWORD`.
2. Generate a long random `ADMIN_SESSION_SECRET`.
3. Generate a separate high-entropy `CSRF_SECRET` of at least 32 characters.
   Do not reuse an admin password, session secret, bot token, or preview-gate
   password. Preview and production startup fail closed when it is absent or
   invalid.
4. Set `ADMIN_ID` only when Telegram actions are explicitly enabled. It is
   read only from the process environment; `config.json.admin_id` is not a
   recipient fallback.

## Database Startup Order

Recommended:

1. Provision PostgreSQL without reusing a production or client database.
2. Prepare and validate a compatible schema through a separately authorized
   process before starting either application service.
3. Configure the admin and bot to use that prepared database.
4. Start the admin and bot only after compatibility has been confirmed.

Why:

- Admin and bot do not call `init_db()` or seed data during ordinary startup.
- Ordinary startup performs only a read-only schema compatibility probe.
- An unavailable or incompatible schema fails closed.
- No automatic initialization or catalog seed exists.
- `RUN_DB_INIT` and `RUN_DB_SEED` do not enable writable startup behavior.
- No writable maintenance command is currently exposed.

### Web session security schema

Admin and master authentication now use shared PostgreSQL-backed session state.
The database must contain compatible `web_sessions` and
`consumed_login_nonces` tables, including the required constraints and indexes,
before either administrative interface is considered ready. Raw session
credentials and pre-authentication nonces are never stored; only
domain-separated cryptographic hashes are persisted. A successful re-login
revokes the previous role/account session, and POST logout revokes the exact
current session before its browser cookie is deleted.

Ordinary startup only checks this schema in a read-only transaction. It does
not create or alter these tables. Provisioning them requires a separately
authorized schema-maintenance step; this repository does not currently expose
such a runtime command.

### CSRF form behavior

Ordinary URL-encoded admin forms submit one hidden CSRF token. JSON requests
and multipart uploads require the same token in the `X-CSRF-Token` header.
The order-weighing photo form uses same-origin JavaScript to add that header
before the multipart body is parsed. With JavaScript disabled, the rendered
`noscript` fallback still permits a URL-encoded weight-only submission; photo
upload is intentionally unavailable in that fallback.

## Clean DB Launch

Use a fresh PostgreSQL database for every new shop demo.

New shop clones should use a new PostgreSQL DB.

Do not reuse a real-client DB for tests, demos, or broadcasts.

Do not reuse a DB from another shop unless you intentionally want old:

- clients
- orders
- carts
- broadcasts
- customer events
- products
- categories

## Legacy Seed Behavior Warning

The repository retains legacy catalog-seeding code, but ordinary admin and bot
startup never invoke it and no supported maintenance command currently exposes
it. Do not expect `products.json` or `categories.json` to populate a deployed
database automatically.

Any future separately authorized import must account for old products,
categories, clients, orders, carts, broadcasts, and events when a database is
reused.

Current demo cleanup assumes:

- product IDs `1` through `8`
- category IDs `1` through `4`

Changing those IDs may require code changes.

## Payment Methods Warning

IBAN, PayPal, and Cash are still fixed in code.

Payment details are configurable in `config.json`, but the available payment methods are not fully configurable yet.

## Image URL Warning

Use direct image links.

Good:

- `https://example.com/product.jpg`
- `https://example.com/product.png`
- `https://example.com/product.webp`

Avoid image page URLs such as generic gallery/share pages. They may not render as product photos in Telegram.

## Clone Time Estimate

- Developer clone today: 1-3 hours.
- After settings/catalog cleanup: 30-45 minutes.
- Later, after full white-label cleanup: 10-15 minutes.

## Troubleshooting

### BOT_TOKEN missing

Symptom:

- Bot service crashes with `BOT_TOKEN is not set`.

Fix:

- Set `BOT_TOKEN` in the bot service env.
- If admin sends Telegram notifications/posts, set `BOT_TOKEN` in admin service env too.

### DATABASE_URL missing

Symptom:

- Bot/admin crashes with `DATABASE_URL is not set`.

Fix:

- Attach PostgreSQL service.
- Set `DATABASE_URL` or `DATABASE_PUBLIC_URL` in both bot and admin services.

### Channel send fails

Likely causes:

- `TELEGRAM_CHANNEL_ID` is missing or wrong.
- Bot is not admin in the channel.
- Bot lacks permission to post.
- `BOT_TOKEN` is missing in admin service.

### Admin login env missing

Symptom:

- Admin auth may be disabled or login fails.

Fix:

- Set `ADMIN_PASSWORD`.
- Set `ADMIN_SESSION_SECRET`.

### Catalog not seeded

Symptom:

- Admin loads, but products/categories are missing.

Fix:

- Stop the rollout and confirm that bot and admin use the intended database.
- Prepare or import the catalog only through a separately authorized process.
- Do not restart services or enable runtime flags expecting them to initialize
  or seed the database.

## Test Checklist After Deployment

Customer bot:

- `/start` works.
- Brand name is correct.
- Categories show.
- Products show.
- Product prices show.
- Add to cart works.
- Cart opens.
- Quantity changes work.
- Checkout starts.
- Minimum order text is acceptable.
- Phone/address flow works.
- Saved contact/address flow works.
- IBAN payment screen shows correct details.
- PayPal payment screen shows correct details.
- Cash flow creates order.
- Payment reported button works.

Admin:

- Login works.
- Dashboard loads.
- Orders list loads.
- Order detail loads.
- Status buttons work.
- Product list loads.
- Category list loads.
- Client list loads.
- Client detail activity loads.
- Logs page loads.
- Channel page works.
- Broadcast draft creates recipients.

Inventory:

- Stock edits work.
- Paid order deducts stock.
- Cancelled paid/preparing order restores stock.
- Low-stock alert does not spam.

Telegram:

- Admin receives new order notifications.
- Channel post sends.
- Broadcast send works only when intended.

## Fast Clone Checklist

1. Copy repo/project.
2. Edit `config.json`.
3. Edit `products.json`.
4. Edit `categories.json`.
5. Add product image URLs.
6. Create Telegram bot.
7. Create Telegram channel.
8. Create Railway project.
9. Add PostgreSQL.
10. Add bot/admin services.
11. Set env variables.
12. Start services.
13. Test bot.
14. Test admin.
15. Create one full test order.
