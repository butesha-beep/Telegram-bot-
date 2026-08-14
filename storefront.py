import html
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from shop_settings import BRAND_NAME, CURRENCY_SYMBOL, get_support_username


router = APIRouter()
PRICING_MODES = {"fixed", "per_kg", "options"}
logger = logging.getLogger(__name__)


PAGE_STYLE = """
<style>
  :root {
    color-scheme: light;
    --page: #f5f7f4;
    --panel: #ffffff;
    --text: #202522;
    --muted: #667069;
    --line: #dfe5df;
    --accent: #176b4d;
    --accent-hover: #10523b;
    --warning: #9a4d12;
    --warning-bg: #fff3e8;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text);
    font-family: Arial, sans-serif;
    font-size: 16px;
    letter-spacing: 0;
  }
  a { color: inherit; }
  .shop-header {
    background: #18362c;
    color: #ffffff;
    border-bottom: 4px solid #d8a23d;
  }
  .shop-header-inner, .shop-main {
    width: min(100% - 32px, 1120px);
    margin: 0 auto;
  }
  .shop-header-inner { padding: 26px 0 24px; }
  .eyebrow {
    display: block;
    margin-bottom: 8px;
    color: #e6c77e;
    font-size: 13px;
    font-weight: 700;
    text-transform: uppercase;
  }
  h1, h2, h3, p { margin-top: 0; }
  h1 { margin-bottom: 8px; font-size: 30px; line-height: 1.15; }
  .shop-lead { margin-bottom: 0; color: #d7e1dc; line-height: 1.5; }
  .shop-main { padding: 26px 0 48px; }
  .category-section { margin-bottom: 34px; }
  .category-title {
    margin-bottom: 14px;
    padding-bottom: 9px;
    border-bottom: 1px solid var(--line);
    font-size: 22px;
    line-height: 1.25;
  }
  .product-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  .product-card {
    display: flex;
    min-width: 0;
    flex-direction: column;
    overflow: hidden;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
  }
  .product-media-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 4 / 3;
    overflow: hidden;
    background: #e7ebe7;
  }
  .product-media {
    display: block;
    width: 100%;
    height: 100%;
    background: #e7ebe7;
    object-fit: cover;
  }
  .product-placeholder {
    display: grid;
    place-items: center;
    color: #7a837d;
    font-size: 14px;
    font-weight: 700;
  }
  .product-body { display: flex; flex: 1; flex-direction: column; padding: 18px; }
  .product-name {
    margin-bottom: 8px;
    overflow-wrap: anywhere;
    font-size: 19px;
    line-height: 1.3;
  }
  .product-description {
    display: -webkit-box;
    max-height: 6em;
    margin-bottom: 8px;
    overflow: hidden;
    overflow-wrap: anywhere;
    color: var(--muted);
    line-height: 1.5;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 4;
  }
  .description-toggle {
    align-self: flex-start;
    margin: 0 0 15px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    font: inherit;
    font-size: 13px;
    font-weight: 700;
  }
  .description-toggle:hover { color: var(--accent-hover); }
  body.modal-open { overflow: hidden; }
  .product-modal {
    width: min(100% - 32px, 680px);
    max-height: min(88vh, 760px);
    margin: auto;
    padding: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--panel);
    color: var(--text);
  }
  .product-modal::backdrop { background: rgb(18 27 22 / 68%); }
  .product-modal-inner {
    display: flex;
    max-height: min(88vh, 760px);
    flex-direction: column;
  }
  .modal-close {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 1;
    display: grid;
    width: 44px;
    height: 44px;
    place-items: center;
    padding: 0;
    border: 1px solid var(--line);
    border-radius: 50%;
    background: var(--panel);
    color: var(--text);
    cursor: pointer;
    font-size: 26px;
    line-height: 1;
  }
  .product-modal-scroll { overflow-y: auto; overscroll-behavior: contain; }
  .product-modal .product-media-wrap { max-height: 310px; }
  .product-modal-content { padding: 22px; }
  .product-modal-description {
    margin-bottom: 18px;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    color: var(--muted);
    line-height: 1.55;
  }
  .product-modal .product-purchase { margin-top: 0; }
  .product-purchase { margin-top: auto; }
  .price-line { margin-bottom: 14px; font-size: 18px; font-weight: 700; }
  .price-label { color: var(--muted); font-size: 13px; font-weight: 400; }
  .price-note { margin: -6px 0 14px; color: var(--muted); font-size: 13px; line-height: 1.4; }
  .variant-list { display: grid; gap: 8px; margin: 0 0 16px; padding: 0; list-style: none; }
  .variant-item {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 7px;
    border-bottom: 1px solid var(--line);
  }
  .variant-price { flex: none; font-weight: 700; }
  .variant-status { display: block; margin-top: 3px; color: var(--accent); font-size: 12px; }
  .variant-status.out { color: var(--warning); }
  .product-footer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .stock-status {
    font-size: 13px;
    font-weight: 700;
    color: var(--accent);
  }
  .stock-status.out { color: var(--warning); }
  .contact-button {
    display: inline-flex;
    min-height: 44px;
    align-items: center;
    justify-content: center;
    padding: 10px 13px;
    border: 0;
    border-radius: 6px;
    background: var(--accent);
    color: #ffffff;
    font-size: 14px;
    font-weight: 700;
    text-align: center;
    text-decoration: none;
  }
  .contact-button:hover { background: var(--accent-hover); }
  .contact-button.disabled { background: #6f7772; cursor: default; }
  .empty-message, .unavailable-panel {
    padding: 20px;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    color: var(--muted);
    line-height: 1.5;
  }
  .unavailable-panel { border-left: 4px solid var(--warning); background: var(--warning-bg); color: var(--text); }
  @media (min-width: 640px) {
    .shop-header-inner, .shop-main { width: min(100% - 48px, 1120px); }
    .product-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 980px) {
    .product-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }
  @media (max-width: 639px) {
    .shop-header-inner, .shop-main { width: min(100% - 24px, 1120px); }
    .product-body { padding: 16px; }
    .product-footer { align-items: stretch; flex-direction: column; }
    .contact-button { width: 100%; }
    .product-modal {
      width: calc(100% - 16px);
      max-height: calc(100dvh - 16px);
    }
    .product-modal-inner { max-height: calc(100dvh - 16px); }
    .product-modal-content { padding: 18px 16px; }
  }
</style>
"""


