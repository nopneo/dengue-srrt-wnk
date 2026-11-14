"""ตัวตรวจจับวนทีละโรค (สเปกข้อ 4.3) — dengue เดิมครอบคลุมใน test_detector.py"""
from datetime import datetime, timedelta

from config import load_settings
from detector import run_cycle
from fakes import FakeSource, make_row
from hosxp import HosxpError

NOW = datetime(2026, 11, 8, 12, 0)
CENTROID = {"40010103": (14.5, 101.8)}
DENGUE_ICDS = ("A90", "A91")
HFMD_ICDS = ("B084",)


def settings(diseases="dengue,hfmd", **env):
    base = {"DENGUE_START_FROM": "2026-11-01", "DENGUE_AREA_AMPHOE": "4001", "DENGUE_DISEASES": diseases}
    base.update(env)
    return load_settings(base)


def row(key, icd, hn="H1", dx=datetime(2026, 11, 8, 9, 0)):
    return make_row(visit_key=key, hn=hn, icd10=icd, dx_at=dx)


def test_cases_are_tagged_with_their_disease(store):
    src = FakeSource([row("VN:1", "A90", "H1"), row("VN:2", "B084", "H2")], centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert result.error is None and len(result.new_case_ids) == 2
    by_key = {c["visit_key"]: c["disease"] for c in store.list_cases(datetime(2026, 1, 1).date())}
    assert by_key == {"VN:1": "dengue", "VN:2": "hfmd"}


def test_same_hn_other_disease_is_a_new_case_but_same_disease_is_followup(store):
    src = FakeSource([row("VN:1", "A90", "H1", datetime(2026, 11, 6, 9, 0)),
                      row("VN:2", "B084", "H1", datetime(2026, 11, 8, 8, 0)),
                      row("VN:3", "A90", "H1", datetime(2026, 11, 8, 9, 0))], centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert len(result.new_case_ids) == 2 and result.followup_count == 1
    assert sorted(c["disease"] for c in store.active_cases()) == ["dengue", "hfmd"]


def test_disabled_disease_is_not_queried_and_creates_no_case(store):
    src = FakeSource([row("VN:1", "A90"), row("VN:2", "B084", "H2")], centroids=CENTROID)
    result = run_cycle(store, src, settings(diseases="dengue"), NOW)
    assert len(result.new_case_ids) == 1
    assert src.icds_seen == [DENGUE_ICDS]
    assert [c["disease"] for c in store.active_cases()] == ["dengue"]


def test_visit_with_two_diseases_makes_one_case_for_the_first_in_registry_order(store):
    src = FakeSource([row("VN:1", "B084"), row("VN:1", "A90")], centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert len(result.new_case_ids) == 1
    assert [c["disease"] for c in store.active_cases()] == ["dengue"]
    again = run_cycle(store, src, settings(), NOW)
    assert again.new_case_ids == [] and len(store.active_cases()) == 1


def test_other_diseases_code_does_not_reactivate_a_cancelled_case(store):
    src = FakeSource([row("VN:1", "A90")], centroids=CENTROID)
    cid = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    store.mark_cancelled(cid)
    src.rows = [row("VN:1", "B084")]            # visit เดิมกลับมาด้วยรหัสของอีกโรค
    result = run_cycle(store, src, settings(), NOW)
    assert result.reactivated_ids == [] and result.new_case_ids == []
    assert store.get_case(cid)["status"] == "cancelled"
    src.rows = [row("VN:1", "A90")]             # รหัสของโรคตัวเองกลับมา = คืนสถานะตามเดิม
    assert run_cycle(store, src, settings(), NOW).reactivated_ids == [cid]


def test_one_disease_read_failure_does_not_stop_the_others(store):
    src = FakeSource([row("VN:1", "A90"), row("VN:2", "B084", "H2")], centroids=CENTROID,
                     failure=HosxpError("down"), fail_methods=[])   # ล้มเฉพาะชุดรหัสใน fail_icds
    src.fail_icds = {HFMD_ICDS}
    result = run_cycle(store, src, settings(), NOW)
    assert result.error and "down" in result.error
    assert [c["disease"] for c in store.active_cases()] == ["dengue"]
    src.fail_icds = set()
    src.failure = None
    assert len(run_cycle(store, src, settings(), NOW).new_case_ids) == 1
    assert sorted(c["disease"] for c in store.active_cases()) == ["dengue", "hfmd"]


def test_failing_disease_does_not_cancel_its_existing_cases(store):
    src = FakeSource([row("VN:2", "B084", "H2")], centroids=CENTROID, failure=HosxpError("down"),
                     fail_methods=[])
    cid = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    src.cancelled.add("VN:2")
    src.fail_icds = {HFMD_ICDS}
    for _ in range(4):
        result = run_cycle(store, src, settings(), NOW)
        assert result.error and result.cancelled_ids == []
    assert store.get_case(cid)["status"] == "active" and store.get_case(cid)["absent_cycles"] == 0


def test_mass_cancel_guard_and_debounce_are_per_disease(store):
    rows = [row(f"VN:{i}", "A90", f"D{i}") for i in (1, 2, 3)] + \
           [row(f"VN:{i}", "B084", f"F{i}") for i in (4, 5, 6)]
    src = FakeSource(rows, centroids=CENTROID)
    run_cycle(store, src, settings(), NOW)
    src.cancelled = {"VN:1", "VN:4", "VN:5", "VN:6"}     # hfmd หายยกชุด (3/3) · dengue หาย 1 จาก 3
    results = [run_cycle(store, src, settings(), NOW) for _ in range(3)]
    status = {c["visit_key"]: c["status"] for c in store.list_cases(datetime(2026, 1, 1).date())}
    assert status["VN:1"] == "cancelled" and status["VN:2"] == status["VN:3"] == "active"
    assert {status[k] for k in ("VN:4", "VN:5", "VN:6")} == {"active"}      # hfmd ถูกกันไว้ ไม่ลากไปด้วย
    assert results[-1].error and "ข้าม" in results[-1].error
    assert len(results[-1].cancelled_ids) == 1
