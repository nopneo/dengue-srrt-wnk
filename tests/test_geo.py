from datetime import datetime, timedelta

from geo import find_clusters, haversine_m, village_code

# 0.0009 deg latitude ~ 100 m
LAT0 = 14.4000
LNG0 = 101.8000
D100 = 0.0009
D90 = 0.00081
T0 = datetime(2026, 11, 1, 8, 0, 0)


def _case(cid, lat=LAT0, lng=LNG0, dx_at=T0, pin_state="confirmed", status="active"):
    return {
        "id": cid,
        "lat": lat,
        "lng": lng,
        "dx_at": dx_at,
        "pin_state": pin_state,
        "status": status,
    }


def test_haversine_100m():
    d = haversine_m(LAT0, LNG0, LAT0 + D100, LNG0)
    assert 95 <= d <= 105


def test_village_code_pads_moo():
    assert village_code("30", "20", "03", "7") == "30200307"


def test_village_code_none_for_blank_or_zero_moo():
    assert village_code("30", "20", "03", "") is None
    assert village_code("30", "20", "03", None) is None
    assert village_code("30", "20", "03", "00") is None
    assert village_code("30", "20", "03", "0") is None
    assert village_code("30", "20", "03", "x") is None


def test_cluster_two_confirmed_within_100m_14d():
    cases = [
        _case(1),
        _case(2, lat=LAT0 + D90, dx_at=T0 + timedelta(days=14)),
    ]
    assert find_clusters(cases, 100, 14, 2) == [frozenset({1, 2})]


def test_cluster_ignores_estimated_pins():
    cases = [
        _case(1, pin_state="estimated"),
        _case(2, pin_state="estimated"),
    ]
    assert find_clusters(cases, 100, 14, 2) == []


def test_cluster_ignores_cancelled():
    cases = [_case(1), _case(2, status="cancelled")]
    assert find_clusters(cases, 100, 14, 2) == []


def test_cluster_chain_links_three_cases():
    a = _case(1)
    b = _case(2, lat=LAT0 + D90)
    c = _case(3, lat=LAT0 + 2 * D90)
    assert haversine_m(a["lat"], a["lng"], c["lat"], c["lng"]) > 100
    assert find_clusters([a, b, c], 100, 14, 2) == [frozenset({1, 2, 3})]


def test_cluster_respects_day_window():
    cases = [
        _case(1),
        _case(2, lat=LAT0 + D90, dx_at=T0 + timedelta(days=15)),
    ]
    assert find_clusters(cases, 100, 14, 2) == []

# ---------- cluster ระดับหมู่บ้าน (find_village_clusters) ----------
from geo import find_village_clusters  # noqa: E402


def _vcase(cid, village="30250101", days=0, status="active"):
    return {"id": cid, "village_code": village, "dx_at": T0 + timedelta(days=days), "status": status}


def test_village_cluster_same_village_within_days():
    cases = [_vcase(1), _vcase(2, days=3), _vcase(3, village="30250102")]
    assert find_village_clusters(cases, 7, 2) == [frozenset({1, 2})]


def test_village_cluster_needs_min_cases_and_respects_window():
    assert find_village_clusters([_vcase(1), _vcase(2, days=8)], 7, 2) == []
    assert find_village_clusters([_vcase(1), _vcase(2)], 7, 3) == []


def test_village_cluster_chain_is_one_group():
    cases = [_vcase(1), _vcase(2, days=6), _vcase(3, days=12)]
    assert find_village_clusters(cases, 7, 2) == [frozenset({1, 2, 3})]


def test_village_cluster_ignores_cancelled_and_missing_village():
    cases = [_vcase(1), _vcase(2, status="cancelled"), _vcase(3, village=None), _vcase(4, village="")]
    assert find_village_clusters(cases, 7, 2) == []


def test_village_cluster_different_villages_never_merge_and_sorted_by_min_id():
    cases = [_vcase(5, "30250102"), _vcase(6, "30250102"), _vcase(1), _vcase(2)]
    assert find_village_clusters(cases, 7, 2) == [frozenset({1, 2}), frozenset({5, 6})]
