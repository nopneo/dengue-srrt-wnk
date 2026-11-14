from dataclasses import replace
from datetime import datetime

import pytest

from config import load_settings
from detector import run_cycle
from fakes import FakeSource, make_row
from hosxp import HosxpError

NOW = datetime(2026, 11, 8, 12, 0)
CENTROID = {"40010103": (14.5, 101.8)}


def settings(**env):
    base = {"DENGUE_START_FROM": "2026-11-01", "DENGUE_AREA_AMPHOE": "4001"}
    base.update(env)
    return load_settings(base)


def test_new_dx_creates_case_with_centroid_pin_estimated(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert len(result.new_case_ids) == 1 and result.error is None
    case = store.get_case(result.new_case_ids[0])
    assert case["village_code"] == "40010103"
    assert case["in_area"] is True
    assert case["pin_state"] == "estimated"
    assert case["house_no"] == "12" and case["sex"] == "1" and case["age_years"] == 30
    pin = store.get_pin(case["id"])
    assert (pin["lat"], pin["lng"]) == (14.5, 101.8)
    assert pin["source"] == "village_centroid"
    assert pin["state"] == "estimated" and pin["set_by"] == "system"


def test_second_cycle_does_not_duplicate(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    run_cycle(store, src, settings(), NOW)
    second = run_cycle(store, src, settings(), NOW)
    assert second.new_case_ids == [] and second.followup_count == 0
    assert len(store.active_cases()) == 1


def test_two_codes_same_visit_one_case(store):
    src = FakeSource([make_row(icd10="A90"), make_row(icd10="A911")],
                     centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert len(result.new_case_ids) == 1 and result.followup_count == 0
    assert len(store.active_cases()) == 1


def test_followup_visit_within_realert_days_is_not_new_case(store):
    src = FakeSource([make_row("VN:1", dx_at=datetime(2026, 11, 2, 9, 0))],
                     centroids=CENTROID)
    run_cycle(store, src, settings(), NOW)
    src.rows.append(make_row("VN:2", dx_at=datetime(2026, 11, 8, 9, 0)))
    result = run_cycle(store, src, settings(), NOW)
    assert result.new_case_ids == []
    assert result.followup_count == 1
    assert len(store.active_cases()) == 1


def test_older_visit_of_known_case_is_not_followup(store):
    # visit เก่าที่เคยเป็นเคสแล้ว ต้องไม่ถูกนับเป็นตามอาการ แม้ HN นั้นมีเคสใหม่กว่า
    src = FakeSource([make_row("VN:1", dx_at=datetime(2026, 11, 1, 9, 0)),
                      make_row("VN:2", dx_at=datetime(2026, 11, 30, 9, 0))],
                     centroids=CENTROID)
    first = run_cycle(store, src, settings(), NOW)
    assert len(first.new_case_ids) == 2
    again = run_cycle(store, src, settings(), NOW)
    assert again.new_case_ids == [] and again.followup_count == 0


def test_visit_after_realert_days_is_new_case(store):
    src = FakeSource([make_row("VN:1", dx_at=datetime(2026, 11, 2, 9, 0)),
                      make_row("VN:2", dx_at=datetime(2026, 11, 30, 9, 0))],
                     centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert len(result.new_case_ids) == 2 and result.followup_count == 0


def test_before_start_from_ignored(store):
    src = FakeSource([make_row("VN:1", dx_at=datetime(2026, 10, 31, 23, 0))],
                     centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    assert result.new_case_ids == []
    assert src.last_since == datetime(2026, 11, 1, 0, 0)
    assert store.active_cases() == []


def test_before_start_from_ignored_even_if_source_returns_it(store):
    class Leaky(FakeSource):
        def new_dx(self, since, icds):
            return list(self.rows)

    src = Leaky([make_row("VN:1", dx_at=datetime(2026, 10, 31, 23, 0))])
    assert run_cycle(store, src, settings(), NOW).new_case_ids == []


def test_in_area_by_amphoe_not_village_table(store):
    src = FakeSource([
        make_row("VN:1", hn="1", tmb="99", moo="5"),  # หมู่บ้านไม่อยู่ใน village แต่อำเภอตรง
        make_row("VN:2", hn="2", amp="02"),            # ต่างอำเภอ
        make_row("VN:3", hn="3", chw="41"),            # ต่างจังหวัด
        make_row("VN:4", hn="4", chw=None),            # ขาดจังหวัด
        make_row("VN:5", hn="5", amp=None),            # ขาดอำเภอ
    ])
    result = run_cycle(store, src, settings(), NOW)
    by_key = {store.get_case(i)["visit_key"]: store.get_case(i)["in_area"]
              for i in result.new_case_ids}
    assert by_key == {"VN:1": True, "VN:2": False, "VN:3": False,
                      "VN:4": False, "VN:5": False}


def test_in_area_falls_back_to_source_default_amphoe(store):
    src = FakeSource([make_row("VN:1", hn="1"), make_row("VN:2", hn="2", amp="02")],
                     area=("4002",))
    result = run_cycle(store, src, settings(DENGUE_AREA_AMPHOE=""), NOW)
    flags = {store.get_case(i)["visit_key"]: store.get_case(i)["in_area"]
             for i in result.new_case_ids}
    assert flags == {"VN:1": False, "VN:2": True}


def test_no_centroid_gives_no_pin_state_none(store):
    result = run_cycle(store, FakeSource([make_row()]), settings(), NOW)
    case = store.get_case(result.new_case_ids[0])
    assert case["pin_state"] == "none"
    assert store.get_pin(case["id"]) is None


def test_missing_village_parts_gives_no_pin(store):
    src = FakeSource([make_row(moo=None)], centroids=CENTROID)
    case = store.get_case(run_cycle(store, src, settings(), NOW).new_case_ids[0])
    assert case["village_code"] is None and case["pin_state"] == "none"


def test_default_pin_from_house_memory_used_as_estimated(store):
    old = store.add_case("VN:OLD", "OPD", "999", "A90", datetime(2026, 9, 1, 9, 0),
                         "40010103", "12", "2", 40, True)
    store.set_pin(old, 14.6, 101.9, "confirmed", "manual", "somchai")
    src = FakeSource([make_row("VN:1", hn="1")], centroids=CENTROID)
    result = run_cycle(store, src, settings(), NOW)
    case = store.get_case(result.new_case_ids[0])
    pin = store.get_pin(case["id"])
    assert case["pin_state"] == "estimated"
    assert pin["source"] == "house_memory" and pin["state"] == "estimated"
    assert (pin["lat"], pin["lng"]) == (14.6, 101.9)
    # หมุดที่ยืนยันแล้วของเคสเก่าไม่ถูกแตะ
    assert store.get_case(old)["pin_state"] == "confirmed"
    assert store.get_pin(old)["state"] == "confirmed"


def test_removed_dx_marks_case_cancelled_after_three_absent_cycles(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    case_id = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    src.cancelled.add("VN:1")
    for expected in (1, 2):
        result = run_cycle(store, src, settings(), NOW)
        assert result.cancelled_ids == [] and result.error is None
        assert store.get_case(case_id)["status"] == "active"
        assert store.get_case(case_id)["absent_cycles"] == expected
    result = run_cycle(store, src, settings(), NOW)
    assert result.cancelled_ids == [case_id]
    assert store.get_case(case_id)["status"] == "cancelled"
    # รอบถัดไปไม่ยกเลิกซ้ำและไม่สร้างเคสใหม่จาก visit เดิม
    again = run_cycle(store, src, settings(), NOW)
    assert again.cancelled_ids == [] and again.new_case_ids == []


@pytest.mark.parametrize("methods", [{"new_dx"}, {"dx_present_many"}, None])
def test_hosxp_error_does_not_cancel_cases(store, methods):
    src = FakeSource([make_row()], centroids=CENTROID)
    case_id = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    src.cancelled.add("VN:1")
    src.failure = HosxpError("down")
    src.fail_methods = methods
    result = run_cycle(store, src, settings(), NOW)
    assert result.error and "down" in result.error
    assert result.cancelled_ids == [] and result.new_case_ids == []
    assert store.get_case(case_id)["status"] == "active"


def test_dx_present_error_creates_no_new_case_either(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    run_cycle(store, src, settings(), NOW)
    src.rows.append(make_row("VN:9", hn="9", dx_at=datetime(2026, 11, 9, 9, 0)))
    src.failure, src.fail_methods = HosxpError("down"), {"dx_present_many"}
    result = run_cycle(store, src, settings(), NOW)
    assert result.error and result.new_case_ids == []
    assert len(store.active_cases()) == 1


def test_centroid_error_still_creates_case_without_pin(store, caplog):
    src = FakeSource([make_row()], centroids=CENTROID,
                     failure=HosxpError("gis down"), fail_methods={"village_centroid"})
    with caplog.at_level("WARNING", logger="detector"):
        result = run_cycle(store, src, settings(), NOW)
    assert result.error is None and len(result.new_case_ids) == 1
    case = store.get_case(result.new_case_ids[0])
    assert case["pin_state"] == "none" and store.get_pin(case["id"]) is None
    assert any("gis down" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)
    assert all("000001" not in r.getMessage() for r in caplog.records)


def test_no_disease_codes_reports_error_and_cancels_nothing(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    case_id = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    src.cancelled.add("VN:1")
    result = run_cycle(store, src, replace(settings(), icds=()), NOW)
    assert result.error and "no disease codes" in result.error
    assert result.cancelled_ids == []
    assert store.get_case(case_id)["status"] == "active"


def test_rows_with_same_visit_are_processed_in_deterministic_order(store):
    # รหัสสองตัวใน visit เดียว: เรียงตาม icd10 จึงได้ icd10 ที่น้อยกว่าเป็นของเคสเสมอ
    src = FakeSource([make_row(icd10="A911"), make_row(icd10="A90")],
                     centroids=CENTROID)
    case = store.get_case(run_cycle(store, src, settings(), NOW).new_case_ids[0])
    assert case["icd10"] == "A90"


def test_empty_new_dx_result_does_not_cancel(store):
    src = FakeSource([make_row()], centroids=CENTROID)
    case_id = run_cycle(store, src, settings(), NOW).new_case_ids[0]
    src.rows.clear()
    result = run_cycle(store, src, settings(), NOW)
    assert result.cancelled_ids == [] and result.error is None
    assert store.get_case(case_id)["status"] == "active"


def test_icds_come_from_settings(store):
    src = FakeSource([make_row()])
    run_cycle(store, src, settings(DENGUE_ICDS="A90, B50"), NOW)
    assert src.last_icds == ("A90", "B50")


# ---------------------------------------------------------------- R24: debounce / guard / reactivate

def _many(store, n, src=None):
    """สร้าง n เคส (VN:1..VN:n คนละ HN) ผ่านรอบจริง คืน (src, ids ตามลำดับ)."""
    src = src or FakeSource([make_row(f"VN:{i}", hn=f"H{i}") for i in range(1, n + 1)],
                            centroids=CENTROID)
    ids = run_cycle(store, src, settings(), NOW).new_case_ids
    assert len(ids) == n
    return src, ids


def _status(store, ids):
    return [store.get_case(i)["status"] for i in ids]


def test_empty_answer_does_not_cancel_for_two_cycles(store):
    src, ids = _many(store, 5)
    src.cancelled = {f"VN:{i}" for i in range(1, 6)}      # ตอบว่างทั้งสองทาง (replica reload)
    for expected in (1, 2):
        result = run_cycle(store, src, settings(), NOW)
        assert result.cancelled_ids == [] and result.error is None
        assert _status(store, ids) == ["active"] * 5
        assert [store.get_case(i)["absent_cycles"] for i in ids] == [expected] * 5


def test_mass_cancel_guard_blocks_when_all_absent_and_sets_error(store):
    src, ids = _many(store, 5)
    src.cancelled = {f"VN:{i}" for i in range(1, 6)}
    for _ in range(2):
        run_cycle(store, src, settings(), NOW)
    third = run_cycle(store, src, settings(), NOW)          # ถึง 3 รอบ แต่ยกเลิก 5/5 = ผิดปกติ
    assert third.cancelled_ids == []
    assert third.error and "ข้าม" in third.error
    assert _status(store, ids) == ["active"] * 5
    assert [store.get_case(i)["absent_cycles"] for i in ids] == [3] * 5   # ตัวนับยังเพิ่ม
    fourth = run_cycle(store, src, settings(), NOW)         # ประเมินใหม่ทุกรอบ
    assert fourth.cancelled_ids == [] and fourth.error
    assert [store.get_case(i)["absent_cycles"] for i in ids] == [4] * 5


def test_mass_cancel_guard_releases_when_answer_recovers(store):
    src, ids = _many(store, 5)
    src.cancelled = {f"VN:{i}" for i in range(1, 6)}
    for _ in range(3):
        run_cycle(store, src, settings(), NOW)
    src.cancelled = {"VN:1", "VN:2"}                        # ฐานกลับมา เหลือถอนจริง 2 จาก 5
    result = run_cycle(store, src, settings(), NOW)
    assert sorted(result.cancelled_ids) == sorted(ids[:2]) and result.error is None
    assert _status(store, ids) == ["cancelled"] * 2 + ["active"] * 3
    assert [store.get_case(i)["absent_cycles"] for i in ids[2:]] == [0, 0, 0]


def test_single_case_absent_three_cycles_cancelled_among_many(store):
    src, ids = _many(store, 5)
    src.cancelled = {"VN:3"}
    results = [run_cycle(store, src, settings(), NOW) for _ in range(3)]
    assert [r.cancelled_ids for r in results] == [[], [], [ids[2]]]
    assert all(r.error is None for r in results)            # ยกเลิก 1 จากหลายเคส ไม่ติด guard
    assert _status(store, ids) == ["active", "active", "cancelled", "active", "active"]


def test_two_of_five_cancelled_is_not_blocked_by_guard(store):
    src, ids = _many(store, 5)
    src.cancelled = {"VN:1", "VN:2"}
    for _ in range(2):
        run_cycle(store, src, settings(), NOW)
    result = run_cycle(store, src, settings(), NOW)
    assert sorted(result.cancelled_ids) == sorted(ids[:2]) and result.error is None


def test_present_again_resets_the_counter(store):
    src, ids = _many(store, 3)
    src.cancelled = {"VN:1"}
    run_cycle(store, src, settings(), NOW)
    run_cycle(store, src, settings(), NOW)
    assert store.get_case(ids[0])["absent_cycles"] == 2
    src.cancelled = set()
    run_cycle(store, src, settings(), NOW)
    assert store.get_case(ids[0])["absent_cycles"] == 0
    src.cancelled = {"VN:1"}
    run_cycle(store, src, settings(), NOW)
    run_cycle(store, src, settings(), NOW)
    assert store.get_case(ids[0])["status"] == "active"     # ต้องหายติดกัน 3 รอบใหม่


def test_row_seen_in_new_dx_counts_as_present_even_if_batch_check_misses(store):
    src, ids = _many(store, 2)
    src.dx_present_many = lambda keys, icds: set()          # ตอบบางส่วน/ว่าง แต่ new_dx ยังเห็นแถว
    for _ in range(4):
        result = run_cycle(store, src, settings(), NOW)
        assert result.cancelled_ids == [] and result.error is None
    assert [store.get_case(i)["absent_cycles"] for i in ids] == [0, 0]


def test_hosxp_error_cycle_changes_no_counters(store):
    src, ids = _many(store, 3)
    src.cancelled = {"VN:1"}
    run_cycle(store, src, settings(), NOW)
    assert store.get_case(ids[0])["absent_cycles"] == 1
    src.failure = HosxpError("down")
    for _ in range(4):
        assert run_cycle(store, src, settings(), NOW).error
    assert [store.get_case(i)["absent_cycles"] for i in ids] == [1, 0, 0]
    assert _status(store, ids) == ["active"] * 3


def test_cancelled_case_reactivated_when_dx_returns(store):
    src, ids = _many(store, 3)
    store.set_pin(ids[0], 14.51, 101.81, "confirmed", "manual", "somchai")
    src.cancelled = {"VN:1"}
    for _ in range(3):
        run_cycle(store, src, settings(), NOW)
    assert store.get_case(ids[0])["status"] == "cancelled"
    src.cancelled = set()                                    # หมอเพิ่มรหัสกลับ
    result = run_cycle(store, src, settings(), NOW)
    assert result.reactivated_ids == [ids[0]]
    assert result.new_case_ids == [] and result.cancelled_ids == []
    case = store.get_case(ids[0])
    assert case["status"] == "active" and case["absent_cycles"] == 0
    assert case["pin_state"] == "confirmed" and store.get_pin(ids[0])["lat"] == 14.51
    assert len(store.list_cases(settings().start_from)) == 3     # ไม่เกิดเคสซ้ำ
    again = run_cycle(store, src, settings(), NOW)
    assert again.reactivated_ids == []


def test_reactivation_does_not_resurrect_before_start_from_rows(store):
    # แถวก่อนวันเริ่มถูกข้ามตามเดิม แม้เป็น visit ที่เคยยกเลิก (กันของเก่าฟื้นเอง)
    src, ids = _many(store, 1)
    store.mark_cancelled(ids[0])
    src.rows[0] = make_row("VN:1", hn="H1", dx_at=datetime(2026, 10, 31, 9, 0))
    assert run_cycle(store, src, settings(), NOW).reactivated_ids == []
    assert store.get_case(ids[0])["status"] == "cancelled"


def test_cancel_check_only_covers_recent_cases(store):
    old_dx = datetime(2026, 7, 1, 9, 0)                      # 130 วันก่อน NOW > 90
    old = store.add_case("VN:OLD", "OPD", "HO", "A90", old_dx, "40010103", "1", "1", 30, True)
    src, ids = _many(store, 2)
    seen = []
    real = src.dx_present_many
    src.dx_present_many = lambda keys, icds: (seen.append(list(keys)), real(keys, icds))[1]
    src.cancelled = {"VN:OLD"}
    for _ in range(4):
        run_cycle(store, src, settings(), NOW)
    assert all("VN:OLD" not in keys for keys in seen) and seen
    assert store.get_case(old)["status"] == "active" and store.get_case(old)["absent_cycles"] == 0


def test_cancel_check_days_is_configurable(store):
    d = datetime(2026, 11, 1, 9, 0)                          # 7 วันก่อน NOW
    cid = store.add_case("VN:X", "OPD", "HX", "A90", d, "40010103", "1", "1", 30, True)
    src = FakeSource([], centroids=CENTROID, cancelled={"VN:X"})
    for _ in range(3):
        run_cycle(store, src, settings(DENGUE_CANCEL_CHECK_DAYS="3"), NOW)
    assert store.get_case(cid)["status"] == "active"         # นอกช่วง 3 วัน ไม่ถูกตรวจ
    for _ in range(3):
        run_cycle(store, src, settings(DENGUE_CANCEL_CHECK_DAYS="30"), NOW)
    assert store.get_case(cid)["status"] == "cancelled"
