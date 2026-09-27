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
    SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, check_login, create_session_token, hash_password,
    is_valid_username, login_throttle, normalize_username, password_problem, user_from_session_token,
    verify_password,
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


ROLE_LABELS_AR = {
    db.PLATFORM_ADMIN_ROLE: "أدمن المنصة",
    "owner": "المالك",
    "accountant": "المحاسب",
    "treasurer": "مدير الخزينة",
    "viewer": "مشاهد",
}
CAN_EDIT_DATA = {"owner", "accountant"}   # entities and period data
CAN_REVIEW = {"owner", "treasurer"}       # the person who enters data doesn't approve it
CAN_MANAGE_TEAM = {"owner"}


def _redirect(location: str) -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": location})


def current_user(request: Request) -> Optional[dict]:
    return user_from_session_token(request.cookies.get(SESSION_COOKIE_NAME))


def _home(user: dict) -> str:
    return "/admin" if user["role"] == db.PLATFORM_ADMIN_ROLE else "/hierarchy"


def require_user(request: Request, *, pending_password_ok: bool = False) -> dict:
    user = current_user(request)
    if not user:
        raise _redirect("/login")
    if user["must_change_password"] and not pending_password_ok:
        raise _redirect("/account/password")
    return user


def require_org_user(request: Request, roles: set[str] | None = None) -> dict:
    """A signed-in member of an organization; every page below works inside
    that organization only."""
    user = require_user(request)
    if user["role"] == db.PLATFORM_ADMIN_ROLE:
        raise _redirect("/admin")
    if roles is not None and user["role"] not in roles:
        raise HTTPException(status_code=403, detail="ليس لديك صلاحية لهذا الإجراء")
    return user


def require_platform_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != db.PLATFORM_ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="هذه الصفحة لأدمن المنصة فقط")
    return user


def _perms(user: Optional[dict]) -> dict:
    role = user["role"] if user else None
    return {
        "edit_data": role in CAN_EDIT_DATA,
        "review": role in CAN_REVIEW,
        "manage_team": role in CAN_MANAGE_TEAM,
        "platform_admin": role == db.PLATFORM_ADMIN_ROLE,
    }


def render(request: Request, name: str, user: Optional[dict], status_code: int = 200, **context):
    return templates.TemplateResponse(
        name,
        {"request": request, "user": user, "perms": _perms(user), "role_labels": ROLE_LABELS_AR, **context},
        status_code=status_code,
    )


def _signed_in_redirect(request: Request, user: dict, location: str) -> RedirectResponse:
    resp = RedirectResponse(location, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE_NAME, create_session_token(user), httponly=True, samesite="lax",
        secure=(is_production() or request.url.scheme == "https"), max_age=SESSION_MAX_AGE_SECONDS,
    )
    return resp


def _get_org_entity(entity_id: str, user: dict) -> dict:
    entity = db.get_entity(entity_id, org_id=user["org_id"])
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


