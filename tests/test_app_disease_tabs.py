"""แท็บเลือกโรคด้านขวาและการกรองหน้ารายการเคสตามโรค (สเปกข้อ 4.6)"""
from datetime import datetime

import pytest

from app import create_app
from config import load_settings
from disease_views import current_rule
from fakes import FakeSender, FakeSource

NOW = datetime.now().replace(microsecond=0)


def make_app(tmp_path, store, diseases=None):
    env = {"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db")}
    if diseases is not None:
        env["DENGUE_DISEASES"] = diseases
    return create_app(settings=load_settings(env), store=store, source=FakeSource(), sender=FakeSender())


@pytest.fixture
def multi(tmp_path, store, login_as):
    app = make_app(tmp_path, store, "dengue,hfmd")
    c = app.test_client()
    login_as(c, "u1", "srrt")
    return c


@pytest.fixture
def single(tmp_path, store, login_as):
    app = make_app(tmp_path, store)
    c = app.test_client()
    login_as(c, "u1", "srrt")
    return c


def add(store, key, disease, icd, hn="H"):
    return store.add_case(key, "OPD", hn + key, icd, NOW, "40010103", "1", "1", 30, True, disease=disease)


def test_current_rule_picks_enabled_key_else_first():
    s = load_settings({"DENGUE_DISEASES": "dengue,hfmd"})
    assert current_rule(s, {"d": "hfmd"}).key == "hfmd"
    assert current_rule(s, {"d": " HFMD "}).key == "hfmd"
    for bad in ({}, {"d": ""}, {"d": "bogus"}, {"d": "lepto"}):       # lepto ปิดอยู่
        assert current_rule(s, bad).key == "dengue"


def test_index_filters_by_selected_disease(multi, store):
    d = add(store, "1", "dengue", "A90")
    h = add(store, "2", "hfmd", "B084")
    page = multi.get("/").get_data(as_text=True)
    assert f"/case/{d}" in page and f"/case/{h}" not in page
    page = multi.get("/?d=hfmd").get_data(as_text=True)
    assert f"/case/{h}" in page and f"/case/{d}" not in page


@pytest.mark.parametrize("q", ["?d=bogus", "?d=lepto", "?d=", "?d=%00"])
def test_bad_disease_param_falls_back_to_first_enabled(multi, store, q):
    d = add(store, "1", "dengue", "A90")
    r = multi.get("/" + q)
    assert r.status_code == 200 and f"/case/{d}" in r.get_data(as_text=True)


def test_tabs_render_with_badges_and_active_marker(multi, store):
    add(store, "1", "dengue", "A90")
    add(store, "2", "hfmd", "B084", "X")
    add(store, "3", "hfmd", "B084", "Y")
    html = multi.get("/?d=hfmd").get_data(as_text=True)
    assert 'class="disease-tabs' in html and 'aria-label="เลือกโรค"' in html
    assert "ไข้เลือดออก" in html and "มือเท้าปาก" in html
    assert html.count('aria-current="page"') == 1
    assert 'data-disease="hfmd"' in html and 'href="/?d=dengue"' in html
    assert "has-tabs" in html


def test_single_enabled_disease_has_no_tabs_and_clean_links(single, store):
    add(store, "1", "dengue", "A90")
    html = single.get("/").get_data(as_text=True)
    assert "disease-tabs" not in html and "has-tabs" not in html
    assert "d=dengue" not in html                                      # ลิงก์กรองเหมือน 1.4.0


def test_filter_links_keep_selected_disease(multi, store):
    html = multi.get("/?d=hfmd").get_data(as_text=True)
    assert "d=hfmd" in html.split("ตัวกรอง")[1]


def test_tabs_not_shown_on_check_or_settings(tmp_path, store, login_as):
    app = make_app(tmp_path, store, "dengue,hfmd")
    c = app.test_client()
    login_as(c, "a1", "admin")
    for path in ("/settings", "/check"):
        r = c.get(path, environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 200 and "disease-tabs" not in r.get_data(as_text=True)


def test_tabs_only_when_logged_in(tmp_path, store):
    app = make_app(tmp_path, store, "dengue,hfmd")
    r = app.test_client().get("/login")
    assert "disease-tabs" not in r.get_data(as_text=True)


def test_case_page_shows_disease_name_and_uses_its_radius(tmp_path, store, login_as):
    app = make_app(tmp_path, store, "dengue,hfmd")
    c = app.test_client()
    login_as(c, "u1", "srrt")
    h = add(store, "2", "hfmd", "B084")
    html = c.get(f"/case/{h}").get_data(as_text=True)
    assert "มือเท้าปาก" in html


def test_static_tab_script_is_served_and_guards_storage(multi):
    r = multi.get("/static/disease_tabs.js")
    assert r.status_code == 200
    js = r.get_data(as_text=True)
    assert "localStorage" in js and "try" in js and "dengue_disease" in js
