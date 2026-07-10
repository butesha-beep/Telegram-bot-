import asyncio
import json
from json.tool import main
import os
import random
import sqlite3
import psycopg2
import os

from db_schema import DATABASE_URL, get_db_connection, init_db
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN is not set")

bot = Bot(token=TOKEN)
dp = Dispatcher()
pending_orders = {}
MAX_CART_ITEM_QUANTITY = 10
CALLBACK_COOLDOWN_SECONDS = 1.0
ABANDONED_CART_REMINDER_HOURS = 0.5
ABANDONED_CART_DELETE_AFTER_REMINDER_HOURS = 0.5
ABANDONED_CART_WORKER_SLEEP_SECONDS = 300
PENDING_ORDER_REMINDER_MINUTES = 30
PENDING_ORDER_CANCEL_HOURS = 2
AWAITING_PAYMENT_REMINDER_MINUTES = 30
AWAITING_PAYMENT_CANCEL_HOURS = 24
UNPAID_ORDER_WORKER_SLEEP_SECONDS = 1800
callback_locks = {}


def log_customer_event(telegram_id, event_type, metadata=None):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO customer_events
            (telegram_id, event_type, metadata)
            VALUES (%s, %s, %s)
            """,
            (
                telegram_id,
                event_type,
                json.dumps(metadata or {})
            )
        )

        conn.commit()
        cursor.close()
        conn.close()

    except Exception as e:
        print(f"Customer event logging failed: {e}")


def is_callback_locked(callback, cooldown=CALLBACK_COOLDOWN_SECONDS):
    now = asyncio.get_event_loop().time()
    key = (callback.from_user.id, callback.data)
    last_seen = callback_locks.get(key, 0)
    if now - last_seen < cooldown:
        return True
    callback_locks[key] = now
    return False


def is_telegram_blocked_error(error):
    error_text = str(error).lower()
    return (
        "403" in error_text
        or "forbidden" in error_text
        or "bot was blocked" in error_text
    )


def mark_cart_active(telegram_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE cart_items
        SET updated_at = NOW(),
            reminded_at = NULL
        WHERE telegram_id = %s
        """,
        (telegram_id,)
    )
    conn.commit()
    conn.close()


def abandoned_cart_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Открыть корзину", callback_data="cart")]
    ])


async def send_abandoned_cart_reminders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT telegram_id
        FROM cart_items
        GROUP BY telegram_id
        HAVING MAX(updated_at) <= NOW() - (%s * INTERVAL '1 hour')
           AND MAX(reminded_at) IS NULL
        """,
        (ABANDONED_CART_REMINDER_HOURS,)
    )
    telegram_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    for telegram_id in telegram_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text="🛒 Вы забыли товары в корзине.\n\nКорзина будет очищена через 30 минут, если заказ не будет оформлен.",
                reply_markup=abandoned_cart_keyboard()
            )

            update_conn = psycopg2.connect(DATABASE_URL)
            try:
                update_cursor = update_conn.cursor()
                update_cursor.execute(
                    """
                    UPDATE cart_items
                    SET reminded_at = NOW()
                    WHERE telegram_id = %s
                      AND reminded_at IS NULL
                    """,
                    (telegram_id,)
                )
                update_conn.commit()
            finally:
                update_conn.close()
            log_customer_event(
                telegram_id,
                "abandoned_cart_reminder_sent",
                {}
            )
        except Exception as e:
            reason = "blocked" if is_telegram_blocked_error(e) else "failed"
            try:
                update_conn = psycopg2.connect(DATABASE_URL)
                try:
                    update_cursor = update_conn.cursor()
                    update_cursor.execute(
                        """
                        UPDATE cart_items
                        SET reminded_at = NOW()
                        WHERE telegram_id = %s
                          AND reminded_at IS NULL
                        """,
                        (telegram_id,)
                    )
                    update_conn.commit()
                finally:
                    update_conn.close()
            except Exception as update_error:
                print(f"ABANDONED CART REMINDER MARK ERROR for {telegram_id}:", update_error)
            log_customer_event(
                telegram_id,
                "abandoned_cart_reminder_failed",
                {"reason": reason}
            )
            print(f"ABANDONED CART REMINDER ERROR for {telegram_id}:", e)


async def clear_expired_abandoned_carts():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT telegram_id
        FROM cart_items
        GROUP BY telegram_id
        HAVING MAX(reminded_at) <= NOW() - (%s * INTERVAL '1 hour')
        """,
        (ABANDONED_CART_DELETE_AFTER_REMINDER_HOURS,)
    )
    telegram_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    for telegram_id in telegram_ids:
        try:
            delete_conn = psycopg2.connect(DATABASE_URL)
            try:
                delete_cursor = delete_conn.cursor()
                delete_cursor.execute(
                    "DELETE FROM cart_items WHERE telegram_id = %s",
                    (telegram_id,)
                )
                delete_conn.commit()
                log_customer_event(
                    telegram_id,
                    "abandoned_cart_cleared",
                    {"reason": "expired_after_reminder"}
                )
            finally:
                delete_conn.close()
        except Exception as e:
            print(f"ABANDONED CART CLEANUP ERROR for {telegram_id}:", e)


async def abandoned_cart_worker():
    while True:
        try:
            await send_abandoned_cart_reminders()
            await clear_expired_abandoned_carts()
        except Exception as e:
            print("ABANDONED CART WORKER ERROR:", e)

        await asyncio.sleep(ABANDONED_CART_WORKER_SLEEP_SECONDS)


def unpaid_order_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Открыть заказ / оплату", callback_data="resume_payment")]
    ])


async def send_pending_order_reminders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT telegram_id
        FROM orders
        WHERE status = 'pending'
          AND payment_method IS NULL
          AND created_at <= NOW() - (%s * INTERVAL '1 minute')
          AND payment_reminded_at IS NULL
        """,
        (PENDING_ORDER_REMINDER_MINUTES,)
    )
    telegram_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    for telegram_id in telegram_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text="🧾 У вас есть незавершённый заказ.\n\nВы ещё не выбрали способ оплаты. Если заказ уже не актуален, он будет отменён автоматически.",
                reply_markup=unpaid_order_keyboard()
            )

            update_conn = psycopg2.connect(DATABASE_URL)
            try:
                update_cursor = update_conn.cursor()
                update_cursor.execute(
                    """
                    UPDATE orders
                    SET payment_reminded_at = NOW(),
                        updated_at = NOW()
                    WHERE telegram_id = %s
                      AND status = 'pending'
                      AND payment_reminded_at IS NULL
                    """,
                    (telegram_id,)
                )
                update_conn.commit()
            finally:
                update_conn.close()
            log_customer_event(
                telegram_id,
                "pending_order_reminder_sent",
                {}
            )
        except Exception as e:
            reason = "blocked" if is_telegram_blocked_error(e) else "failed"
            try:
                update_conn = psycopg2.connect(DATABASE_URL)
                try:
                    update_cursor = update_conn.cursor()
                    update_cursor.execute(
                        """
                        UPDATE orders
                        SET payment_reminded_at = NOW(),
                            updated_at = NOW()
                        WHERE telegram_id = %s
                          AND status = 'pending'
                          AND payment_reminded_at IS NULL
                        """,
                        (telegram_id,)
                    )
                    update_conn.commit()
                finally:
                    update_conn.close()
            except Exception as update_error:
                print(f"PENDING ORDER REMINDER MARK ERROR for {telegram_id}:", update_error)
            log_customer_event(
                telegram_id,
                "pending_order_reminder_failed",
                {"reason": reason}
            )
            print(f"PENDING ORDER REMINDER ERROR for {telegram_id}:", e)


async def cancel_expired_pending_orders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE orders
        SET status = 'cancelled',
            updated_at = NOW()
        WHERE status = 'pending'
          AND payment_method IS NULL
          AND created_at <= NOW() - (%s * INTERVAL '1 hour')
        """,
        (PENDING_ORDER_CANCEL_HOURS,)
    )
    conn.commit()
    conn.close()


