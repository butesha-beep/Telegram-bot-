"""Channel-independent order-creation core (Orders v2, Checkpoint F).

Owns exactly two responsibilities, both already proven correct and stable
in bot.py's per-mode Commerce Foundation before this module existed:

1. price_single_line -- pure per-line pricing math (per_kg / fixed /
   options). Extracted verbatim from bot.py's price_cart_items so both
   the Telegram bot and the admin manual-order form are guaranteed to
   price a line identically -- there is exactly one implementation of
   "how much does this line cost."

2. insert_order -- given already-priced lines plus a resolved
   client/contact/delivery snapshot, writes the orders + order_items rows
   with Orders v2 defaults (payment_status/fulfillment_status/source).
   Never touches inventory (deduction only happens at
   fulfillment_status='packed', in admin_app.py, unchanged by this
   checkpoint) and never sends any notification.

Deliberately narrow: this module does NOT own cart/session state, client
search/creation UX, Telegram-specific messaging, or admin-form parsing --
those differ structurally per channel and stay in bot.py / admin_app.py
respectively. Fusing them here would turn this into the broad
service/repository architecture this checkpoint explicitly avoids.
"""


class OrderCreationError(ValueError):
    """Raised for a caller programming error (not a user input validation
    error -- callers are expected to validate user input themselves before
    reaching this module)."""


ORDER_INITIAL_PAYMENT_STATUSES = ("unpaid", "paid")


def price_single_line(product, weight, option_id, option_price):
    """Given a product row (dict with pricing_mode/price_per_kg/
    fixed_price) and the selected weight/option for one order line,
    returns (price, pricing_mode, price_per_kg_snapshot) -- the exact
    Commerce Foundation per-mode rules:

    - fixed: fixed_price, as-is.
    - options (option_id + option_price given): the option's own price,
      as-is -- never derived from the product's price_per_kg. This
      includes a per_kg product sold via preset size options ("variable
      weight" buttons) -- the price still comes from the option, but the
      product remains pricing_mode='per_kg', so price_per_kg_snapshot is
      still captured below (computed independently of which branch priced
      the line, matching the pre-extraction behavior exactly).
    - per_kg (no usable option): price_per_kg * weight / 1000.

    Pure function: no side effects, no DB access.
    """
    pricing_mode = product.get("pricing_mode") or "per_kg"
    if pricing_mode == "fixed":
        price = float(product.get("fixed_price") or 0)
    elif option_id and option_price is not None:
        price = float(option_price)
    else:
        price = product["price_per_kg"] * weight / 1000
    price_per_kg_snapshot = product["price_per_kg"] if pricing_mode == "per_kg" else None
    return price, pricing_mode, price_per_kg_snapshot


def insert_order(
    cursor,
    *,
    source,
    priced_items,
    client_id=None,
    telegram_id=None,
    username=None,
    customer_name=None,
    phone=None,
    address=None,
    payment_method=None,
    payment_status="unpaid",
    delivery_method=None,
    delivery_street=None,
    delivery_house_number=None,
    delivery_postcode=None,
    delivery_city=None,
    delivery_country=None,
    delivery_notes=None,
    source_reference=None,
    order_id=None,
):
    """Writes one order and its lines inside the caller's existing
    transaction (this function never commits/rollbacks/closes anything).

    priced_items: list of dicts, each with product_id, product_name,
    weight, option_id, price, pricing_mode, price_per_kg_snapshot (i.e.
    the output shape of price_single_line plus display/identity fields).

    order_id: pass the channel's own value (e.g. Telegram's
    user_id+timestamp scheme) to preserve it exactly. Pass None to let
    this order be identified by its own orders.id -- the row is inserted
    first (auto-generating id), then order_id is set equal to id, so
    every existing order_id-keyed mechanism (order_items, order_events,
    inventory_movements, every admin route) keeps working unchanged for
    orders created this way. This is the manual-order path; Telegram
    orders always pass an explicit order_id and never touch this branch.

    Always writes fulfillment_status='new' and never deducts inventory --
    deduction only ever happens later, at the fulfillment_status='packed'
    transition (admin_app.py), regardless of channel or payment_status.

    Raises OrderCreationError if payment_status is not an allowed initial
    value or priced_items is empty -- both are caller bugs, not user
    input to recover from here.
    """
    if payment_status not in ORDER_INITIAL_PAYMENT_STATUSES:
        raise OrderCreationError(
            f"invalid initial payment_status: {payment_status!r}"
        )
    if not priced_items:
        raise OrderCreationError("an order must have at least one line")

    total = sum(item["price"] for item in priced_items)

    columns = [
        "telegram_id", "username", "phone", "address", "total",
        "source", "source_reference", "client_id", "payment_status",
        "fulfillment_status", "payment_method", "customer_name",
        "delivery_method", "delivery_street", "delivery_house_number",
        "delivery_postcode", "delivery_city", "delivery_country",
        "delivery_notes",
    ]
    values = [
        telegram_id, username, phone, address, total,
        source, source_reference, client_id, payment_status,
        "new", payment_method, customer_name,
        delivery_method, delivery_street, delivery_house_number,
        delivery_postcode, delivery_city, delivery_country,
        delivery_notes,
    ]
    if order_id is not None:
        columns = ["order_id"] + columns
        values = [order_id] + values

    column_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(values))
    returning_clause = "" if order_id is not None else "RETURNING id"
    cursor.execute(
        f"""
        INSERT INTO orders ({column_list}, created_at, updated_at)
        VALUES ({placeholders}, NOW(), NOW())
        {returning_clause}
        """,
        values,
    )
    if order_id is None:
        order_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE orders SET order_id = %s WHERE id = %s",
            (order_id, order_id),
        )

    for item in priced_items:
        cursor.execute(
            "INSERT INTO order_items "
            "(order_id, product_id, product_name, weight, price, option_id, pricing_mode, price_per_kg_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                order_id,
                item["product_id"],
                item["product_name"],
                item["weight"],
                item["price"],
                item["option_id"],
                item["pricing_mode"],
                item["price_per_kg_snapshot"],
            ),
        )

    return order_id, total
