import os


TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
ALLOWED_APP_ENVIRONMENTS = frozenset({"local", "test", "preview", "production"})

# B11B must add trusted proxy handling, HTTPS enforcement, and HSTS before a
# preview deployment is approved for public exposure.
PREVIEW_PUBLIC_EXPOSURE_APPROVED = False
PREVIEW_PUBLIC_EXPOSURE_REQUIREMENTS = (
    "trusted_proxy_handling",
    "https_enforcement",
    "hsts",
)


def env_flag_enabled(name):
    value = os.getenv(name)
    if value is None:
        return False
    return value.strip().lower() in TRUE_ENV_VALUES


def get_app_env():
    value = os.getenv("APP_ENV", "local")
    if value not in ALLOWED_APP_ENVIRONMENTS:
        raise RuntimeError("APP_ENV is invalid")
    return value