async def send_awaiting_payment_reminders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT telegram_id
        FROM orders
        WHERE status = 'awaiting_payment'
          AND payment_selected_at <= NOW() - (%s * INTERVAL '1 minute')
          AND payment_reminded_at IS NULL
        """,
        (AWAITING_PAYMENT_REMINDER_MINUTES,)
    )
    telegram_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    for telegram_id in telegram_ids:
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text="💳 Вы выбрали оплату, но заказ ещё не отмечен как оплаченный.\n\nЕсли вы уже оплатили, нажмите «Я оплатил» в сообщении с оплатой или напишите администратору.",
                reply_markup=unpaid_order_keyboard()
            )

            update_conn = psycopg2.connect(DATABASE_URL)
            try:
                update_cursor = update_conn.cursor()
                update_cursor.execute(
                    """
                    UPDATE orders
                    SET payment_reminded_at = NOW(),
                        updated_at = NOW()
                    WHERE telegram_id = %s
                      AND status = 'awaiting_payment'
                      AND payment_reminded_at IS NULL
                    """,
                    (telegram_id,)
                )
                update_conn.commit()
            finally:
                update_conn.close()
            log_customer_event(
                telegram_id,
                "awaiting_payment_reminder_sent",
                {}
            )
        except Exception as e:
            reason = "blocked" if is_telegram_blocked_error(e) else "failed"
            try:
                update_conn = psycopg2.connect(DATABASE_URL)
                try:
                    update_cursor = update_conn.cursor()
                    update_cursor.execute(
                        """
                        UPDATE orders
                        SET payment_reminded_at = NOW(),
                            updated_at = NOW()
                        WHERE telegram_id = %s
                          AND status = 'awaiting_payment'
                          AND payment_reminded_at IS NULL
                        """,
                        (telegram_id,)
                    )
                    update_conn.commit()
                finally:
                    update_conn.close()
            except Exception as update_error:
                print(f"AWAITING PAYMENT REMINDER MARK ERROR for {telegram_id}:", update_error)
            log_customer_event(
                telegram_id,
                "awaiting_payment_reminder_failed",
                {"reason": reason}
            )
            print(f"AWAITING PAYMENT REMINDER ERROR for {telegram_id}:", e)


async def cancel_expired_awaiting_payment_orders():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE orders
        SET status = 'cancelled',
            updated_at = NOW()
        WHERE status = 'awaiting_payment'
          AND payment_selected_at <= NOW() - (%s * INTERVAL '1 hour')
        """,
        (AWAITING_PAYMENT_CANCEL_HOURS,)
    )
    conn.commit()
    conn.close()


async def unpaid_order_worker():
    while True:
        try:
            await send_pending_order_reminders()
            await cancel_expired_pending_orders()
            await send_awaiting_payment_reminders()
            await cancel_expired_awaiting_payment_orders()
        except Exception as e:
            print("UNPAID ORDER WORKER ERROR:", e)

        await asyncio.sleep(UNPAID_ORDER_WORKER_SLEEP_SECONDS)


def load_json(filename):
    with open(filename, "r", encoding="utf-8") as file:
        return json.load(file)
    

def get_products():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, category_id, name, price_per_kg, description, image_url, is_active, stock_grams, is_out_of_stock
            FROM products
            WHERE is_active = TRUE
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        if rows:
            products = []
            for row in rows:
                product_id, category_id, name, price_per_kg, description, image_url, is_active, stock_grams, is_out_of_stock = row
                products.append({
                    "id": product_id,
                    "category_id": category_id,
                    "name": name,
                    "price_per_kg": price_per_kg,
                    "description": description,
                    "image_url": image_url,
                    "photo": image_url,
                    "is_active": is_active,
                    "stock_grams": stock_grams,
                    "is_out_of_stock": is_out_of_stock
                })
            return products
    except Exception:
        pass

    return load_json("products.json")


def get_promotion_products():
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, category_id, name, price_per_kg, description, image_url, stock_grams, is_out_of_stock
        FROM products
        WHERE is_active = TRUE
          AND is_promotion = TRUE
        ORDER BY promotion_sort_order, sort_order, id
    """)
    rows = cursor.fetchall()
    conn.close()

    products = []
    for row in rows:
        product_id, category_id, name, price_per_kg, description, image_url, stock_grams, is_out_of_stock = row
        products.append({
            "id": product_id,
            "category_id": category_id,
            "name": name,
            "price_per_kg": price_per_kg,
            "description": description,
            "image_url": image_url,
            "photo": image_url,
            "stock_grams": stock_grams,
            "is_out_of_stock": is_out_of_stock
        })
    return products


def get_cart_product_ids(telegram_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT product_id FROM cart_items WHERE telegram_id = %s",
        (telegram_id,)
    )
    cart_product_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return cart_product_ids


def has_available_promotions(cart_product_ids=None):
    cart_product_ids = cart_product_ids or set()
    for product in get_promotion_products():
        if product["id"] in cart_product_ids:
            continue
        if is_product_out_of_stock(product):
            continue
        return True
    return False


OUT_OF_STOCK_TEXT = (
    "❌ Сейчас нет в наличии.\n"
    "Напишите администратору — подберём замену или сообщим о поступлении."
)


def is_product_out_of_stock(product):
    if product.get("is_out_of_stock"):
        return True
    stock_grams = product.get("stock_grams")
    if stock_grams is None:
        return False
    return int(stock_grams or 0) <= 0


def get_alternative_products(category_id, exclude_product_id, limit=3):
    products = get_products()
    alternatives = [
        product for product in products
        if product.get("category_id") == category_id
        and product["id"] != exclude_product_id
        and not is_product_out_of_stock(product)
    ]
    return alternatives[:limit]


UPSELL_INTRO_PHRASES = [
    "😋 Кстати, очень рекомендую попробовать ещё:",
    "🔥 Пока собираем ваш заказ, обратите внимание:",
    "❤️ Многие наши покупатели ещё берут:",
    "👌 Отлично дополнит ваш выбор:",
    "🤤 Это тоже стоит попробовать:",
]

AUTO_RECOMMENDATION_PHRASES = [
    "😋 Возможно, вам понравится ещё:",
    "👌 Обратите внимание ещё на эти товары:",
    "🔥 Может быть, захотите добавить что-нибудь ещё:",
]


def get_recommended_products(product_id, cart_product_ids=None, limit=3):
    cart_product_ids = cart_product_ids or set()

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT recommended_product_id
        FROM product_recommendations
        WHERE product_id = %s
          AND recommendation_type = 'frequently_bought_together'
          AND is_active = TRUE
        ORDER BY sort_order
        """,
        (product_id,)
    )
    recommended_ids = [row[0] for row in cursor.fetchall()]
    conn.close()

    if not recommended_ids:
        return []

    products = get_products()
    eligible_products = []
    for recommended_id in recommended_ids:
        if recommended_id in cart_product_ids:
            continue
        product = next((p for p in products if p["id"] == recommended_id), None)
        if not product:
            continue
        if is_product_out_of_stock(product):
            continue
        eligible_products.append(product)

    if not eligible_products:
        return []

    return random.sample(eligible_products, min(limit, len(eligible_products)))


def get_automatic_recommendations(product_id, cart_product_ids=None, limit=3):
    cart_product_ids = cart_product_ids or set()
    products = get_products()

    eligible_products = [
        product for product in products
        if product["id"] != product_id
        and product["id"] not in cart_product_ids
        and not is_product_out_of_stock(product)
    ]

    if not eligible_products:
        return []

    products_by_category = {}
    for product in eligible_products:
        products_by_category.setdefault(product["category_id"], []).append(product)

    category_ids = list(products_by_category.keys())
    random.shuffle(category_ids)

    selected_products = []
    selected_ids = set()

    for category_id in category_ids:
        if len(selected_products) >= limit:
            break
        candidate = random.choice(products_by_category[category_id])
        selected_products.append(candidate)
        selected_ids.add(candidate["id"])

    if len(selected_products) < limit:
        remaining_products = [
            product for product in eligible_products
            if product["id"] not in selected_ids
        ]
        random.shuffle(remaining_products)
        for product in remaining_products:
            if len(selected_products) >= limit:
                break
            selected_products.append(product)
            selected_ids.add(product["id"])

    return selected_products


def out_of_stock_keyboard(category_id=None, alternatives=None):
    config = load_json("config.json")
    support_username = str(config.get("support_username", "")).strip().lstrip("@")
    contact_button = InlineKeyboardButton(
        text="💬 Написать администратору",
        url=f"https://t.me/{support_username}"
    ) if support_username else InlineKeyboardButton(
        text="💬 Написать администратору",
        callback_data="support"
    )
    back_callback = f"category_{category_id}" if category_id else "back_to_menu"

    keyboard_rows = []
    for product in (alternatives or []):
        keyboard_rows.append([
            InlineKeyboardButton(
                text=DEAL_MARKET_PRODUCT_BUTTON_LABELS.get(product["name"], product["name"]),
                callback_data=f"product_{product['id']}"
            )
        ])

    keyboard_rows.append([contact_button])
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def seed_products_from_json():
    categories = load_json("categories.json")
    products = load_json("products.json")

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    for category in categories:
        cursor.execute(
            """
            INSERT INTO categories (id, name, sort_order, is_active)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                sort_order = EXCLUDED.sort_order,
                is_active = EXCLUDED.is_active
            """,
            (
                category["id"],
                category["name"],
                category.get("sort_order", 0),
                category.get("is_active", True)
            )
        )

    for product in products:
        cursor.execute(
            """
            INSERT INTO products (
                id,
                category_id,
                name,
                price_per_kg,
                description,
                image_url,
                is_active,
                sort_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                category_id = EXCLUDED.category_id,
                name = EXCLUDED.name,
                price_per_kg = EXCLUDED.price_per_kg,
                description = EXCLUDED.description,
                image_url = EXCLUDED.image_url,
                is_active = EXCLUDED.is_active,
                sort_order = EXCLUDED.sort_order
            """,
            (
                product["id"],
                product["category_id"],
                product["name"],
                product["price_per_kg"],
                product.get("description"),
                product.get("image_url") or product.get("photo"),
                product.get("is_active", True),
                product.get("sort_order", 0)
            )
        )

    if os.getenv("ENABLE_DEMO_CATALOG_CLEANUP") == "1":
        cleanup_demo_catalog(cursor)
    conn.commit()
    conn.close()