PAGE_SCRIPT = """
<script>
  document.addEventListener('DOMContentLoaded', function () {
    var returnFocus = new WeakMap();

    function syncDescriptionButton(button) {
      var description = document.getElementById(button.dataset.descriptionId);
      button.hidden = !description || description.scrollHeight <= description.clientHeight + 1;
    }

    var descriptionButtons = document.querySelectorAll('.description-toggle');
    descriptionButtons.forEach(syncDescriptionButton);
    window.addEventListener('resize', function () {
      descriptionButtons.forEach(syncDescriptionButton);
    });

    document.querySelectorAll('[data-modal-target]').forEach(function (button) {
      button.addEventListener('click', function () {
        var dialog = document.getElementById(button.dataset.modalTarget);
        if (!dialog || typeof dialog.showModal !== 'function') return;
        returnFocus.set(dialog, button);
        document.body.classList.add('modal-open');
        dialog.showModal();
        var closeButton = dialog.querySelector('.modal-close');
        if (closeButton) closeButton.focus();
      });
    });

    document.querySelectorAll('.product-modal').forEach(function (dialog) {
      var closeButton = dialog.querySelector('.modal-close');
      if (closeButton) {
        closeButton.addEventListener('click', function () { dialog.close(); });
      }
      dialog.addEventListener('click', function (event) {
        if (event.target !== dialog) return;
        var bounds = dialog.getBoundingClientRect();
        var inside = event.clientX >= bounds.left && event.clientX <= bounds.right &&
          event.clientY >= bounds.top && event.clientY <= bounds.bottom;
        if (!inside) dialog.close();
      });
      dialog.addEventListener('close', function () {
        if (!document.querySelector('.product-modal[open]')) {
          document.body.classList.remove('modal-open');
        }
        var trigger = returnFocus.get(dialog);
        if (trigger) trigger.focus();
      });
    });
  });
</script>
"""


