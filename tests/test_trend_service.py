"""TrendService — แคชรายปี พ.ศ. ลายเซ็นค่าตั้ง หมดอายุ ปีปัจจุบัน และการล้มของ HOSxP"""
from datetime import date, datetime, timedelta

import pytest

from config import load_settings
from fakes import FakeSource, HosxpError
from trend import VisitRow
from trend_service import TrendService

NOW = datetime(2026, 11, 10, 9, 0)   # ปี พ.ศ. 2569 (ม.ค.–ธ.ค. 2026) ยังไม่จบ


def make_settings(tmp_path, **extra):
    env = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
           "DENGUE_AREA_AMPHOE": "3630"}
    env.update(extra)
    return load_settings(env)


def v(day, hn="1", amp="30"):
    return VisitRow(hn, day, "36", amp, "01", "3")


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def mono():
    return Mono()


def service(store, tmp_path, source, clock, mono, **extra):
    return TrendService(store, source, make_settings(tmp_path, **extra), clock=clock, monotonic=mono)


CLOSED_ROWS = [v(date(2025, 3, 5)), v(date(2025, 3, 20), hn="2"), v(date(2025, 3, 6), amp="31", hn="3")]


def test_closed_year_computed_once_then_cached(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=CLOSED_ROWS)
    svc = service(store, tmp_path, src, clock, mono)
    r1 = svc.year(2568)
    assert sum(c.cases for c in r1.rows if c.in_area) == 2 and r1.error is None and not r1.stale
    assert len(src.episode_calls) == 1
    r2 = svc.year(2568)
    assert len(src.episode_calls) == 1 and r2.rows == r1.rows and r2.computed_at == NOW


def test_signature_change_recomputes(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=CLOSED_ROWS)
    service(store, tmp_path, src, clock, mono).year(2568)
    service(store, tmp_path, src, clock, mono, DENGUE_REALERT_DAYS="30").year(2568)
    assert len(src.episode_calls) == 2
    service(store, tmp_path, src, clock, mono, DENGUE_REALERT_DAYS="30", DENGUE_ICDS="A90").year(2568)
    assert len(src.episode_calls) == 3
    service(store, tmp_path, src, clock, mono, DENGUE_REALERT_DAYS="30", DENGUE_ICDS="A90",
            DENGUE_AREA_AMPHOE="3631").year(2568)
    assert len(src.episode_calls) == 4


def test_empty_area_setting_uses_source_default_in_signature(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=CLOSED_ROWS, area=("3630",))
    svc = service(store, tmp_path, src, clock, mono, DENGUE_AREA_AMPHOE="")
    r = svc.year(2568)
    assert sum(c.cases for c in r.rows if c.in_area) == 2


def test_current_year_range_ends_today_and_starts_realert_days_early(store, tmp_path, clock, mono):
    src = FakeSource()
    service(store, tmp_path, src, clock, mono).year(2569)
    (icds, start, end), = src.episode_calls
    assert icds == ("A90", "A91")
    assert start == date(2026, 1, 1) - timedelta(days=21) and end == date(2026, 11, 10)
    service(store, tmp_path, src, clock, mono).year(2568)
    assert src.episode_calls[-1][1:] == (date(2025, 1, 1) - timedelta(days=21), date(2025, 12, 31))


def test_followup_across_year_boundary_not_counted_as_new(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=[v(date(2025, 12, 25)), v(date(2026, 1, 3))])
    r = service(store, tmp_path, src, clock, mono).year(2569)
    assert r.rows == []


def test_current_year_expires_after_24h(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=[v(date(2026, 10, 5))])
    svc = service(store, tmp_path, src, clock, mono)
    svc.year(2570)
    clock.now = NOW + timedelta(hours=23)
    svc.year(2570)
    assert len(src.episode_calls) == 1
    clock.now = NOW + timedelta(hours=25)
    r = svc.year(2570)
    assert len(src.episode_calls) == 2 and r.computed_at == clock.now