def cleanup_demo_catalog(cursor):
    demo_product_ids = (1, 2, 3, 4, 5, 6, 7, 8)
    demo_category_ids = (1, 2, 3, 4)

    cursor.execute(
        """
        UPDATE products
        SET is_active = FALSE
        WHERE id NOT IN %s
        """,
        (demo_product_ids,)
    )
    cursor.execute(
        """
        UPDATE categories c
        SET is_active = FALSE
        WHERE c.id NOT IN %s
          AND NOT EXISTS (
              SELECT 1
              FROM products p
              WHERE p.category_id = c.id
                AND p.is_active = TRUE
          )
        """,
        (demo_category_ids,)
    )
    cursor.execute(
        """
        INSERT INTO product_options (
            product_id,
            label,
            weight,
            price,
            sort_order,
            is_active
        )
        SELECT
            p.id,
            option_data.label,
            option_data.weight,
            p.price_per_kg * option_data.weight / 1000,
            option_data.weight,
            TRUE
        FROM products p
        CROSS JOIN (
            VALUES
                ('50 г', 50),
                ('100 г', 100),
                ('200 г', 200),
                ('500 г', 500)
        ) AS option_data(label, weight)
        WHERE p.id IN %s
          AND p.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1
              FROM product_options po
              WHERE po.product_id = p.id
                AND po.is_active = TRUE
          )
        """,
        (demo_product_ids,)
    )
    print("Demo catalog cleanup completed.")


def save_client(user):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO clients (
            telegram_id,
            username,
            first_name
        )
        VALUES (%s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET
            username = EXCLUDED.username,
            first_name = EXCLUDED.first_name
    """, (
        user.id,
        user.username,
        user.first_name
    ))

    conn.commit()
    conn.close()


DEAL_MARKET_CATEGORY_LABELS = {
    1: "🐟 Рыбные снеки",
    2: "🥩 Мясные снеки",
    3: "🔥 Копчёная рыба",
    4: "🎁 Подарочные наборы",
}


DEAL_MARKET_CATEGORY_INTROS = {
    1: (
        "🐟 Рыбные снеки\n\n"
        "🌊 Подборка популярных рыбных деликатесов и закусок.\n\n"
        "🍺 Отлично подходят к пиву\n"
        "🐟 Натуральный вкус\n"
        "⭐️ Проверенные хиты продаж\n\n"
        "👇 Выберите товар ниже:"
    ),
    2: (
        "🥩 Мясные снеки\n\n"
        "🔥 Настоящее мясо. Настоящий вкус.\n\n"
        "Отборные мясные закуски ручного приготовления без лишних добавок.\n\n"
        "🍺 Отлично подходят к пиву\n"
        "🔥 Для компании друзей\n"
        "🥩 Для настоящих ценителей мяса\n\n"
        "👇 Выберите понравившийся товар:"
    ),
    3: (
        "🔥 Копчёная рыба\n\n"
        "Традиционное копчение и насыщенный вкус.\n\n"
        "🐟 Отборная рыба\n"
        "🔥 Аромат натурального копчения\n"
        "🍺 Отличный выбор к пиву\n\n"
        "👇 Выберите товар ниже:"
    ),
    4: (
        "🎁 Подарочные наборы\n\n"
        "Готовые наборы для подарка, отдыха или праздничного стола.\n\n"
        "🎉 Для друзей и близких\n"
        "🍺 Для компании\n"
        "🎁 Уже готово к вручению\n\n"
        "👇 Выберите подходящий вариант:"
    ),
}


DEAL_MARKET_PRODUCT_BUTTON_LABELS = {
    "Янтарная с перцем": "🌶 Янтарная с перцем",
    "Тунец сушёный": "🐟 Тунец сушёный",
    "Кальмар кольца": "🦑 Кальмар кольца",
}


DEAL_MARKET_PRODUCT_CARD_ICONS = {
    1: "🐟",
    2: "🥩",
    3: "🔥",
    4: "🎁",
}


DEAL_MARKET_WELCOME_MESSAGE = (
    "👋 Добро пожаловать в Deal Market NL!\n\n"
    "Здесь вы найдёте натуральные рыбные и мясные снеки, которые отлично подойдут к пиву, в дорогу, на подарок или просто для удовольствия.\n\n"
    "⭐ Что у нас есть:\n\n"
    "🐟 Рыбные снеки\n"
    "🥩 Мясные снеки\n"
    "🎁 Подарочные наборы\n"
    "🚚 Доставка по Нидерландам и странам ЕС\n\n"
    "👇 Выберите категорию ниже — и я помогу подобрать самые вкусные варианты.\n\n"
    "💬 Не знаете, что выбрать? Просто напишите мне обычным сообщением.\n\n"
    "Например:\n\n"
    "• «Посоветуй что-нибудь к пиву» 🍻\n"
    "• «Нужен набор на компанию»\n"
    "• «Что сейчас самое вкусное?»\n\n"
    "Я помогу подобрать лучший вариант 😊"
)


DEAL_MARKET_SUPPORT_MESSAGE = (
    "💬 Поддержка Deal Market NL\n\n"
    "Если у вас возник вопрос по заказу, доставке или ассортименту — напишите нам.\n\n"
    "Мы постараемся ответить максимально быстро."
)


EMPTY_CART_MESSAGE = (
    "🛒 Корзина пока пустая\n\n"
    "🍻 Не уходите с пустыми руками!\n\n"
    "В каталоге уже ждут:\n\n"
    "🐟 Рыбные деликатесы\n"
    "🥩 Мясные снеки\n"
    "🔥 Копчёная рыба\n"
    "🎁 Подарочные наборы\n\n"
    "🚚 Доставка по Нидерландам и странам ЕС."
)


def empty_cart_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Открыть каталог", callback_data="back_to_menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_menu")],
    ])


PAYMENT_PLACEHOLDER_VALUES = {
    "nl00bank0000000000",
    "your@email.com",
    "todo",
    "tbd",
    "test",
    "fake",
    "placeholder",
}


def is_valid_live_value(value):
    cleaned = str(value or "").strip()
    if not cleaned:
        return False
    normalized = cleaned.lower()
    if normalized in PAYMENT_PLACEHOLDER_VALUES:
        return False
    return not any(marker in normalized for marker in ("todo", "example", "fake"))


def get_live_config_value(config, key, env_name):
    value = os.getenv(env_name) or config.get(key, "")
    value = str(value or "").strip()
    return value if is_valid_live_value(value) else ""


def get_payment_details(config=None):
    config = config or load_json("config.json")
    return {
        "iban": get_live_config_value(config, "iban", "SHOP_IBAN"),
        "receiver_name": get_live_config_value(config, "receiver_name", "SHOP_RECEIVER_NAME"),
        "paypal": get_live_config_value(config, "paypal", "SHOP_PAYPAL"),
    }


def support_link_from_config(config=None):
    config = config or load_json("config.json")
    raw_support = str(config.get("support_username", "")).strip()
    if not raw_support:
        return ""
    if raw_support.startswith(("http://", "https://")):
        return raw_support
    if raw_support.startswith("t.me/"):
        return f"https://{raw_support}"
    return f"https://t.me/{raw_support.lstrip('@')}"


def support_message():
    link = support_link_from_config()
    if not link:
        return DEAL_MARKET_SUPPORT_MESSAGE
    return f"{DEAL_MARKET_SUPPORT_MESSAGE}\n\nНаписать нам: {link}"


def main_menu():
    categories = []

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name
            FROM categories
            WHERE is_active = TRUE
            ORDER BY sort_order, id
        """)
        rows = cursor.fetchall()
        conn.close()

        if rows:
            categories = [
                {"id": row[0], "name": row[1]}
                for row in rows
            ]
    except Exception:
        categories = []

    if not categories:
        categories = load_json("categories.json")

    keyboard = []

    for category in categories:
        keyboard.append([
            InlineKeyboardButton(
                text=DEAL_MARKET_CATEGORY_LABELS.get(category["id"], category["name"]),
                callback_data=f"category_{category['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="🔥 Акции",
            callback_data="promotions"
        )
    ])
    keyboard.append([
        InlineKeyboardButton(
            text="💬 Поддержка",
            callback_data="support"
        )
    ])
    keyboard.append([
    InlineKeyboardButton(
        text="🛒 Корзина",
        callback_data="cart"
    )
])
    
    return InlineKeyboardMarkup(
        inline_keyboard=keyboard
    )