def _escape(value):
    return html.escape(str("" if value is None else value), quote=True)


def _format_price(value, currency_symbol):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return "Цена уточняется"
    currency = str(currency_symbol or "").strip()
    return f"{amount:.2f} {currency}".strip()


def safe_image_url(value):
    raw_value = str(value or "").strip()
    if not raw_value or any(ord(character) < 32 for character in raw_value):
        return ""
    try:
        parsed = urlparse(raw_value)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        if not parsed.hostname or parsed.username or parsed.password:
            return ""
    except (TypeError, ValueError):
        return ""
    return raw_value


def public_contact_url(value):
    raw_value = str(value or "")
    if not raw_value or raw_value != raw_value.strip():
        return ""

    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", raw_value):
        return f"https://t.me/{raw_value}"
    return ""


def _positive_finite_price(value, field_name):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return value


def _optional_inventory_count(value, field_name, positive=False):
    if value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if positive and count <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    if not positive and count < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return count


def _normalize_catalog_product_pricing(
    pricing_mode,
    price_per_kg,
    fixed_price,
    sale_unit,
    unit_weight_grams,
    stock_quantity,
    stock_grams,
):
    if pricing_mode not in PRICING_MODES:
        raise ValueError(f"Unknown pricing_mode: {pricing_mode!r}")
    if pricing_mode == "fixed":
        fixed_price = _positive_finite_price(fixed_price, "fixed_price")
        sale_unit = str(sale_unit or "").strip()
        if not sale_unit:
            raise ValueError("sale_unit cannot be blank for fixed pricing")
        stock_quantity = _optional_inventory_count(
            stock_quantity, "stock_quantity"
        )
        return 0, fixed_price, sale_unit, None, stock_quantity, stock_grams
    if pricing_mode == "per_kg":
        price_per_kg = _positive_finite_price(price_per_kg, "price_per_kg")
        stock_grams = _optional_inventory_count(stock_grams, "stock_grams")
        if stock_grams is None:
            raise ValueError("stock_grams is required for per_kg pricing")
        unit_weight_grams = _optional_inventory_count(
            unit_weight_grams, "unit_weight_grams", positive=True
        )
        return price_per_kg, None, None, unit_weight_grams, None, stock_grams
    return 0, None, None, None, None, stock_grams


