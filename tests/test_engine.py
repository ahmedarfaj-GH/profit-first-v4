import pytest

from app.engine.allocation_engine import load_policy, run_allocation, validate_override
from app.engine.liquidity_engine import compute_liquidity

REFERENCE = {
    "opening_cash_balance": 6585.81, "total_collections": 160944.33,
    "total_operating_expenses_paid": 55695.40, "payroll_due": 58043.79, "vat_due": 20984.88,
    "operational_reserve_target": 20000,
}
FUNDING_GAP = {
    "opening_cash_balance": 6850.37, "total_collections": 119142.98,
    "total_operating_expenses_paid": 59231.64, "payroll_due": 41231.64, "vat_due": 11384.90,
    "operational_reserve_target": 20000,
}


def allocate(inputs):
    liquidity = compute_liquidity(inputs)
    return liquidity, run_allocation(inputs, liquidity, load_policy())


def by_bucket(allocation):
    return {a["bucket_id"]: a for a in allocation["allocations"]}


def test_reference_dataset_matches_documented_liquidity():
    liquidity, allocation = allocate(REFERENCE)
    assert liquidity["net_operating_cash"] == 111834.74
    assert liquidity["estimated_available_liquidity"] == 12806.07
    assert allocation["overall_status"] == "FULLY_FUNDED_WITH_SURPLUS"
    assert allocation["surplus_discretionary"] == 12806.07


def test_funding_gap_partially_funds_the_reserve_only():
    liquidity, allocation = allocate(FUNDING_GAP)
    buckets = by_bucket(allocation)
    assert liquidity["estimated_available_liquidity"] == -5854.83
    assert buckets["payroll"]["status"] == "FULLY_FUNDED"
    assert buckets["vat"]["status"] == "FULLY_FUNDED"
    assert buckets["operational_reserve"]["status"] == "PARTIAL"
    assert buckets["operational_reserve"]["allocated"] == 14145.17
    assert allocation["total_funding_gap"] == 5854.83
    assert allocation["overall_status"] == "FUNDING_GAP"


def test_waterfall_starves_lower_priorities_first():
    _, allocation = allocate({"opening_cash_balance": 1000, "payroll_due": 5000, "vat_due": 500, "suppliers_due": 300})
    buckets = by_bucket(allocation)
    assert buckets["payroll"]["status"] == "PARTIAL" and buckets["payroll"]["allocated"] == 1000
    assert buckets["vat"]["status"] == "UNFUNDED"
    assert buckets["suppliers"]["status"] == "UNFUNDED"
    assert allocation["total_funding_gap"] == 4800


def test_allocations_never_exceed_the_cash_pool():
    liquidity, allocation = allocate(FUNDING_GAP)
    total = sum(a["allocated"] for a in allocation["allocations"])
    assert total == pytest.approx(liquidity["net_operating_cash"], abs=0.01)


def test_protected_override_requires_a_justification():
    _, allocation = allocate(REFERENCE)
    buckets = allocation["allocations"]
    with pytest.raises(ValueError):
        validate_override({"payroll": 100}, buckets, None)
    with pytest.raises(ValueError):
        validate_override({"payroll": 100}, buckets, "   ")
    assert validate_override({"payroll": 100}, buckets, "agreed deferral") == ["payroll"]


def test_unprotected_override_needs_no_justification():
    _, allocation = allocate(REFERENCE)
    assert validate_override({"suppliers": 10}, allocation["allocations"], None) == []


@pytest.mark.parametrize("override", [{"nonexistent": 1}, {"suppliers": -1}, {"suppliers": float("nan")}, {"suppliers": True}])
def test_invalid_overrides_are_rejected(override):
    _, allocation = allocate(REFERENCE)
    with pytest.raises(ValueError):
        validate_override(override, allocation["allocations"], "reason")