@dp.callback_query(F.data.startswith("category_"))
async def show_category(callback: types.CallbackQuery):

    category_id = int(callback.data.split("_")[1])
    log_customer_event(
        callback.from_user.id,
        "view_category",
        {"category_id": category_id}
    )

    products = get_products()

    category_products = [
        product
        for product in products
        if product["category_id"] == category_id
    ]

    if not category_products:
        await callback.message.answer(
            "📭 В этой категории пока нет товаров."
        )
        await callback.answer()
        return

    keyboard = []

    for product in category_products:
        keyboard.append([
            InlineKeyboardButton(
                text=DEAL_MARKET_PRODUCT_BUTTON_LABELS.get(product["name"], product["name"]),
                callback_data=f"product_{product['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ К категориям",
            callback_data="back_to_menu"
        )
    ])

    await callback.message.answer(
        DEAL_MARKET_CATEGORY_INTROS.get(category_id, "Выберите товар:"),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )

    await callback.answer()


@dp.callback_query(F.data == "promotions")
async def show_promotions(callback: types.CallbackQuery):
    log_customer_event(callback.from_user.id, "view_promotions", {})

    promotion_products = get_promotion_products()

    if not promotion_products:
        await callback.message.answer(
            "🔥 Сейчас активных акций нет.\n\n"
            "Загляните немного позже ❤️"
        )
        await callback.answer()
        return

    cart_product_ids = get_cart_product_ids(callback.from_user.id)
    available_promotion_products = [
        product for product in promotion_products
        if product["id"] not in cart_product_ids
        and not is_product_out_of_stock(product)
    ]

    if not available_promotion_products:
        await callback.message.answer(
            "🔥 Все доступные акционные товары уже добавлены в вашу корзину.\n\n"
            "Загляните сюда позже ❤️"
        )
        await callback.answer()
        return

    keyboard = []

    for product in available_promotion_products:
        keyboard.append([
            InlineKeyboardButton(
                text=DEAL_MARKET_PRODUCT_BUTTON_LABELS.get(product["name"], product["name"]),
                callback_data=f"product_{product['id']}"
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="back_to_menu"
        )
    ])

    await callback.message.answer(
        "🔥 Акции\n\n"
        "Именно сейчас рекомендуем обратить внимание:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=keyboard
        )
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("product_"))
async def show_product(callback: types.CallbackQuery):

    product_id = int(callback.data.split("_")[1])

    products = get_products()

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer(
            "❌ Товар не найден."
        )
        await callback.answer()
        return
    log_customer_event(
        callback.from_user.id,
        "view_product",
        {"product_id": product_id}
    )

    price_100g = product["price_per_kg"] / 10
    product_icon = DEAL_MARKET_PRODUCT_CARD_ICONS.get(
        product.get("category_id"),
        "🛒"
    )

    text = (
        f"{product_icon} {product['name']}\n\n"
        f"{product['description']}\n\n"
        f"💰 Цена: {product['price_per_kg']} €/кг\n"
        f"⚖️ 100 г: {price_100g:.2f} €\n\n"
        f"👇 Выберите вес:"
    )

    if is_product_out_of_stock(product):
        alternatives = get_alternative_products(product["category_id"], product_id)
        text = f"{text}\n\n{OUT_OF_STOCK_TEXT}"
        if alternatives:
            text = f"{text}\n\n🔎 Похожее в наличии:"
        keyboard = out_of_stock_keyboard(product["category_id"], alternatives)
        photo = product.get("photo") or product.get("image_url")
        if photo:
            try:
                await callback.message.answer_photo(
                    photo=photo,
                    caption=text,
                    reply_markup=keyboard
                )
            except Exception:
                await callback.message.answer(text, reply_markup=keyboard)
        else:
            await callback.message.answer(text, reply_markup=keyboard)
        await callback.answer()
        return

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, label, weight, price
            FROM product_options
            WHERE product_id = %s
              AND is_active = TRUE
            ORDER BY sort_order, id
            """,
            (product_id,)
        )
        options = cursor.fetchall()
        conn.close()
    except Exception:
        options = []

    is_variable_weight = any(row[2] is None for row in options)

    if is_variable_weight:
        text = (
            f"{product_icon} {product['name']}\n\n"
            f"{product['description']}\n\n"
            f"💰 Цена: {product['price_per_kg']} €/кг\n\n"
            f"👇 Выберите размер:\n\n"
            f"⚖️ Вес будет уточнён после сборки заказа.\n"
            f"Администратор взвесит товар и отправит финальную сумму к оплате."
        )

    if options:
        option_rows = []
        option_row = []
        for option_id, label, weight, option_price in options:
            option_row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"option_{option_id}"
                )
            )
            if len(option_row) == 2:
                option_rows.append(option_row)
                option_row = []
        if option_row:
            option_rows.append(option_row)
        option_rows.append([
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"category_{product['category_id']}"
            )
        ])
        keyboard = InlineKeyboardMarkup(inline_keyboard=option_rows)
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="50 г",
                        callback_data=f"weight_{product_id}_50"
                    ),
                    InlineKeyboardButton(
                        text="100 г",
                        callback_data=f"weight_{product_id}_100"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="200 г",
                        callback_data=f"weight_{product_id}_200"
                    ),
                    InlineKeyboardButton(
                        text="500 г",
                        callback_data=f"weight_{product_id}_500"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="⬅️ Назад",
                        callback_data=f"category_{product['category_id']}"
                    )
                ]
            ]
        )

    photo = product.get("photo") or product.get("image_url")

    if photo:
        try:
            await callback.message.answer_photo(
                photo=photo,
                caption=text,
                reply_markup=keyboard
            )
        except Exception:
            await callback.message.answer(
                text,
                reply_markup=keyboard
            )
    else:
        await callback.message.answer(
            text,
            reply_markup=keyboard
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("option_"))
async def choose_option(callback: types.CallbackQuery):
    option_id = int(callback.data.split("_")[1])

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT product_id, label, weight, price
        FROM product_options
        WHERE id = %s
          AND is_active = TRUE
        """,
        (option_id,)
    )
    option = cursor.fetchone()
    conn.close()

    if not option:
        await callback.message.answer("❌ Вариант не найден.")
        await callback.answer()
        return

    product_id, label, weight, price = option

    products = get_products()
    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer(
            "❌ Товар не найден."
        )
        await callback.answer()
        return

    if is_product_out_of_stock(product):
        await callback.message.answer(
            OUT_OF_STOCK_TEXT,
            reply_markup=out_of_stock_keyboard(product.get("category_id"))
        )
        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Добавить в корзину",
                    callback_data=f"cart_add_option_{option_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"product_{product_id}"
                )
            ]
        ]
    )

    if weight is None:
        preview_text = (
            f"🧮 Расчёт заказа\n\n"
            f"Товар: {product['name']}\n"
            f"Предварительный размер: {label}\n\n"
            f"💶 Цена будет рассчитана после взвешивания.\n"
            f"⚖️ Вес будет определён администратором после сборки заказа."
        )
    else:
        preview_text = (
            f"🧮 Расчёт заказа\n\n"
            f"Товар: {product['name']}\n"
            f"Вариант: {label}\n"
            f"Сумма: {float(price):.2f} €"
        )

    await callback.message.answer(
        preview_text,
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("weight_"))
async def choose_weight(callback: types.CallbackQuery):

    _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    products = get_products()

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer(
            "❌ Товар не найден."
        )
        await callback.answer()
        return

    price = product["price_per_kg"] * weight / 1000

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
    text="🛒 Добавить в корзину",
    callback_data=f"cart_add_{product_id}_{weight}"
)
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"product_{product_id}"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"🧮 Расчёт заказа\n\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €",
        reply_markup=keyboard
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("cart_add_option_"))
async def add_option_to_cart(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    option_id = int(callback.data.split("_")[3])

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT po.product_id, po.weight, po.price, po.label, p.category_id, p.stock_grams, p.is_out_of_stock
        FROM product_options po
        JOIN products p ON p.id = po.product_id
        WHERE po.id = %s
          AND po.is_active = TRUE
          AND p.is_active = TRUE
        """,
        (option_id,)
    )
    option = cursor.fetchone()

    if not option:
        conn.close()
        await callback.message.answer("❌ Вариант не найден.")
        await callback.answer()
        return

    product_id, weight, price, label, category_id, stock_grams, is_out_of_stock = option
    if is_product_out_of_stock({
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock
    }):
        conn.close()
        await callback.message.answer(
            OUT_OF_STOCK_TEXT,
            reply_markup=out_of_stock_keyboard(category_id)
        )
        await callback.answer()
        return

    cursor.execute(
        "SELECT COUNT(*) FROM cart_items WHERE telegram_id = %s AND option_id = %s",
        (callback.from_user.id, option_id)
    )
    quantity = cursor.fetchone()[0]
    if quantity >= MAX_CART_ITEM_QUANTITY:
        conn.close()
        await callback.answer("Максимум 10 шт одного варианта", show_alert=True)
        return

    cursor.execute("""
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight,
            option_id
        )
        VALUES (%s, %s, %s, %s)
    """, (
        callback.from_user.id,
        product_id,
        weight,
        option_id
    ))

    conn.commit()
    conn.close()
    log_customer_event(
        callback.from_user.id,
        "add_to_cart",
        {"product_id": product_id, "option_id": option_id}
    )
    mark_cart_active(callback.from_user.id)

    # The product just added is already committed to cart_items above, so it is
    # already a member of cart_product_ids and gets excluded automatically below —
    # no separate "exclude the product just added" check is needed.
    cart_product_ids = get_cart_product_ids(callback.from_user.id)
    recommended_products = get_recommended_products(product_id, cart_product_ids)
    automatic_recommendations = []
    if not recommended_products:
        automatic_recommendations = get_automatic_recommendations(product_id, cart_product_ids)
    has_promotions = has_available_promotions(cart_product_ids)

    display_recommendations = recommended_products or automatic_recommendations

    keyboard_rows = []
    for recommended_product in display_recommendations:
        keyboard_rows.append([
            InlineKeyboardButton(
                text=recommended_product["name"],
                callback_data=f"product_{recommended_product['id']}"
            )
        ])
    if has_promotions:
        keyboard_rows.append([
            InlineKeyboardButton(
                text="🔥 Посмотреть акции",
                callback_data="promotions"
            )
        ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="back_to_menu"
        )
    ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text="🛒 Корзина",
            callback_data="cart"
        )
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    success_text = (
        "✅ Отличный выбор!\n\n"
        "🛒 Товар успешно добавлен в корзину.\n\n"
        "🍺 Соберите свой идеальный набор или переходите к оформлению заказа.\n\n"
        "👇 Выберите следующее действие:"
    )
    if recommended_products:
        success_text += f"\n\n{random.choice(UPSELL_INTRO_PHRASES)}"
    elif automatic_recommendations:
        success_text += f"\n\n{random.choice(AUTO_RECOMMENDATION_PHRASES)}"
    elif has_promotions:
        success_text += "\n\n🔥 Для вас сейчас есть выгодные предложения:"

    await callback.message.answer(
        success_text,
        reply_markup=keyboard
    )

    await callback.answer()


def is_legacy_cart_add(callback: types.CallbackQuery):
    parts = callback.data.split("_") if callback.data else []
    return (
        len(parts) == 4
        and parts[0] == "cart"
        and parts[1] == "add"
        and parts[2].isdigit()
        and parts[3].isdigit()
    )


@dp.callback_query(is_legacy_cart_add)
async def add_to_cart(callback: types.CallbackQuery):
    _, _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT category_id, stock_grams, is_out_of_stock
        FROM products
        WHERE id = %s
          AND is_active = TRUE
        """,
        (product_id,)
    )
    product_row = cursor.fetchone()
    if not product_row:
        conn.close()
        await callback.message.answer("❌ Товар не найден.")
        await callback.answer()
        return

    category_id, stock_grams, is_out_of_stock = product_row
    if is_product_out_of_stock({
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock
    }):
        conn.close()
        await callback.message.answer(
            OUT_OF_STOCK_TEXT,
            reply_markup=out_of_stock_keyboard(category_id)
        )
        await callback.answer()
        return

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM cart_items
        WHERE telegram_id = %s
          AND product_id = %s
          AND weight = %s
          AND option_id IS NULL
        """,
        (callback.from_user.id, product_id, weight)
    )
    quantity = cursor.fetchone()[0]
    if quantity >= MAX_CART_ITEM_QUANTITY:
        conn.close()
        await callback.answer("Максимум 10 шт одного варианта", show_alert=True)
        return

    cursor.execute("""
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight
        )
        VALUES (%s, %s, %s)
    """, (
        callback.from_user.id,
        product_id,
        weight
    ))

    conn.commit()
    conn.close()
    log_customer_event(
        callback.from_user.id,
        "add_to_cart",
        {"product_id": product_id, "weight": weight}
    )
    mark_cart_active(callback.from_user.id)

    # The product just added is already committed to cart_items above, so it is
    # already a member of cart_product_ids and gets excluded automatically below —
    # no separate "exclude the product just added" check is needed.
    cart_product_ids = get_cart_product_ids(callback.from_user.id)
    recommended_products = get_recommended_products(product_id, cart_product_ids)
    automatic_recommendations = []
    if not recommended_products:
        automatic_recommendations = get_automatic_recommendations(product_id, cart_product_ids)
    has_promotions = has_available_promotions(cart_product_ids)

    display_recommendations = recommended_products or automatic_recommendations

    keyboard_rows = []
    for recommended_product in display_recommendations:
        keyboard_rows.append([
            InlineKeyboardButton(
                text=recommended_product["name"],
                callback_data=f"product_{recommended_product['id']}"
            )
        ])
    if has_promotions:
        keyboard_rows.append([
            InlineKeyboardButton(
                text="🔥 Посмотреть акции",
                callback_data="promotions"
            )
        ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="back_to_menu"
        )
    ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text="🛒 Корзина",
            callback_data="cart"
        )
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    success_text = (
        "✅ Отличный выбор!\n\n"
        "🛒 Товар успешно добавлен в корзину.\n\n"
        "🍺 Соберите свой идеальный набор или переходите к оформлению заказа.\n\n"
        "👇 Выберите следующее действие:"
    )
    if recommended_products:
        success_text += f"\n\n{random.choice(UPSELL_INTRO_PHRASES)}"
    elif automatic_recommendations:
        success_text += f"\n\n{random.choice(AUTO_RECOMMENDATION_PHRASES)}"
    elif has_promotions:
        success_text += "\n\n🔥 Для вас сейчас есть выгодные предложения:"

    await callback.message.answer(
        success_text,
        reply_markup=keyboard
    )

    await callback.answer()

@dp.callback_query(F.data.startswith("order_"))
async def create_order(callback: types.CallbackQuery):

    _, product_id, weight = callback.data.split("_")

    product_id = int(product_id)
    weight = int(weight)

    products = get_products()
    config = load_json("config.json")

    product = next(
        (p for p in products if p["id"] == product_id),
        None
    )

    if not product:
        await callback.message.answer("❌ Товар не найден.")
        await callback.answer()
        return

    price = product["price_per_kg"] * weight / 1000
    order_id = callback.from_user.id + int(asyncio.get_event_loop().time())

    username = callback.from_user.username
    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    order_text = (
        f"🧾 Заказ #{order_id}\n\n"
        f"Товар: {product['name']}\n"
        f"Вес: {weight} г\n"
        f"Сумма: {price:.2f} €\n\n"
        f"Покупатель: {user_text}"
    )

    await callback.message.answer(
        f"✅ Заказ успешно оформлен!\n\n"
        f"📦 Номер заказа: #{order_id}\n\n"
        f"Спасибо за ваш заказ ❤️\n\n"
        f"Мы получили заявку и приступим к обработке в ближайшее время.\n\n"
        f"🍺 Deal Market NL"
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=f"📦 Новый заказ!\n\n{order_text}"
    )

    await callback.answer()
@dp.callback_query(F.data == "support")
async def support(callback: types.CallbackQuery):

    await callback.message.answer(
        support_message()
    )

    await callback.answer()
@dp.callback_query(F.data == "cart")
async def show_cart(callback: types.CallbackQuery):

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            MIN(ci.id),
            ci.product_id,
            ci.weight,
            ci.option_id,
            po.label,
            po.price,
            COUNT(*)
        FROM cart_items ci
        JOIN products p ON p.id = ci.product_id
        LEFT JOIN product_options po ON po.id = ci.option_id
        WHERE ci.telegram_id = %s
        GROUP BY ci.product_id, ci.option_id, ci.weight, po.label, po.price, p.category_id, p.sort_order, p.id, po.sort_order
        ORDER BY p.category_id ASC, p.sort_order ASC, p.id ASC, COALESCE(po.sort_order, ci.weight) ASC, ci.weight ASC
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()
    conn.close()
    log_customer_event(callback.from_user.id, "open_cart", {})

    if not cart_items:
        await callback.message.answer(
            EMPTY_CART_MESSAGE,
            reply_markup=empty_cart_keyboard()
        )
        await callback.answer()
        return

    products = get_products()

    text = "🛒 Ваша корзина:\n\n"
    total = 0
    has_pending_weighing = False
    remove_buttons = []

    for cart_item_id, product_id, weight, option_id, option_label, option_price, quantity in cart_items:

        product = next(
            (p for p in products if p["id"] == product_id),
            None
        )

        if not product:
            remove_buttons.append([
                InlineKeyboardButton(
                    text="❌ Удалить товар",
                    callback_data=f"remove_item_{cart_item_id}"
                )
            ])
            continue

        if option_id and option_label is not None and option_price is not None:
            unit_price = float(option_price)
            price = unit_price * quantity
            item_detail = f"  Вариант: {option_label}\n"
            remove_label = option_label
        else:
            if option_id and option_price is not None:
                unit_price = float(option_price)
                price = unit_price * quantity
                item_detail = f"  Вариант: {option_label}\n"
                remove_label = option_label if option_label else f"{weight} г"
            else:
                unit_price = product["price_per_kg"] * weight / 1000
                price = unit_price * quantity
                item_detail = f"  Вес: {weight} г\n"
                remove_label = f"{weight} г"
            item_detail = f"  Вес: {weight} г\n"
        total += price
        total_weight = weight * quantity if weight is not None else None
        if total_weight is None:
            total_weight_text = "-"
        elif total_weight >= 1000:
            kg = total_weight / 1000
            total_weight_text = f"{kg:g} кг"
        else:
            total_weight_text = f"{total_weight} г"

        if option_id and option_label is not None and option_price is not None:
            if weight is None:
                has_pending_weighing = True
                item_sum_line = "  Сумма: уточняется после взвешивания\n\n"
            else:
                item_sum_line = f"  Сумма: {price:.2f} €\n\n"
            text += (
                f"• {product['name']}\n"
                f"{item_detail}"
                f"  Количество: {quantity} шт\n"
                f"  Общий вес: {total_weight_text}\n"
                f"{item_sum_line}"
            )
            remove_buttons.append([
                InlineKeyboardButton(
                    text=f"➖ {remove_label}",
                    callback_data=f"remove_item_{cart_item_id}"
                ),
                InlineKeyboardButton(
                    text=f"➕ {remove_label}",
                    callback_data=f"cart_plus_option_{option_id}"
                )
            ])
            continue

        text += (
            f"• {product['name']}\n"
            f"  Вес: {weight} г\n"
            f"  Количество: {quantity} шт\n"
            f"  Общий вес: {total_weight_text}\n"
            f"  Сумма: {price:.2f} €\n\n"
        )
        remove_buttons.append([
            InlineKeyboardButton(
                text=f"➖ {remove_label}",
                callback_data=f"remove_item_{cart_item_id}"
            ),
            InlineKeyboardButton(
                text=f"➕ {remove_label}",
                callback_data=f"cart_plus_weight_{product_id}_{weight}"
            )
        ])

    if has_pending_weighing:
        text += (
            f"💰 Предварительная сумма: {total:.2f} €\n"
            f"⚖️ Финальная сумма будет рассчитана после взвешивания."
        )
    else:
        text += f"💰 Общая сумма: {total:.2f} €"

    keyboard = InlineKeyboardMarkup(
    inline_keyboard=remove_buttons + [
        [
            InlineKeyboardButton(
                text="✅ Оформить заказ",
                callback_data="checkout"
            )
        ],
        [
            InlineKeyboardButton(
                text="🗑 Очистить корзину",
                callback_data="clear_cart"
            )
        ],
        [
    InlineKeyboardButton(
        text="🏠 Главное меню",
        callback_data="back_to_menu"
    )
]
    ]
)

    await callback.message.answer(
    text,
    reply_markup=keyboard
)

    await callback.answer()


@dp.callback_query(F.data.startswith("cart_plus_option_"))
async def cart_plus_option(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    option_id = int(callback.data.split("_")[3])

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM cart_items WHERE telegram_id = %s AND option_id = %s",
        (callback.from_user.id, option_id)
    )
    quantity = cursor.fetchone()[0]
    if quantity >= MAX_CART_ITEM_QUANTITY:
        conn.close()
        await callback.answer("Максимум 10 шт одного варианта", show_alert=True)
        return

    cursor.execute(
        """
        SELECT po.product_id, po.weight, p.category_id, p.stock_grams, p.is_out_of_stock
        FROM product_options po
        JOIN products p ON p.id = po.product_id
        WHERE po.id = %s
          AND po.is_active = TRUE
          AND p.is_active = TRUE
        """,
        (option_id,)
    )
    option = cursor.fetchone()
    if not option:
        conn.close()
        await callback.answer("Вариант не найден", show_alert=True)
        return

    product_id, weight, category_id, stock_grams, is_out_of_stock = option
    if is_product_out_of_stock({
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock
    }):
        conn.close()
        await callback.message.answer(
            OUT_OF_STOCK_TEXT,
            reply_markup=out_of_stock_keyboard(category_id)
        )
        await callback.answer()
        return

    cursor.execute(
        """
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight,
            option_id
        )
        VALUES (%s, %s, %s, %s)
        """,
        (callback.from_user.id, product_id, weight, option_id)
    )
    conn.commit()
    conn.close()
    mark_cart_active(callback.from_user.id)
    await show_cart(callback)


@dp.callback_query(F.data.startswith("cart_plus_weight_"))
async def cart_plus_weight(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    _, _, _, product_id, weight = callback.data.split("_")
    product_id = int(product_id)
    weight = int(weight)

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT category_id, stock_grams, is_out_of_stock
        FROM products
        WHERE id = %s
          AND is_active = TRUE
        """,
        (product_id,)
    )
    product_row = cursor.fetchone()
    if not product_row:
        conn.close()
        await callback.answer("Товар не найден", show_alert=True)
        return

    category_id, stock_grams, is_out_of_stock = product_row
    if is_product_out_of_stock({
        "stock_grams": stock_grams,
        "is_out_of_stock": is_out_of_stock
    }):
        conn.close()
        await callback.message.answer(
            OUT_OF_STOCK_TEXT,
            reply_markup=out_of_stock_keyboard(category_id)
        )
        await callback.answer()
        return

    cursor.execute(
        """
        SELECT COUNT(*)
        FROM cart_items
        WHERE telegram_id = %s
          AND product_id = %s
          AND weight = %s
          AND option_id IS NULL
        """,
        (callback.from_user.id, product_id, weight)
    )
    quantity = cursor.fetchone()[0]
    if quantity >= MAX_CART_ITEM_QUANTITY:
        conn.close()
        await callback.answer("Максимум 10 шт одного варианта", show_alert=True)
        return

    cursor.execute(
        """
        INSERT INTO cart_items (
            telegram_id,
            product_id,
            weight
        )
        VALUES (%s, %s, %s)
        """,
        (callback.from_user.id, product_id, weight)
    )
    conn.commit()
    conn.close()
    mark_cart_active(callback.from_user.id)
    await show_cart(callback)