def assemble_catalog(category_rows, product_rows, option_rows):
    categories = []
    categories_by_id = {}

    for category_id, name, sort_order, is_active in category_rows:
        if not is_active:
            continue
        category = {
            "id": category_id,
            "name": name,
            "sort_order": sort_order or 0,
            "products": [],
        }
        categories.append(category)
        categories_by_id[category_id] = category

    products_by_id = {}
    for row in product_rows:
        (
            product_id,
            category_id,
            name,
            description,
            price_per_kg,
            image_url,
            is_out_of_stock,
            stock_grams,
            sort_order,
            is_active,
            pricing_mode,
            fixed_price,
            sale_unit,
            unit_weight_grams,
            stock_quantity,
        ) = row
        category = categories_by_id.get(category_id)
        if not is_active or not category:
            continue
        if pricing_mode not in PRICING_MODES:
            logger.error(
                "Hiding product %s with unsupported pricing mode %r",
                product_id,
                pricing_mode,
            )
            continue
        (
            price_per_kg,
            fixed_price,
            sale_unit,
            unit_weight_grams,
            stock_quantity,
            stock_grams,
        ) = _normalize_catalog_product_pricing(
            pricing_mode,
            price_per_kg,
            fixed_price,
            sale_unit,
            unit_weight_grams,
            stock_quantity,
            stock_grams,
        )
        product = {
            "id": product_id,
            "name": name,
            "description": description,
            "price_per_kg": price_per_kg,
            "image_url": image_url,
            "is_out_of_stock": bool(is_out_of_stock),
            "stock_grams": stock_grams,
            "sort_order": sort_order or 0,
            "pricing_mode": pricing_mode,
            "fixed_price": fixed_price,
            "sale_unit": sale_unit,
            "unit_weight_grams": unit_weight_grams,
            "stock_quantity": stock_quantity,
            "options": [],
        }
        category["products"].append(product)
        products_by_id[product_id] = product

    for (
        option_id,
        product_id,
        label,
        weight,
        price,
        sort_order,
        is_active,
        stock_quantity,
        is_out_of_stock,
    ) in option_rows:
        product = products_by_id.get(product_id)
        if not is_active or not product:
            continue
        if product["pricing_mode"] != "options":
            continue
        label = str(label or "").strip()
        if not label:
            raise ValueError("Active product option label cannot be blank")
        price = _positive_finite_price(price, "option price")
        weight = _optional_inventory_count(weight, "option weight", positive=True)
        stock_quantity = _optional_inventory_count(
            stock_quantity, "option stock_quantity"
        )
        product["options"].append({
            "id": option_id,
            "label": label,
            "weight": weight,
            "price": price,
            "sort_order": sort_order or 0,
            "stock_quantity": stock_quantity,
            "is_out_of_stock": bool(is_out_of_stock),
        })

    categories.sort(key=lambda category: (category["sort_order"], category["id"]))
    for category in categories:
        category["products"].sort(key=lambda product: (product["sort_order"], product["id"]))
        for product in category["products"]:
            product["options"].sort(key=lambda option: (option["sort_order"], option["id"]))
    return categories


