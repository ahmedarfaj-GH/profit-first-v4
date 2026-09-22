"""
Storage layer. One code path for both engines:
  - local development: SQLite (default, zero setup)
  - production (MVP): PostgreSQL via DATABASE_URL (e.g. Neon)

All queries are parameterized — never build SQL by string concatenation.
"""
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import bindparam, create_engine, event, text
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
    """CREATE TABLE IF NOT EXISTS entities (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        parent_id TEXT REFERENCES entities(id),
        currency TEXT NOT NULL DEFAULT 'SAR',
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
]


def init_db():
    with get_db() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _all(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def _one(result) -> dict | None:
    row = result.first()
    return dict(row._mapping) if row else None


# --- entities --------------------------------------------------------------
def upsert_entity(id: str, name: str, type_: str, parent_id: str | None, currency: str):
    with get_db() as conn:
        conn.execute(
            text(
                """INSERT INTO entities (id, name, type, parent_id, currency, created_at)
                   VALUES (:id, :name, :type, :parent_id, :currency, :created_at)
                   ON CONFLICT (id) DO UPDATE SET name=excluded.name, type=excluded.type,
                       parent_id=excluded.parent_id, currency=excluded.currency"""
            ),
            {"id": id, "name": name, "type": type_, "parent_id": parent_id,
             "currency": currency, "created_at": now_iso()},
        )


def get_entity(id: str) -> dict | None:
    with get_db() as conn:
        return _one(conn.execute(text("SELECT * FROM entities WHERE id=:id"), {"id": id}))


def list_entities() -> list[dict]:
    with get_db() as conn:
        return _all(conn.execute(text("SELECT * FROM entities ORDER BY created_at, id")))


def all_entities_map() -> dict:
    with get_db() as conn:
        rows = _all(conn.execute(text("SELECT * FROM entities")))
    return {r["id"]: r for r in rows}


def would_create_cycle(entity_id: str, parent_id: str | None) -> bool:
    """True if making `parent_id` the parent of `entity_id` would close a loop
    (including an entity being its own parent)."""
    if not parent_id:
        return False
    parents = {eid: e["parent_id"] for eid, e in all_entities_map().items()}
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


def get_run(run_id: str) -> dict | None:
    with get_db() as conn:
        row = _one(conn.execute(text("SELECT * FROM runs WHERE run_id=:run_id"), {"run_id": run_id}))
    return _run_to_dict(row) if row else None


def get_latest_run(entity_id: str) -> dict | None:
    with get_db() as conn:
        row = _one(
            conn.execute(
                text("SELECT * FROM runs WHERE entity_id=:e ORDER BY created_at DESC, run_id DESC LIMIT 1"),
                {"e": entity_id},
            )
        )
    return _run_to_dict(row) if row else None


def latest_runs_by_entity() -> dict:
    with get_db() as conn:
        rows = _all(
            conn.execute(
                text(
                    """SELECT * FROM (
                           SELECT r.*, ROW_NUMBER() OVER (
                               PARTITION BY entity_id ORDER BY created_at DESC, run_id DESC
                           ) AS rn
                           FROM runs r
                       ) ranked WHERE rn = 1"""
                )
            )
        )
    return {r["entity_id"]: _run_to_dict(r) for r in rows}


def list_all_runs() -> list[dict]:
    """Every run ever computed, across every entity, newest first — the
    full audit trail (each run keeps its own inputs/result/dataset_hash
    regardless of what later runs supersede it as "latest")."""
    with get_db() as conn:
        rows = _all(conn.execute(text("SELECT * FROM runs ORDER BY created_at DESC, run_id DESC")))
    return [_run_to_dict(r) for r in rows]


def list_runs_for_entity(entity_id: str) -> list[dict]:
    with get_db() as conn:
        rows = _all(
            conn.execute(
                text("SELECT * FROM runs WHERE entity_id=:e ORDER BY created_at DESC, run_id DESC"),
                {"e": entity_id},
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
def build_network_summary(entity_id: str) -> dict | None:
    entities = all_entities_map()
    if entity_id not in entities:
        return None
    latest = latest_runs_by_entity()

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
