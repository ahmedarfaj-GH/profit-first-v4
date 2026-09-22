import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.auth import (
    SESSION_COOKIE_NAME, check_login, create_session_token, login_throttle, verify_session_token,
)
from app.config import is_production
from app.engine.allocation_engine import load_policy, run_allocation, validate_override
from app.engine.liquidity_engine import compute_liquidity
from app.engine.xlsx_template import TemplateParseError, build_template_workbook, parse_intake_workbook
from app.validation import is_finite_non_negative, is_valid_entity_id, validate_period

VALID_DECISIONS = {"APPROVE", "MODIFY", "REJECT"}
MAX_AMOUNT = 1_000_000_000_000  # sanity ceiling for any single monetary figure

MAX_UPLOAD_BYTES = 2 * 1024 * 1024  # 2 MB is generous for this template; reject anything larger

class _Templates(Jinja2Templates):
    """Keeps the `TemplateResponse(name, context)` call style while using Starlette's
    current (request-first) signature underneath."""

    def TemplateResponse(self, name, context, status_code=200, headers=None, media_type=None, background=None):
        return super().TemplateResponse(context["request"], name, context, status_code, headers, media_type, background)


router = APIRouter()
templates = _Templates(directory=str(Path(__file__).resolve().parent / "templates"))

FIELD_LABELS_AR = {
    "opening_cash_balance": "الرصيد الافتتاحي",
    "total_collections": "إجمالي التحصيلات",
    "total_operating_expenses_paid": "المصروفات التشغيلية المدفوعة",
    "payroll_due": "الرواتب المستحقة",
    "vat_due": "ضريبة القيمة المضافة المستحقة",
    "royalty_due": "الرويالتي المستحقة",
    "suppliers_due": "مستحقات الموردين",
    "other_short_term_due": "التزامات قصيرة الأجل أخرى",
    "operational_reserve_target": "الاحتياطي التشغيلي المستهدف",
}

ENTITY_TYPE_LABELS_AR = {
    "franchisor": "مانح الامتياز",
    "master_franchisee": "صاحب امتياز رئيسي",
    "franchisee": "صاحب امتياز / فرع",
}


def current_user(request: Request) -> Optional[str]:
    return verify_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def _new_run_id(entity_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{entity_id}-{ts}-{uuid.uuid4().hex[:6].upper()}"


@router.get("/")
def root(request: Request):
    return RedirectResponse("/hierarchy" if current_user(request) else "/login", status_code=303)


@router.get("/login")
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/hierarchy", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
def login_submit(request: Request, username: str = Form(..., max_length=200), password: str = Form(..., max_length=1000)):
    client = f"ip:{request.client.host if request.client else 'unknown'}"
    account = f"user:{username.strip().lower()}"

    if login_throttle.is_blocked(client, account):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "محاولات دخول فاشلة كثيرة — انتظر ١٥ دقيقة ثم حاول مجددًا"},
            status_code=429,
        )

    if check_login(username, password):
        login_throttle.reset(client, account)
        token = create_session_token(username.strip())
        resp = RedirectResponse("/hierarchy", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE_NAME, token, httponly=True, samesite="lax",
            secure=(is_production() or request.url.scheme == "https"), max_age=8 * 3600,
        )
        return resp

    login_throttle.record_failure(client, account)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "بيانات الدخول غير صحيحة"}, status_code=401
    )


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@router.get("/hierarchy")
def hierarchy_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    entities = db.list_entities()
    latest = db.latest_runs_by_entity()
    return templates.TemplateResponse(
        "hierarchy.html",
        {
            "request": request, "user": user, "entities": entities, "latest": latest,
            "entity_type_labels": ENTITY_TYPE_LABELS_AR,
        },
    )


