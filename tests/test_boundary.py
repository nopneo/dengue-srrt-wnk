"""boundary.py + deploy/fetch_boundary.py — ตรวจ/โหลดเส้นขอบอำเภอ (ข้อมูลสังเคราะห์ ไม่ออกเน็ต)"""
import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest
import requests

import boundary
from boundary import BoundaryError, MAX_BYTES

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("fetch_boundary", ROOT / "deploy" / "fetch_boundary.py")
fetch_boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_boundary)

RING = [[101.5, 14.2], [102.0, 14.2], [102.0, 14.6], [101.5, 14.6], [101.5, 14.2]]


def poly(ring=RING):
    return {"type": "Polygon", "coordinates": [ring]}


def fc(geometry=None, **props):
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": props, "geometry": geometry or poly()}]}


def result(osm_id=18988716, geometry=None, typ="administrative", osm_type="relation", name="อำเภอทดสอบ"):
    return {"osm_type": osm_type, "osm_id": osm_id, "type": typ, "display_name": name,
            "geojson": geometry or poly()}


# ---------- validate_geojson ----------

def test_validate_accepts_polygon_multipolygon_and_bare_feature():
    assert boundary.validate_geojson(fc())["type"] == "FeatureCollection"
    multi = {"type": "MultiPolygon", "coordinates": [[RING], [[[101.6, 14.3], [101.7, 14.3], [101.7, 14.4],
                                                              [101.6, 14.3]]]]}
    assert len(boundary.validate_geojson(fc(multi))["features"]) == 1
    bare = {"type": "Feature", "properties": {}, "geometry": poly()}
    out = boundary.validate_geojson(bare)
    assert out["type"] == "FeatureCollection" and len(out["features"]) == 1
    holed = {"type": "Polygon", "coordinates": [RING, [[101.7, 14.3], [101.8, 14.3], [101.8, 14.4],
                                                          [101.7, 14.3]]]}
    boundary.validate_geojson(fc(holed))


@pytest.mark.parametrize("geometry", [
    {"type": "Point", "coordinates": [101.5, 14.2]},
    {"type": "LineString", "coordinates": RING},
    {"type": "GeometryCollection", "geometries": []},
    None,
])
def test_validate_rejects_bad_geometry_types(geometry):
    doc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geometry}]}
    with pytest.raises(BoundaryError):
        boundary.validate_geojson(doc)


@pytest.mark.parametrize("doc", [None, [], "x", {}, {"type": "FeatureCollection", "features": []},
                                 {"type": "FeatureCollection"}])
def test_validate_rejects_non_geojson(doc):
    with pytest.raises(BoundaryError):
        boundary.validate_geojson(doc)


@pytest.mark.parametrize("bad", [
    [[float("nan"), 14.2], [102.0, 14.2], [102.0, 14.6], [float("nan"), 14.2]],
    [[float("inf"), 14.2], [102.0, 14.2], [102.0, 14.6], [float("inf"), 14.2]],
    [["101.5", 14.2], [102.0, 14.2], [102.0, 14.6], ["101.5", 14.2]],
    [[True, 14.2], [102.0, 14.2], [102.0, 14.6], [True, 14.2]],
    [[14.2, 101.5], [14.2, 102.0], [14.6, 102.0], [14.2, 101.5]],        # สลับ lat/lng -> นอกกรอบ
    [[-74.0, 40.7], [-73.9, 40.7], [-73.9, 40.8], [-74.0, 40.7]],        # นอกประเทศไทย
    [[101.5, 14.2], [102.0, 14.2], [101.5, 14.2]],                       # < 4 จุด
    [[101.5], [102.0, 14.2], [102.0, 14.6], [101.5, 14.2]],              # จุดไม่ครบสองค่า
])
def test_validate_rejects_bad_coordinates(bad):
    with pytest.raises(BoundaryError):
        boundary.validate_geojson(fc(poly(bad)))


# ---------- load_boundary / load_boundaries ----------

def write(base, code, doc=None, raw=None):
    p = base / f"{code}.geojson"
    p.write_bytes(raw if raw is not None else json.dumps(doc or fc(code=code)).encode("utf-8"))
    return p


