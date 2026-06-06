import json
import os


_CONFIG_CACHE = None


def get_shop_config():
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        try:
            with open("config.json", "r", encoding="utf-8") as file:
                _CONFIG_CACHE = json.load(file)
        except FileNotFoundError:
            _CONFIG_CACHE = {}
    return _CONFIG_CACHE


def get_config_value(key, default=None):
    return get_shop_config().get(key, default)


BRAND_NAME = get_config_value("brand_name", "Shop")
SUPPORT_USERNAME = get_config_value("support_username", "")
CURRENCY_CODE = get_config_value("currency", "EUR")
CURRENCY_SYMBOL = get_config_value("currency_symbol", "€")
ADMIN_PANEL_TITLE = get_config_value("admin_panel_title", BRAND_NAME)
MINIMUM_ORDER_AMOUNT = get_config_value("minimum_order_amount", 20)
IBAN = get_config_value("iban", "")
RECEIVER_NAME = get_config_value("receiver_name", "")
PAYPAL = get_config_value("paypal", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
