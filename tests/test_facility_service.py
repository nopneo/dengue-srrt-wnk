"""FacilityService — รายชื่อ รพ./รพ.สต. จาก HOSxP + พิกัดใน SQLite + การวาง/ย้ายหมุดโดยผู้ดูแล"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from config import load_settings
from facility_service import FacilityService
from fakes import FakeSource, HosxpError

NOW = datetime(2026, 11, 10, 9, 0)
RPST = "โรงพยาบาลส่งเสริมสุขภาพตำบล"
CATALOG = [
    {"hospcode": "02846", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503"},
    {"hospcode": "02844", "name": RPST + "ยุบอีปูน", "kind": "rpst", "tmb": "302502"},
    {"hospcode": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital", "tmb": "302505"},
]


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def mono():
    return Mono()


def make(store, tmp_path, mono, source="default", trend=None, **env):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
                              "DENGUE_AREA_AMPHOE": "3025", **env})
    if source == "default":
        source = FakeSource(facilities=CATALOG)
    svc = FacilityService(store, source, settings, trend=trend, clock=lambda: NOW, monotonic=mono)
    return svc, source


def seed(store, code="02846", name=RPST + "ระเริง", kind="rpst", tmb="302503", lat=14.43, lng=101.82, src="csv"):
    store.facility_upsert(code, name, kind, tmb, lat, lng, src, "import", NOW)


def test_catalog_cached_one_hour(store, tmp_path, mono):
    svc, src = make(store, tmp_path, mono)
    assert svc.catalog() == (CATALOG, False)
    svc.catalog()
    assert src.facilities_calls == 1
    mono.t += 3599
    svc.catalog()
    assert src.facilities_calls == 1
    mono.t += 2
    svc.catalog()
    assert src.facilities_calls == 2


def test_catalog_failure_backs_off_and_is_not_cached_as_empty(store, tmp_path, mono):
    src = FakeSource(facilities=CATALOG, failure=HosxpError("down"), fail_methods=["facilities"])
    svc, _ = make(store, tmp_path, mono, source=src)
    assert svc.catalog() == ([], True)
    assert svc.catalog() == ([], True)
    assert src.facilities_calls == 1                      # พัก 60 วินาที ไม่ยิงฐานซ้ำ
    src.failure = None
    mono.t += 61
    assert svc.catalog() == (CATALOG, False)              # ไม่ถูกจำเป็น "ไม่มีสถานพยาบาล"


def test_catalog_failure_after_success_keeps_stale_list_flagged(store, tmp_path, mono):
    svc, src = make(store, tmp_path, mono)
    svc.catalog()
    src.failure, src.fail_methods = HosxpError("down"), {"facilities"}
    mono.t += 3601
    cat, failed = svc.catalog()
    assert cat == CATALOG and failed is True


def test_catalog_without_source_or_area(store, tmp_path, mono):
    assert make(store, tmp_path, mono, source=None)[0].catalog() == ([], False)
    svc, src = make(store, tmp_path, mono, DENGUE_AREA_AMPHOE="")        # ไม่ตั้งอำเภอและไม่มี trend -> ไม่มีรายชื่อ
    assert svc.catalog() == ([], False) and src.facilities_calls == 0


def test_catalog_uses_trend_default_area_when_not_configured(store, tmp_path, mono):
    trend = SimpleNamespace(area=lambda: ("3025",), names=lambda codes: {})
    svc, src = make(store, tmp_path, mono, trend=trend, DENGUE_AREA_AMPHOE="")
    assert svc.catalog() == (CATALOG, False)


def test_snapshot_merges_sqlite_and_unplaced(store, tmp_path, mono):
    seed(store)
    svc, _ = make(store, tmp_path, mono)
    snap = svc.snapshot(editable=False)
    assert [f["code"] for f in snap["facilities"]] == ["02846"] and snap["editable"] is False
    assert snap["facilities"][0] == {"code": "02846", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503",
                                     "tmb_name": None, "lat": 14.43, "lng": 101.82, "source": "csv"}
    assert snap["unplaced"] == [{"code": "02844", "name": RPST + "ยุบอีปูน", "kind": "rpst"},
                                {"code": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital"}]
    assert svc.snapshot(editable=True)["editable"] is True


def test_snapshot_tmb_name_from_trend_names_or_none(store, tmp_path, mono):
    seed(store)
    calls = []
    trend = SimpleNamespace(area=lambda: ("3025",), names=lambda codes: calls.append(list(codes)) or {"302503": "ระเริง"})
    svc, _ = make(store, tmp_path, mono, trend=trend)
    assert svc.snapshot(False)["facilities"][0]["tmb_name"] == "ระเริง"
    assert calls == [["302503"]]
    svc2, _ = make(store, tmp_path, mono, trend=SimpleNamespace(area=lambda: (), names=lambda codes: {}))
    assert svc2.snapshot(False)["facilities"][0]["tmb_name"] is None


def test_snapshot_hosxp_down_uses_sqlite_only(store, tmp_path, mono):
    seed(store)
    src = FakeSource(facilities=CATALOG, failure=HosxpError("down"), fail_methods=["facilities"])
    svc, _ = make(store, tmp_path, mono, source=src)
    snap = svc.snapshot(False)
    assert [f["code"] for f in snap["facilities"]] == ["02846"] and snap["unplaced"] == []


def test_place_updates_existing_row_keeping_identity_fields_and_marks_manual(store, tmp_path, mono):
    seed(store)
    svc, _ = make(store, tmp_path, mono)
    assert svc.place("02846", 14.5, 101.9, "admin1").status == "ok"
    got = store.facility_get("02846")
    assert (got["lat"], got["lng"], got["source"], got["updated_by"]) == (14.5, 101.9, "manual", "admin1")
    assert got["name"] == RPST + "ระเริง" and got["kind"] == "rpst" and got["tmb"] == "302503"
    assert got["updated_at"] == NOW.isoformat(timespec="seconds")


def test_place_creates_from_catalog(store, tmp_path, mono):
    svc, _ = make(store, tmp_path, mono)
    assert svc.place("02844", 14.4, 101.7, "admin1").status == "ok"
    got = store.facility_get("02844")
    assert got["name"] == RPST + "ยุบอีปูน" and got["tmb"] == "302502" and got["source"] == "manual"


def test_place_unknown_code_is_unknown_and_creates_nothing(store, tmp_path, mono):
    svc, _ = make(store, tmp_path, mono)
    assert svc.place("99999", 14.4, 101.8, "admin1").status == "unknown"
    assert store.facility_all() == []


def test_place_when_hosxp_down_and_no_row_is_unavailable(store, tmp_path, mono):
    src = FakeSource(facilities=CATALOG, failure=HosxpError("down"), fail_methods=["facilities"])
    svc, _ = make(store, tmp_path, mono, source=src)
    assert svc.place("02844", 14.4, 101.7, "admin1").status == "unavailable"
    assert store.facility_all() == []


def test_place_when_hosxp_down_but_row_exists_still_ok(store, tmp_path, mono):
    seed(store)
    src = FakeSource(facilities=CATALOG, failure=HosxpError("down"), fail_methods=["facilities"])
    svc, _ = make(store, tmp_path, mono, source=src)
    assert svc.place("02846", 14.5, 101.9, "admin1").status == "ok"


def test_place_uses_stale_catalog_when_hosxp_goes_down_later(store, tmp_path, mono):
    svc, src = make(store, tmp_path, mono)
    svc.catalog()
    src.failure, src.fail_methods = HosxpError("down"), {"facilities"}
    mono.t += 3601
    assert svc.place("02844", 14.4, 101.7, "admin1").status == "ok"
    assert svc.place("99999", 14.4, 101.7, "admin1").status == "unavailable"


@pytest.mark.parametrize("lat,lng", [(float("nan"), 101.8), (14.4, float("inf")), (True, 101.8), (14.4, False),
                                     ("14.4", "101.8"), (None, 101.8), (0, 0), (4.0, 101.8), (14.4, 120.0),
                                     (101.8, 14.4)])
def test_place_rejects_bad_coords(store, tmp_path, mono, lat, lng):
    seed(store)
    svc, _ = make(store, tmp_path, mono)
    assert svc.place("02846", lat, lng, "admin1").status == "bad_coords"
    assert store.facility_get("02846")["lat"] == 14.43


def test_place_writes_audit_with_only_code_and_coords(store, tmp_path, mono):
    seed(store)
    svc, _ = make(store, tmp_path, mono)
    svc.place("02846", 14.5, 101.9, "admin1")
    rows = [r for r in store.audit() if r["action"] == "facility_place"]
    assert len(rows) == 1 and rows[0]["user"] == "admin1"
    detail = rows[0]["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail == {"hospcode": "02846", "lat": 14.5, "lng": 101.9}