def _get_org_run(run_id: str, user: dict) -> dict:
    run = db.get_run(run_id, org_id=user["org_id"])
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _new_run_id(entity_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{entity_id}-{ts}-{uuid.uuid4().hex[:6].upper()}"


@router.get("/")
def root(request: Request):
    user = current_user(request)
    return RedirectResponse(_home(user) if user else "/login", status_code=303)


@router.get("/login")
def login_page(request: Request):
    user = current_user(request)
    if user:
        return RedirectResponse(_home(user), status_code=303)
    return render(request, "login.html", None, error=None)


@router.post("/login")
def login_submit(request: Request, username: str = Form(..., max_length=200), password: str = Form(..., max_length=1000)):
    client = f"ip:{request.client.host if request.client else 'unknown'}"
    account = f"user:{normalize_username(username)}"

    if login_throttle.is_blocked(client, account):
        return render(request, "login.html", None, status_code=429,
                      error="محاولات دخول فاشلة كثيرة — انتظر ١٥ دقيقة ثم حاول مجددًا")

    user = check_login(username, password)
    if user:
        login_throttle.reset(client, account)
        return _signed_in_redirect(request, user, "/account/password" if user["must_change_password"] else _home(user))

    login_throttle.record_failure(client, account)
    return render(request, "login.html", None, status_code=401, error="بيانات الدخول غير صحيحة")


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


# --- own account --------------------------------------------------------------
@router.get("/account/password")
def password_page(request: Request):
    user = require_user(request, pending_password_ok=True)
    return render(request, "password.html", user, error=None)


@router.post("/account/password")
def password_submit(
    request: Request,
    current_password: str = Form(..., max_length=1000),
    new_password: str = Form(..., max_length=1000),
    confirm_password: str = Form(..., max_length=1000),
):
    user = require_user(request, pending_password_ok=True)

    def error(message: str, status_code: int = 400):
        return render(request, "password.html", user, status_code=status_code, error=message)

    if not verify_password(user, current_password):
        return error("كلمة المرور الحالية غير صحيحة", 401)
    problem = password_problem(new_password)
    if problem:
        return error(problem)
    if new_password != confirm_password:
        return error("كلمتا المرور الجديدتان غير متطابقتين")
    if new_password == current_password:
        return error("اختر كلمة مرور مختلفة عن الحالية")

    db.set_password(user["id"], hash_password(new_password), must_change_password=False)
    # Other sessions are now revoked; this one gets a fresh token.
    return _signed_in_redirect(request, db.get_user(user["id"]), _home(user))


@router.get("/hierarchy")
def hierarchy_page(request: Request):
    user = require_org_user(request)
    entities = db.list_entities(org_id=user["org_id"])
    latest = db.latest_runs_by_entity(org_id=user["org_id"])
    return render(request, "hierarchy.html", user, entities=entities, latest=latest,
                  entity_type_labels=ENTITY_TYPE_LABELS_AR)


@router.get("/runs")
def all_runs_page(request: Request, entity_id: str = ""):
    user = require_org_user(request)
    org_id = user["org_id"]

    entities = db.all_entities_map(org_id=org_id)
    runs = db.list_runs_for_entity(entity_id, org_id=org_id) if entity_id else db.list_all_runs(org_id=org_id)
    reviews = db.reviews_by_run_ids([r["run_id"] for r in runs])

    return render(request, "runs_list.html", user, runs=runs, entities=entities, reviews=reviews,
                  selected_entity_id=entity_id)


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
    user = require_org_user(request, CAN_EDIT_DATA)
    org_id = user["org_id"]

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
    if parent_id and not db.get_entity(parent_id, org_id=org_id):
        raise HTTPException(status_code=400, detail=f"Unknown parent_id: {parent_id}")
    if db.would_create_cycle(id, parent_id, org_id=org_id):
        raise HTTPException(status_code=400, detail="لا يمكن جعل الكيان تابعًا لنفسه أو لأحد فروعه")

    if not db.upsert_entity(id, name, type, parent_id, currency, org_id=org_id):
        raise HTTPException(status_code=400, detail="هذا المعرّف مستخدم — اختر معرّفًا آخر")
    return RedirectResponse("/hierarchy", status_code=303)


@router.get("/entities/{entity_id}/new-run")
def new_run_page(request: Request, entity_id: str):
    user = require_org_user(request, CAN_EDIT_DATA)
    entity = _get_org_entity(entity_id, user)
    return render(request, "intake.html", user, entity=entity, fields=FIELD_LABELS_AR, error=None)


@router.get("/entities/{entity_id}/template.xlsx")
def download_template(request: Request, entity_id: str):
    user = require_org_user(request, CAN_EDIT_DATA)
    entity = _get_org_entity(entity_id, user)
    buf = build_template_workbook(entity_name=entity["name"])
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="profit-first-intake-{entity_id}.xlsx"'},
    )