def test_refresh_throttled_5_minutes(store, tmp_path, clock, mono):
    src = FakeSource()
    svc = service(store, tmp_path, src, clock, mono)
    svc.year(2570)
    r = svc.year(2570, refresh=True)           # ครั้งแรกที่ขอ refresh ทำได้
    assert len(src.episode_calls) == 2 and not r.throttled
    mono.t += 299
    r = svc.year(2570, refresh=True)
    assert len(src.episode_calls) == 2 and r.throttled
    mono.t += 2
    r = svc.year(2570, refresh=True)
    assert len(src.episode_calls) == 3 and not r.throttled


def test_source_failure_without_cache_returns_error_and_empty(store, tmp_path, clock, mono):
    src = FakeSource(failure=HosxpError("secret detail 10.1.2.3"), fail_methods=["episode_rows"])
    r = service(store, tmp_path, src, clock, mono).year(2568)
    assert r.rows == [] and r.computed_at is None and r.error
    assert "secret" not in r.error and "10.1.2.3" not in r.error


def test_source_failure_with_stale_cache_returns_cache_flagged_stale(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=[v(date(2026, 10, 5))])
    svc = service(store, tmp_path, src, clock, mono)
    first = svc.year(2570)
    src.failure, src.fail_methods = HosxpError("down"), {"episode_rows"}
    clock.now = NOW + timedelta(hours=30)
    r = svc.year(2570)
    assert r.rows == first.rows and r.stale and r.error and r.computed_at == NOW
    # ฐานล่มตอนหาพื้นที่ตั้งต้น (ไม่ได้ตั้ง DENGUE_AREA_AMPHOE) ก็ไม่ล้ม
    src.fail_methods = {"default_area_amphoe"}
    svc2 = service(store, tmp_path, src, clock, mono, DENGUE_AREA_AMPHOE="")
    r2 = svc2.year(2570)
    assert r2.stale and r2.rows == first.rows


def test_names_cached_and_failure_returns_empty(store, tmp_path, clock, mono):
    src = FakeSource(names={"363001": "วังน้ำเขียว"})
    svc = service(store, tmp_path, src, clock, mono)
    assert svc.names(["363001", "363009"]) == {"363001": "วังน้ำเขียว"}
    src.failure, src.fail_methods = HosxpError("down"), {"address_names"}
    assert svc.names(["363001"]) == {"363001": "วังน้ำเขียว"}   # จากแคชในหน่วยความจำ
    assert svc.names(["363777"]) == {}


# ---- รอบแก้หลังรีวิวสุดท้าย ----
def test_year_cached_while_open_is_recomputed_after_it_closes(store, tmp_path, clock, mono):
    # คำนวณปี พ.ศ. 2569 ตอนยังไม่จบ (15 ธ.ค. 2026) — visit วันที่ 25 ธ.ค. ยังไม่เกิด; เมื่อปีจบแล้วต้องไม่ค้างข้อมูลไม่ครบ
    src = FakeSource(visit_rows=[v(date(2026, 12, 25))])
    svc = service(store, tmp_path, src, clock, mono)
    clock.now = datetime(2026, 12, 15, 9, 0)
    assert svc.year(2569).rows == []
    clock.now = datetime(2027, 1, 10, 9, 0)
    r = svc.year(2569)
    assert len(src.episode_calls) == 2 and sum(c.cases for c in r.rows) == 1
    svc.year(2569)
    assert len(src.episode_calls) == 2          # ภายใน 24 ชม. ไม่ดึงซ้ำ
    clock.now = datetime(2027, 2, 20, 9, 0)     # เลยช่วงผ่อนผันลงรหัสช้า (45 วันหลังปีจบ)
    svc.year(2569)
    assert len(src.episode_calls) == 3
    clock.now = datetime(2027, 6, 1, 9, 0)
    svc.year(2569)
    assert len(src.episode_calls) == 3          # ปี พ.ศ.นิ่งแล้ว ไม่ดึงอีก


