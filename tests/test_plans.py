import re

import pytest

from app.engine.allocation_engine import load_policy, run_allocation
from app.engine.distribution_plan import default_lines, normalize_lines, run_plan_allocation
from app.engine.liquidity_engine import compute_liquidity
from tests.conftest import add_user, login
from tests.test_app import API_HEADERS, MANUAL_RUN, create_entity, submit_run


def line(name, basis, value=None, input_field=None, protected=False):
    return {"name": name, "basis": basis, "value": value, "input_field": input_field, "protected": protected}


VAT = line("الضريبة", "percent", 13.0435, protected=True)
PAYROLL = line("الرواتب", "input", input_field="payroll_due", protected=True)
RENT = line("الإيجار", "fixed", 3000)


# --- plan rules ------------------------------------------------------------------
def test_mixed_plan_fills_percent_fixed_and_period_lines_in_order():
    inputs = {"total_collections": 10000, "total_operating_expenses_paid": 2000, "payroll_due": 5000}
    liquidity = compute_liquidity(inputs)  # net operating cash 8000
    allocation = run_plan_allocation(inputs, liquidity, normalize_lines([VAT, PAYROLL, RENT]), "DP-v1")
    vat, payroll, rent, surplus = allocation["allocations"]
    assert (vat["target"], vat["status"]) == (1304.35, "FULLY_FUNDED")
    assert (payroll["allocated"], payroll["status"]) == (5000, "FULLY_FUNDED")
    assert (rent["allocated"], rent["status"]) == (1695.65, "PARTIAL")
    assert allocation["total_funding_gap"] == 1304.35 and surplus["allocated"] == 0
    assert vat["bucket_id"] == "L1" and vat["protected"] and "13.0435%" in vat["basis_label"]
    assert allocation["policy_id"] == "DP-v1"
    assert any("الإيجار (مبلغ ثابت 3,000.00)" in text for text in allocation["explainability"])


def test_default_lines_reproduce_the_default_policy():
    inputs = {"opening_cash_balance": 6850.37, "total_collections": 119142.98, "total_operating_expenses_paid": 59231.64,
              "payroll_due": 41231.64, "vat_due": 11384.9, "operational_reserve_target": 20000}
    liquidity = compute_liquidity(inputs)
    as_plan = run_plan_allocation(inputs, liquidity, normalize_lines(default_lines()), "DP-v1")
    as_policy = run_allocation(inputs, liquidity, load_policy())
    assert [a["allocated"] for a in as_plan["allocations"]] == [a["allocated"] for a in as_policy["allocations"]]
    assert as_plan["total_funding_gap"] == as_policy["total_funding_gap"]


@pytest.mark.parametrize("lines,message", [
    ([], "بندًا واحدًا"),
    ([line("", "fixed", 1)], "الاسم مطلوب"),
    ([RENT, {**RENT, "value": 5}], "مكرر"),
    ([line("A", "percent", 0)], "النسبة"),
    ([line("A", "percent", 101)], "النسبة"),
    ([line("A", "percent", 60), line("B", "percent", 50)], "يتجاوز 100%"),
    ([line("A", "fixed", -5)], "أكبر من صفر"),
    ([line("A", "fixed", float("inf"))], "رقمًا"),
    ([line("A", "fixed", None)], "رقمًا"),
    ([line("A", "input", input_field="bank_balance")], "مصدر المبلغ"),
    ([PAYROLL, {**PAYROLL, "name": "رواتب 2"}], "مستخدم في بند آخر"),
    ([line("A", "magic", 1)], "طريقة التحديد"),
    ([line(f"L{i}", "fixed", 1) for i in range(21)], "الحد الأقصى"),
])
def test_invalid_plans_are_rejected(lines, message):
    with pytest.raises(ValueError, match=message):
        normalize_lines(lines)


# --- workflow ----------------------------------------------------------------------
def form_for(lines):
    """The editor form for these lines, in the given order."""
    return {
        "line_name": [ln["name"] for ln in lines],
        "line_basis": [f"input:{ln['input_field']}" if ln["basis"] == "input" else ln["basis"] for ln in lines],
        "line_value": ["" if ln["value"] is None else str(ln["value"]) for ln in lines],
        "line_protected": ["1" if ln["protected"] else "0" for ln in lines],
        "line_order": [str(i) for i in range(1, len(lines) + 1)],
    }


def post(client, path, data=None):
    return client.post(path, data=data or {}, follow_redirects=False)


def new_draft(client):
    response = post(client, "/plans")
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


@pytest.fixture()
def team(client, db, org):
    """An organization with two owners and an accountant, signed in as the accountant."""
    for username, role in (("owner1", "owner"), ("owner2", "owner"), ("acct", "accountant")):
        add_user(db, username, org, role)
    login(client, "acct")
    return client


