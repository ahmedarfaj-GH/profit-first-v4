"""
Storage layer. One code path for both engines:
  - local development: SQLite (default, zero setup)
  - production (MVP): PostgreSQL via DATABASE_URL (e.g. Neon)

All queries are parameterized — never build SQL by string concatenation.
"""
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import bindparam, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        path = os.environ.get(
            "DATABASE_PATH", str(Path(__file__).resolve().parent.parent / "profit_first.db")
        )
        return f"sqlite:///{path}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@lru_cache(maxsize=1)
def get_engine():
    url = _database_url()
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False}, future=True)

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

        return engine
    # Neon suspends idle compute and fronts it with pgbouncer: pre-ping drops dead
    # pooled connections, and disabling server-side prepared statements keeps
    # psycopg compatible with transaction-mode pooling.
    return create_engine(
        url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300,
        connect_args={"prepare_threshold": None}, future=True,
    )


@contextmanager
def get_db():
    with get_engine().begin() as conn:
        yield conn


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS organizations (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        org_id TEXT REFERENCES organizations(id),
        role TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        must_change_password INTEGER NOT NULL DEFAULT 0,
        session_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        parent_id TEXT REFERENCES entities(id),
        currency TEXT NOT NULL DEFAULT 'SAR',
        org_id TEXT REFERENCES organizations(id),
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL REFERENCES entities(id),
        period_start TEXT,
        period_end TEXT,
        dataset_hash TEXT,
        inputs_json TEXT,
        liquidity_json TEXT,
        allocation_json TEXT,
        policy_id TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS reviews (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        decision TEXT NOT NULL,
        override_json TEXT,
        justification TEXT,
        decided_by TEXT NOT NULL,
        actual_outcome TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_entity_created ON runs (entity_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_users_org ON users (org_id)",
    """CREATE TABLE IF NOT EXISTS distribution_plans (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        version INTEGER NOT NULL,
        status TEXT NOT NULL,
        lines_json TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_by TEXT,
        submitted_at TEXT,
        approved_by TEXT,
        approved_at TEXT,
        UNIQUE (org_id, version)
    )""",
    # At most one approved (active) version, and at most one version being worked
    # on (draft or awaiting approval), per organization.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_plans_active ON distribution_plans (org_id) WHERE status = 'active'",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_plans_open ON distribution_plans (org_id)
       WHERE status IN ('draft', 'pending')""",
    """CREATE TABLE IF NOT EXISTS plan_events (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES distribution_plans(id),
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plan_events_plan ON plan_events (plan_id, created_at)",
]

LEGACY_ORG_ID = "ORG-LEGACY"
LEGACY_ORG_NAME = "المنشأة الأولى (بيانات ما قبل الحسابات المتعددة)"


def init_db():
    with get_db() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))
        # Databases created before multi-tenancy have entities without an owner
        # organization: add the column, then park those entities in one legacy
        # organization so no row is left visible to everyone.
        columns = {c["name"] for c in inspect(conn).get_columns("entities")}
        if "org_id" not in columns:
            conn.execute(text("ALTER TABLE entities ADD COLUMN org_id TEXT REFERENCES organizations(id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_entities_org ON entities (org_id)"))
        orphans = conn.execute(text("SELECT 1 FROM entities WHERE org_id IS NULL LIMIT 1")).first()
        if orphans:
            if not conn.execute(text("SELECT 1 FROM organizations WHERE id=:id"), {"id": LEGACY_ORG_ID}).first():
                conn.execute(
                    text("INSERT INTO organizations (id, name, status, created_at) VALUES (:id, :name, 'active', :t)"),
                    {"id": LEGACY_ORG_ID, "name": LEGACY_ORG_NAME, "t": now_iso()},
                )
            conn.execute(text("UPDATE entities SET org_id=:id WHERE org_id IS NULL"), {"id": LEGACY_ORG_ID})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _all(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def _one(result) -> dict | None:
    row = result.first()
    return dict(row._mapping) if row else None


# --- tenancy scoping ----------------------------------------------------------
# Every entity/run read takes `org_id` with no default, so each caller decides
# explicitly: an organization id limits the query to that tenant, and None
# (platform API key only) means every tenant.
def _org_filter(org_id: str | None, column: str = "org_id") -> tuple[str, dict]:
    if org_id is None:
        return "", {}
    return f" AND {column} = :org_id", {"org_id": org_id}


# --- entities --------------------------------------------------------------
def upsert_entity(id: str, name: str, type_: str, parent_id: str | None, currency: str, *, org_id: str) -> bool:
    """Creates the entity, or updates it when it already belongs to `org_id`.
    Returns False (changing nothing) when the id is taken by another organization."""
    with get_db() as conn:
        result = conn.execute(
            text(
                """INSERT INTO entities (id, name, type, parent_id, currency, org_id, created_at)
                   VALUES (:id, :name, :type, :parent_id, :currency, :org_id, :created_at)
                   ON CONFLICT (id) DO UPDATE SET name=excluded.name, type=excluded.type,
                       parent_id=excluded.parent_id, currency=excluded.currency
                   WHERE entities.org_id = excluded.org_id"""
            ),
            {"id": id, "name": name, "type": type_, "parent_id": parent_id,
             "currency": currency, "org_id": org_id, "created_at": now_iso()},
        )
    return result.rowcount == 1


def get_entity(id: str, *, org_id: str | None) -> dict | None:
    clause, params = _org_filter(org_id)
    with get_db() as conn:
        return _one(conn.execute(text("SELECT * FROM entities WHERE id=:id" + clause), {"id": id, **params}))


def list_entities(*, org_id: str | None) -> list[dict]:
    clause, params = _org_filter(org_id)
    with get_db() as conn:
        return _all(conn.execute(text("SELECT * FROM entities WHERE 1=1" + clause + " ORDER BY created_at, id"), params))


def all_entities_map(*, org_id: str | None) -> dict:
    return {e["id"]: e for e in list_entities(org_id=org_id)}


def would_create_cycle(entity_id: str, parent_id: str | None, *, org_id: str | None) -> bool:
    """True if making `parent_id` the parent of `entity_id` would close a loop
    (including an entity being its own parent)."""
    if not parent_id:
        return False
    parents = {eid: e["parent_id"] for eid, e in all_entities_map(org_id=org_id).items()}
    seen: set[str] = set()
    current = parent_id
    while current:
        if current == entity_id or current in seen:
            return True
        seen.add(current)
        current = parents.get(current)
    return False


# --- runs --------------------------------------------------------------------
def insert_run(run_id, entity_id, period_start, period_end, dataset_hash, inputs, liquidity, allocation, policy_id):
    with get_db() as conn:
        conn.execute(
            text(
                """INSERT INTO runs (run_id, entity_id, period_start, period_end, dataset_hash,
                                      inputs_json, liquidity_json, allocation_json, policy_id, created_at)
                   VALUES (:run_id, :entity_id, :period_start, :period_end, :dataset_hash,
                           :inputs_json, :liquidity_json, :allocation_json, :policy_id, :created_at)"""
            ),
            {
                "run_id": run_id, "entity_id": entity_id, "period_start": period_start,
                "period_end": period_end, "dataset_hash": dataset_hash,
                "inputs_json": json.dumps(inputs, ensure_ascii=False),
                "liquidity_json": json.dumps(liquidity, ensure_ascii=False),
                "allocation_json": json.dumps(allocation, ensure_ascii=False),
                "policy_id": policy_id, "created_at": now_iso(),
            },
        )


def _run_to_dict(row: dict) -> dict:
    return {
        "run_id": row["run_id"],
        "entity_id": row["entity_id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "dataset_hash": row["dataset_hash"],
        "policy_id": row["policy_id"],
        "inputs": json.loads(row["inputs_json"]),
        "liquidity": json.loads(row["liquidity_json"]),
        "allocation": json.loads(row["allocation_json"]),
        "created_at": row["created_at"],
    }


_RUNS_WITH_ORG = "SELECT r.* FROM runs r JOIN entities e ON e.id = r.entity_id WHERE 1=1"


def get_run(run_id: str, *, org_id: str | None) -> dict | None:
    clause, params = _org_filter(org_id, "e.org_id")
    with get_db() as conn:
        row = _one(conn.execute(text(_RUNS_WITH_ORG + " AND r.run_id=:run_id" + clause), {"run_id": run_id, **params}))
    return _run_to_dict(row) if row else None


def get_latest_run(entity_id: str, *, org_id: str | None) -> dict | None:
    runs = list_runs_for_entity(entity_id, org_id=org_id)
    return runs[0] if runs else None


def latest_runs_by_entity(*, org_id: str | None) -> dict:
    clause, params = _org_filter(org_id, "e.org_id")
    with get_db() as conn:
        rows = _all(
            conn.execute(
                text(
                    """SELECT * FROM (
                           SELECT r.*, ROW_NUMBER() OVER (
                               PARTITION BY r.entity_id ORDER BY r.created_at DESC, r.run_id DESC
                           ) AS rn
                           FROM runs r JOIN entities e ON e.id = r.entity_id
                           WHERE 1=1""" + clause + """
                       ) ranked WHERE rn = 1"""
                ),
                params,
            )
        )
    return {r["entity_id"]: _run_to_dict(r) for r in rows}


def list_all_runs(*, org_id: str | None) -> list[dict]:
    """Every run ever computed, across every entity, newest first — the
    full audit trail (each run keeps its own inputs/result/dataset_hash
    regardless of what later runs supersede it as "latest")."""
    clause, params = _org_filter(org_id, "e.org_id")
    with get_db() as conn:
        rows = _all(conn.execute(text(_RUNS_WITH_ORG + clause + " ORDER BY r.created_at DESC, r.run_id DESC"), params))
    return [_run_to_dict(r) for r in rows]


def get_latest_org_run(*, org_id: str) -> dict | None:
    with get_db() as conn:
        row = _one(conn.execute(
            text(_RUNS_WITH_ORG + " AND e.org_id=:org_id ORDER BY r.created_at DESC, r.run_id DESC LIMIT 1"),
            {"org_id": org_id},
        ))
    return _run_to_dict(row) if row else None


def list_runs_for_entity(entity_id: str, *, org_id: str | None) -> list[dict]:
    clause, params = _org_filter(org_id, "e.org_id")
    with get_db() as conn:
        rows = _all(
            conn.execute(
                text(_RUNS_WITH_ORG + " AND r.entity_id=:e" + clause + " ORDER BY r.created_at DESC, r.run_id DESC"),
                {"e": entity_id, **params},
            )
        )
    return [_run_to_dict(r) for r in rows]


def reviews_by_run_ids(run_ids: list[str]) -> dict:
    if not run_ids:
        return {}
    query = text("SELECT run_id, decision FROM reviews WHERE run_id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    with get_db() as conn:
        rows = _all(conn.execute(query, {"ids": list(run_ids)}))
    return {r["run_id"]: r["decision"] for r in rows}


# --- reviews -------------------------------------------------------------------
def save_review(run_id, decision, override, justification, decided_by, actual_outcome) -> bool:
    """Append-only: the first recorded decision for a run is final. Returns False
    (and changes nothing) if the run was already reviewed."""
    try:
        with get_db() as conn:
            conn.execute(
                text(
                    """INSERT INTO reviews (run_id, decision, override_json, justification,
                                            decided_by, actual_outcome, created_at)
                       VALUES (:run_id, :decision, :override_json, :justification,
                               :decided_by, :actual_outcome, :created_at)"""
                ),
                {
                    "run_id": run_id, "decision": decision,
                    "override_json": json.dumps(override, ensure_ascii=False) if override else None,
                    "justification": justification, "decided_by": decided_by,
                    "actual_outcome": actual_outcome, "created_at": now_iso(),
                },
            )
    except IntegrityError:
        return False
    return True


def get_review(run_id: str) -> dict | None:
    with get_db() as conn:
        row = _one(conn.execute(text("SELECT * FROM reviews WHERE run_id=:run_id"), {"run_id": run_id}))
    if not row:
        return None
    return {
        "decision": row["decision"],
        "override": json.loads(row["override_json"]) if row["override_json"] else None,
        "justification": row["justification"],
        "decided_by": row["decided_by"],
        "actual_outcome": row["actual_outcome"],
        "decided_at": row["created_at"],
    }


# --- network rollup --------------------------------------------------------------
def build_network_summary(entity_id: str, *, org_id: str | None) -> dict | None:
    entities = all_entities_map(org_id=org_id)
    if entity_id not in entities:
        return None
    latest = latest_runs_by_entity(org_id=org_id)

    children_map: dict = {}
    for eid, e in entities.items():
        children_map.setdefault(e["parent_id"], []).append(eid)

    def build(eid, ancestors=()):
        e = entities[eid]
        node = {"entity_id": eid, "name": e["name"], "type": e["type"]}
        run = latest.get(eid)
        if run:
            node["latest_run_id"] = run["run_id"]
            node["estimated_available_liquidity"] = run["liquidity"]["estimated_available_liquidity"]
            node["total_due_obligations"] = run["liquidity"]["total_due_obligations"]
        else:
            node["estimated_available_liquidity"] = None
            node["total_due_obligations"] = None
        # `ancestors` guards against a loop that pre-dates the cycle check.
        child_nodes = [
            build(c, ancestors + (eid,)) for c in children_map.get(eid, []) if c not in ancestors and c != eid
        ]
        if child_nodes:
            node["children"] = child_nodes
        return node

    tree = build(entity_id)

    def sum_liquidity(node):
        total = node["estimated_available_liquidity"] or 0
        obligations = node["total_due_obligations"] or 0
        for c in node.get("children", []):
            t, o = sum_liquidity(c)
            total += t
            obligations += o
        return total, obligations

    total_liquidity, total_obligations = sum_liquidity(tree)

    return {
        "root_entity_id": entity_id,
        "network_total_estimated_available_liquidity": round(total_liquidity, 2),
        "network_total_due_obligations": round(total_obligations, 2),
        "tree": tree,
    }


# --- organizations and users ---------------------------------------------------
PLATFORM_ADMIN_ROLE = "platform_admin"
ORG_ROLES = ("owner", "accountant", "treasurer", "viewer")

_USER_WITH_ORG = """SELECT u.*, o.name AS org_name, o.status AS org_status
                    FROM users u LEFT JOIN organizations o ON o.id = u.org_id"""


def _user(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {**row, "is_active": bool(row["is_active"]), "must_change_password": bool(row["must_change_password"])}


def create_organization(name: str) -> str:
    org_id = f"ORG-{uuid.uuid4().hex[:12].upper()}"
    with get_db() as conn:
        conn.execute(
            text("INSERT INTO organizations (id, name, status, created_at) VALUES (:id, :name, 'active', :t)"),
            {"id": org_id, "name": name, "t": now_iso()},
        )
    return org_id


def get_organization(org_id: str) -> dict | None:
    with get_db() as conn:
        return _one(conn.execute(text("SELECT * FROM organizations WHERE id=:id"), {"id": org_id}))


def list_organizations() -> list[dict]:
    with get_db() as conn:
        return _all(
            conn.execute(
                text(
                    """SELECT o.*,
                              (SELECT COUNT(*) FROM users u WHERE u.org_id = o.id) AS user_count,
                              (SELECT COUNT(*) FROM entities e WHERE e.org_id = o.id) AS entity_count
                       FROM organizations o ORDER BY o.created_at, o.id"""
                )
            )
        )


def set_organization_status(org_id: str, status: str) -> None:
    with get_db() as conn:
        conn.execute(text("UPDATE organizations SET status=:s WHERE id=:id"), {"s": status, "id": org_id})


def create_user(username: str, password_hash: str, *, org_id: str | None, role: str,
                must_change_password: bool = True) -> str | None:
    """Returns the new user's id, or None when the username is already taken."""
    user_id = uuid.uuid4().hex
    try:
        with get_db() as conn:
            conn.execute(
                text(
                    """INSERT INTO users (id, username, password_hash, org_id, role, is_active,
                                          must_change_password, session_version, created_at)
                       VALUES (:id, :username, :hash, :org_id, :role, 1, :must_change, 1, :t)"""
                ),
                {"id": user_id, "username": username, "hash": password_hash, "org_id": org_id,
                 "role": role, "must_change": int(must_change_password), "t": now_iso()},
            )
    except IntegrityError:
        return None
    return user_id


def get_user(user_id: str) -> dict | None:
    with get_db() as conn:
        return _user(_one(conn.execute(text(_USER_WITH_ORG + " WHERE u.id=:id"), {"id": user_id})))


def get_user_by_username(username: str) -> dict | None:
    with get_db() as conn:
        return _user(_one(conn.execute(text(_USER_WITH_ORG + " WHERE u.username=:u"), {"u": username})))


def list_users(*, org_id: str) -> list[dict]:
    with get_db() as conn:
        rows = _all(conn.execute(text(_USER_WITH_ORG + " WHERE u.org_id=:o ORDER BY u.created_at"), {"o": org_id}))
    return [_user(r) for r in rows]


def platform_admin_exists() -> bool:
    with get_db() as conn:
        return conn.execute(
            text("SELECT 1 FROM users WHERE role=:r LIMIT 1"), {"r": PLATFORM_ADMIN_ROLE}
        ).first() is not None


def set_password(user_id: str, password_hash: str, *, must_change_password: bool) -> None:
    """Also bumps session_version, which signs out every existing session."""
    with get_db() as conn:
        conn.execute(
            text(
                """UPDATE users SET password_hash=:h, must_change_password=:m,
                                    session_version = session_version + 1 WHERE id=:id"""
            ),
            {"h": password_hash, "m": int(must_change_password), "id": user_id},
        )


def set_user_active(user_id: str, active: bool) -> None:
    with get_db() as conn:
        conn.execute(
            text("UPDATE users SET is_active=:a, session_version = session_version + 1 WHERE id=:id"),
            {"a": int(active), "id": user_id},
        )


# --- distribution plans ---------------------------------------------------------
# Lifecycle: draft -> pending (submitted) -> active (approved by an owner who
# neither submitted nor edited it); approving supersedes the previous active version, and a
# rejection sends a pending version back to draft. Every step is logged in
# plan_events.
PLAN_STATUSES = ("draft", "pending", "active", "superseded")


class PlanTransitionRejected(Exception):
    """Raised inside a transaction to roll it back when a transition doesn't apply."""


def _plan(row: dict | None) -> dict | None:
    if row is None:
        return None
    plan = {k: v for k, v in row.items() if k != "lines_json"}
    plan["lines"] = json.loads(row["lines_json"])
    return plan


def _add_plan_event(conn, plan_id: str, action: str, actor: str, note: str | None = None) -> None:
    conn.execute(
        text("""INSERT INTO plan_events (id, plan_id, action, actor, note, created_at)
                VALUES (:id, :plan_id, :action, :actor, :note, :t)"""),
        {"id": uuid.uuid4().hex, "plan_id": plan_id, "action": action, "actor": actor, "note": note, "t": now_iso()},
    )


def get_plan(plan_id: str, *, org_id: str) -> dict | None:
    with get_db() as conn:
        return _plan(_one(conn.execute(
            text("SELECT * FROM distribution_plans WHERE id=:id AND org_id=:o"), {"id": plan_id, "o": org_id})))


def get_active_plan(org_id: str) -> dict | None:
    with get_db() as conn:
        return _plan(_one(conn.execute(
            text("SELECT * FROM distribution_plans WHERE org_id=:o AND status='active'"), {"o": org_id})))


def get_open_plan(org_id: str) -> dict | None:
    with get_db() as conn:
        return _plan(_one(conn.execute(
            text("SELECT * FROM distribution_plans WHERE org_id=:o AND status IN ('draft', 'pending')"),
            {"o": org_id})))


def list_plans(org_id: str) -> list[dict]:
    with get_db() as conn:
        rows = _all(conn.execute(
            text("SELECT * FROM distribution_plans WHERE org_id=:o ORDER BY version DESC"), {"o": org_id}))
    return [_plan(r) for r in rows]


def list_plan_events(plan_id: str) -> list[dict]:
    with get_db() as conn:
        return _all(conn.execute(
            text("SELECT * FROM plan_events WHERE plan_id=:p ORDER BY created_at, id"), {"p": plan_id}))


def create_draft_plan(org_id: str, lines: list, actor: str) -> str | None:
    """Starts the next version as a draft. Returns None when the organization
    already has a draft or a version awaiting approval."""
    plan_id = uuid.uuid4().hex
    try:
        with get_db() as conn:
            version = conn.execute(
                text("SELECT COALESCE(MAX(version), 0) + 1 FROM distribution_plans WHERE org_id=:o"), {"o": org_id}
            ).scalar_one()
            t = now_iso()
            conn.execute(
                text("""INSERT INTO distribution_plans (id, org_id, version, status, lines_json,
                                                        created_by, created_at, updated_by, updated_at)
                        VALUES (:id, :o, :v, 'draft', :lines, :actor, :t, :actor, :t)"""),
                {"id": plan_id, "o": org_id, "v": version, "lines": json.dumps(lines, ensure_ascii=False),
                 "actor": actor, "t": t},
            )
            _add_plan_event(conn, plan_id, "created", actor)
    except IntegrityError:
        return None
    return plan_id


def update_draft_lines(plan_id: str, org_id: str, lines: list, actor: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            text("""UPDATE distribution_plans SET lines_json=:lines, updated_by=:actor, updated_at=:t
                    WHERE id=:id AND org_id=:o AND status='draft'"""),
            {"lines": json.dumps(lines, ensure_ascii=False), "actor": actor, "t": now_iso(), "id": plan_id, "o": org_id},
        )
        if result.rowcount != 1:
            return False
        _add_plan_event(conn, plan_id, "edited", actor)
    return True


def submit_plan(plan_id: str, org_id: str, actor: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            text("""UPDATE distribution_plans SET status='pending', submitted_by=:actor, submitted_at=:t
                    WHERE id=:id AND org_id=:o AND status='draft'"""),
            {"actor": actor, "t": now_iso(), "id": plan_id, "o": org_id},
        )
        if result.rowcount != 1:
            return False
        _add_plan_event(conn, plan_id, "submitted", actor)
    return True


def approve_plan(plan_id: str, org_id: str, actor: str) -> bool:
    """Activates a pending version and supersedes the current one, atomically.
    The approver must be neither its submitter nor anyone who edited it."""
    try:
        with get_db() as conn:
            conn.execute(
                text("UPDATE distribution_plans SET status='superseded' WHERE org_id=:o AND status='active'"),
                {"o": org_id},
            )
            result = conn.execute(
                text("""UPDATE distribution_plans SET status='active', approved_by=:actor, approved_at=:t
                        WHERE id=:id AND org_id=:o AND status='pending' AND submitted_by <> :actor
                          AND NOT EXISTS (SELECT 1 FROM plan_events
                                          WHERE plan_id=:id AND action='edited' AND actor=:actor)"""),
                {"actor": actor, "t": now_iso(), "id": plan_id, "o": org_id},
            )
            if result.rowcount != 1:
                raise PlanTransitionRejected()
            _add_plan_event(conn, plan_id, "approved", actor)
    except PlanTransitionRejected:
        return False
    return True


def reject_plan(plan_id: str, org_id: str, actor: str, note: str) -> bool:
    with get_db() as conn:
        result = conn.execute(
            text("""UPDATE distribution_plans SET status='draft', submitted_by=NULL, submitted_at=NULL
                    WHERE id=:id AND org_id=:o AND status='pending'"""),
            {"id": plan_id, "o": org_id},
        )
        if result.rowcount != 1:
            return False
        _add_plan_event(conn, plan_id, "rejected", actor, note)
    return True


def plan_designers(plan_id: str) -> set[str]:
    """Everyone who edited or submitted a version — none of them may approve it."""
    with get_db() as conn:
        rows = _all(conn.execute(
            text("SELECT DISTINCT actor FROM plan_events WHERE plan_id=:p AND action IN ('edited', 'submitted')"),
            {"p": plan_id}))
    return {r["actor"] for r in rows}
