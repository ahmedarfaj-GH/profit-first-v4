"""
Allocation Engine
-----------------
Allocates Net Operating Cash sequentially across buckets by priority
(waterfall, not pro-rata), per the allocation policy. Each bucket is either
FULLY_FUNDED, PARTIAL, UNFUNDED, or NOT_APPLICABLE (zero target).

Protected buckets (payroll, VAT, royalty, operational reserve) are never
reallocated once funded, except through a human review override that
carries a non-empty justification.
"""
import json
import math
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "allocation_policy.json"


def load_policy() -> dict:
    with open(POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)


def run_allocation(inputs: dict, liquidity: dict, policy: dict) -> dict:
    net_operating_cash = liquidity["net_operating_cash"]

    targets = {
        "payroll": inputs.get("payroll_due", 0),
        "vat": inputs.get("vat_due", 0),
        "royalty": inputs.get("royalty_due", 0),
        "suppliers": inputs.get("suppliers_due", 0),
        "other_short_term": inputs.get("other_short_term_due", 0),
        "operational_reserve": inputs.get("operational_reserve_target", 0),
    }

    bucket_order = [b["bucket_id"] for b in sorted(policy["buckets"], key=lambda b: b["priority"])]

    pool = net_operating_cash
    allocations = []
    funding_gap_total = 0.0

    for bucket_id in bucket_order:
        if bucket_id == "surplus_discretionary":
            continue
        target = float(targets.get(bucket_id, 0) or 0)
        if target <= 0:
            status = "NOT_APPLICABLE"
            allocated = 0.0
        elif pool >= target:
            allocated = target
            pool -= allocated
            status = "FULLY_FUNDED"
        elif pool > 0:
            allocated = pool
            funding_gap_total += target - allocated
            pool = 0.0
            status = "PARTIAL"
        else:
            allocated = 0.0
            funding_gap_total += target
            status = "UNFUNDED"

        bucket_meta = next(b for b in policy["buckets"] if b["bucket_id"] == bucket_id)
        allocations.append({
            "bucket_id": bucket_id,
            "name_ar": bucket_meta["name_ar"],
            "priority": bucket_meta["priority"],
            "target": round(target, 2),
            "allocated": round(allocated, 2),
            "status": status,
            "mandatory": bucket_meta.get("mandatory", False),
            "protected": bucket_meta.get("protected", False),
        })

    surplus = round(pool, 2)
    allocations.append({
        "bucket_id": "surplus_discretionary",
        "name_ar": "الفائض / الاستخدامات الاختيارية",
        "priority": 7,
        "target": None,
        "allocated": surplus,
        "status": "AVAILABLE" if surplus > 0 else "NONE",
        "mandatory": False,
        "protected": False,
    })

    return {
        "policy_id": policy["policy_id"],
        "net_operating_cash": round(net_operating_cash, 2),
        "allocations": allocations,
        "total_funding_gap": round(funding_gap_total, 2),
        "surplus_discretionary": surplus,
        "overall_status": (
            "FUNDING_GAP" if funding_gap_total > 0
            else "FULLY_FUNDED_WITH_SURPLUS" if surplus > 0
            else "FULLY_FUNDED_EXACT"
        ),
        "explainability": build_explainability(allocations, funding_gap_total, surplus),
        "human_review_required": True,
    }


def build_explainability(allocations: list, funding_gap_total: float, surplus: float) -> list:
    lines = []
    for a in allocations:
        if a["bucket_id"] == "surplus_discretionary":
            continue
        if a["status"] == "FULLY_FUNDED":
            lines.append(f"{a['name_ar']}: مُموّل بالكامل بمبلغ {a['allocated']:,.2f} (الأولوية {a['priority']}).")
        elif a["status"] == "PARTIAL":
            lines.append(f"{a['name_ar']}: مُموّل جزئيًا بمبلغ {a['allocated']:,.2f} من أصل {a['target']:,.2f} — عجز تمويلي.")
        elif a["status"] == "UNFUNDED":
            lines.append(f"{a['name_ar']}: غير مُموّل — لا توجد سيولة متبقية بعد تغطية البنود الأعلى أولوية.")
        elif a["status"] == "NOT_APPLICABLE":
            lines.append(f"{a['name_ar']}: لا يوجد التزام مستحق لهذه الفترة.")
    if funding_gap_total > 0:
        lines.append(f"إجمالي العجز التمويلي: {funding_gap_total:,.2f} — يتطلب مراجعة بشرية وقرار تمويل بديل.")
    elif surplus > 0:
        lines.append(f"بعد تغطية جميع الالتزامات والاحتياطي بالكامل، تبقى فائض قدره {surplus:,.2f} متاح للقرار البشري.")
    else:
        lines.append("تمت تغطية جميع الالتزامات والاحتياطي بدقة، بدون فائض أو عجز.")
    return lines


def validate_override(override: dict, allocations: list, justification: str | None) -> list:
    """Raises ValueError if `override` touches a protected bucket without a
    non-empty justification. Returns the sorted list of protected bucket_ids
    touched (may be empty) when the override is allowed."""
    known_ids = {a["bucket_id"] for a in allocations}
    unknown = sorted(set(override or {}) - known_ids)
    if unknown:
        raise ValueError(f"Unknown bucket(s) in override: {unknown}")
    for bucket_id, value in (override or {}).items():
        if (
            not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value < 0
        ):
            raise ValueError(f"Override value for '{bucket_id}' must be a finite, non-negative number")

    protected_ids = {a["bucket_id"] for a in allocations if a.get("protected")}
    touched = sorted(protected_ids.intersection(override.keys())) if override else []
    if touched and not (justification and justification.strip()):
        raise ValueError(
            f"Overriding protected bucket(s) {touched} requires a non-empty justification. "
            "Policy default: protected buckets are never reallocated once funded; "
            "this is the strictly-controlled exception path."
        )
    return touched