def fetch_catalog(connection_factory=None):
    if connection_factory is None:
        if not (os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")):
            raise RuntimeError("Database is unavailable")
        from db_schema import get_db_connection

        connection_factory = get_db_connection

    connection = None
    cursor = None
    try:
        connection = connection_factory()
        cursor = connection.cursor()
        cursor.execute("""
            SELECT id, name, sort_order, is_active
            FROM categories
            WHERE is_active = TRUE
            ORDER BY sort_order, id
        """)
        category_rows = cursor.fetchall()

        cursor.execute("""
            SELECT
                p.id,
                p.category_id,
                p.name,
                p.description,
                p.price_per_kg,
                p.image_url,
                p.is_out_of_stock,
                p.stock_grams,
                p.sort_order,
                p.is_active,
                p.pricing_mode,
                p.fixed_price,
                p.sale_unit,
                p.unit_weight_grams,
                p.stock_quantity
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.is_active = TRUE
              AND c.is_active = TRUE
            ORDER BY c.sort_order, c.id, p.sort_order, p.id
        """)
        product_rows = cursor.fetchall()

        cursor.execute("""
            SELECT
                po.id,
                po.product_id,
                po.label,
                po.weight,
                po.price,
                po.sort_order,
                po.is_active,
                po.stock_quantity,
                po.is_out_of_stock
            FROM product_options po
            JOIN products p ON p.id = po.product_id
            JOIN categories c ON c.id = p.category_id
            WHERE po.is_active = TRUE
              AND p.is_active = TRUE
              AND c.is_active = TRUE
            ORDER BY c.sort_order, c.id, p.sort_order, p.id, po.sort_order, po.id
        """)
        option_rows = cursor.fetchall()
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()

    return assemble_catalog(category_rows, product_rows, option_rows)


def _option_is_out_of_stock(option):
    if option["is_out_of_stock"]:
        return True
    stock_quantity = option["stock_quantity"]
    return stock_quantity is None or int(stock_quantity or 0) <= 0


def _product_is_out_of_stock(product):
    if product["is_out_of_stock"]:
        return True
    pricing_mode = product.get("pricing_mode")
    if pricing_mode not in PRICING_MODES:
        logger.error(
            "Hiding product %s with unsupported pricing mode %r",
            product.get("id"),
            pricing_mode,
        )
        return True
    if pricing_mode == "fixed":
        stock_quantity = product["stock_quantity"]
        return stock_quantity is None or int(stock_quantity or 0) <= 0
    if pricing_mode == "options":
        return not product["options"] or all(
            _option_is_out_of_stock(option) for option in product["options"]
        )
    stock_grams = product["stock_grams"]
    return stock_grams is None or int(stock_grams or 0) <= 0


def _format_weight(value):
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return ""
    if weight <= 0:
        return ""
    if weight >= 1000 and weight % 1000 == 0:
        return f"{weight // 1000} кг"
    return f"{weight} г"


def _contact_element(contact_url):
    if contact_url:
        return (
            f'<a class="contact-button" href="{_escape(contact_url)}" '
            'target="_blank" rel="noopener noreferrer">Связаться для заказа</a>'
        )
    return '<span class="contact-button disabled" aria-disabled="true">Связаться для заказа</span>'


def render_catalog_page(categories, brand_name=BRAND_NAME, currency_symbol=CURRENCY_SYMBOL, support_username=None):
    category_sections = []
    if support_username is None:
        support_username = get_support_username()
    contact_url = public_contact_url(support_username)

    for category in categories:
        product_cards = []
        for product in category["products"]:
            if _product_is_out_of_stock(product):
                continue
            product_name = _escape(product["name"])
            image_url = safe_image_url(product["image_url"])
            placeholder_html = (
                '<div class="product-media product-placeholder" '
                'role="img" aria-label="Фотография отсутствует"><span>Фото скоро</span></div>'
            )
            if image_url:
                media_html = (
                    '<div class="product-media-wrap">'
                    f'<img class="product-media" src="{_escape(image_url)}" '
                    f'alt="{product_name}" loading="lazy" referrerpolicy="no-referrer" '
                    'onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
                    f'{placeholder_html.replace("<div ", "<div hidden ", 1)}</div>'
                )
            else:
                media_html = (
                    f'<div class="product-media-wrap">{placeholder_html}</div>'
                )

            description_html = ""
            description = ""
            description_id = f'product-description-{_escape(product["id"])}'
            modal_id = f'product-modal-{_escape(product["id"])}'
            if product["description"]:
                description = str(product["description"])
                toggle_html = (
                    f'<button class="description-toggle" type="button" hidden '
                    f'aria-haspopup="dialog" aria-controls="{modal_id}" '
                    f'data-description-id="{description_id}" '
                    f'data-modal-target="{modal_id}">Подробнее</button>'
                )
                description_html = (
                    f'<p class="product-description" id="{description_id}">'
                    f'{_escape(description)}</p>{toggle_html}'
                )

            price_html = ""
            variants_html = ""
            pricing_mode = product["pricing_mode"]
            if pricing_mode == "fixed" and product["fixed_price"] is not None:
                price_html = (
                    '<p class="price-line">'
                    f'{_escape(_format_price(product["fixed_price"], currency_symbol))} '
                    f'<span class="price-label">{_escape(product["sale_unit"] or "за упаковку")}</span></p>'
                )
            elif pricing_mode == "per_kg" and product["price_per_kg"] is not None:
                price_html = (
                    '<p class="price-line">'
                    f'{_escape(_format_price(product["price_per_kg"], currency_symbol))} '
                    '<span class="price-label">за кг</span></p>'
                )
                weight_text = _format_weight(product["unit_weight_grams"])
                weight_prefix = f"Ориентировочный вес: {weight_text}. " if weight_text else ""
                price_html += (
                    f'<p class="price-note">{_escape(weight_prefix)}'
                    'Итоговая стоимость зависит от фактического веса.</p>'
                )
            elif pricing_mode == "options" and product["options"]:
                variant_items = []
                for option in product["options"]:
                    option_is_out = _option_is_out_of_stock(option)
                    option_status = "Нет в наличии" if option_is_out else "В наличии"
                    option_status_class = "variant-status out" if option_is_out else "variant-status"
                    variant_items.append(
                        '<li class="variant-item">'
                        f'<span>{_escape(option["label"])}'
                        f'<span class="{option_status_class}">{option_status}</span></span>'
                        f'<span class="variant-price">{_escape(_format_price(option["price"], currency_symbol))}</span>'
                        '</li>'
                    )
                variants_html = f'<ul class="variant-list">{"".join(variant_items)}</ul>'
            elif pricing_mode == "options":
                variants_html = '<p class="price-note">Варианты временно недоступны.</p>'

            stock_class = "stock-status"
            stock_text = "В наличии"
            modal_html = ""
            if description:
                modal_html = (
                    f'<dialog class="product-modal" id="{modal_id}" '
                    'aria-modal="true" '
                    f'aria-labelledby="{modal_id}-title" aria-describedby="{modal_id}-description">'
                    '<div class="product-modal-inner">'
                    '<button class="modal-close" type="button" aria-label="Закрыть">&times;</button>'
                    '<div class="product-modal-scroll">'
                    f'{media_html}'
                    '<div class="product-modal-content">'
                    f'<h2 class="product-name" id="{modal_id}-title">{product_name}</h2>'
                    f'<p class="product-modal-description" id="{modal_id}-description">{_escape(description)}</p>'
                    f'<div class="product-purchase">{price_html}{variants_html}'
                    '<div class="product-footer">'
                    f'<span class="{stock_class}">{stock_text}</span>'
                    f'{_contact_element(contact_url)}'
                    '</div></div></div></div></div></dialog>'
                )
            product_cards.append(
                '<article class="product-card">'
                f'{media_html}'
                '<div class="product-body">'
                f'<h3 class="product-name">{product_name}</h3>'
                f'{description_html}<div class="product-purchase">{price_html}{variants_html}'
                '<div class="product-footer">'
                f'<span class="{stock_class}">{stock_text}</span>'
                f'{_contact_element(contact_url)}'
                f'</div></div></div></article>{modal_html}'
            )

        if not product_cards:
            continue
        products_html = f'<div class="product-grid">{"".join(product_cards)}</div>'
        category_sections.append(
            f'<section class="category-section" id="category-{_escape(category["id"])}">'
            f'<h2 class="category-title">{_escape(category["name"])}</h2>'
            f'{products_html}</section>'
        )

    catalog_html = (
        "".join(category_sections)
        if category_sections
        else '<p class="empty-message">Ассортимент обновляется. Пожалуйста, загляните позже.</p>'
    )
    safe_brand_name = _escape(brand_name or "Deal Market")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_brand_name}</title>
  {PAGE_STYLE}
</head>
<body>
  <header class="shop-header">
    <div class="shop-header-inner">
      <span class="eyebrow">Каталог</span>
      <h1>{safe_brand_name}</h1>
      <p class="shop-lead">Актуальный ассортимент и цены</p>
    </div>
  </header>
  <main class="shop-main">{catalog_html}</main>
  {PAGE_SCRIPT}
</body>
</html>"""


def render_unavailable_page(brand_name=BRAND_NAME):
    safe_brand_name = _escape(brand_name or "Deal Market")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_brand_name}</title>
  {PAGE_STYLE}
</head>
<body>
  <header class="shop-header">
    <div class="shop-header-inner"><span class="eyebrow">Каталог</span><h1>{safe_brand_name}</h1></div>
  </header>
  <main class="shop-main">
    <section class="unavailable-panel">
      <h2>Магазин временно недоступен</h2>
      <p>Пожалуйста, попробуйте открыть страницу немного позже.</p>
    </section>
  </main>
</body>
</html>"""


@router.get("/shop", response_class=HTMLResponse)
async def shop_page():
    try:
        categories = fetch_catalog()
        page = render_catalog_page(categories)
    except Exception:
        return HTMLResponse(render_unavailable_page(), status_code=503)
    return HTMLResponse(page)
