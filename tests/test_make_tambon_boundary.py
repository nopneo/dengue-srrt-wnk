"""deploy/make_tambon_boundary.py — สร้างไฟล์ขอบเขตตำบลจาก GeoJSON ใดก็ได้ (ข้อมูลสังเคราะห์ ไม่ออกเน็ต)"""
import importlib.util
import io
import json
import math
import random
from datetime import date
from pathlib import Path

import pytest

import boundary
from boundary import BoundaryError

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("make_tambon_boundary", ROOT / "deploy" / "make_tambon_boundary.py")
mtb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mtb)


def square(lng0, lat0, size=0.04):
    return {"type": "Polygon", "coordinates": [[[lng0, lat0], [lng0 + size, lat0], [lng0 + size, lat0 + size],
                                                 [lng0, lat0 + size], [lng0, lat0]]]}


def feat(code, name, lng0=101.7, lat0=14.3, props=None, geometry=None):
    p = {"ADM3_PCODE": code, "ADM3_TH": name}
    p.update(props or {})
    return {"type": "Feature", "properties": p, "geometry": geometry or square(lng0, lat0)}


DOC = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
       "other": [{"x": 1, "k": [1, 2, {"z": "}"}]}, {"y": 2}],
       "features": [
           feat("TH302501", 'วัง{น้ำ}[เขียว]', 101.70),
           feat("TH302502", 'ก"ข', 101.76),
           feat("TH302503", "ก\\ข", 101.82),
           feat("TH262501", "อำเภออื่น", 101.88),
           feat("TH302504", "อุดมทรัพย์", 101.94),
           feat("TH302505", "ไทยสามัคคี", 102.00, props={"note": "] } \" \\\\ {"})]}


def dump(doc, **kw):
    kw.setdefault("ensure_ascii", False)
    return json.dumps(doc, **kw)


# ---------- iter_features ----------

@pytest.mark.parametrize("text", [dump(DOC), dump(DOC, indent=2).replace("\n", "\r\n"), dump(DOC, indent=1),
                                  dump(DOC, ensure_ascii=True)], ids=["compact", "crlf", "indent1", "ascii"])
@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 1 << 20])
def test_iter_features_matches_json_load_for_any_chunk_size(text, chunk):
    got = list(mtb.iter_features(io.StringIO(text), chunk_size=chunk))
    assert got == [f for f in json.loads(text)["features"]]
    assert len(got) == 6


def test_iter_features_does_not_read_whole_stream():
    text = dump({"type": "FeatureCollection", "features": [feat(f"TH30250{i}", f"ต{i}", 101.7 + i * 0.05) for i in range(1, 6)]})

    class Counting(io.StringIO):
        sizes = []

        def read(self, n=-1):
            out = super().read(n)
            Counting.sizes.append(len(out))
            return out

    s = Counting(text)
    it = mtb.iter_features(s, chunk_size=64)
    next(it)
    assert max(Counting.sizes) <= 64
    assert sum(Counting.sizes) < len(text)                    # ยังไม่อ่านจนจบไฟล์เมื่อได้ feature แรก


def test_iter_features_ignores_objects_outside_features_array():
    got = list(mtb.iter_features(io.StringIO(dump(DOC))))
    assert all(f.get("type") == "Feature" and "ADM3_PCODE" in f["properties"] for f in got)


# ---------- normalize_code / guess_props ----------

@pytest.mark.parametrize("raw,expected", [("TH302501", "302501"), ("th302501", "302501"), ("302501", "302501"),
                                          (" 302501 ", "302501"), (302501, "302501"),
                                          ("", None), (None, None), ("30250", None), ("TH", None),
                                          ("30250a", None), ("٣٠٢٥٠١", None), ("3025011", None)])
def test_normalize_code(raw, expected):
    assert mtb.normalize_code(raw) == expected


def test_guess_props_known_schemas_and_failure():
    assert mtb.guess_props({"ADM3_PCODE": "TH1", "ADM3_TH": "x", "ADM3_EN": "y"}) == ("ADM3_PCODE", "ADM3_TH")
    assert mtb.guess_props({"tam_code": "1", "tam_th": "x"}) == ("tam_code", "tam_th")
    assert mtb.guess_props({"code": "1", "name": "x"}) == ("code", "name")
    assert mtb.guess_props({"foo": 1, "bar": 2}) is None
    assert mtb.guess_props({"ADM3_PCODE": "TH1"}) is None          # ต้องพบทั้งรหัสและชื่อ