@pytest.mark.parametrize("code", ["../x", "30250", "30 5", "", "3025/", "abcd", "٣٠٢٥", None])
def test_load_boundary_rejects_non_four_digit_codes(tmp_path, code):
    outside = tmp_path / "outside.geojson"
    outside.write_text(json.dumps(fc()), encoding="utf-8")
    base = tmp_path / "b"
    base.mkdir()
    assert boundary.load_boundary(code, base) is None


def test_load_boundary_missing_oversize_and_corrupt_files_return_none(tmp_path):
    assert boundary.load_boundary("3025", tmp_path) is None                       # ไม่มีไฟล์
    write(tmp_path, "3026", raw=b"{not json")
    assert boundary.load_boundary("3026", tmp_path) is None                       # JSON เสีย
    write(tmp_path, "3027", raw=b" " * (MAX_BYTES + 1))
    assert boundary.load_boundary("3027", tmp_path) is None                       # ใหญ่เกิน
    write(tmp_path, "3028", fc({"type": "Point", "coordinates": [101.5, 14.2]}))
    assert boundary.load_boundary("3028", tmp_path) is None                       # geometry ผิด
    write(tmp_path, "3029", fc())
    assert boundary.load_boundary("3029", tmp_path)["type"] == "FeatureCollection"


def test_load_boundaries_returns_features_and_missing_in_order(tmp_path):
    write(tmp_path, "3025")
    write(tmp_path, "3027", raw=b"nope")
    features, missing = boundary.load_boundaries(["3026", "3025", "3027", "../x"], tmp_path)
    assert len(features) == 1 and missing == ["3026", "3027", "../x"]


def test_load_boundary_does_not_log_file_content(tmp_path, caplog):
    write(tmp_path, "3026", raw=b'{"secret-looking": "SENSITIVE-TEXT"')
    with caplog.at_level("DEBUG"):
        boundary.load_boundary("3026", tmp_path)
    assert "SENSITIVE-TEXT" not in caplog.text


# ---------- pick_result / build_document ----------

def test_pick_result_requires_unique_admin_polygon():
    one = result(1)
    point = result(2, geometry={"type": "Point", "coordinates": [101.5, 14.2]})
    assert boundary.pick_result([point, one], None)["osm_id"] == 1
    with pytest.raises(BoundaryError) as e:
        boundary.pick_result([result(11, name="A"), result(22, name="B")], None)
    assert "11" in str(e.value) and "22" in str(e.value)
    with pytest.raises(BoundaryError):
        boundary.pick_result([point], None)
    with pytest.raises(BoundaryError):
        boundary.pick_result([], None)
    assert boundary.pick_result([result(11), result(22)], 22)["osm_id"] == 22
    with pytest.raises(BoundaryError):
        boundary.pick_result([result(11)], 99)
    with pytest.raises(BoundaryError):
        boundary.pick_result([result(11, osm_type="way")], 11)       # ต้องเป็น relation


def test_build_document_properties_and_validates():
    doc = boundary.build_document("3025", result(18988716, name="อำเภอวังน้ำเขียว"), date(2026, 11, 10))
    props = doc["features"][0]["properties"]
    assert props == {"code": "3025", "name": "อำเภอวังน้ำเขียว",
                     "source": "OpenStreetMap relation 18988716", "fetched": "2026-11-10"}
    boundary.validate_geojson(doc)
    with pytest.raises(BoundaryError):
        boundary.build_document("3025", result(1, geometry={"type": "Point", "coordinates": [101.5, 14.2]}),
                                date(2026, 11, 10))


# ---------- สคริปต์ดึง ----------

def run(argv, tmp_path, results=None, error=None):
    calls = []

    def fetch(**kw):
        calls.append(kw)
        if error:
            raise error
        return results if results is not None else [result()]

    rc = fetch_boundary.main(argv + ["--out-dir", str(tmp_path)], fetch=fetch)
    return rc, calls


def test_fetch_script_writes_atomically_and_refuses_overwrite_without_force(tmp_path):
    rc, calls = run(["3025", "--relation", "18988716"], tmp_path)
    assert rc == 0 and calls == [{"query": None, "relation": 18988716}]
    out = tmp_path / "3025.geojson"
    first = out.read_bytes()
    assert b"\r" not in first and boundary.load_boundary("3025", tmp_path) is not None
    assert not list(tmp_path.glob("*.tmp"))
    rc, _ = run(["3025", "--relation", "18988716"], tmp_path, results=[result(name="เปลี่ยน")])
    assert rc == 1 and out.read_bytes() == first
    rc, _ = run(["3025", "--relation", "18988716", "--force"], tmp_path, results=[result(name="เปลี่ยน")])
    assert rc == 0 and out.read_bytes() != first