@dp.callback_query(F.data.startswith("remove_item_"))
async def remove_item(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    cart_item_id = int(callback.data.split("_")[2])

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM cart_items WHERE id = %s AND telegram_id = %s",
        (cart_item_id, callback.from_user.id)
    )
    cursor.execute(
        "SELECT COUNT(*) FROM cart_items WHERE telegram_id = %s",
        (callback.from_user.id,)
    )
    remaining_items = cursor.fetchone()[0]

    conn.commit()
    conn.close()
    if remaining_items:
        mark_cart_active(callback.from_user.id)

    await show_cart(callback)

@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        "DELETE FROM cart_items WHERE telegram_id = %s",
        (callback.from_user.id,)
    )

    conn.commit()
    conn.close()

    await callback.message.answer(
        "🗑 Корзина очищена.",
        reply_markup=main_menu()
    )

    await callback.answer()


@dp.message(Command("start"))
async def start(message: types.Message):
    # Reset checkout state if user is in pending orders
    user_id = message.from_user.id
    if user_id in pending_orders:
        del pending_orders[user_id]
    
    save_client(message.from_user)
    log_customer_event(message.from_user.id, "start", {})
    
    await message.answer(
        DEAL_MARKET_WELCOME_MESSAGE,
        reply_markup=main_menu()
    )