# ---------- simplify_geometry ----------

def jagged_ring(n=2000, radius=0.05, noise=0.0003, seed=1):
    rnd = random.Random(seed)
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = radius + rnd.uniform(-noise, noise)
        pts.append([round(101.8 + r * math.cos(a), 6), round(14.4 + r * math.sin(a), 6)])
    pts.append(pts[0])
    return pts


def test_simplify_geometry_reduces_points_keeps_rings_closed_and_valid():
    ring = jagged_ring()
    geom = {"type": "Polygon", "coordinates": [ring]}
    out = mtb.simplify_geometry(geom, 0.0004)
    new = out["coordinates"][0]
    assert len(new) < len(ring) / 3 and new[0] == new[-1] and len(new) >= 4
    boundary.validate_geojson({"type": "FeatureCollection",
                               "features": [{"type": "Feature", "properties": {}, "geometry": out}]})
    multi = {"type": "MultiPolygon", "coordinates": [[ring], [[[101.0, 14.0], [101.1, 14.0], [101.1, 14.1], [101.0, 14.0]]]]}
    out2 = mtb.simplify_geometry(multi, 0.0004)
    assert len(out2["coordinates"]) == 2 and out2["coordinates"][1][0][0] == out2["coordinates"][1][0][-1]
    tiny = {"type": "Polygon", "coordinates": [[[101.0, 14.0], [101.001, 14.0], [101.001, 14.001], [101.0, 14.0]]]}
    assert mtb.simplify_geometry(tiny, 5.0)["coordinates"][0] == tiny["coordinates"][0]    # ยุบจนต่ำกว่า 4 จุด -> คงเดิม


# ---------- build_collection ----------

def test_build_collection_filters_by_amphoe_and_sets_properties():
    col = mtb.build_collection("3025", DOC["features"], "ADM3_PCODE", "ADM3_TH", "ทดสอบ", date(2026, 11, 10))
    codes = [f["properties"]["code"] for f in col["features"]]
    assert codes == ["302501", "302502", "302503", "302504", "302505"]
    assert col["features"][0]["properties"] == {"code": "302501", "name": "วัง{น้ำ}[เขียว]",
                                                "source": "ทดสอบ", "fetched": "2026-11-10"}


def test_build_collection_rejects_duplicates_and_blank_names():
    dup = [feat("TH302501", "ก"), feat("TH302501", "ข", 101.8)]
    with pytest.raises(BoundaryError):
        mtb.build_collection("3025", dup, "ADM3_PCODE", "ADM3_TH", "s", date(2026, 11, 10))
    blank = [feat("TH302501", "  ")]
    with pytest.raises(BoundaryError):
        mtb.build_collection("3025", blank, "ADM3_PCODE", "ADM3_TH", "s", date(2026, 11, 10))


# ---------- main ----------

def write_source(tmp_path, doc=None, text=None):
    p = tmp_path / "src.geojson"
    p.write_text(text if text is not None else dump(doc or DOC), encoding="utf-8")
    return p


def run(tmp_path, *args, doc=None, text=None):
    src = write_source(tmp_path, doc, text)
    out = tmp_path / "out"
    return mtb.main(["3025", str(src), "--out-dir", str(out), *args]), out


def test_main_writes_validated_file_with_atomic_replace(tmp_path, capsys):
    rc, out = run(tmp_path)
    assert rc == 0
    f = out / "3025.tambon.geojson"
    doc = boundary.load_tambons("3025", out)
    assert doc is not None and len(doc["features"]) == 5
    raw = f.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and raw.count(b"\n") == 1 and "ไทยสามัคคี".encode() in raw
    assert not list(out.glob("*.tmp"))
    said = capsys.readouterr().out
    assert "ADM3_PCODE" in said and "ADM3_TH" in said and "5" in said


def test_main_expect_missing_refuses_to_write(tmp_path):
    rc, out = run(tmp_path, "--expect", "302501,302506")
    assert rc == 1 and not (out / "3025.tambon.geojson").exists()
    rc, out = run(tmp_path, "--expect", "302501,302505")
    assert rc == 0


