"""
Distribution plans (جدول التوزيعات)
------------------------------------
An organization's own ordered list of allocation lines, replacing the built-in
default policy once a version is approved. Each line's target for a period is
set by one of three bases:

  - percent: a share of the period's total collections (e.g. VAT at 13.04%)
  - fixed:   the same amount every period (e.g. rent)
  - input:   an amount entered with the period data (e.g. payroll due)

Lines are then filled in order by the same waterfall as the default policy.
"""
import math

from app.engine.allocation_engine import LEGACY_TARGET_FIELDS, allocate_waterfall, load_policy

BASIS_PERCENT = "percent"
BASIS_FIXED = "fixed"
BASIS_INPUT = "input"

# Period-data amounts a line may take its target from.
INPUT_FIELD_LABELS_AR = {
    "payroll_due": "الرواتب المستحقة",
    "vat_due": "ضريبة القيمة المضافة المستحقة",
    "royalty_due": "الرويالتي المستحقة",
    "suppliers_due": "مستحقات الموردين",
    "other_short_term_due": "التزامات قصيرة الأجل أخرى",
    "operational_reserve_target": "الاحتياطي التشغيلي المستهدف",
}

MAX_LINES = 20
MAX_NAME_LENGTH = 100
MAX_AMOUNT = 1_000_000_000_000


def default_lines() -> list[dict]:
    """The built-in default policy expressed as plan lines — the starting point of
    an organization's first draft."""
    policy = load_policy()
    return [
        {
            "name": b["name_ar"],
            "basis": BASIS_INPUT,
            "value": None,
            "input_field": LEGACY_TARGET_FIELDS[b["bucket_id"]],
            "protected": bool(b.get("protected")),
        }
        for b in sorted(policy["buckets"], key=lambda b: b["priority"])
        if b["bucket_id"] != "surplus_discretionary"
    ]


def normalize_lines(raw_lines: list[dict]) -> list[dict]:
    """Validates lines (already in priority order) and returns them in canonical
    form. Raises ValueError with an Arabic message on the first problem."""
    if not raw_lines:
        raise ValueError("الجدول يحتاج بندًا واحدًا على الأقل")
    if len(raw_lines) > MAX_LINES:
        raise ValueError(f"الحد الأقصى {MAX_LINES} بندًا")

    lines, names, used_fields, percent_total = [], set(), set(), 0.0
    for position, raw in enumerate(raw_lines, start=1):
        name = (raw.get("name") or "").strip()
        where = f"البند {position}"
        if not name or len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"{where}: الاسم مطلوب (حتى {MAX_NAME_LENGTH} خانة)")
        if name in names:
            raise ValueError(f"{where}: الاسم «{name}» مكرر")
        names.add(name)

        basis = raw.get("basis")
        line = {"name": name, "basis": basis, "value": None, "input_field": None,
                "protected": bool(raw.get("protected"))}
        value = raw.get("value")
        if basis in (BASIS_PERCENT, BASIS_FIXED):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{where} «{name}»: أدخل رقمًا صحيحًا")
            if basis == BASIS_PERCENT:
                if not 0 < value <= 100:
                    raise ValueError(f"{where} «{name}»: النسبة يجب أن تكون أكبر من 0 وحتى 100")
                percent_total += value
            elif not 0 < value <= MAX_AMOUNT:
                raise ValueError(f"{where} «{name}»: المبلغ يجب أن يكون أكبر من صفر")
            line["value"] = round(float(value), 4 if basis == BASIS_PERCENT else 2)
        elif basis == BASIS_INPUT:
            field = raw.get("input_field")
            if field not in INPUT_FIELD_LABELS_AR:
                raise ValueError(f"{where} «{name}»: مصدر المبلغ غير معروف")
            if field in used_fields:  # the same amount must not be reserved twice
                raise ValueError(f"{where} «{name}»: «{INPUT_FIELD_LABELS_AR[field]}» مستخدم في بند آخر")
            used_fields.add(field)
            line["input_field"] = field
        else:
            raise ValueError(f"{where} «{name}»: طريقة التحديد غير معروفة")
        lines.append(line)

    if percent_total > 100 + 1e-9:
        raise ValueError(f"مجموع النسب {percent_total:g}% يتجاوز 100% من التحصيلات")
    return lines


def basis_label(line: dict) -> str:
    if line["basis"] == BASIS_PERCENT:
        return f"{line['value']:g}% من التحصيلات"
    if line["basis"] == BASIS_FIXED:
        return f"مبلغ ثابت {line['value']:,.2f}"
    return f"من بيانات الفترة: {INPUT_FIELD_LABELS_AR[line['input_field']]}"


def line_target(line: dict, inputs: dict) -> float:
    if line["basis"] == BASIS_PERCENT:
        return round(float(inputs.get("total_collections", 0) or 0) * line["value"] / 100, 2)
    if line["basis"] == BASIS_FIXED:
        return float(line["value"])
    return float(inputs.get(line["input_field"], 0) or 0)


def run_plan_allocation(inputs: dict, liquidity: dict, lines: list[dict], policy_id: str) -> dict:
    built = [
        {
            "bucket_id": f"L{position}",
            "name_ar": line["name"],
            "target": line_target(line, inputs),
            "protected": line["protected"],
            "basis_label": basis_label(line),
        }
        for position, line in enumerate(lines, start=1)
    ]
    return allocate_waterfall(liquidity["net_operating_cash"], built, policy_id)
