import math
import re
from datetime import date

ENTITY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_ENTITY_ID_RE = re.compile(ENTITY_ID_PATTERN)


def is_valid_entity_id(value: str) -> bool:
    return bool(_ENTITY_ID_RE.fullmatch(value or ""))


def validate_period(period_start: str, period_end: str) -> None:
    """Both must be ISO dates (YYYY-MM-DD) with start <= end; raises ValueError."""
    try:
        start = date.fromisoformat(period_start)
        end = date.fromisoformat(period_end)
    except (TypeError, ValueError):
        raise ValueError("تاريخ الفترة غير صالح — الصيغة المطلوبة YYYY-MM-DD") from None
    if start > end:
        raise ValueError("بداية الفترة يجب أن تكون قبل نهايتها أو مساوية لها")


def is_finite_non_negative(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
