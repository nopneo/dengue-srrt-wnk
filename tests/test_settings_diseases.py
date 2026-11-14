"""เปิด/ปิดโรคที่ติดตามจากหน้า /settings (ตัวแปร DENGUE_DISEASES) — สเปกข้อ 4.5"""
import json
import re

import pytest

import settings_store as ss
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource

HDR = {"X-Requested-With": "dengue"}
LAN = {"X-Forwarded-For": "192.168.70.135"}
PW = "correct-horse-pw-1"


# ---------------------------------------------------------------- ตัวตรวจ / ไฟล์

@pytest.mark.parametrize("raw,expected", [
    ("dengue", "dengue"), ("dengue,hfmd", "dengue,hfmd"), (" HFMD , dengue ", "dengue,hfmd"),
    ("diarrhea,lepto", "lepto,diarrhea"), ("hfmd,hfmd,dengue", "dengue,hfmd")])
def test_validate_diseases_normalizes_to_registry_order(raw, expected):
    assert ss.validate("DENGUE_DISEASES", raw) == expected


@pytest.mark.parametrize("bad", ["", " ", ",", "bogus", "dengue,bogus", "dengue,,", "dengue hfmd", "dengue;hfmd"])
def test_validate_diseases_rejects_bad_values_without_echoing_them(bad):
    with pytest.raises(ss.SettingsError) as ei:
        ss.validate("DENGUE_DISEASES", bad)
    assert ei.value.key == "DENGUE_DISEASES"
    if bad.strip(" ,;"):
        assert bad not in str(ei.value)


def test_diseases_key_is_allowed_not_secret_and_round_trips(tmp_path):
    assert "DENGUE_DISEASES" in ss.ALLOWED and not ss.ALLOWED["DENGUE_DISEASES"].secret
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DENGUE_DISEASES": "hfmd, dengue"}, "boss")
    assert json.loads(p.read_text(encoding="utf-8"))["values"] == {"DENGUE_DISEASES": "dengue,hfmd"}
    assert ss.read_overlay(p).values == {"DENGUE_DISEASES": "dengue,hfmd"}
    base = {"DENGUE_DB_PATH": str(tmp_path / "dengue.db")}
    assert [r.key for r in load_settings(ss.effective_env(base)).enabled_rules()] == ["dengue", "hfmd"]


def test_invalid_stored_diseases_value_is_dropped_on_read(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "values": {"DENGUE_DISEASES": "bogus"}}), encoding="utf-8")
    o = ss.read_overlay(p)
    assert o.values == {} and "DENGUE_DISEASES" in o.error


# ---------------------------------------------------------------- หน้า /settings และการบันทึก

@pytest.fixture
def app(tmp_path, store):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db"),
                              "DENGUE_DISEASES": "dengue,hfmd"})
    app = create_app(settings=settings, store=store, source=FakeSource(), sender=FakeSender())
    app.config["RESTART_HOOK"] = lambda: None
    app.config["RESTART_DELAY"] = 0
    return app


@pytest.fixture
def admin(app, store, login_as):
    store.add_user("boss", PW, "admin")
    c = app.test_client()
    login_as(c, "boss", "admin")
    return c


def page(client):
    html = client.get("/settings", headers=LAN).get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="settings-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1)), html


def test_settings_page_shows_diseases_field_with_effective_value_and_rules_table(admin):
    data, html = page(admin)
    item = next(i for i in data["items"] if i["key"] == "DENGUE_DISEASES")
    assert item["value"] == "dengue,hfmd" and item["secret"] is False
    assert 'data-field="DENGUE_DISEASES"' in html
    assert "ไข้เลือดออก" in html and "มือเท้าปาก" in html
    assert "DENGUE_HFMD_DAYS" in html and "DENGUE_CLUSTER_DAYS" in html
    table = html.split('data-testid="disease-rules"')[1].split("</table>")[0]
    assert "มือเท้าปาก" in table and "สุกใส" in table and table.count("ปิดอยู่") == 5   # โรคที่ปิดแสดงพร้อมป้าย (ไว้ดูเกณฑ์ก่อนเปิด)


def test_save_diseases_writes_overlay_audits_and_schedules_restart(admin, app):
    r = admin.post("/api/settings/save", json={"values": {"DENGUE_DISEASES": "hfmd,dengue,diarrhea"}, "clear": [],
                                               "password": PW}, headers={**HDR, **LAN})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["changed"] == ["DENGUE_DISEASES"]
    assert ss.read_overlay(app.config["SETTINGS_PATH"]).values["DENGUE_DISEASES"] == "dengue,hfmd,diarrhea"
    assert any(a["action"] == "settings_change" and "DENGUE_DISEASES" in (a["detail"] or "")
               for a in app.config["STORE"].audit())


def test_save_rejects_unknown_disease_key_and_empty(admin, app):
    for bad in ("bogus", ""):
        r = admin.post("/api/settings/save", json={"values": {"DENGUE_DISEASES": bad}, "clear": [], "password": PW},
                       headers={**HDR, **LAN})
        assert r.status_code == 400
    assert not app.config["SETTINGS_PATH"].exists()


def test_save_diseases_requires_admin(app, login_as):
    c = app.test_client()
    login_as(c, "u1", "srrt")
    r = c.post("/api/settings/save", json={"values": {"DENGUE_DISEASES": "dengue"}, "clear": [], "password": PW},
               headers={**HDR, **LAN})
    assert r.status_code == 403
