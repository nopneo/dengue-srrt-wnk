"""ตรวจก่อนใช้ต่อโรค: setup_check + หน้า /check (สเปกข้อ 4.7) — แสดงเฉพาะจำนวนนับ"""
from datetime import date

import pytest

import setup_check
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource, HosxpError

LAN = {"X-Forwarded-For": "192.168.70.135"}
DENGUE_ROW = "รหัสโรค DENGUE_ICDS"


def make_settings(tmp_path, diseases="dengue,hfmd", **env):
    base = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
            "DENGUE_DATA_AUDITED": "1", "DENGUE_START_FROM": date.today().isoformat(),
            "DENGUE_DISEASES": diseases}
    base.update(env)
    return load_settings(base)


def rows(settings, source):
    return {name: (status, detail) for status, name, detail in
            setup_check.run_checks(settings, source, moph_configured=True)}


def test_single_disease_has_only_the_dengue_row(tmp_path):
    r = rows(make_settings(tmp_path, "dengue"), FakeSource())
    assert DENGUE_ROW in r and not any(n.startswith("รหัสโรค ") and n != DENGUE_ROW for n in r)


def test_enabled_disease_gets_its_own_row_ok_when_codes_found(tmp_path):
    src = FakeSource()
    src.icd_found["B084"] = 3
    status, detail = rows(make_settings(tmp_path), src)["รหัสโรค มือเท้าปาก"]
    assert status == setup_check.OK and "B084" in detail and "3 visit" in detail


def test_enabled_disease_without_codes_in_data_is_warning_naming_the_env(tmp_path):
    status, detail = rows(make_settings(tmp_path), FakeSource())["รหัสโรค มือเท้าปาก"]
    assert status == setup_check.WARN and "DENGUE_HFMD_ICDS" in detail


def test_one_disease_read_failure_does_not_break_other_rows(tmp_path):
    src = FakeSource(failure=HosxpError("down"), fail_methods=["icd_counts"])
    r = rows(make_settings(tmp_path), src)
    assert r[DENGUE_ROW][0] == setup_check.FAIL and r["รหัสโรค มือเท้าปาก"][0] == setup_check.FAIL
    assert r["Python"][0] == setup_check.OK                       # แถวอื่นยังทำงาน


def test_disabled_disease_has_no_row(tmp_path):
    assert "รหัสโรค สุกใส" not in rows(make_settings(tmp_path), FakeSource())


def test_audit_lists_other_enabled_diseases_with_counts_only(tmp_path):
    src = FakeSource()
    src.icd_found["B084"] = 3
    src.icd_found["B0841"] = 2
    _, audit = setup_check.run_report(make_settings(tmp_path), src, moph_configured=True)
    d = next(x for x in audit["diseases"] if x["key"] == "hfmd")
    assert d["enabled"] is True and next(x for x in audit["diseases"] if x["key"] == "lepto")["enabled"] is False
    assert d["key"] == "hfmd" and d["name"] == "มือเท้าปาก" and d["used"] == ("B084",)
    assert d["rows"][0]["codes"] == [("B084", 3), ("B0841", 2)] and d["rows"][0]["visits"] == 5
    assert d["env"]["days"] == "DENGUE_HFMD_DAYS" and d["error"] is None
    assert set(d) >= {"min_cases", "days", "radius_m", "mode", "group"}


def test_audit_disease_error_is_isolated(tmp_path):
    src = FakeSource(failure=HosxpError("down"), fail_methods=["icd_counts"])
    _, audit = setup_check.run_report(make_settings(tmp_path), src, moph_configured=True)
    assert all(d["error"] for d in audit["diseases"]) and audit["icd"]["error"]


@pytest.fixture
def client(tmp_path, store, login_as):
    src = FakeSource()
    src.icd_found["B084"] = 3
    app = create_app(settings=make_settings(tmp_path), store=store, source=src, sender=FakeSender())
    c = app.test_client()
    login_as(c, "u1", "viewer")
    return c


def test_check_page_has_disease_section_and_env_rows(client):
    html = client.get("/check", headers=LAN).get_data(as_text=True)
    assert 'data-testid="disease-audit-hfmd"' in html and "มือเท้าปาก" in html and "B084" in html
    for name in ("DENGUE_HFMD_ICDS", "DENGUE_HFMD_MIN_CASES", "DENGUE_HFMD_DAYS"):
        assert name in html
    assert "DENGUE_HFMD_RADIUS_M" not in html                       # โรคจัดกลุ่มตามหมู่บ้านไม่มีรัศมี


def test_check_page_shows_closed_diseases_in_a_details_block_to_check_before_enabling(tmp_path, store, login_as):
    src = FakeSource()
    src.icd_found["B084"] = 3
    app = create_app(settings=make_settings(tmp_path, "dengue"), store=store, source=src, sender=FakeSender())
    c = app.test_client()
    login_as(c, "u1", "viewer")
    html = c.get("/check", headers=LAN).get_data(as_text=True)
    closed = html.split("<details")[1].split("</details>")[0]
    assert 'data-testid="disease-audit-hfmd"' in closed and "ปิดอยู่" in closed and "B084" in closed
    assert "DENGUE_HFMD_DAYS" in html


def test_enabled_disease_section_is_outside_the_closed_details_block(client):
    html = client.get("/check", headers=LAN).get_data(as_text=True)
    assert html.index('data-testid="disease-audit-hfmd"') < html.index("<details")
