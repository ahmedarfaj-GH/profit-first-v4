import pytest
from sqlalchemy.exc import IntegrityError

LIQUIDITY = {"estimated_available_liquidity": 100.0, "total_due_obligations": 40.0}
ALLOCATION = {"allocations": [], "overall_status": "FULLY_FUNDED_EXACT"}


def add_run(db, run_id, entity_id, liquidity=100.0):
    db.insert_run(run_id, entity_id, "2026-07-01", "2026-07-31", "hash", {},
                  {**LIQUIDITY, "estimated_available_liquidity": liquidity}, ALLOCATION, "AP-V1")


def test_entity_upsert_updates_in_place(db):
    db.upsert_entity("A", "First", "franchisee", None, "SAR")
    db.upsert_entity("A", "Renamed", "franchisor", None, "SAR")
    assert db.get_entity("A")["name"] == "Renamed"
    assert len(db.list_entities()) == 1


def test_cycle_detection(db):
    db.upsert_entity("A", "A", "x", None, "SAR")
    db.upsert_entity("B", "B", "x", "A", "SAR")
    db.upsert_entity("C", "C", "x", "B", "SAR")
    assert db.would_create_cycle("A", "A")
    assert db.would_create_cycle("A", "C")
    assert not db.would_create_cycle("C", "A")
    assert not db.would_create_cycle("A", None)


def test_latest_run_per_entity_is_chosen_independently(db):
    db.upsert_entity("A", "A", "x", None, "SAR")
    db.upsert_entity("B", "B", "x", None, "SAR")
    add_run(db, "RUN-A-1", "A", 10)
    add_run(db, "RUN-A-2", "A", 20)
    add_run(db, "RUN-B-1", "B", 30)
    latest = db.latest_runs_by_entity()
    assert latest["A"]["run_id"] == "RUN-A-2"
    assert latest["B"]["run_id"] == "RUN-B-1"
    assert [r["run_id"] for r in db.list_runs_for_entity("A")] == ["RUN-A-2", "RUN-A-1"]
    assert len(db.list_all_runs()) == 3


def test_runs_require_an_existing_entity(db):
    with pytest.raises(IntegrityError):
        add_run(db, "RUN-X-1", "does-not-exist")


def test_first_review_is_final(db):
    db.upsert_entity("A", "A", "x", None, "SAR")
    add_run(db, "RUN-A-1", "A")
    assert db.save_review("RUN-A-1", "APPROVE", None, None, "first", "PENDING") is True
    assert db.save_review("RUN-A-1", "REJECT", None, None, "second", "PENDING") is False
    review = db.get_review("RUN-A-1")
    assert review["decision"] == "APPROVE" and review["decided_by"] == "first"
    assert db.reviews_by_run_ids(["RUN-A-1", "RUN-none"]) == {"RUN-A-1": "APPROVE"}


def test_network_summary_rolls_up_children(db):
    db.upsert_entity("ROOT", "Root", "franchisor", None, "SAR")
    db.upsert_entity("KID1", "Kid 1", "franchisee", "ROOT", "SAR")
    db.upsert_entity("KID2", "Kid 2", "franchisee", "ROOT", "SAR")
    add_run(db, "RUN-KID1-1", "KID1", 100)
    add_run(db, "RUN-KID2-1", "KID2", -30)
    summary = db.build_network_summary("ROOT")
    assert summary["network_total_estimated_available_liquidity"] == 70
    assert summary["network_total_due_obligations"] == 80
    assert {c["entity_id"] for c in summary["tree"]["children"]} == {"KID1", "KID2"}
    assert db.build_network_summary("missing") is None


def test_network_summary_survives_a_legacy_loop(db):
    db.upsert_entity("A", "A", "x", None, "SAR")
    db.upsert_entity("B", "B", "x", "A", "SAR")
    db.upsert_entity("A", "A", "x", "B", "SAR")  # bypasses route-level validation
    assert db.build_network_summary("A")["root_entity_id"] == "A"
