"""
Allocation Engine
-----------------
Allocates Net Operating Cash sequentially across buckets by priority
(waterfall, not pro-rata), per the allocation policy: either the built-in
default (AP-V1) or an organization's approved distribution plan. Each bucket is either
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


# Where the default policy (AP-V1) takes each bucket's target from the period data.
LEGACY_TARGET_FIELDS = {
    "payroll": "payroll_due",
    "vat": "vat_due",
    "royalty": "royalty_due",
    "suppliers": "suppliers_due",
    "other_short_term": "other_short_term_due",
    "operational_reserve": "operational_reserve_target",
}


def run_allocation(inputs: dict, liquidity: dict, policy: dict) -> dict:
    """Allocation under the built-in default policy (AP-V1)."""
    lines = [
        {
            "bucket_id": b["bucket_id"],
            "name_ar": b["name_ar"],
            "target": float(inputs.get(LEGACY_TARGET_FIELDS[b["bucket_id"]], 0) or 0),
            "mandatory": b.get("mandatory", False),
            "protected": b.get("protected", False),
        }
        for b in sorted(policy["buckets"], key=lambda b: b["priority"])
        if b["bucket_id"] != "surplus_discretionary"
    ]
    return allocate_waterfall(liquidity["net_operating_cash"], lines, policy["policy_id"])


def allocate_waterfall(net_operating_cash: float, lines: list, policy_id: str) -> dict:
    """Fills `lines` in order (their priority) from Net Operating Cash. Each line
    carries bucket_id, name_ar, target and protected, and optionally mandatory
    and basis_label (how its target was set)."""
    pool = net_operating_cash
    allocations = []
    funding_gap_total = 0.0

    for priority, line in enumerate(lines, start=1):
        target = float(line["target"] or 0)
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

        allocation = {
            "bucket_id": line["bucket_id"],
            "name_ar": line["name_ar"],
            "priority": priority,
            "target": round(target, 2),
            "allocated": round(allocated, 2),
            "status": status,
            "mandatory": line.get("mandatory", True),
            "protected": line.get("protected", False),
        }
        if line.get("basis_label"):
            allocation["basis_label"] = line["basis_label"]
        allocations.append(allocation)

    surplus = round(pool, 2)
    allocations.append({
        "bucket_id": "surplus_discretionary",
        "name_ar": "الفائض / الاستخدامات الاختيارية",
        "priority": len(lines) + 1,
        "target": None,
        "allocated": surplus,
        "status": "AVAILABLE" if surplus > 0 else "NONE",
        "mandatory": False,
        "protected": False,
    })

    return {
        "policy_id": policy_id,
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
        if a.get("basis_label"):
            a = {**a, "name_ar": f"{a['name_ar']} ({a['basis_label']})"}
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