def test_accountant_designs_and_another_person_approves(team, db, org):
    plan_id = new_draft(team)
    assert len(db.get_plan(plan_id, org_id=org)["lines"]) == 6  # starts from the default policy

    assert post(team, f"/plans/{plan_id}/lines", form_for([VAT, PAYROLL, RENT])).status_code == 303
    assert [ln["name"] for ln in db.get_plan(plan_id, org_id=org)["lines"]] == ["الضريبة", "الرواتب", "الإيجار"]
    assert post(team, f"/plans/{plan_id}/submit").status_code == 303
    assert post(team, f"/plans/{plan_id}/lines", form_for([RENT])).status_code == 409  # locked once submitted
    assert post(team, f"/plans/{plan_id}/approve").status_code == 403                  # accountants don't approve

    login(team, "owner1")
    assert post(team, f"/plans/{plan_id}/approve").status_code == 303
    plan = db.get_active_plan(org)
    assert plan["id"] == plan_id and plan["submitted_by"] == "acct" and plan["approved_by"] == "owner1"
    assert [e["action"] for e in db.list_plan_events(plan_id)] == ["created", "edited", "submitted", "approved"]

    create_entity(team, "SHOP-1")
    run_id = submit_run(team, "SHOP-1")
    run = db.get_run(run_id, org_id=org)
    assert run["policy_id"] == "DP-v1"
    allocation, liquidity = run["allocation"], run["liquidity"]
    # Available liquidity is what the plan's lines leave over (negative = the funding gap).
    assert liquidity["estimated_available_liquidity"] == pytest.approx(
        allocation["surplus_discretionary"] - allocation["total_funding_gap"], abs=0.01)
    assert liquidity["plan_targets_total"] == pytest.approx(15540.41 + 41231.64 + 3000, abs=0.01)
    assert [a["name_ar"] for a in run["allocation"]["allocations"]][:3] == ["الضريبة", "الرواتب", "الإيجار"]
    page = team.get(f"/runs/{run_id}").text
    assert "جدول التوزيعات، الإصدار 1" in page and "إجمالي مستهدفات بنود جدول التوزيعات" in page


def test_submitter_cannot_approve_their_own_version(team, db, org):
    login(team, "owner1")
    plan_id = new_draft(team)
    post(team, f"/plans/{plan_id}/submit")
    refused = post(team, f"/plans/{plan_id}/approve")
    assert refused.status_code == 403 and "مالك آخر" in refused.text
    assert db.get_active_plan(org) is None
    assert db.approve_plan(plan_id, org, "owner1") is False  # enforced by the database too

    login(team, "owner2")
    assert post(team, f"/plans/{plan_id}/approve").status_code == 303
    assert db.get_active_plan(org)["id"] == plan_id


def test_rejection_sends_the_version_back_with_a_reason(team, db, org):
    plan_id = new_draft(team)
    post(team, f"/plans/{plan_id}/submit")
    login(team, "owner1")
    assert post(team, f"/plans/{plan_id}/reject", {"note": "  "}).status_code == 400
    assert post(team, f"/plans/{plan_id}/reject", {"note": "الإيجار ناقص"}).status_code == 303
    plan = db.get_plan(plan_id, org_id=org)
    assert plan["status"] == "draft" and plan["submitted_by"] is None

    login(team, "acct")
    assert "الإيجار ناقص" in team.get(f"/plans/{plan_id}").text
    assert post(team, f"/plans/{plan_id}/lines", form_for([VAT, RENT])).status_code == 303


def test_approving_a_new_version_supersedes_the_old_one(team, db, org):
    first = new_draft(team)
    post(team, f"/plans/{first}/submit")
    login(team, "owner1")
    post(team, f"/plans/{first}/approve")

    login(team, "acct")
    second = new_draft(team)
    assert new_draft(team) == second  # only one version is worked on at a time
    assert db.get_plan(second, org_id=org)["version"] == 2
    post(team, f"/plans/{second}/lines", form_for([RENT, VAT]))
    post(team, f"/plans/{second}/submit")
    login(team, "owner2")
    post(team, f"/plans/{second}/approve")

    assert db.get_plan(first, org_id=org)["status"] == "superseded"
    assert db.get_active_plan(org)["id"] == second
    assert [p["version"] for p in db.list_plans(org)] == [2, 1]


def test_invalid_edits_keep_what_was_typed(team, db, org):
    plan_id = new_draft(team)
    bad = form_for([line("إيجار المستودع", "percent", 150)])
    response = post(team, f"/plans/{plan_id}/lines", bad)
    assert response.status_code == 400 and "إيجار المستودع" in response.text
    assert len(db.get_plan(plan_id, org_id=org)["lines"]) == 6  # nothing saved

    reordered = form_for([VAT, RENT])
    reordered["line_order"] = ["2", "1"]
    reordered["line_name"].append("")  # a blank row is ignored
    for key, value in (("line_basis", "fixed"), ("line_value", ""), ("line_protected", "0"), ("line_order", "3")):
        reordered[key].append(value)
    post(team, f"/plans/{plan_id}/lines", reordered)
    assert [ln["name"] for ln in db.get_plan(plan_id, org_id=org)["lines"]] == ["الإيجار", "الضريبة"]

    mismatched = {**form_for([RENT]), "line_order": []}
    assert post(team, f"/plans/{plan_id}/lines", mismatched).status_code == 400


