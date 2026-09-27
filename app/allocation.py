"""Computes a run: liquidity from the period data, then allocation under the
organization's approved distribution plan — or the built-in default policy
(AP-V1) until one is approved."""
from app import db
from app.engine.allocation_engine import load_policy, run_allocation
from app.engine.distribution_plan import run_plan_allocation
from app.engine.liquidity_engine import compute_liquidity


def plan_policy_id(plan: dict) -> str:
    return f"DP-v{plan['version']}"


def compute_run(org_id: str, inputs: dict) -> tuple[dict, dict]:
    liquidity = compute_liquidity(inputs)
    plan = db.get_active_plan(org_id)
    if not plan:
        return liquidity, run_allocation(inputs, liquidity, load_policy())

    allocation = run_plan_allocation(inputs, liquidity, plan["lines"], plan_policy_id(plan))
    # A plan's lines are the period's needs, so available liquidity is what's left
    # after all of them (under the default policy this is the same formula).
    plan_total = sum(a["target"] for a in allocation["allocations"] if a["target"] is not None)
    liquidity = {
        **liquidity,
        "plan_targets_total": round(plan_total, 2),
        "estimated_available_liquidity": round(liquidity["net_operating_cash"] - plan_total, 2),
    }
    return liquidity, allocation