@dp.message(Command("orders"))
async def show_orders(message: types.Message):
    config = load_json("config.json")

    if message.from_user.id != config["admin_id"]:
        await message.answer("⛔️ У вас нет доступа.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT order_id, telegram_id, username, phone, address, total, status, payment_method
        FROM orders
        ORDER BY id DESC
        LIMIT 20
    """)

    orders = cursor.fetchall()
    conn.close()

    if not orders:
        await message.answer("📭 Заказов пока нет.")
        return

    text = "📦 Последние заказы:\n\n"

    for order_id, telegram_id, username, phone, address, total, status, payment_method in orders:

        text += (
            f"🧾 Заказ #{order_id}\n"
            f"👤 Клиент: @{username if username else 'без username'}\n"
            f"🆔 ID: {telegram_id}\n"
            f"📞 Телефон: {phone}\n"
            f"🏠 Адрес: {address}\n"
            f"💰 Сумма: {total:.2f} €\n"
            f"📌 Статус: {status}\n"
            f"💳 Оплата: {payment_method if payment_method else 'не выбрана'}\n\n"
        )

    await message.answer(text)


@dp.message(Command("clients"))
async def show_clients(message: types.Message):
    config = load_json("config.json")

    if message.from_user.id != config["admin_id"]:
        await message.answer("⛔️ У вас нет доступа.")
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT 
            c.telegram_id,
            c.username,
            c.first_name,
            c.phone,
            c.address,
            COUNT(o.id) as order_count,
            COALESCE(SUM(o.total), 0) as total_spent,
            MAX(o.id) as last_order_id,
            (SELECT status FROM orders WHERE telegram_id = c.telegram_id ORDER BY id DESC LIMIT 1) as last_order_status
        FROM clients c
        LEFT JOIN orders o ON c.telegram_id = o.telegram_id
        GROUP BY c.id, c.telegram_id, c.username, c.first_name, c.phone, c.address
        ORDER BY c.id DESC
        LIMIT 20
    """)

    clients = cursor.fetchall()
    conn.close()

    if not clients:
        await message.answer("📭 Клиентов пока нет.")
        return

    text = "👥 База клиентов:\n\n"

    for telegram_id, username, first_name, phone, address, order_count, total_spent, last_order_id, last_order_status in clients:
        user_display = f"@{username}" if username else "без username"

        text += (
            f"👤 {user_display}\n"
            f"📝 Имя: {first_name if first_name else 'не указано'}\n"
        )
        
        if phone:
            text += f"📞 Телефон: {phone}\n"
        if address:
            text += f"🏠 Адрес: {address}\n"
        
        text += f"🆔 ID: {telegram_id}\n"
        text += f"📦 Заказов: {order_count}\n"
        
        if order_count > 0:
            text += f"💰 Потрачено: {total_spent:.2f} €\n"
            if last_order_status:
                text += f"📌 Последний заказ: #{last_order_id} ({last_order_status})\n"
        else:
            text += "📦 Заказов пока нет\n"
        
        text += "\n"

    await message.answer(text)