@pytest.mark.parametrize("role", ["treasurer", "viewer"])
def test_other_roles_can_view_but_not_design(client, db, org, role):
    add_user(db, "acct", org, "accountant")
    add_user(db, "member", org, role)
    login(client, "acct")
    plan_id = new_draft(client)

    login(client, "member")
    assert client.get("/plans").status_code == 200
    assert client.get(f"/plans/{plan_id}").status_code == 200
    assert post(client, "/plans").status_code == 403
    assert post(client, f"/plans/{plan_id}/lines", form_for([RENT])).status_code == 403
    assert post(client, f"/plans/{plan_id}/submit").status_code == 403
    assert post(client, f"/plans/{plan_id}/approve").status_code == 403


def test_plans_are_private_to_their_organization(team, db, org):
    plan_id = new_draft(team)
    post(team, f"/plans/{plan_id}/submit")

    rival = db.create_organization("Rival Co")
    add_user(db, "rival", rival, "owner")
    login(team, "rival")
    assert team.get(f"/plans/{plan_id}").status_code == 404
    assert post(team, f"/plans/{plan_id}/approve").status_code == 404
    assert post(team, f"/plans/{plan_id}/reject", {"note": "x"}).status_code == 404
    assert plan_id not in team.get("/plans").text
    assert db.get_plan(plan_id, org_id=org)["status"] == "pending"


def test_plan_pages_preview_the_latest_period_and_ship_no_inline_scripts(team, db, org):
    create_entity(team, "SHOP-1")
    submit_run(team, "SHOP-1")
    plan_id = new_draft(team)
    post(team, f"/plans/{plan_id}/lines", form_for([VAT, RENT]))
    page = team.get(f"/plans/{plan_id}").text
    assert "معاينة على آخر بيانات فترة" in page and "15540.41" in page  # 13.0435% of 119142.98 collections
    for html in (page, team.get("/plans").text):
        assert not re.search(r"<script(?![^>]*\bsrc=)", html) and not re.search(r"\son\w+\s*=", html)


def test_api_snapshots_use_the_organizations_plan(client, db, org):
    add_user(db, "owner1", org, "owner")
    plan_id = db.create_draft_plan(org, normalize_lines([RENT]), "acct")
    db.submit_plan(plan_id, org, "acct")
    assert db.approve_plan(plan_id, org, "owner1")

    client.post("/api/v1/entities", json={"org_id": org, "id": "API-1", "name": "Shop"}, headers=API_HEADERS)
    body = {"period_start": "2026-07-01", "period_end": "2026-07-31",
            "inputs": {k: float(v) for k, v in MANUAL_RUN.items() if not k.startswith("period")}}
    snapshot = client.post("/api/v1/entities/API-1/snapshots", json=body, headers=API_HEADERS).json()
    assert snapshot["allocation"]["policy_id"] == "DP-v1"
    assert snapshot["allocation"]["allocations"][0]["name_ar"] == "الإيجار"


def test_a_refused_approval_leaves_the_current_version_active(db, org):
    first = db.create_draft_plan(org, normalize_lines([RENT]), "acct")
    db.submit_plan(first, org, "acct")
    assert db.approve_plan(first, org, "owner1")

    second = db.create_draft_plan(org, normalize_lines([VAT]), "owner1")
    db.submit_plan(second, org, "owner1")
    assert db.approve_plan(second, org, "owner1") is False  # own submission: the whole transaction rolls back
    assert db.get_active_plan(org)["id"] == first
    assert db.get_plan(second, org_id=org)["status"] == "pending"


def test_an_owner_who_edited_a_version_cannot_approve_it(team, db, org):
    login(team, "owner1")
    plan_id = new_draft(team)
    post(team, f"/plans/{plan_id}/lines", form_for([RENT, VAT]))
    login(team, "acct")
    post(team, f"/plans/{plan_id}/submit")  # someone else pressing "submit" doesn't make it their design

    login(team, "owner1")
    refused = post(team, f"/plans/{plan_id}/approve")
    assert refused.status_code == 403 and "مالك آخر" in refused.text
    assert db.approve_plan(plan_id, org, "owner1") is False
    assert post(team, f"/plans/{plan_id}/reject", {"note": "أراجعها لاحقًا"}).status_code == 303  # may send it back

    login(team, "acct")
    post(team, f"/plans/{plan_id}/submit")
    login(team, "owner2")
    assert post(team, f"/plans/{plan_id}/approve").status_code == 303
