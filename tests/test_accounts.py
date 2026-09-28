import pytest

from tests.conftest import TEST_LOGIN_PASSWORD, add_user, login, use_session
from tests.test_app import MANUAL_RUN, create_entity, submit_run

NEW_PASSWORD = "a-brand-new-passphrase"


def post(client, path, data):
    return client.post(path, data=data, follow_redirects=False)


# --- platform admin -----------------------------------------------------------
def test_first_platform_admin_comes_from_the_environment_once(client, db, monkeypatch):
    from app.auth import bootstrap_platform_admin, hash_password

    admin = db.get_user_by_username("manager")
    assert admin["role"] == db.PLATFORM_ADMIN_ROLE and not admin["must_change_password"]
    monkeypatch.setenv("UI_LOGIN_PASSWORD_HASH", hash_password("some-other-password"))
    bootstrap_platform_admin()
    assert db.get_user_by_username("manager")["password_hash"] == admin["password_hash"]


def test_admin_creates_an_organization_whose_owner_must_change_the_password(admin, db):
    bad = post(admin, "/admin/organizations", {"name": "Cafe", "owner_username": "x", "owner_password": "short"})
    assert bad.status_code == 400
    assert post(admin, "/admin/organizations", {
        "name": "Cafe", "owner_username": "Owner@Cafe.sa", "owner_password": TEST_LOGIN_PASSWORD}).status_code == 200
    taken = post(admin, "/admin/organizations", {
        "name": "Cafe 2", "owner_username": "owner@cafe.sa", "owner_password": TEST_LOGIN_PASSWORD})
    assert taken.status_code == 400

    login(admin, "owner@cafe.sa")
    assert admin.get("/hierarchy", follow_redirects=False).headers["location"] == "/account/password"
    wrong = post(admin, "/account/password", {
        "current_password": "nope", "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert wrong.status_code == 401
    mismatch = post(admin, "/account/password", {
        "current_password": TEST_LOGIN_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": "other-other-other"})
    assert mismatch.status_code == 400
    changed = post(admin, "/account/password", {
        "current_password": TEST_LOGIN_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert changed.status_code == 303 and changed.headers["location"] == "/hierarchy"
    assert admin.get("/hierarchy").status_code == 200

    admin.cookies.clear()
    assert post(admin, "/login", {"username": "owner@cafe.sa", "password": TEST_LOGIN_PASSWORD}).status_code == 401
    login(admin, "owner@cafe.sa", NEW_PASSWORD)


def test_admin_and_organization_areas_are_separate(admin, db, org):
    assert admin.get("/hierarchy", follow_redirects=False).headers["location"] == "/admin"
    assert admin.get("/runs", follow_redirects=False).headers["location"] == "/admin"
    add_user(db, "owner1", org, "owner")
    login(admin, "owner1")
    assert admin.get("/admin").status_code == 403
    assert post(admin, f"/admin/organizations/{org}/status", {"status": "suspended"}).status_code == 403


def test_suspending_an_organization_locks_out_its_members(admin, db, org):
    add_user(db, "owner1", org, "owner")
    admin_token = admin.cookies.get("pf_session")
    owner_token = login(admin, "owner1")

    use_session(admin, admin_token)
    assert post(admin, f"/admin/organizations/{org}/status", {"status": "suspended"}).status_code == 303
    use_session(admin, owner_token)
    assert admin.get("/hierarchy", follow_redirects=False).headers["location"] == "/login"
    admin.cookies.clear()
    assert post(admin, "/login", {"username": "owner1", "password": TEST_LOGIN_PASSWORD}).status_code == 401

    use_session(admin, admin_token)
    post(admin, f"/admin/organizations/{org}/status", {"status": "active"})
    login(admin, "owner1")


def test_admin_can_add_an_owner_to_an_existing_organization(admin, db, org):
    assert post(admin, f"/admin/organizations/{org}/owners",
                {"username": "second-owner", "password": TEST_LOGIN_PASSWORD}).status_code == 200
    assert db.get_user_by_username("second-owner")["org_id"] == org
    assert post(admin, "/admin/organizations/ORG-NOPE/owners",
                {"username": "ghost-owner", "password": TEST_LOGIN_PASSWORD}).status_code == 404


# --- isolation between organizations -------------------------------------------------
def test_an_organization_cannot_see_or_touch_another_organizations_data(logged_in, db):
    create_entity(logged_in, "SHOP-1")
    run_id = submit_run(logged_in, "SHOP-1")

    rival = db.create_organization("Rival Co")
    add_user(db, "rival", rival, "owner")
    login(logged_in, "rival")

    assert "SHOP-1" not in logged_in.get("/hierarchy").text
    assert run_id not in logged_in.get("/runs").text
    assert run_id not in logged_in.get("/runs?entity_id=SHOP-1").text
    assert logged_in.get(f"/runs/{run_id}").status_code == 404
    assert logged_in.get("/entities/SHOP-1/new-run").status_code == 404
    assert logged_in.get("/entities/SHOP-1/template.xlsx").status_code == 404
    assert logged_in.get("/entities/SHOP-1/network-summary").status_code == 404
    assert post(logged_in, "/entities/SHOP-1/runs", MANUAL_RUN).status_code == 404
    assert post(logged_in, f"/runs/{run_id}/review", {"decision": "APPROVE"}).status_code == 404
    assert create_entity(logged_in, "SHOP-1").status_code == 400          # can't take over the id
    assert create_entity(logged_in, "MINE", parent_id="SHOP-1").status_code == 400  # nor hang off it
    assert db.get_review(run_id) is None
    assert db.get_entity("SHOP-1", org_id=None)["org_id"] != rival


# --- roles inside an organization ------------------------------------------------------
@pytest.mark.parametrize("role,can_edit,can_review,can_manage", [
    ("owner", True, True, True),
    ("accountant", True, False, False),
    ("treasurer", False, True, False),
    ("viewer", False, False, False),
])
def test_role_permissions(client, db, org, role, can_edit, can_review, can_manage):
    add_user(db, "owner1", org, "owner")
    login(client, "owner1")
    create_entity(client, "SHOP-1")
    run_id = submit_run(client, "SHOP-1")

    add_user(db, "member", org, role)
    login(client, "member")
    assert client.get("/hierarchy").status_code == 200
    assert client.get(f"/runs/{run_id}").status_code == 200

    def allowed(response):
        return response.status_code != 403

    assert allowed(create_entity(client, "SHOP-2")) is can_edit
    assert allowed(client.get("/entities/SHOP-1/new-run")) is can_edit
    assert allowed(client.get("/entities/SHOP-1/template.xlsx")) is can_edit
    assert allowed(post(client, "/entities/SHOP-1/runs", MANUAL_RUN)) is can_edit
    assert allowed(post(client, f"/runs/{run_id}/review", {"decision": "APPROVE"})) is can_review
    assert allowed(client.get("/team")) is can_manage
    assert ("إدخال بيانات فترة" in client.get("/hierarchy").text) is can_edit
    if can_review:
        assert db.get_review(run_id)["decided_by"] == "member"


# --- team management ----------------------------------------------------------------
def test_owner_adds_team_members(logged_in, db, org):
    assert post(logged_in, "/team/users", {"username": "Sara", "role": "accountant",
                                           "password": TEST_LOGIN_PASSWORD}).status_code == 200
    sara = db.get_user_by_username("sara")
    assert sara["org_id"] == org and sara["role"] == "accountant" and sara["must_change_password"]

    for bad in ({"username": "sara", "role": "viewer", "password": TEST_LOGIN_PASSWORD},     # taken
                {"username": "x", "role": "viewer", "password": TEST_LOGIN_PASSWORD},        # too short
                {"username": "omar", "role": "viewer", "password": "short"},                 # weak password
                {"username": "omar", "role": "platform_admin", "password": TEST_LOGIN_PASSWORD}):
        assert post(logged_in, "/team/users", bad).status_code == 400
    assert db.get_user_by_username("omar") is None


def test_deactivating_a_member_ends_their_session(logged_in, db, org):
    owner_token = logged_in.cookies.get("pf_session")
    member_id = add_user(db, "sara", org, "accountant")
    member_token = login(logged_in, "sara")

    use_session(logged_in, owner_token)
    assert post(logged_in, f"/team/users/{member_id}/active", {"active": "0"}).status_code == 303
    use_session(logged_in, member_token)
    assert logged_in.get("/hierarchy", follow_redirects=False).headers["location"] == "/login"
    logged_in.cookies.clear()
    assert post(logged_in, "/login", {"username": "sara", "password": TEST_LOGIN_PASSWORD}).status_code == 401

    use_session(logged_in, owner_token)
    post(logged_in, f"/team/users/{member_id}/active", {"active": "1"})
    login(logged_in, "sara")


def test_owner_resets_a_members_password(logged_in, db, org):
    owner_token = logged_in.cookies.get("pf_session")
    member_id = add_user(db, "sara", org, "accountant")
    member_token = login(logged_in, "sara")

    use_session(logged_in, owner_token)
    assert post(logged_in, f"/team/users/{member_id}/password", {"password": "short"}).status_code == 400
    assert post(logged_in, f"/team/users/{member_id}/password", {"password": NEW_PASSWORD}).status_code == 200
    use_session(logged_in, member_token)
    assert logged_in.get("/hierarchy", follow_redirects=False).headers["location"] == "/login"
    login(logged_in, "sara", NEW_PASSWORD)
    assert logged_in.get("/hierarchy", follow_redirects=False).headers["location"] == "/account/password"


def test_owner_cannot_manage_themselves_or_other_organizations(logged_in, db, org):
    me = db.get_user_by_username("owner1")
    assert post(logged_in, f"/team/users/{me['id']}/active", {"active": "0"}).status_code == 400
    rival = db.create_organization("Rival Co")
    outsider = add_user(db, "outsider", rival, "viewer")
    assert post(logged_in, f"/team/users/{outsider}/active", {"active": "0"}).status_code == 404
    assert post(logged_in, f"/team/users/{outsider}/password", {"password": NEW_PASSWORD}).status_code == 404
    assert db.get_user(outsider)["is_active"]
    assert "outsider" not in logged_in.get("/team").text


# --- sessions --------------------------------------------------------------------------
def test_changing_a_password_signs_out_other_sessions(logged_in):
    other_device = logged_in.cookies.get("pf_session")
    changed = post(logged_in, "/account/password", {
        "current_password": TEST_LOGIN_PASSWORD, "new_password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    assert changed.status_code == 303
    assert logged_in.get("/hierarchy").status_code == 200  # this device got a fresh session
    use_session(logged_in, other_device)
    assert logged_in.get("/hierarchy", follow_redirects=False).headers["location"] == "/login"


def test_sessions_from_before_accounts_are_rejected(client):
    from app.auth import _serializer

    client.cookies.set("pf_session", _serializer().dumps({"u": "manager"}))  # the old token shape
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/login"
