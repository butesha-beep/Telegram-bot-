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
- `admin_id`
- `iban`
- `receiver_name`
- `paypal`
- `currency`
- `currency_symbol`

For EUR shops, keep:

```json
"currency": "EUR",
"currency_symbol": "€"
```

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
- `ADMIN_ID`
- `TELEGRAM_CHANNEL_ID`

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
3. Set `ADMIN_ID` to the admin Telegram numeric user ID.
4. Also update `config.json.admin_id` as fallback.

## Database Startup Order

Recommended:

1. Deploy PostgreSQL.
2. Deploy admin service.
3. Deploy bot service.
4. Start bot service at least once.

Why:

- `admin_app.py` runs `init_db()` and creates schema.
- `bot.py` runs `init_db()` and seeds products/categories from JSON.
- Catalog seeding currently happens from bot startup.

## Clean DB Launch

Use a fresh PostgreSQL database for every new shop demo.

Do not reuse a DB from another shop unless you intentionally want old:

- clients
- orders
- carts
- broadcasts
- customer events
- products
- categories

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