def test_old_fiscal_year_signature_cache_is_recomputed(store, tmp_path, clock, mono):
    # แคชรุ่นปีงบ (v1) คีย์เป็นตัวเลขปีเดียวกันแต่ความหมายต่าง ต้องไม่ถูกใช้ซ้ำ
    from trend import CountRow
    store.trend_put(2568, "v1|A90,A91|21|3630", [CountRow(0, True, "363001", "03", 99)], datetime(2025, 12, 1, 9, 0))
    src = FakeSource(visit_rows=CLOSED_ROWS)
    r = service(store, tmp_path, src, clock, mono).year(2568)
    assert len(src.episode_calls) == 1 and sum(c.cases for c in r.rows if c.in_area) == 2   # ไม่ใช่ 99 ของแคชเก่า


def test_default_area_fetched_once_and_closed_year_survives_outage(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=CLOSED_ROWS, area=("3630",))
    svc = service(store, tmp_path, src, clock, mono, DENGUE_AREA_AMPHOE="")
    svc.year(2568)
    svc.year(2568)
    svc.year(2567)
    assert src.default_area_calls == 1
    src.failure, src.fail_methods = HosxpError("down"), {"default_area_amphoe", "episode_rows"}
    r = svc.year(2568)
    assert not r.stale and r.error is None and r.rows
    mono.t += 3601                                # หมดอายุแคชพื้นที่ -> ล้ม -> ใช้แคชผลเก่าเป็น stale
    assert svc.year(2568).stale


def test_failed_compute_backs_off_for_5_minutes(store, tmp_path, clock, mono):
    src = FakeSource(failure=HosxpError("slow"), fail_methods=["episode_rows"])
    svc = service(store, tmp_path, src, clock, mono)
    assert svc.year(2568).error
    assert svc.year(2568).error
    assert src.episode_attempts == 1            # ช่วงพัก ไม่ยิงฐานซ้ำทุกการรีโหลด
    mono.t += 301
    svc.year(2568)
    assert src.episode_attempts == 2


def test_concurrent_request_returns_busy_instead_of_blocking(store, tmp_path, clock, mono):
    src = FakeSource(visit_rows=CLOSED_ROWS)
    svc = TrendService(store, src, make_settings(tmp_path), clock=clock, monotonic=mono, lock_wait=0.01)
    lock = svc._lock_for(("dengue", 2568))
    lock.acquire()
    try:
        r = svc.year(2568)
    finally:
        lock.release()
    assert r.busy and r.rows == [] and src.episode_attempts == 0
    assert not svc.year(2568).busy


# ---- จุดกลางหมู่บ้านและพื้นที่ (เลเยอร์แผนที่) ----
def test_centroids_cached_one_hour_and_misses_five_minutes(store, tmp_path, clock, mono):
    src = FakeSource(centroids={"30250103": (14.3, 101.8)})
    svc = service(store, tmp_path, src, clock, mono)
    assert svc.centroids(["30250103", "30250104"]) == {"30250103": (14.3, 101.8)}
    assert src.centroid_requests == [["30250103", "30250104"]]
    mono.t += 100
    svc.centroids(["30250103", "30250104"])
    assert len(src.centroid_requests) == 1                         # ทั้งที่พบและที่ไม่พบอยู่ในแคช
    mono.t += 201                                                  # รวม +301: ที่ไม่พบหมดอายุ
    svc.centroids(["30250103", "30250104"])
    assert src.centroid_requests[-1] == ["30250104"]
    mono.t += 3300                                                 # รวม +3601: ที่พบหมดอายุ
    svc.centroids(["30250103"])
    assert src.centroid_requests[-1] == ["30250103"]