def test_main_refuses_overwrite_without_force_and_force_replaces(tmp_path):
    rc, out = run(tmp_path)
    f = out / "3025.tambon.geojson"
    first = f.read_bytes()
    rc2 = mtb.main(["3025", str(tmp_path / "src.geojson"), "--out-dir", str(out), "--source-label", "ใหม่"])
    assert rc2 == 1 and f.read_bytes() == first
    rc3 = mtb.main(["3025", str(tmp_path / "src.geojson"), "--out-dir", str(out), "--source-label", "ใหม่", "--force"])
    assert rc3 == 0 and "ใหม่".encode() in f.read_bytes()


def test_main_guess_failure_lists_properties_and_exits_1(tmp_path, capsys):
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"foo": "1", "bar": "x"}, "geometry": square(101.7, 14.3)}]}
    rc, out = run(tmp_path, doc=doc)
    assert rc == 1 and "foo" in capsys.readouterr().out and not out.exists()
    rc, _ = run(tmp_path, "--code-prop", "foo", "--name-prop", "bar", doc=doc)       # ระบุเองได้ แต่ไม่มีตำบลของอำเภอ
    assert rc == 1


def test_main_custom_props_and_no_matching_tambon(tmp_path):
    doc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"c": "302501", "n": "ก"}, "geometry": square(101.7, 14.3)}]}
    rc, out = run(tmp_path, "--code-prop", "c", "--name-prop", "n", doc=doc)
    assert rc == 0 and boundary.load_tambons("3025", out) is not None
    only_other = {"type": "FeatureCollection", "features": [feat("TH262501", "อื่น")]}
    sub = tmp_path / "x"
    sub.mkdir()
    rc, _ = run(sub, doc=only_other)
    assert rc == 1


def test_main_rejects_bad_amphoe_code_and_missing_source(tmp_path):
    src = write_source(tmp_path)
    assert mtb.main(["30x5", str(src), "--out-dir", str(tmp_path / "o")]) == 1
    assert mtb.main(["3025", str(tmp_path / "nope.geojson"), "--out-dir", str(tmp_path / "o")]) == 1


def big_doc():
    ring = jagged_ring(3000)
    return {"type": "FeatureCollection", "features": [feat("TH302501", "ก", geometry={"type": "Polygon", "coordinates": [ring]})]}


def test_main_simplifies_when_over_size_limit(tmp_path, monkeypatch):
    full = len(dump(big_doc()).encode())
    monkeypatch.setattr(boundary, "MAX_BYTES", full // 3)
    rc, out = run(tmp_path, doc=big_doc())
    assert rc == 0
    f = out / "3025.tambon.geojson"
    assert f.stat().st_size <= full // 3 and boundary.load_tambons("3025", out) is not None


def test_main_oversize_after_simplify_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(boundary, "MAX_BYTES", 200)
    rc, out = run(tmp_path, doc=big_doc())
    assert rc == 1 and not (out / "3025.tambon.geojson").exists() and not list(out.glob("*.tmp"))


def test_main_makes_no_network_calls(tmp_path, monkeypatch):
    import socket

    import requests

    def boom(*a, **k):
        raise AssertionError("ห้ามออกเครือข่าย")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(requests, "get", boom)
    rc, _ = run(tmp_path)
    assert rc == 0


def test_guess_props_is_case_insensitive_and_knows_ocha_lowercase_schema():
    # ไฟล์จริงของ UN OCHA/HDX (tha_admin3.geojson): adm3_pcode + ชื่อไทยที่ adm3_name1 (adm3_name เป็นอังกฤษ)
    real = {"adm3_name": "Phraborom Maharatchawang", "adm3_name1": "พระบรมมหาราชวัง", "adm3_name2": None,
            "adm3_pcode": "TH100101", "adm2_pcode": "TH1001", "adm1_pcode": "TH10"}
    assert mtb.guess_props(real) == ("adm3_pcode", "adm3_name1")
    assert mtb.guess_props({"Tam_Code": "1", "TAM_TH": "x"}) == ("Tam_Code", "TAM_TH")   # คืนชื่อฟิลด์ตามที่เขียนในไฟล์
    assert mtb.guess_props({"ADM3_PCODE": "TH1", "ADM3_TH": "x", "ADM3_EN": "y"}) == ("ADM3_PCODE", "ADM3_TH")
    assert mtb.guess_props({"adm3_pcode": "TH1", "adm3_name": "English only"}) == ("adm3_pcode", "adm3_name")