@router.post("/entities/{entity_id}/runs/upload")
async def submit_run_from_upload(request: Request, entity_id: str, file: UploadFile = File(...)):
    user = require_org_user(request, CAN_EDIT_DATA)
    entity = _get_org_entity(entity_id, user)

    def error_page(message: str, status_code: int = 400):
        return render(request, "intake.html", user, status_code=status_code,
                      entity=entity, fields=FIELD_LABELS_AR, error=message)

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
    user = require_org_user(request, CAN_EDIT_DATA)
    entity = _get_org_entity(entity_id, user)

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
        return render(request, "intake.html", user, status_code=400,
                      entity=entity, fields=FIELD_LABELS_AR, error=message)

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
    user = require_org_user(request)
    run = _get_org_run(run_id, user)
    entity = _get_org_entity(run["entity_id"], user)
    review = db.get_review(run_id)
    return render(request, "result.html", user, run=run, entity=entity, review=review, error=None)


@router.post("/runs/{run_id}/review")
def submit_review_form(
    request: Request,
    run_id: str,
    decision: str = Form(...),
    justification: str = Form("", max_length=2000),
    override_bucket: str = Form(""),
    override_value: str = Form(""),
):
    user = require_org_user(request, CAN_REVIEW)
    run = _get_org_run(run_id, user)
    entity = _get_org_entity(run["entity_id"], user)

    def result_error(message: str, status_code: int = 400):
        return render(request, "result.html", user, status_code=status_code,
                      run=run, entity=entity, review=None, error=message)

    if decision not in VALID_DECISIONS:
        return result_error("القرار يجب أن يكون Approve أو Modify أو Reject")

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

    # Each person has their own account, so the login itself says who decided.
    if not db.save_review(run_id, decision, override, justification or None, user["username"], "PENDING"):
        return result_error("تمت مراجعة هذه العملية مسبقًا — القرار الأول نهائي ولا يمكن استبداله", 409)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/entities/{entity_id}/network-summary")
def network_summary_page(request: Request, entity_id: str):
    user = require_org_user(request)
    summary = db.build_network_summary(entity_id, org_id=user["org_id"])
    if summary is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return render(request, "network_summary.html", user, summary=summary)


# --- team (organization owner) ------------------------------------------------------
def _team_page(request: Request, user: dict, error: Optional[str] = None, notice: Optional[str] = None,
               status_code: int = 200):
    return render(request, "team.html", user, status_code=status_code, error=error, notice=notice,
                  members=db.list_users(org_id=user["org_id"]), org_roles=db.ORG_ROLES)


def _new_account_problem(username: str, password: str) -> Optional[str]:
    if not is_valid_username(username):
        return "اسم المستخدم: 3 إلى 64 خانة من حروف إنجليزية صغيرة وأرقام و . _ @ - (يصلح البريد الإلكتروني)"
    problem = password_problem(password)
    if problem:
        return problem
    if db.get_user_by_username(username):
        return "اسم المستخدم مستخدم — اختر اسمًا آخر"
    return None


def _get_team_member(user_id: str, owner: dict) -> dict:
    member = db.get_user(user_id)
    if not member or member["org_id"] != owner["org_id"]:
        raise HTTPException(status_code=404, detail="User not found")
    if member["id"] == owner["id"]:
        raise HTTPException(status_code=400, detail="لا يمكنك تعديل حسابك من هنا — استخدم صفحة كلمة المرور")
    return member


@router.get("/team")
def team_page(request: Request):
    user = require_org_user(request, CAN_MANAGE_TEAM)
    return _team_page(request, user)