def test_fetch_script_exit_codes(tmp_path):
    rc, _ = run(["3025", "--query", "x"], tmp_path, results=[])
    assert rc == 1 and not list(tmp_path.iterdir())                         # ไม่เจอ
    rc, _ = run(["3025", "--query", "x"], tmp_path, results=[result(1), result(2)])
    assert rc == 1 and not list(tmp_path.iterdir())                         # กำกวม
    rc, _ = run(["3025", "--query", "x"], tmp_path, error=requests.ConnectionError("down"))
    assert rc == 2 and not list(tmp_path.iterdir())                         # เครือข่ายล้ม
    rc, _ = run(["30x5", "--query", "x"], tmp_path)
    assert rc == 1
    with pytest.raises(SystemExit):                                         # ไม่ระบุทั้ง --relation และ --query
        fetch_boundary.main(["3025", "--out-dir", str(tmp_path)], fetch=lambda **k: [])
    with pytest.raises(SystemExit):                                         # ระบุทั้งสองอย่าง
        fetch_boundary.main(["3025", "--query", "a", "--relation", "1", "--out-dir", str(tmp_path)],
                            fetch=lambda **k: [])


def test_fetch_script_rejects_oversize_document(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, "MAX_BYTES", 100)
    monkeypatch.setattr(fetch_boundary.boundary, "MAX_BYTES", 100)
    rc, _ = run(["3025", "--relation", "1"], tmp_path, results=[result(1)])
    assert rc == 1 and not (tmp_path / "3025.geojson").exists()


# ---------- ไฟล์ที่ส่งไปกับโค้ด ----------

def test_committed_boundary_for_3025_is_valid():
    doc = boundary.load_boundary("3025", boundary.BOUNDARY_DIR)
    assert doc is not None
    props = doc["features"][0]["properties"]
    assert props["code"] == "3025" and props["source"].startswith("OpenStreetMap relation")
    raw = (boundary.BOUNDARY_DIR / "3025.geojson").read_bytes()
    assert b"\r" not in raw and len(raw) <= MAX_BYTES


# ---------- ไฟล์ขอบเขตตำบล ----------

def tambon_doc(*items):
    feats = [{"type": "Feature", "properties": p, "geometry": poly()} for p in items]
    return {"type": "FeatureCollection", "features": feats}


def tp(code="302501", name="วังน้ำเขียว"):
    return {"code": code, "name": name}


def test_validate_tambon_collection_ok_and_errors():
    ok = boundary.validate_tambon_collection(tambon_doc(tp("302501"), tp("302502", "วังหมี")), "3025")
    assert len(ok["features"]) == 2
    bad = [tp("30250"), tp("3025011"), tp("30250a"), tp("262501"), tp("٣٠٢٥٠١"), {"code": 302501, "name": "x"},
           tp("302501", ""), tp("302501", "   "), {"code": "302501"}, {"name": "x"}, {}]
    for props in bad:
        with pytest.raises(BoundaryError):
            boundary.validate_tambon_collection(tambon_doc(props), "3025")
    with pytest.raises(BoundaryError):                                         # รหัสซ้ำ
        boundary.validate_tambon_collection(tambon_doc(tp("302501"), tp("302501", "ซ้ำ")), "3025")
    with pytest.raises(BoundaryError):                                         # geometry ผิดยังถูกปฏิเสธ
        doc = tambon_doc(tp())
        doc["features"][0]["geometry"] = {"type": "Point", "coordinates": [101.5, 14.2]}
        boundary.validate_tambon_collection(doc, "3025")