@router.get("/runs")
def all_runs_page(request: Request, entity_id: str = ""):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    entities = {e["id"]: e for e in db.list_entities()}
    runs = db.list_runs_for_entity(entity_id) if entity_id else db.list_all_runs()
    reviews = db.reviews_by_run_ids([r["run_id"] for r in runs])

    return templates.TemplateResponse(
        "runs_list.html",
        {
            "request": request, "user": user, "runs": runs, "entities": entities,
            "reviews": reviews, "selected_entity_id": entity_id,
        },
    )


@router.post("/entities")
def create_entity_form(
    request: Request,
    id: str = Form(...),
    name: str = Form(...),
    type: str = Form("franchisee"),
    type_other: str = Form(""),
    parent_id: str = Form(""),
    currency: str = Form("SAR"),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if type == "__other__":
        type = type_other.strip()
    id, name, type = id.strip(), name.strip(), (type.strip() or "franchisee")
    parent_id = parent_id.strip() or None
    currency = (currency.strip() or "SAR").upper()

    if not is_valid_entity_id(id):
        raise HTTPException(status_code=400, detail="المعرّف: حروف إنجليزية وأرقام و- و_ فقط (حتى 64 خانة)")
    if not name or len(name) > 200 or len(type) > 50:
        raise HTTPException(status_code=400, detail="الاسم مطلوب (حتى 200 خانة) والنوع حتى 50 خانة")
    if not (len(currency) == 3 and currency.isalpha()):
        raise HTTPException(status_code=400, detail="العملة رمز من 3 حروف مثل SAR")
    if parent_id and not db.get_entity(parent_id):
        raise HTTPException(status_code=400, detail=f"Unknown parent_id: {parent_id}")
    if db.would_create_cycle(id, parent_id):
        raise HTTPException(status_code=400, detail="لا يمكن جعل الكيان تابعًا لنفسه أو لأحد فروعه")

    db.upsert_entity(id, name, type, parent_id, currency)
    return RedirectResponse("/hierarchy", status_code=303)


@router.get("/entities/{entity_id}/new-run")
def new_run_page(request: Request, entity_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    entity = db.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return templates.TemplateResponse(
        "intake.html",
        {"request": request, "user": user, "entity": entity, "fields": FIELD_LABELS_AR, "error": None},
    )


@router.get("/entities/{entity_id}/template.xlsx")
def download_template(request: Request, entity_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    entity = db.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    buf = build_template_workbook(entity_name=entity.name)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="profit-first-intake-{entity_id}.xlsx"'},
    )


@router.post("/entities/{entity_id}/runs/upload")
async def submit_run_from_upload(request: Request, entity_id: str, file: UploadFile = File(...)):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    entity = db.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    def error_page(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            "intake.html",
            {"request": request, "user": user, "entity": entity, "fields": FIELD_LABELS_AR, "error": message},
            status_code=status_code,
        )

    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        return error_page("الملف يجب أن يكون بصيغة xlsx/xlsm — استخدم القالب الرسمي.")

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return error_page("حجم الملف أكبر من الحد المسموح (2 ميجا).")

    try:
        parsed = parse_intake_workbook(raw, expected_entity_id=entity_id)
    except TemplateParseError as e:
        return error_page(str(e))

    values = parsed["inputs"]
    try:
        validate_period(parsed["period_start"], parsed["period_end"])
    except ValueError as e:
        return error_page(str(e))
    if any(not is_finite_non_negative(v) or v > MAX_AMOUNT for v in values.values()):
        return error_page("الملف يحتوي قيمة مالية غير صالحة (سالبة أو غير محدودة أو ضخمة جدًا)")

    liquidity = compute_liquidity(values)
    policy = load_policy()
    allocation = run_allocation(values, liquidity, policy)

    run_id = _new_run_id(entity_id)
    dataset_hash = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    db.insert_run(
        run_id, entity_id, parsed["period_start"], parsed["period_end"], dataset_hash,
        values, liquidity, allocation, policy["policy_id"],
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.post("/entities/{entity_id}/runs")
def submit_run(
    request: Request,
    entity_id: str,
    period_start: str = Form(...),
    period_end: str = Form(...),
    opening_cash_balance: float = Form(0),
    total_collections: float = Form(0),
    total_operating_expenses_paid: float = Form(0),
    payroll_due: float = Form(0),
    vat_due: float = Form(0),
    royalty_due: float = Form(0),
    suppliers_due: float = Form(0),
    other_short_term_due: float = Form(0),
    operational_reserve_target: float = Form(0),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    entity = db.get_entity(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    values = {
        "opening_cash_balance": opening_cash_balance,
        "total_collections": total_collections,
        "total_operating_expenses_paid": total_operating_expenses_paid,
        "payroll_due": payroll_due,
        "vat_due": vat_due,
        "royalty_due": royalty_due,
        "suppliers_due": suppliers_due,
        "other_short_term_due": other_short_term_due,
        "operational_reserve_target": operational_reserve_target,
    }
    def intake_error(message: str):
        return templates.TemplateResponse(
            "intake.html",
            {"request": request, "user": user, "entity": entity, "fields": FIELD_LABELS_AR, "error": message},
            status_code=400,
        )

    try:
        validate_period(period_start, period_end)
    except ValueError as e:
        return intake_error(str(e))
    for key, value in values.items():
        if not is_finite_non_negative(value) or value > MAX_AMOUNT:
            return intake_error(
                f"القيمة غير صالحة لحقل \"{FIELD_LABELS_AR.get(key, key)}\": يجب أن تكون رقمًا غير سالب"
            )

    liquidity = compute_liquidity(values)
    policy = load_policy()
    allocation = run_allocation(values, liquidity, policy)

    run_id = _new_run_id(entity_id)
    dataset_hash = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    db.insert_run(run_id, entity_id, period_start, period_end, dataset_hash, values, liquidity, allocation, policy["policy_id"])

    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}")
def run_result_page(request: Request, run_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    entity = db.get_entity(run["entity_id"])
    review = db.get_review(run_id)
    return templates.TemplateResponse(
        "result.html",
        {"request": request, "user": user, "run": run, "entity": entity, "review": review, "error": None},
    )


@router.post("/runs/{run_id}/review")
def submit_review_form(
    request: Request,
    run_id: str,
    decision: str = Form(...),
    decided_by: str = Form(..., max_length=100),
    justification: str = Form("", max_length=2000),
    override_bucket: str = Form(""),
    override_value: str = Form(""),
):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    entity = db.get_entity(run["entity_id"])

    def result_error(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            "result.html",
            {"request": request, "user": user, "run": run, "entity": entity, "review": None, "error": message},
            status_code=status_code,
        )

    if decision not in VALID_DECISIONS:
        return result_error("القرار يجب أن يكون Approve أو Modify أو Reject")
    if not decided_by.strip():
        return result_error("اسم المُراجِع مطلوب")

    override = None
    if override_bucket.strip() and override_value.strip():
        try:
            override = {override_bucket.strip(): float(override_value)}
        except ValueError:
            return result_error("قيمة التعديل غير صالحة — يجب أن تكون رقمًا")

    if override:
        try:
            validate_override(override, run["allocation"]["allocations"], justification)
        except ValueError as e:
            return result_error(str(e))

    # The typed name says who decided; the login says which account recorded it.
    recorded_by = f"{decided_by.strip()} ({user})"
    if not db.save_review(run_id, decision, override, justification or None, recorded_by, "PENDING"):
        return result_error("تمت مراجعة هذه العملية مسبقًا — القرار الأول نهائي ولا يمكن استبداله", 409)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/entities/{entity_id}/network-summary")
def network_summary_page(request: Request, entity_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    summary = db.build_network_summary(entity_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return templates.TemplateResponse(
        "network_summary.html", {"request": request, "user": user, "summary": summary}
    )
