import os


def is_production() -> bool:
    return os.environ.get("APP_ENV", "development").strip().lower() == "production"


def validate_config() -> None:
    """Fail fast at startup in production instead of running half-configured."""
    if not is_production():
        return

    problems = []
    if len(os.environ.get("SESSION_SECRET", "")) < 32:
        problems.append("SESSION_SECRET must be set and at least 32 characters")
    if len(os.environ.get("API_KEY", "")) < 32:
        problems.append("API_KEY must be set and at least 32 characters")
    if not os.environ.get("UI_LOGIN_USER"):
        problems.append("UI_LOGIN_USER must be set")
    if not os.environ.get("UI_LOGIN_PASSWORD_HASH", "").startswith("$2"):
        problems.append("UI_LOGIN_PASSWORD_HASH must be a bcrypt hash")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or database_url.startswith("sqlite"):
        problems.append("DATABASE_URL must point to a PostgreSQL database in production")

    if problems:
        raise RuntimeError("Invalid production configuration: " + "; ".join(problems))
