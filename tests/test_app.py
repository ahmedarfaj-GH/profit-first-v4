import re
from io import BytesIO

import openpyxl
import pytest

from app.config import validate_config
from tests.conftest import TEST_LOGIN_PASSWORD

MANUAL_RUN = {
    "period_start": "2026-07-01", "period_end": "2026-08-31",
    "opening_cash_balance": "6850.37", "total_collections": "119142.98",
    "total_operating_expenses_paid": "59231.64", "payroll_due": "41231.64", "vat_due": "11384.9",
    "royalty_due": "0", "suppliers_due": "0", "other_short_term_due": "0",
    "operational_reserve_target": "20000",
}
API_HEADERS = {"X-API-Key": "k" * 40}


def create_entity(client, entity_id="SHOP-1", **extra):
    return client.post("/entities", data={"id": entity_id, "name": "Shop", "type": "franchisee", **extra},
                       follow_redirects=False)


def submit_run(client, entity_id="SHOP-1"):
    response = client.post(f"/entities/{entity_id}/runs", data=MANUAL_RUN, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[-1]


# --- authentication ---------------------------------------------------------
def test_pages_require_login(client):
    for path in ("/", "/hierarchy", "/runs", "/entities/X/new-run"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"


def test_login_rejects_wrong_password_and_non_ascii_username(client):
    assert client.post("/login", data={"username": "manager", "password": "nope"}).status_code == 401
    assert client.post("/login", data={"username": "مدير", "password": "nope"}).status_code == 401


def test_login_succeeds_and_sets_hardened_cookie(client):
    response = client.post("/login", data={"username": "manager", "password": TEST_LOGIN_PASSWORD},
                           follow_redirects=False)
    cookie = response.headers["set-cookie"].lower()
    assert response.status_code == 303 and "httponly" in cookie and "samesite=lax" in cookie


def test_repeated_failures_lock_the_login(client):
    for _ in range(5):
        assert client.post("/login", data={"username": "manager", "password": "bad"}).status_code == 401
    locked = client.post("/login", data={"username": "manager", "password": TEST_LOGIN_PASSWORD})
    assert locked.status_code == 429


def test_tampered_session_cookie_is_rejected(client):
    client.cookies.set("pf_session", "not-a-real-token")
    assert client.get("/hierarchy", follow_redirects=False).status_code == 303


# --- hardening --------------------------------------------------------------
def test_cross_origin_form_post_is_blocked(logged_in):
    response = logged_in.post("/entities", data={"id": "EVIL", "name": "x"},
                              headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert response.status_code == 403
    assert logged_in.get("/hierarchy").status_code == 200


def test_security_headers_are_present(client):
    headers = client.get("/login").headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"


def test_pages_ship_no_inline_scripts(logged_in):
    create_entity(logged_in)
    for path in ("/hierarchy", "/login", "/entities/SHOP-1/new-run"):
        html = logged_in.get(path).text
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), path
        assert not re.search(r"\son\w+\s*=", html), path


def test_production_config_fails_fast_when_weak(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "short")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        validate_config()


def test_health_endpoints(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/health/db").json()["database"] == "ok"


# --- entities ---------------------------------------------------------------
def test_entity_validation(logged_in):
    assert create_entity(logged_in, "bad id/../x").status_code == 400
    assert create_entity(logged_in, "SHOP-1").status_code == 303
    assert create_entity(logged_in, "SHOP-1", parent_id="SHOP-1").status_code == 400
    assert create_entity(logged_in, "SHOP-2", parent_id="ghost").status_code == 400
    assert create_entity(logged_in, "SHOP-3", currency="12").status_code == 400


# --- runs and review --------------------------------------------------------
def test_manual_run_and_review_flow(logged_in):
    create_entity(logged_in)
    run_id = submit_run(logged_in)
    page = logged_in.get(f"/runs/{run_id}").text
    assert "-5854.83" in page and "FUNDING_GAP" in page

    assert logged_in.post(f"/runs/{run_id}/review", data={"decision": "MAYBE", "decided_by": "Sara"}).status_code == 400
    protected = logged_in.post(f"/runs/{run_id}/review", data={
        "decision": "MODIFY", "decided_by": "Sara", "override_bucket": "payroll", "override_value": "100"})
    assert protected.status_code == 400
    approved = logged_in.post(f"/runs/{run_id}/review", data={"decision": "APPROVE", "decided_by": "Sara"},
                              follow_redirects=False)
    assert approved.status_code == 303
    again = logged_in.post(f"/runs/{run_id}/review", data={"decision": "REJECT", "decided_by": "Omar"})
    assert again.status_code == 409
    assert "Sara (manager)" in logged_in.get(f"/runs/{run_id}").text


@pytest.mark.parametrize("field,value", [("total_collections", "-1"), ("vat_due", "nan"), ("payroll_due", "inf"),
                                          ("period_end", "2026-06-30")])
def test_invalid_manual_inputs_are_rejected(logged_in, field, value):
    create_entity(logged_in)
    response = logged_in.post("/entities/SHOP-1/runs", data={**MANUAL_RUN, field: value})
    assert response.status_code == 400
    assert logged_in.get("/runs").text.count("عرض") == 0


def test_excel_upload_creates_a_run(logged_in):
    from app.engine.xlsx_template import build_template_workbook

    create_entity(logged_in)
    wb = openpyxl.load_workbook(build_template_workbook("Shop"))
    values = {2: "01/07/2026", 3: "31/07/2026", 6: 1000, 7: 5000, 8: 2000, 10: 500, 11: 100, 14: 300}
    for row in wb.active.iter_rows(min_row=4):
        if row[0].value in values:
            row[4].value = values[row[0].value]
    buf = BytesIO()
    wb.save(buf)

    response = logged_in.post("/entities/SHOP-1/runs/upload", files={"file": ("data.xlsx", buf.getvalue())},
                              follow_redirects=False)
    assert response.status_code == 303
    assert "3100.00" in logged_in.get(response.headers["location"]).text  # 1000+5000-2000-500-100-300

    bad = logged_in.post("/entities/SHOP-1/runs/upload", files={"file": ("data.xlsx", b"junk")})
    assert bad.status_code == 400


# --- JSON API ---------------------------------------------------------------
def test_api_requires_the_key(client):
    assert client.get("/api/v1/entities/X/latest").status_code == 401
    assert client.get("/api/v1/entities/X/latest", headers={"X-API-Key": "wrong"}).status_code == 401


def test_key_comparison_survives_non_ascii_input():
    from app.auth import _safe_equal

    assert _safe_equal("مفتاح", "مفتاح") is True
    assert _safe_equal("مفتاح", "key") is False


def test_api_full_flow_and_validation(client):
    post = lambda path, body: client.post(f"/api/v1{path}", json=body, headers=API_HEADERS)  # noqa: E731
    assert post("/entities", {"id": "bad/id", "name": "x"}).status_code == 422
    assert post("/entities", {"id": "API-1", "name": "Api shop"}).status_code == 200
    assert post("/entities", {"id": "API-1", "name": "Api shop", "parent_id": "API-1"}).status_code == 400

    body = {"period_start": "2026-07-01", "period_end": "2026-07-31", "inputs": {"opening_cash_balance": 1000}}
    # httpx refuses to serialise Infinity, so send it as raw JSON text.
    raw = '{"period_start":"2026-07-01","period_end":"2026-07-31","inputs":{"opening_cash_balance":Infinity}}'
    infinite = client.post("/api/v1/entities/API-1/snapshots", content=raw,
                           headers={**API_HEADERS, "Content-Type": "application/json"})
    assert infinite.status_code == 422 and "Infinity" not in infinite.text and "inf" not in infinite.text.lower().replace("info", "")
    assert post("/entities/API-1/snapshots", {**body, "period_end": "2026-06-01"}).status_code == 422
    snapshot = post("/entities/API-1/snapshots", body)
    assert snapshot.status_code == 200
    run_id = snapshot.json()["run_id"]

    review = {"decision": "APPROVE", "decided_by": "auditor"}
    assert post(f"/runs/{run_id}/review", {**review, "override": {"ghost": 1}}).status_code == 400
    assert post(f"/runs/{run_id}/review", review).status_code == 200
    assert post(f"/runs/{run_id}/review", review).status_code == 409