def price_cart_items(cart_items, products):
    total = 0
    priced_items = []

    for product_id, weight, option_id, option_label, option_price in cart_items:
        product = next((p for p in products if p["id"] == product_id), None)
        if not product:
            continue

        if option_id and option_price is not None:
            price = float(option_price)
            item_detail = f"  Вариант: {option_label}\n"
        else:
            price = product["price_per_kg"] * weight / 1000
            item_detail = f"  Вес: {weight} г\n"

        total += price
        priced_items.append({
            "product": product,
            "product_id": product_id,
            "weight": weight,
            "option_id": option_id,
            "price": price,
            "item_detail": item_detail
        })

    return total, priced_items


def create_order_from_cart(
    user_id,
    username,
    phone,
    address,
    cart_items,
    products,
    first_name=None,
    save_contact=False
):
    total, priced_items = price_cart_items(cart_items, products)

    order_id = user_id + int(asyncio.get_event_loop().time())

    user_text = f"@{username}" if username else f"ID: {user_id}"

    order_text = f"🧾 Заказ #{order_id}\n\n"
    for item in priced_items:
        order_text += (
            f"• {item['product']['name']}\n"
            f"{item['item_detail']}"
            f"  Сумма: {item['price']:.2f} €\n\n"
        )
    order_text += (
        f"💰 Итого: {total:.2f} €\n\n"
        f"👤 Покупатель: {user_text}\n"
        f"📞 Телефон: {phone}\n"
        f"🏠 Адрес: {address}"
    )

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    if save_contact:
        cursor.execute("""
            INSERT INTO clients (
                telegram_id,
                username,
                first_name,
                phone,
                address
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                phone = EXCLUDED.phone,
                address = EXCLUDED.address
        """, (
            user_id,
            username,
            first_name,
            phone,
            address
        ))

    cursor.execute("""
        INSERT INTO orders (
            order_id,
            telegram_id,
            username,
            phone,
            address,
            total,
            status,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
    """, (
        order_id,
        user_id,
        username,
        phone,
        address,
        total,
        "pending"
    ))

    for item in priced_items:
        cursor.execute(
            "INSERT INTO order_items (order_id, product_id, product_name, weight, price, option_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (
                order_id,
                item["product_id"],
                item["product"]["name"],
                item["weight"],
                item["price"],
                item["option_id"]
            )
        )

    cursor.execute(
        "DELETE FROM cart_items WHERE telegram_id = %s",
        (user_id,)
    )
    conn.commit()
    conn.close()

    log_customer_event(user_id, "order_created", {"order_id": order_id})

    return order_id, order_text, total


def order_needs_weighing(order_id):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM order_items WHERE order_id = %s AND weight IS NULL LIMIT 1",
        (order_id,)
    )
    needs_weighing = cursor.fetchone() is not None
    conn.close()
    return needs_weighing


@dp.message()
async def handle_order_data(message: types.Message):
    user_id = message.from_user.id

    if user_id not in pending_orders:
        return

    # Don't save commands as phone or address
    if message.text and message.text.startswith("/"):
        return

    step = pending_orders[user_id]["step"]

    if step == "phone":
        pending_orders[user_id]["phone"] = message.text
        pending_orders[user_id]["step"] = "address"

        await message.answer(
            "🏠 Теперь введите адрес доставки:"
        )
        return

    if step == "address":
        pending_orders[user_id]["address"] = message.text

        cart_items = pending_orders[user_id]["cart_items"]
        phone = pending_orders[user_id]["phone"]
        address = pending_orders[user_id]["address"]

        products = get_products()
        config = load_json("config.json")

        order_id, order_text, _ = create_order_from_cart(
            user_id=user_id,
            username=message.from_user.username,
            phone=phone,
            address=address,
            cart_items=cart_items,
            products=products,
            first_name=message.from_user.first_name,
            save_contact=True
        )

        # Send confirmation to customer
        if order_needs_weighing(order_id):
            await message.answer(
                "⚖️ Заказ принят. Администратор взвесит товар и отправит финальную сумму к оплате."
            )
        else:
            await message.answer(
                f"✅ Заказ успешно оформлен!\n\n"
                f"📦 Номер заказа: #{order_id}\n\n"
                f"Спасибо за ваш заказ ❤️\n\n"
                f"Мы получили заявку и приступим к обработке в ближайшее время.\n\n"
                f"🍺 Deal Market NL",
                reply_markup=payment_menu()
            )

        # Send notification to admin
        await bot.send_message(
            chat_id=config["admin_id"],
            text=f"📦 Новый заказ!\n\n{order_text}"
        )

        # Clear pending order state
        del pending_orders[user_id]



@dp.callback_query(F.data == "checkout")
async def checkout(callback: types.CallbackQuery):
    if is_callback_locked(callback):
        await callback.answer("Подождите секунду", show_alert=False)
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            ci.product_id,
            ci.weight,
            ci.option_id,
            po.label,
            po.price
        FROM cart_items ci
        LEFT JOIN product_options po ON po.id = ci.option_id
        WHERE ci.telegram_id = %s
    """, (callback.from_user.id,))

    cart_items = cursor.fetchall()

    cursor.execute(
        "SELECT phone, address FROM clients WHERE telegram_id = %s",
        (callback.from_user.id,)
    )
    client_contact = cursor.fetchone()
    conn.close()

    if not cart_items:
        await callback.message.answer(
            EMPTY_CART_MESSAGE,
            reply_markup=empty_cart_keyboard()
        )
        await callback.answer()
        return

    products = get_products()
    config = load_json("config.json")
    minimum_order_amount = config.get("minimum_order_amount", 20)

    total, _ = price_cart_items(cart_items, products)
    has_pending_weighing = any(row[1] is None for row in cart_items)

    if not has_pending_weighing and total < minimum_order_amount:
        missing = minimum_order_amount - total

        cart_product_ids = {row[0] for row in cart_items}
        has_promotions = has_available_promotions(cart_product_ids)

        keyboard_rows = [
            [
                InlineKeyboardButton(
                    text="🛍 Вернуться к товарам",
                    callback_data="back_to_menu"
                )
            ]
        ]
        if has_promotions:
            keyboard_rows.append([
                InlineKeyboardButton(
                    text="🔥 Посмотреть акции",
                    callback_data="promotions"
                )
            ])
        keyboard_rows.append([
            InlineKeyboardButton(
                text="🛒 Корзина",
                callback_data="cart"
            )
        ])

        await callback.message.answer(
            f"🚚 Минимальная сумма заказа для доставки: {minimum_order_amount:.2f} €\n\n"
            f"До оформления осталось добавить товаров ещё на {missing:.2f} €.\n\n"
            f"Выберите, что сделать дальше 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        )
        await callback.answer()
        return
    log_customer_event(callback.from_user.id, "checkout_started", {})

    if client_contact and client_contact[0] and client_contact[1]:
        phone, address = client_contact
        pending_orders[callback.from_user.id] = {
            "cart_items": cart_items,
            "step": "use_saved",
            "phone": phone,
            "address": address
        }

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Использовать сохранённые данные",
                        callback_data="use_saved_data"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Ввести новые данные",
                        callback_data="enter_new_data"
                    )
                ]
            ]
        )

        await callback.message.answer(
            f"Мы нашли сохранённые контактные данные:\n📞 {phone}\n🏠 {address}\n\n"
            "Использовать их для оформления заказа?",
            reply_markup=keyboard
        )
        await callback.answer()
        return

    pending_orders[callback.from_user.id] = {
        "cart_items": cart_items,
        "step": "phone"
    }

    await callback.message.answer(
        "📞 Введите ваш номер телефона для связи:"
    )

    await callback.answer()


@dp.callback_query(F.data == "resume_payment")
async def resume_payment(callback: types.CallbackQuery):
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT order_id, total, status
        FROM orders
        WHERE telegram_id = %s
          AND status IN ('pending', 'awaiting_payment')
        ORDER BY id DESC
        LIMIT 1
        """,
        (callback.from_user.id,)
    )
    order_row = cursor.fetchone()
    conn.close()

    if not order_row:
        await callback.message.answer(
            "Активный заказ для оплаты не найден.\n\n"
            "Если нужна помощь, напишите нам.",
            reply_markup=main_menu()
        )
        await callback.answer()
        return

    order_id, total, status = order_row
    status_text = "ожидает выбора оплаты" if status == "pending" else "ожидает оплаты"
    await callback.message.answer(
        f"🧾 Заказ #{order_id} {status_text}.\n\n"
        f"Сумма: {float(total or 0):.2f} €\n\n"
        "Выберите способ оплаты:",
        reply_markup=payment_menu()
    )
    await callback.answer()


