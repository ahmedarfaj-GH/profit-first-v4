import pytest
from sqlalchemy.exc import IntegrityError

LIQUIDITY = {"estimated_available_liquidity": 100.0, "total_due_obligations": 40.0}
ALLOCATION = {"allocations": [], "overall_status": "FULLY_FUNDED_EXACT"}


def add_run(db, run_id, entity_id, liquidity=100.0):
    db.insert_run(run_id, entity_id, "2026-07-01", "2026-07-31", "hash", {},
                  {**LIQUIDITY, "estimated_available_liquidity": liquidity}, ALLOCATION, "AP-V1")


def test_entity_upsert_updates_in_place(db, org):
    db.upsert_entity("A", "First", "franchisee", None, "SAR", org_id=org)
    db.upsert_entity("A", "Renamed", "franchisor", None, "SAR", org_id=org)
    assert db.get_entity("A", org_id=org)["name"] == "Renamed"
    assert len(db.list_entities(org_id=org)) == 1


def test_cycle_detection(db, org):
    db.upsert_entity("A", "A", "x", None, "SAR", org_id=org)
    db.upsert_entity("B", "B", "x", "A", "SAR", org_id=org)
    db.upsert_entity("C", "C", "x", "B", "SAR", org_id=org)
    assert db.would_create_cycle("A", "A", org_id=org)
    assert db.would_create_cycle("A", "C", org_id=org)
    assert not db.would_create_cycle("C", "A", org_id=org)
    assert not db.would_create_cycle("A", None, org_id=org)


def test_latest_run_per_entity_is_chosen_independently(db, org):
    db.upsert_entity("A", "A", "x", None, "SAR", org_id=org)
    db.upsert_entity("B", "B", "x", None, "SAR", org_id=org)
    add_run(db, "RUN-A-1", "A", 10)
    add_run(db, "RUN-A-2", "A", 20)
    add_run(db, "RUN-B-1", "B", 30)
    latest = db.latest_runs_by_entity(org_id=org)
    assert latest["A"]["run_id"] == "RUN-A-2"
    assert latest["B"]["run_id"] == "RUN-B-1"
    assert [r["run_id"] for r in db.list_runs_for_entity("A", org_id=org)] == ["RUN-A-2", "RUN-A-1"]
    assert len(db.list_all_runs(org_id=org)) == 3


def test_runs_require_an_existing_entity(db, org):
    with pytest.raises(IntegrityError):
        add_run(db, "RUN-X-1", "does-not-exist")


def test_first_review_is_final(db, org):
    db.upsert_entity("A", "A", "x", None, "SAR", org_id=org)
    add_run(db, "RUN-A-1", "A")
    assert db.save_review("RUN-A-1", "APPROVE", None, None, "first", "PENDING") is True
    assert db.save_review("RUN-A-1", "REJECT", None, None, "second", "PENDING") is False
    review = db.get_review("RUN-A-1")
    assert review["decision"] == "APPROVE" and review["decided_by"] == "first"
    assert db.reviews_by_run_ids(["RUN-A-1", "RUN-none"]) == {"RUN-A-1": "APPROVE"}


def test_network_summary_rolls_up_children(db, org):
    db.upsert_entity("ROOT", "Root", "franchisor", None, "SAR", org_id=org)
    db.upsert_entity("KID1", "Kid 1", "franchisee", "ROOT", "SAR", org_id=org)
    db.upsert_entity("KID2", "Kid 2", "franchisee", "ROOT", "SAR", org_id=org)
    add_run(db, "RUN-KID1-1", "KID1", 100)
    add_run(db, "RUN-KID2-1", "KID2", -30)
    summary = db.build_network_summary("ROOT", org_id=org)
    assert summary["network_total_estimated_available_liquidity"] == 70
    assert summary["network_total_due_obligations"] == 80
    assert {c["entity_id"] for c in summary["tree"]["children"]} == {"KID1", "KID2"}
    assert db.build_network_summary("missing", org_id=org) is None


def test_network_summary_survives_a_legacy_loop(db, org):
    db.upsert_entity("A", "A", "x", None, "SAR", org_id=org)
    db.upsert_entity("B", "B", "x", "A", "SAR", org_id=org)
    db.upsert_entity("A", "A", "x", "B", "SAR", org_id=org)  # bypasses route-level validation
    assert db.build_network_summary("A", org_id=org)["root_entity_id"] == "A"


def test_entities_and_runs_are_scoped_to_their_organization(db, org):
    other = db.create_organization("Other Co")
    db.upsert_entity("A", "Mine", "x", None, "SAR", org_id=org)
    add_run(db, "RUN-A-1", "A")
    assert db.upsert_entity("A", "Hijack", "x", None, "SAR", org_id=other) is False
    assert db.get_entity("A", org_id=org)["name"] == "Mine"
    assert db.get_entity("A", org_id=other) is None
    assert db.get_run("RUN-A-1", org_id=other) is None
    assert db.list_entities(org_id=other) == [] and db.list_all_runs(org_id=other) == []
    assert db.latest_runs_by_entity(org_id=other) == {}
    assert db.build_network_summary("A", org_id=other) is None
    assert db.get_run("RUN-A-1", org_id=None)["entity_id"] == "A"  # platform-wide API view


def test_legacy_database_is_migrated_into_one_organization(tmp_path, monkeypatch, db):
    from sqlalchemy import text

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'legacy.db'}")
    db.get_engine.cache_clear()
    try:
        with db.get_db() as conn:  # the pre-accounts schema
            conn.execute(text("""CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
                                 parent_id TEXT REFERENCES entities(id), currency TEXT NOT NULL DEFAULT 'SAR',
                                 created_at TEXT NOT NULL)"""))
            conn.execute(text("INSERT INTO entities VALUES ('OLD', 'Old shop', 'franchisee', NULL, 'SAR', 'x')"))
        db.init_db()
        db.init_db()  # idempotent
        assert db.get_entity("OLD", org_id=db.LEGACY_ORG_ID)["name"] == "Old shop"
        assert [o["id"] for o in db.list_organizations()] == [db.LEGACY_ORG_ID]
    finally:
        db.get_engine().dispose()
        db.get_engine.cache_clear()