def test_load_tambons_rejects_bad_codes_and_files(tmp_path):
    for code in ("../x", "30250", "", None, "abcd", "٣٠٢٥"):
        assert boundary.load_tambons(code, tmp_path) is None
    assert boundary.load_tambons("3025", tmp_path) is None                      # ไม่มีไฟล์
    (tmp_path / "3026.tambon.geojson").write_bytes(b"{not json")
    assert boundary.load_tambons("3026", tmp_path) is None
    (tmp_path / "3027.tambon.geojson").write_bytes(b" " * (MAX_BYTES + 1))
    assert boundary.load_tambons("3027", tmp_path) is None
    (tmp_path / "3028.tambon.geojson").write_text(json.dumps(tambon_doc(tp("262501"))), encoding="utf-8")
    assert boundary.load_tambons("3028", tmp_path) is None                      # รหัสตำบลไม่ขึ้นต้นด้วยอำเภอ
    (tmp_path / "3029.tambon.geojson").write_text(json.dumps(tambon_doc(tp("302901", "ก"))), encoding="utf-8")
    assert boundary.load_tambons("3029", tmp_path)["type"] == "FeatureCollection"


def test_load_tambons_many_orders_features_and_missing(tmp_path):
    (tmp_path / "3025.tambon.geojson").write_text(json.dumps(tambon_doc(tp("302501"), tp("302502", "ข"))), encoding="utf-8")
    (tmp_path / "3029.tambon.geojson").write_bytes(b"nope")
    feats, missing = boundary.load_tambons_many(["3026", "3025", "3029"], tmp_path)
    assert [f["properties"]["code"] for f in feats] == ["302501", "302502"] and missing == ["3026", "3029"]


def test_point_in_boundary_polygon_hole_and_multipolygon():
    outer = [[101.5, 14.2], [102.0, 14.2], [102.0, 14.6], [101.5, 14.6], [101.5, 14.2]]
    hole = [[101.7, 14.3], [101.8, 14.3], [101.8, 14.4], [101.7, 14.4], [101.7, 14.3]]
    second = [[101.0, 14.0], [101.1, 14.0], [101.1, 14.1], [101.0, 14.1], [101.0, 14.0]]
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [outer, hole]}},
        {"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": [[second]]}}]}
    assert boundary.point_in_boundary(14.5, 101.6, doc) is True          # ในวงนอก
    assert boundary.point_in_boundary(14.35, 101.75, doc) is False       # ในรู
    assert boundary.point_in_boundary(14.05, 101.05, doc) is True        # ในพื้นที่ที่สองของ MultiPolygon
    assert boundary.point_in_boundary(15.0, 101.6, doc) is False
    assert boundary.point_in_boundary(14.5, 103.0, doc) is False


def test_point_in_boundary_real_district_file():
    doc = boundary.load_boundary("3025", boundary.BOUNDARY_DIR)
    assert boundary.point_in_boundary(14.41768128, 101.8630457, doc) is True      # รพ.วังน้ำเขียว
    assert boundary.point_in_boundary(13.75, 100.5, doc) is False                  # กรุงเทพ


def test_committed_tambon_file_for_3025_is_valid_and_credited():
    doc = boundary.load_tambons("3025", boundary.BOUNDARY_DIR)
    assert doc is not None
    codes = sorted(f["properties"]["code"] for f in doc["features"])
    assert codes == ["302501", "302502", "302503", "302504", "302505"]
    names = {f["properties"]["code"]: f["properties"]["name"] for f in doc["features"]}
    assert names == {"302501": "วังน้ำเขียว", "302502": "วังหมี", "302503": "ระเริง", "302504": "อุดมทรัพย์",
                     "302505": "ไทยสามัคคี"}
    for f in doc["features"]:
        assert "CC BY-IGO" in f["properties"]["source"] and "OCHA" in f["properties"]["source"]
    raw = (boundary.BOUNDARY_DIR / "3025.tambon.geojson").read_bytes()
    assert b"\r" not in raw and len(raw) <= MAX_BYTES
    # ตำบลทั้งหมดอยู่ในกรอบอำเภอ: จุดกึ่งกลางกรอบของแต่ละตำบลต้องตกใกล้/ในเส้นขอบอำเภอ (กันไฟล์ผิดอำเภอ)
    district = boundary.load_boundary("3025", boundary.BOUNDARY_DIR)
    inside = 0
    for f in doc["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        pts = [p for poly in polys for p in poly[0]]
        lng = sum(p[0] for p in pts) / len(pts)
        lat = sum(p[1] for p in pts) / len(pts)
        inside += boundary.point_in_boundary(lat, lng, district)
    assert inside >= 4