def test_centroids_failure_returns_cached_and_never_raises(store, tmp_path, clock, mono):
    src = FakeSource(centroids={"30250103": (14.3, 101.8)})
    svc = service(store, tmp_path, src, clock, mono)
    svc.centroids(["30250103"])
    src.failure, src.fail_methods = HosxpError("down"), {"village_centroids"}
    assert svc.centroids(["30250103", "30250199"]) == {"30250103": (14.3, 101.8)}

    class Boom(FakeSource):
        def village_centroids(self, codes):
            raise RuntimeError("driver exploded")

    svc2 = service(store, tmp_path, Boom(), clock, mono)
    assert svc2.centroids(["30250103"]) == {}


def test_area_public_matches_configured_and_default(store, tmp_path, clock, mono):
    src = FakeSource(area=("3025",))
    assert service(store, tmp_path, src, clock, mono).area() == ("3630",)            # ค่าตั้งชนะ
    assert service(store, tmp_path, src, clock, mono, DENGUE_AREA_AMPHOE="").area() == ("3025",)


def test_centroid_lookup_reports_failure_and_does_not_negative_cache_it(store, tmp_path, clock, mono):
    src = FakeSource(centroids={"30250103": (14.3, 101.8)}, failure=HosxpError("down"),
                     fail_methods=["village_centroids"])
    svc = service(store, tmp_path, src, clock, mono)
    assert svc.centroid_lookup(["30250103"]) == ({}, True)
    assert svc.centroid_lookup(["30250103"]) == ({}, True)
    assert len(src.centroid_requests) == 1                       # พักสั้น ๆ ไม่ยิงฐานซ้ำทุกคำขอ
    src.failure = None
    mono.t += 61
    assert svc.centroid_lookup(["30250103"]) == ({"30250103": (14.3, 101.8)}, False)
    # ล้มไม่ถูกจำเป็น "ไม่มีพิกัด" 5 นาที — ผลที่พบจริงต้องได้ทันทีหลังพักจบ
    src2 = FakeSource(centroids={}, failure=HosxpError("down"), fail_methods=["village_centroids"])
    svc2 = service(store, tmp_path, src2, clock, mono)
    svc2.centroid_lookup(["30250105"])
    src2.failure, src2.centroids = None, {"30250105": (1.0, 2.0)}
    mono.t += 61
    assert svc2.centroid_lookup(["30250105"])[0] == {"30250105": (1.0, 2.0)}
    # ไม่มีพิกัดจริง (ฐานตอบว่าง) ไม่ใช่ความล้มเหลว
    src3 = FakeSource(centroids={})
    assert service(store, tmp_path, src3, clock, mono).centroid_lookup(["30250199"]) == ({}, False)


def test_names_not_found_and_failures_are_negative_cached_for_five_minutes(store, tmp_path, clock, mono):
    # ฐานล่ม: address_names จริงกลืนข้อผิดพลาดแล้วคืน {} — ต้องไม่ถามซ้ำทุกคำขอ (ไม่งั้นทุกการเปิด /map รอ connect timeout)
    src = FakeSource(names={"363001": "วังน้ำเขียว"}, failure=HosxpError("down"), fail_methods=["address_names"])
    svc = service(store, tmp_path, src, clock, mono)
    assert svc.names(["363001", "363002"]) == {}
    assert svc.names(["363001", "363002"]) == {}
    assert src.address_names_calls == 1
    mono.t += 301
    src.failure = None
    assert svc.names(["363001", "363002"]) == {"363001": "วังน้ำเขียว"}       # หมดอายุแล้วถามใหม่ ได้ชื่อที่มี
    assert src.address_names_calls == 2
    assert svc.names(["363001", "363002"]) == {"363001": "วังน้ำเขียว"}
    assert src.address_names_calls == 2                                       # ที่พบแคชถาวร ที่ไม่พบรอ 5 นาที
    mono.t += 301
    svc.names(["363002"])
    assert src.address_names_calls == 3                                      # ที่ไม่พบถามใหม่ได้หลังหมดอายุ