@router.post("/team/users")
def team_add_user(
    request: Request,
    username: str = Form(..., max_length=200),
    role: str = Form(...),
    password: str = Form(..., max_length=1000),
):
    user = require_org_user(request, CAN_MANAGE_TEAM)
    username = normalize_username(username)
    if role not in db.ORG_ROLES:
        return _team_page(request, user, error="الدور غير صالح", status_code=400)
    problem = _new_account_problem(username, password)
    if problem:
        return _team_page(request, user, error=problem, status_code=400)
    if not db.create_user(username, hash_password(password), org_id=user["org_id"], role=role):
        return _team_page(request, user, error="اسم المستخدم مستخدم — اختر اسمًا آخر", status_code=400)
    return _team_page(request, user, notice=f"أُضيف {username}. سيُطلب منه تغيير كلمة المرور المؤقتة عند أول دخول.")


@router.post("/team/users/{user_id}/active")
def team_set_active(request: Request, user_id: str, active: str = Form(...)):
    user = require_org_user(request, CAN_MANAGE_TEAM)
    member = _get_team_member(user_id, user)
    db.set_user_active(member["id"], active == "1")
    return RedirectResponse("/team", status_code=303)


@router.post("/team/users/{user_id}/password")
def team_reset_password(request: Request, user_id: str, password: str = Form(..., max_length=1000)):
    user = require_org_user(request, CAN_MANAGE_TEAM)
    member = _get_team_member(user_id, user)
    problem = password_problem(password)
    if problem:
        return _team_page(request, user, error=problem, status_code=400)
    db.set_password(member["id"], hash_password(password), must_change_password=True)
    return _team_page(request, user, notice=f"أُعيد تعيين كلمة مرور {member['username']} وأُنهيت جلساته المفتوحة.")


# --- platform admin -----------------------------------------------------------------
def _admin_page(request: Request, user: dict, error: Optional[str] = None, notice: Optional[str] = None,
                status_code: int = 200):
    return render(request, "admin.html", user, status_code=status_code, error=error, notice=notice,
                  organizations=db.list_organizations())


@router.get("/admin")
def admin_page(request: Request):
    user = require_platform_admin(request)
    return _admin_page(request, user)


@router.post("/admin/organizations")
def admin_create_organization(
    request: Request,
    name: str = Form(..., max_length=200),
    owner_username: str = Form(..., max_length=200),
    owner_password: str = Form(..., max_length=1000),
):
    user = require_platform_admin(request)
    name, owner_username = name.strip(), normalize_username(owner_username)
    if not name:
        return _admin_page(request, user, error="اسم المنشأة مطلوب", status_code=400)
    problem = _new_account_problem(owner_username, owner_password)
    if problem:
        return _admin_page(request, user, error=problem, status_code=400)
    org_id = db.create_organization(name)
    if not db.create_user(owner_username, hash_password(owner_password), org_id=org_id, role="owner"):
        return _admin_page(request, user, error="اسم المستخدم مستخدم — أضف مالكًا للمنشأة باسم آخر", status_code=400)
    return _admin_page(request, user, notice=f"أُنشئت المنشأة «{name}» ومالكها {owner_username}.")


@router.post("/admin/organizations/{org_id}/owners")
def admin_add_owner(
    request: Request,
    org_id: str,
    username: str = Form(..., max_length=200),
    password: str = Form(..., max_length=1000),
):
    user = require_platform_admin(request)
    if not db.get_organization(org_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    username = normalize_username(username)
    problem = _new_account_problem(username, password)
    if problem:
        return _admin_page(request, user, error=problem, status_code=400)
    if not db.create_user(username, hash_password(password), org_id=org_id, role="owner"):
        return _admin_page(request, user, error="اسم المستخدم مستخدم — اختر اسمًا آخر", status_code=400)
    return _admin_page(request, user, notice=f"أُضيف {username} مالكًا للمنشأة.")


@router.post("/admin/organizations/{org_id}/status")
def admin_set_organization_status(request: Request, org_id: str, status: str = Form(...)):
    require_platform_admin(request)
    if status not in ("active", "suspended"):
        raise HTTPException(status_code=400, detail="Invalid status")
    if not db.get_organization(org_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    db.set_organization_status(org_id, status)
    return RedirectResponse("/admin", status_code=303)