@dp.callback_query(F.data == "use_saved_data")
async def use_saved_data(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_orders:
        await callback.answer()
        return

    if pending_orders[user_id].get("step") != "use_saved":
        await callback.answer()
        return

    cart_items = pending_orders[user_id]["cart_items"]
    phone = pending_orders[user_id]["phone"]
    address = pending_orders[user_id]["address"]

    products = get_products()
    config = load_json("config.json")

    order_id, order_text, _ = create_order_from_cart(
        user_id=user_id,
        username=callback.from_user.username,
        phone=phone,
        address=address,
        cart_items=cart_items,
        products=products
    )

    if order_needs_weighing(order_id):
        await callback.message.answer(
            "⚖️ Заказ принят. Администратор взвесит товар и отправит финальную сумму к оплате."
        )
    else:
        await callback.message.answer(
            f"✅ Заказ успешно оформлен!\n\n"
            f"📦 Номер заказа: #{order_id}\n\n"
            f"Спасибо за ваш заказ ❤️\n\n"
            f"Мы получили заявку и приступим к обработке в ближайшее время.\n\n"
            f"🍺 Deal Market NL",
            reply_markup=payment_menu()
        )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=f"📦 Новый заказ!\n\n{order_text}"
    )

    del pending_orders[user_id]
    await callback.answer()


@dp.callback_query(F.data == "enter_new_data")
async def enter_new_data(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in pending_orders:
        await callback.answer()
        return

    pending_orders[user_id]["step"] = "phone"

    await callback.message.answer(
        "📞 Введите ваш номер телефона для связи:"
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_iban")
async def pay_iban(callback: types.CallbackQuery):
    config = load_json("config.json")
    payment_details = get_payment_details(config)
    iban = payment_details["iban"]
    receiver_name = payment_details["receiver_name"]
    if not iban or not receiver_name:
        await callback.message.answer(
            "🏦 Оплата IBAN сейчас недоступна.\n\n"
            "Выберите другой способ оплаты или напишите в поддержку.",
            reply_markup=payment_menu()
        )
        await callback.answer()
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s,
            updated_at = NOW(),
            payment_selected_at = NOW()
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        RETURNING order_id
        """,
        ("IBAN", "awaiting_payment", callback.from_user.id)
    )
    order_row = cursor.fetchone()

    conn.commit()
    conn.close()
    if not order_row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    order_id = order_row[0]
    log_customer_event(
        callback.from_user.id,
        "payment_method_selected",
        {"order_id": order_id, "payment_method": "IBAN"}
    )

    paid_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    callback_data=f"payment_done_{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"🏦 Оплата банковским переводом / IBAN\n\n"
        f"IBAN: {iban}\n"
        f"Получатель: {receiver_name}\n\n"
        f"В назначении платежа обязательно укажите номер заказа: #{order_id}.\n"
        f"После оплаты нажмите «Я оплатил», чтобы мы проверили платёж.",
        reply_markup=paid_keyboard
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_paypal")
async def pay_paypal(callback: types.CallbackQuery):
    config = load_json("config.json")
    payment_details = get_payment_details(config)
    paypal = payment_details["paypal"]
    if not paypal:
        await callback.message.answer(
            "🅿️ Оплата PayPal сейчас недоступна.\n\n"
            "Выберите другой способ оплаты или напишите в поддержку.",
            reply_markup=payment_menu()
        )
        await callback.answer()
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s,
            updated_at = NOW(),
            payment_selected_at = NOW()
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        RETURNING order_id
        """,
        ("PayPal", "awaiting_payment", callback.from_user.id)
    )
    order_row = cursor.fetchone()

    conn.commit()
    conn.close()
    if not order_row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    order_id = order_row[0]
    log_customer_event(
        callback.from_user.id,
        "payment_method_selected",
        {"order_id": order_id, "payment_method": "PayPal"}
    )

    paid_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Я оплатил",
                    callback_data=f"payment_done_{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_menu"
                )
            ]
        ]
    )

    await callback.message.answer(
        f"💬 Оплата через PayPal\n\n"
        f"PayPal: {paypal}\n\n"
        f"В комментарии к платежу обязательно укажите номер заказа: #{order_id}.\n"
        f"После оплаты нажмите «Я оплатил», чтобы мы проверили платёж.",
        reply_markup=paid_keyboard
    )

    await callback.answer()


@dp.callback_query(F.data == "pay_cash")
async def pay_cash(callback: types.CallbackQuery):
    config = load_json("config.json")
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET payment_method = %s,
            status = %s,
            updated_at = NOW()
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        RETURNING order_id
        """,
        ("Cash", "cash_on_delivery", callback.from_user.id)
    )
    order_row = cursor.fetchone()

    conn.commit()
    conn.close()
    if not order_row:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    if order_row:
        log_customer_event(
            callback.from_user.id,
            "payment_method_selected",
            {"order_id": order_row[0], "payment_method": "Cash"}
        )

    username = callback.from_user.username

    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await callback.message.answer(
        "💵 Оплата наличными при получении\n\n"
        "✅ Ваш заказ принят в обработку.\n\n"
        "🔖 Сохраните номер заказа.\n\n"
        "Если у вас возникнут вопросы по заказу или доставке, напишите нашему администратору:\n\n"
        "👉 @Deal_Market_NL\n\n"
        "Пожалуйста, укажите номер заказа в сообщении.\n\n"
        "🚚 Оплата производится при получении заказа.\n\n"
        "Спасибо, что выбрали Deal Market NL! ❤️"
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💵 Клиент выбрал оплату наличными.\n\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.answer()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: types.CallbackQuery):
    await callback.message.answer(
        "Выберите категорию ниже:",
        reply_markup=main_menu()
    )

    await callback.answer()

def payment_menu():
    payment_details = get_payment_details()
    keyboard = []
    if payment_details["iban"] and payment_details["receiver_name"]:
        keyboard.append([
            InlineKeyboardButton(
                text="🏦 Оплата IBAN",
                callback_data="pay_iban"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(
            text="💵 Оплата наличными",
            callback_data="pay_cash"
        )
    ])
    if payment_details["paypal"]:
        keyboard.append([
            InlineKeyboardButton(
                text="🅿️ PayPal",
                callback_data="pay_paypal"
            )
        ])
    keyboard.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="back_to_menu"
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)
@dp.callback_query(F.data.startswith("payment_done_"))
async def payment_done_for_order(callback: types.CallbackQuery):
    raw_order_id = callback.data.replace("payment_done_", "", 1)
    try:
        order_id = int(raw_order_id)
    except (TypeError, ValueError):
        await callback.answer("Не удалось определить заказ.", show_alert=True)
        return

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET status = %s,
            updated_at = NOW(),
            payment_reported_at = NOW()
        WHERE order_id = %s
          AND telegram_id = %s
          AND status = 'awaiting_payment'
        """,
        ("payment_reported", order_id, callback.from_user.id)
    )

    if cursor.rowcount == 0:
        conn.rollback()
        conn.close()
        await callback.answer(
            "Не удалось отметить оплату. Возможно, заказ уже подтверждён, отменён или ожидает другого действия.",
            show_alert=True
        )
        return

    conn.commit()
    conn.close()
    log_customer_event(
        callback.from_user.id,
        "payment_reported",
        {"order_id": order_id}
    )

    config = load_json("config.json")

    username = callback.from_user.username
    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await callback.message.answer(
        "Спасибо! Мы получили отметку об оплате. Проверим платёж и подтвердим заказ в ближайшее время."
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💸 Клиент сообщил об оплате.\n\n"
            f"Заказ: {order_id}\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.answer()


@dp.callback_query(F.data == "payment_done")
async def payment_done(callback: types.CallbackQuery):

    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE orders
        SET status = %s,
            updated_at = NOW(),
            payment_reported_at = NOW()
        WHERE id = (
            SELECT id
            FROM orders
            WHERE telegram_id = %s
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        ("payment_reported", callback.from_user.id)
    )

    conn.commit()
    conn.close()

    config = load_json("config.json")

    username = callback.from_user.username

    if username:
        user_text = f"@{username}"
    else:
        user_text = f"ID: {callback.from_user.id}"

    await callback.message.answer(
        "✅ Информация об оплате отправлена продавцу.\n\n"
        "Ожидайте подтверждения заказа."
    )

    await bot.send_message(
        chat_id=config["admin_id"],
        text=(
            "💸 Клиент сообщил об оплате.\n\n"
            f"Покупатель: {user_text}"
        )
    )

    await callback.answer()


async def main():
    init_db()
    seed_products_from_json()
    asyncio.create_task(abandoned_cart_worker())
    asyncio.create_task(unpaid_order_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
