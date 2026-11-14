"""facilities.py — จัดประเภท/จับคู่สถานพยาบาล และอ่าน CSV พิกัด (ข้อมูลสังเคราะห์โครงเดียวกับไฟล์จริงของ รพ.)"""
import math
from pathlib import Path

import pytest

import facilities
from facilities import CsvRow

HEADER = "รหัสหน่วยงาน,กระทรวง,กรม,หน่วยงาน,หมุดที่ไม่พบความผิดปกติ,ที่อยู่จุดบริการ,ละติจูด,ลองติจูด"


def line(name, lat, lng, code="8.89921E+12"):
    return f"{code},กระทรวงสาธารณสุข,สำนักงานปลัดกระทรวงสาธารณสุข,{name},Y,หมู่ 1 ต.ระเริง,{lat},{lng}"


PROV = " จังหวัดนครราชสีมา"
RPST = "โรงพยาบาลส่งเสริมสุขภาพตำบล"
CATALOG = [
    {"hospcode": "02846", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503"},
    {"hospcode": "02844", "name": RPST + "ยุบอีปูน", "kind": "rpst", "tmb": "302502"},
    {"hospcode": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital", "tmb": "302505"},
]


def write_csv(tmp_path, *lines, bom=True, name="f.csv", head=HEADER, trailing="\n\n"):
    p = tmp_path / name
    text = "\n".join([head, *lines]) + trailing
    p.write_bytes((("﻿" if bom else "") + text).encode("utf-8"))
    return p


def test_classify():
    assert facilities.classify(RPST + "ระเริง") == "rpst"
    assert facilities.classify("โรงพยาบาลวังน้ำเขียว") == "hospital"
    assert facilities.classify("โรงพยาบาลค่ายสุรนารี") == "hospital"
    for other in ("สำนักงานสาธารณสุขอำเภอวังน้ำเขียว", "คลินิกนายแพทย์วิชาญ", "", "  ", "รพ.สต.ระเริง"):
        assert facilities.classify(other) is None


def test_normalize_name_strips_province_suffix_and_spaces():
    assert facilities.normalize_name(RPST + "ระเริง" + PROV) == RPST + "ระเริง"
    assert facilities.normalize_name("  โรงพยาบาล   วังน้ำเขียว   จังหวัดนครราชสีมา ") == "โรงพยาบาล วังน้ำเขียว"
    assert facilities.normalize_name("โรงพยาบาลวังน้ำเขียว") == "โรงพยาบาลวังน้ำเขียว"
    assert facilities.normalize_name("") == ""


@pytest.mark.parametrize("raw,expected", [("8.89921E+12", True), ("8.89921e12", True), ("1E5", True),
                                          ("12345", False), ("02846", False), ("GA0002846", False), ("", False),
                                          ("1.5", False)])
def test_looks_scientific(raw, expected):
    assert facilities.looks_scientific(raw) is expected


@pytest.mark.parametrize("lat,lng", [(float("nan"), 101.8), (14.4, float("inf")), (True, 101.8), (14.4, False),
                                     ("14.4", "101.8"), (None, 101.8), (0, 0), (0.0, 0.0), (4.9, 101.8), (21.1, 101.8),
                                     (14.4, 96.9), (14.4, 106.1), (101.8, 14.4), ([14.4], 101.8)])
def test_valid_coords_rejects_bad_values(lat, lng):
    assert facilities.valid_coords(lat, lng) is None


def test_valid_coords_accepts_good_values():
    assert facilities.valid_coords(14.42899329, 101.8211519) == (14.42899329, 101.8211519)
    assert facilities.valid_coords(14, 102) == (14.0, 102.0)


def test_read_csv_handles_bom_headers_blank_rows_and_scientific_code(tmp_path):
    p = write_csv(tmp_path, line(RPST + "ระเริง" + PROV, "14.42899329", "101.8211519"),
                  line("โรงพยาบาลวังน้ำเขียว" + PROV, "14.41768128", "101.8630457"), ",,,,,,,", "")
    rows, problems = facilities.read_csv(p)
    assert [r.name for r in rows] == [RPST + "ระเริง" + PROV, "โรงพยาบาลวังน้ำเขียว" + PROV]
    assert rows[0].lat == pytest.approx(14.42899329) and rows[0].lng == pytest.approx(101.8211519)
    assert all(r.code is None for r in rows)                  # 8.89921E+12 ไม่ถูกใช้เป็นรหัส
    assert rows[0].line == 2 and not problems


def test_read_csv_without_bom_and_crlf(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes((HEADER + "\r\n" + line(RPST + "ระเริง", "14.4", "101.8") + "\r\n").encode("utf-8"))
    rows, _ = facilities.read_csv(p)
    assert len(rows) == 1 and rows[0].lat == 14.4


def test_read_csv_accepts_alternative_headers_and_real_codes(tmp_path):
    p = tmp_path / "alt.csv"
    p.write_text("hospcode,name,lat,lng\n02846,ระเริง,14.4,101.8\n2846,อื่น,14.5,101.9\nGA0002846,ก,14.1,101.1\n",
                 encoding="utf-8")
    rows, _ = facilities.read_csv(p)
    assert [r.code for r in rows] == ["02846", None, None]    # เฉพาะเลข 5 หลักตรง ๆ
    p2 = tmp_path / "alt2.csv"
    p2.write_text("รหัสสถานพยาบาล,ชื่อ,latitude,ลองจิจูด\n10894,รพ,14.4,101.8\n", encoding="utf-8")
    rows2, _ = facilities.read_csv(p2)
    assert rows2[0].code == "10894" and rows2[0].lng == 101.8


def test_read_csv_non_numeric_coords_are_kept_raw_for_reporting(tmp_path):
    p = write_csv(tmp_path, line("ก", "abc", "101.8"), line("ข", "", "101.8"))
    rows, _ = facilities.read_csv(p)
    assert [r.lat for r in rows] == ["abc", ""]
    assert facilities.valid_coords(rows[0].lat, rows[0].lng) is None


def test_read_csv_missing_columns_raises_thai_message(tmp_path):
    p = write_csv(tmp_path, "x,y", head="a,b")
    with pytest.raises(ValueError) as e:
        facilities.read_csv(p)
    assert "คอลัมน์" in str(e.value)
    p2 = tmp_path / "nolng.csv"
    p2.write_text("หน่วยงาน,ละติจูด\nก,14.4\n", encoding="utf-8")
    with pytest.raises(ValueError) as e2:
        facilities.read_csv(p2)
    assert "ลอง" in str(e2.value)


def test_read_csv_undecodable_raises(tmp_path):
    p = tmp_path / "tis.csv"
    p.write_bytes((HEADER + "\n" + line("รพ.สต.ระเริง", "14.4", "101.8")).encode("cp874"))
    with pytest.raises(ValueError) as e:
        facilities.read_csv(p)
    assert "UTF-8" in str(e.value)
    with pytest.raises(ValueError):
        facilities.read_csv(tmp_path / "does-not-exist.csv")


def csv_row(name, lat=14.43, lng=101.82, code=None, n=2):
    return CsvRow(line=n, name=name, code=code, lat=lat, lng=lng)


def test_match_rows_by_name_when_code_is_scientific():
    res = facilities.match_rows([csv_row(RPST + "ระเริง" + PROV), csv_row("โรงพยาบาลวังน้ำเขียว" + PROV, 14.41, 101.86)],
                                CATALOG)
    assert [(c["hospcode"], lat, lng) for c, lat, lng in res.matched] == [("02846", 14.43, 101.82),
                                                                          ("10894", 14.41, 101.86)]
    assert res.unmatched == res.ambiguous == res.bad_coords == []
    assert [m["hospcode"] for m in res.missing] == ["02844"]


def test_match_rows_prefers_valid_code():
    res = facilities.match_rows([csv_row("ชื่ออื่นไม่ตรง", code="02846")], CATALOG)
    assert [c["hospcode"] for c, _, _ in res.matched] == ["02846"] and not res.unmatched
    res2 = facilities.match_rows([csv_row("ชื่ออื่นไม่ตรง", code="99999")], CATALOG)
    assert res2.matched == [] and res2.unmatched == ["ชื่ออื่นไม่ตรง"]


def test_match_rows_reports_unmatched_ambiguous_bad_coords_and_missing():
    catalog = CATALOG + [{"hospcode": "02900", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503"}]
    rows = [csv_row(RPST + "ระเริง"),                          # ชื่อตรงสองรหัส -> กำกวม
            csv_row("ไม่มีในฮอสพ"),                              # ไม่ตรง
            csv_row("โรงพยาบาลวังน้ำเขียว", lat="x"),            # พิกัดเสีย
            csv_row(RPST + "ยุบอีปูน", lat=float("nan"))]        # พิกัดเสีย
    res = facilities.match_rows(rows, catalog)
    assert res.matched == []
    assert res.ambiguous == [RPST + "ระเริง"] and res.unmatched == ["ไม่มีในฮอสพ"]
    assert res.bad_coords == ["โรงพยาบาลวังน้ำเขียว", RPST + "ยุบอีปูน"]
    assert sorted(m["hospcode"] for m in res.missing) == ["02844", "02846", "02900", "10894"]


def test_match_rows_duplicate_in_file_first_wins_rest_ambiguous():
    rows = [csv_row("โรงพยาบาลวังน้ำเขียว", 14.41, 101.86, n=2), csv_row("โรงพยาบาลวังน้ำเขียว", 14.42, 101.87, n=3)]
    res = facilities.match_rows(rows, CATALOG)
    assert len(res.matched) == 1 and res.matched[0][1] == 14.41
    assert res.ambiguous == ["ซ้ำในไฟล์: โรงพยาบาลวังน้ำเขียว"]


def test_match_rows_swapped_lat_lng_is_bad_coords():
    res = facilities.match_rows([csv_row("โรงพยาบาลวังน้ำเขียว", lat=101.86, lng=14.41)], CATALOG)
    assert res.matched == [] and res.bad_coords == ["โรงพยาบาลวังน้ำเขียว"]
