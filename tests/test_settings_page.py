"""หน้าจอ /settings (Task 6): เทมเพลต + static/settings.js + เมนู — ต้องไม่หลุดความลับ ไม่มี JS inline ไม่ใช้ innerHTML."""
import json
import re
import shutil
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import pytest

import settings_store
from app import create_app
from config import load_settings
from fakes import FakeSender, FakeSource

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "settings.html"
JS = ROOT / "static" / "settings.js"
LOGIC = ROOT / "static" / "settings_logic.js"
HARNESS = ROOT / "tests" / "settings_logic_harness.js"
LAN = {"X-Forwarded-For": "192.168.70.135"}
PUBLIC = {"X-Forwarded-For": "203.0.113.9"}
PW = "correct-horse-pw-1"
SECRETS = {"DB_PASS": "UNIQUE-DB-PASSWORD-9f3a", "MOPH_CLIENT_KEY": "UNIQUE-CLIENT-KEY-7712",
           "MOPH_SECRET_KEY": "UNIQUE-SECRET-KEY-8823"}


@pytest.fixture
def app(tmp_path, store):
    settings = load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                              "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    app = create_app(settings=settings, store=store, source=FakeSource(), sender=FakeSender())
    app.config["CLOCK"] = lambda: datetime(2026, 11, 9, 10, 0)
    return app


@pytest.fixture
def path(app):
    return app.config["SETTINGS_PATH"]


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin(client, login_as):
    login_as(client, "boss", "admin")
    return client


def get_page(client, headers=LAN):
    r = client.get("/settings", headers=headers)
    assert r.status_code == 200
    return r.get_data(as_text=True)


def page_data(html):
    m = re.search(r'<script type="application/json" id="settings-data">(.*?)</script>', html, re.S)
    assert m, "ไม่พบ settings-data"
    return json.loads(m.group(1))


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def tags(html, name=None):
    p = _Tags()
    p.feed(html)
    return [(t, a) for t, a in p.tags if name is None or t == name]


def main_of(html):
    return html.split('<main class="content">', 1)[1].split("</main>", 1)[0]


def js_source():
    return JS.read_text(encoding="utf-8")


def logic(expr):
    """รันนิพจน์ JS กับ static/settings_logic.js (ตัวแปร L) ใน node แล้วคืนค่า JSON; ไม่มี node = ข้ามเทสต์"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("ไม่มี node")
    r = subprocess.run([node, str(HARNESS)], input=expr, capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---------------------------------------------------------------- เทมเพลต

def test_settings_template_has_no_safe_filter_or_external_assets(admin):
    src = TEMPLATE.read_text(encoding="utf-8")
    assert not re.search(r"\|\s*safe\b", src) and "Markup" not in src and "autoescape" not in src
    html = get_page(admin)
    for text in (src, html):
        assert not re.search(r'<script[^>]+src="(?:https?:)?//', text)
        assert not re.search(r'<link[^>]+href="(?:https?:)?//', text)
        assert not re.search(r"@import|url\(\s*['\"]?(?:https?:)?//", text)
    assert "/static/settings.js" in html
    assert '<html lang="th">' in html


def test_settings_page_has_four_groups_and_every_field(admin):
    html = get_page(admin)
    main = main_of(html)
    for head in ("ฐานข้อมูล HOSxP", "MOPH Notify (LINE)", "วัน go-live และลิงก์", "การยืนยันตัวเลข"):
        assert head in main
    keyed = {a["data-key"] for _t, a in tags(main) if "data-key" in a}
    assert keyed == set(settings_store.ALLOWED)
    for label in ("ทดสอบ HOSxP", "ทดสอบ MOPH", "ส่งข้อความทดสอบเข้ากลุ่ม", "บันทึก",
                  "รหัสผ่านของคุณ (ยืนยันการแก้ไข)"):
        assert label in main
    # สถานะ: ข้อความให้ตัวอ่านหน้าจอ
    assert 'role="status"' in main and 'role="alert"' in main


def test_secret_inputs_have_no_value_attribute(admin, path):
    settings_store.write_overlay(path, dict(SECRETS), "seed")
    html = get_page(admin)
    pw = [a for t, a in tags(html, "input") if a.get("type") == "password"]
    keyed = {a["data-key"] for a in pw if "data-key" in a}
    assert keyed == {"DB_PASS", "MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"}
    assert len(pw) == 4  # + ช่องรหัสผ่านของผู้ดูแลเอง
    for a in pw:
        assert "value" not in a
        # ช่องความลับของระบบ: new-password (ไม่ให้เบราว์เซอร์เติม/จำ); ช่องรหัสผ่านของผู้ดูแลเอง: off
        # (M-4: current-password ทำให้เบราว์เซอร์เติมรหัสผ่านผู้ดูแลให้เอง)
        assert a.get("autocomplete") == ("new-password" if "data-key" in a else "off")
        if "data-key" in a:   # ป้ายสถานะผูกกับช่องผ่าน aria-describedby
            assert a["aria-describedby"].split()[0] == "b-" + a["data-key"]
            assert f'id="b-{a["data-key"]}"' in html
    # ช่องที่ไม่ลับแสดงค่าที่มีผลจริง
    plain = {a["data-key"]: a for t, a in tags(html, "input") if a.get("data-key") and a.get("type") != "password"}
    assert plain["DB_HOST"].get("type") == "text"


def test_secret_state_badges_show_source_without_value(admin, path):
    settings_store.write_overlay(path, {"DB_PASS": SECRETS["DB_PASS"]}, "seed")
    main = main_of(get_page(admin))
    assert "ตั้งแล้ว (จากหน้าตั้งค่า)" in main
    assert "ยังไม่ได้ตั้ง" in main
    assert main.count("ล้างค่า") >= 3


def test_settings_json_block_contains_no_secret_values(admin, path):
    settings_store.write_overlay(path, {**SECRETS, "DB_HOST": "10.1.2.3"}, "seed")
    html = get_page(admin)
    for value in SECRETS.values():
        assert value not in html
    data = page_data(html)
    for item in data["items"]:
        if item["secret"]:
            assert item["value"] is None
    # คีย์ที่ JS ใช้ (ไม่มีค่าลับ)
    for key in ("save_url", "revert_url", "test_url", "send_test_url", "healthz_url", "login_url",
                "server_today", "start_from_warning", "has_prev", "overlay_error", "updated_at_text", "boot_id",
                "insecure_transport", "start_from_default", "start_from_unset_warning"):
        assert key in data
    assert data["test_url"].endswith("/api/settings/test")
    assert data["send_test_url"].endswith("/api/settings/send-test")
    assert data["healthz_url"].endswith("/healthz")
    assert "/login" in data["login_url"]


def test_boot_id_in_healthz_and_page_data_matches_and_differs_per_app(admin, app, tmp_path, store):
    health = admin.get("/healthz").get_json()
    assert re.fullmatch(r"[0-9a-f]{16}", health["boot_id"])
    assert admin.get("/healthz").get_json()["boot_id"] == health["boot_id"]       # คงที่ในแอปเดียว
    assert page_data(get_page(admin))["boot_id"] == health["boot_id"]
    assert app.config["BOOT_ID"] == health["boot_id"]
    other = create_app(settings=load_settings({"SECRET_KEY": "test-secret-key-0123456789",
                                               "DENGUE_DB_PATH": str(tmp_path / "e.db")}),
                       store=store, source=FakeSource(), sender=FakeSender())
    assert other.test_client().get("/healthz").get_json()["boot_id"] != health["boot_id"]   # เริ่มใหม่ = รหัสใหม่


def test_overlay_error_banner_shown_without_secret(admin, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json " + SECRETS["DB_PASS"], encoding="utf-8")
    html = get_page(admin)
    assert SECRETS["DB_PASS"] not in html
    banner = re.search(r'<div[^>]*id="settings-overlay-error"[^>]*>(.*?)</div>', html, re.S)
    assert banner and 'role="alert"' in banner.group(0)
    assert page_data(html)["overlay_error"] in banner.group(1)
    # ไม่มีข้อผิดพลาด = ไม่มีแถบ
    path.unlink()
    assert "settings-overlay-error" not in get_page(admin)


def test_start_from_past_warning_text_present_in_data(admin, path):
    settings_store.write_overlay(path, {"DENGUE_START_FROM": "2026-10-01",
                                        "MOPH_CLIENT_KEY": SECRETS["MOPH_CLIENT_KEY"],
                                        "MOPH_SECRET_KEY": SECRETS["MOPH_SECRET_KEY"]}, "seed")
    html = get_page(admin)
    data = page_data(html)
    # วันนี้ของเซิร์ฟเวอร์ (CLOCK) ส่งให้ JS เทียบ — ทดสอบได้ไม่ผูกกับเวลาจริง
    assert data["server_today"] == "2026-11-09"
    assert "ตั้งวันเริ่มย้อนหลัง" in data["start_from_warning"]
    assert "ถ้าเป็นกลุ่มจริง ให้ตั้งเป็นวัน go-live" in data["start_from_warning"]
    items = {i["key"]: i for i in data["items"]}
    assert items["DENGUE_START_FROM"]["value"] == "2026-10-01"
    assert items["MOPH_CLIENT_KEY"]["state"] == "set" and items["MOPH_SECRET_KEY"]["state"] == "set"
    # JS ใช้ข้อมูลสองตัวนี้ และมีองค์ประกอบแจ้งเตือนในหน้า
    js = js_source()
    assert "server_today" in js and "start_from_warning" in js
    assert 'id="start-from-warning"' in html


def test_revert_button_only_when_previous_exists(admin, path):
    btn = [a for t, a in tags(get_page(admin), "button") if a.get("id") == "btn-revert"]
    assert btn and "hidden" in btn[0]   # ไม่มีค่าก่อนหน้า = ซ่อนปุ่ม
    settings_store.write_overlay(path, {"DENGUE_BASE_URL": "https://a.example/d"}, "seed")
    settings_store.write_overlay(path, {"DENGUE_BASE_URL": "https://b.example/d"}, "seed")
    html = get_page(admin)
    btn = [a for t, a in tags(html, "button") if a.get("id") == "btn-revert"]
    assert btn and "hidden" not in btn[0]
    assert page_data(html)["has_prev"] is True


def test_last_saved_by_and_when_in_buddhist_year(admin, path):
    assert "ยังไม่เคยบันทึกจากหน้านี้" in main_of(get_page(admin))
    settings_store.write_overlay(path, {"DENGUE_BASE_URL": "https://a.example/d"}, "somchai",
                                 now=datetime(2026, 11, 9, 14, 5, 30))
    main = main_of(get_page(admin))
    assert "somchai" in main and "9 พ.ย. 2569 14:05" in main


def test_last_saved_by_is_escaped(admin, path):
    settings_store.write_overlay(path, {"DENGUE_BASE_URL": "https://a.example/d"}, "<b>x</b>")
    html = get_page(admin)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


# ---------------------------------------------------------------- JS (ตรวจระดับซอร์ส)

def test_settings_js_sends_csrf_header_and_uses_textcontent():
    for src in (js_source(), LOGIC.read_text(encoding="utf-8")):
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", src)
    js = js_source()
    assert "textContent" in js and "createElement" in js
    assert 'X-Requested-With' in js and '"dengue"' in js
    assert 'application/json' in js and "same-origin" in js
    assert '"/api/' not in js  # URL มาจากบล็อกข้อมูล (url_for) ไม่เขียนตายตัว


def test_settings_js_clears_inputs_after_success():
    js = js_source()
    # ฟังก์ชันที่ล้าง: ช่อง password ทั้งหมด + ติ๊กทั้งหมด + ซ่อน force; และถูกเรียกเป็นขั้นแรกของทางสำเร็จ (afterSuccess)
    m = re.search(r"function clearSecretInputs\(\) \{(.*?)\n  \}\n", js, re.S)
    assert m and 'input[type="password"]' in m.group(1) and '.value = ""' in m.group(1)
    assert ".checked = false" in m.group(1) and "forceWrap.hidden = true" in m.group(1)
    m = re.search(r"function afterSuccess\(.*?\{\n(.*?)\n  \}\n", js, re.S)
    assert m and m.group(1).lstrip().startswith("clearSecretInputs();")
    # ช่องรหัสผ่านของผู้ดูแลล้างทุกครั้งหลังส่ง (ทั้งบันทึกและย้อนกลับ)
    assert js.count('confirmInput.value = "";') == 2


def test_settings_page_loads_logic_before_script_and_keeps_status_regions_visible(admin):
    html = get_page(admin)
    assert html.index("/static/settings_logic.js") < html.index("/static/settings.js")
    for tag_id in ("settings-status", "settings-error", "start-from-warning"):
        tag = [a for t, a in tags(html) if a.get("id") == tag_id]
        assert tag and "hidden" not in tag[0] and tag[0].get("role") in ("status", "alert"), tag_id
    js = js_source()
    assert "statusEl.hidden" not in js and "errorEl.hidden" not in js and "warnEl.hidden" not in js


# ---------------- ตรรกะล้วนของหน้า (รันจริงใน node) ----------------

ITEMS = ('{DB_HOST:{secret:false,value:"10.1.1.1"},DB_PORT:{secret:false,value:null},DB_NAME:{secret:false,value:"hos"},'
         'DB_USER:{secret:false,value:"ro"},DB_PASS:{secret:true,value:null},'
         'MOPH_CLIENT_KEY:{secret:true,value:null},MOPH_SECRET_KEY:{secret:true,value:null},'
         'DENGUE_START_FROM:{secret:false,value:"2026-11-09"},DENGUE_BASE_URL:{secret:false,value:"https://a.example/d"},'
         'DENGUE_DATA_AUDITED:{secret:false,value:"0"}}')
ALL = ('["DB_HOST","DB_PORT","DB_NAME","DB_USER","DB_PASS","MOPH_CLIENT_KEY","MOPH_SECRET_KEY",'
       '"DENGUE_START_FROM","DENGUE_BASE_URL","DENGUE_DATA_AUDITED"]')
RAW = ('{DB_HOST:"10.1.1.1",DB_PORT:"",DB_NAME:"hos",DB_USER:"ro",DB_PASS:"",MOPH_CLIENT_KEY:"",MOPH_SECRET_KEY:"",'
       'DENGUE_START_FROM:"2026-11-09",DENGUE_BASE_URL:"https://a.example/d",DENGUE_DATA_AUDITED:"0"}')


def collect(raw_overrides="{}", clears="{}", keys=ALL, items=ITEMS):
    return logic(f"L.collect({items}, Object.assign({RAW}, {raw_overrides}), {clears}, {keys})")


def test_logic_collect_sends_nothing_when_nothing_changed():
    assert collect() == {"values": {}, "clear": []}


def test_logic_collect_db_pass_is_sent_exactly_untrimmed():
    assert collect('{DB_PASS:"  pa ss  "}')["values"] == {"DB_PASS": "  pa ss  "}
    # รหัสเป็นช่องว่างล้วนก็เป็นรหัสที่ถูกต้อง
    assert collect('{DB_PASS:"   "}')["values"] == {"DB_PASS": "   "}


def test_logic_collect_blank_and_whitespace_only_secrets_are_dropped():
    assert collect('{MOPH_CLIENT_KEY:"   ", MOPH_SECRET_KEY:"", DB_PASS:""}') == {"values": {}, "clear": []}
    assert collect('{MOPH_CLIENT_KEY:"abcdefgh12"}')["values"] == {"MOPH_CLIENT_KEY": "abcdefgh12"}


def test_logic_collect_ticked_clear_goes_to_clear_list():
    assert collect(clears='{MOPH_CLIENT_KEY:true, DB_PASS:false}') == {"values": {}, "clear": ["MOPH_CLIENT_KEY"]}
    conflict = logic(f"L.findConflict(L.collect({ITEMS}, Object.assign({RAW}, {{MOPH_CLIENT_KEY:'abcdefgh12'}}), "
                     f"{{MOPH_CLIENT_KEY:true}}, {ALL}))")
    assert conflict == "MOPH_CLIENT_KEY"
    assert logic(f"L.findConflict(L.collect({ITEMS}, {RAW}, {{MOPH_CLIENT_KEY:true}}, {ALL}))") is None


def test_logic_collect_sends_only_changed_non_secret_fields():
    r = collect('{DB_NAME:"hos2", DENGUE_BASE_URL:"  https://a.example/d  ", DENGUE_DATA_AUDITED:"1"}')
    # ค่าเดิม (แม้มีช่องว่างหัวท้าย) ไม่ถูกส่ง; DATA_AUDITED "0"->"1" ถูกส่งเป็นสตริง
    assert r["values"] == {"DB_NAME": "hos2", "DENGUE_DATA_AUDITED": "1"}
    assert collect('{DENGUE_DATA_AUDITED:"0"}')["values"] == {}
    # ช่องที่เคยมีค่าถูกลบจนว่าง: ส่งไปให้เซิร์ฟเวอร์ปฏิเสธ (ไม่ทิ้งเงียบ ๆ)
    assert collect('{DB_HOST:""}')["values"] == {"DB_HOST": ""}


def test_logic_collect_respects_the_keys_filter():
    r = collect('{DB_HOST:"x", MOPH_CLIENT_KEY:"abcdefgh12"}', keys='["MOPH_CLIENT_KEY","MOPH_SECRET_KEY"]')
    assert r["values"] == {"MOPH_CLIENT_KEY": "abcdefgh12"}


def test_logic_port_normalization_matches_server():
    # wiring._port: int(strip) ไม่งั้น 3306 — ว่างหรือ 03306 = 3306 เท่ากับค่าที่ยังไม่ตั้ง จึงไม่ถือว่าเปลี่ยน
    assert collect('{DB_PORT:"03306"}')["values"] == {}
    assert collect('{DB_PORT:" 3306 "}')["values"] == {}
    assert collect('{DB_PORT:"3307"}')["values"] == {"DB_PORT": "3307"}
    assert logic('[L.portNorm("03306"), L.portNorm(""), L.portNorm("+3_307"), L.portNorm("abc"), L.portNorm(null)]') == [
        "3306", "3306", "3307", "3306", "3306"]
    # ไม่ใช่ตัวเลข = ส่งไปให้เซิร์ฟเวอร์ปฏิเสธ (ไม่กลืนเป็น 3306)
    assert collect('{DB_PORT:"abc"}')["values"] == {"DB_PORT": "abc"}
    # ค่าเดิม 3307: พิมพ์ 03307 ไม่เปลี่ยน
    items = ITEMS.replace("DB_PORT:{secret:false,value:null}", 'DB_PORT:{secret:false,value:"3307"}')
    assert collect('{DB_PORT:"03307"}', items=items)["values"] == {}
    assert collect('{DB_PORT:"3306"}', items=items)["values"] == {"DB_PORT": "3306"}


def test_logic_needs_db_password_when_host_port_or_user_changes_without_new_pass():
    for key in ("DB_HOST", "DB_PORT", "DB_USER"):
        assert logic(f'L.needsDbPassword({{{key}:"x"}})') is True
        assert logic(f'L.needsDbPassword({{{key}:"x", DB_PASS:" "}})') is False
    assert logic('L.needsDbPassword({DB_NAME:"x"})') is False
    assert logic('L.needsDbPassword({})') is False


def _fail(status, body="{}"):
    return logic(f'L.describeFailure({{status:{status}, body:{body}}}, ["DB_HOST","DB_PASS","DENGUE_BASE_URL"])')


def test_logic_every_error_code_maps_to_its_own_message_and_action():
    M = logic("L.M")
    cases = {
        "invalid": (_fail(400, '{error:"invalid", key:"DENGUE_BASE_URL", message:"ค่า URL ผิด"}'),
                    "ค่า URL ผิด", "DENGUE_BASE_URL", "none"),
        "invalid_unknown_key": (_fail(400, '{error:"invalid", key:"SECRET_KEY", message:"x"}'), "x", None, "none"),
        "invalid_null_key": (_fail(400, '{error:"invalid", key:null}'), M["invalid"], None, "none"),
        "password_required": (_fail(400, '{error:"password_required", message:"อะไรก็ได้จากเซิร์ฟเวอร์"}'),
                              M["needDbPass"], "DB_PASS", "none"),
        "bad_request": (_fail(400, '{error:"bad_request"}'), M["badRequest"], None, "none"),
        "non_json_400": (_fail(400, "{}"), M["badRequest"], None, "none"),
        "password": (_fail(403, '{error:"password", message:"x"}'), M["wrongPassword"], "confirm", "none"),
        "csrf": (_fail(403, '{error:"csrf"}'), M["csrf"], None, "none"),
        "forbidden": (_fail(403, '{error:"ไม่มีสิทธิ์ใช้งานส่วนนี้"}'), M["forbidden"], None, "none"),
        "untested": (_fail(409, '{error:"untested", steps:[{name:"a",status:"fail",detail:"b"}]}'),
                     M["untested"], None, "offer_force"),
        "no_prev": (_fail(409, '{error:"no_prev"}'), M["noPrev"], None, "hide_revert"),
        "too_large": (_fail(413, '{error:"too_large", message:"ข้อความอื่น"}'), M["tooLarge"], None, "none"),
        "unsupported": (_fail(415, '{error:"ต้องส่งเป็น application/json"}'), M["unsupported"], None, "none"),
        "rate_limited": (_fail(429, '{error:"rate_limited"}'), M["rateLimited"], None, "none"),
        "throttled": (_fail(429, '{error:"throttled", retry_after:300}'), M["throttled"], None, "none"),
        "session": (_fail(401, '{error:"กรุณาเข้าสู่ระบบ"}'), M["session"], None, "login"),
        "server": (_fail(500, '{error:"server"}'), M["server"], None, "none"),
        "timeout": (logic('L.describeFailure({timeout:true,status:0,body:{}}, [])'), M["timeout"], None, "reload"),
        "network": (logic('L.describeFailure({network:true,status:0,body:{}}, [])'), M["network"], None, "none"),
        "non_json_500": (logic('L.describeFailure({status:500,body:null}, [])'), M["server"], None, "none"),
        "other": (_fail(502), M["unexpected"] + " (HTTP 502)", None, "none"),
    }
    for name, (got, message, field, action) in cases.items():
        assert got["message"] == message, name
        assert got["field"] == field and got["action"] == action, name
    # ข้อความของแต่ละกรณีไม่ซ้ำกัน
    distinct = [cases[k][0]["message"] for k in ("password_required", "bad_request", "password", "csrf", "forbidden",
                                                  "untested", "no_prev", "too_large", "unsupported", "rate_limited",
                                                  "throttled", "session", "server", "timeout", "network")]
    assert len(set(distinct)) == len(distinct)
    assert cases["untested"][0]["steps"] == [{"name": "a", "status": "fail", "detail": "b"}]


def test_logic_server_message_used_only_when_a_short_string():
    # 429/500/invalid ใช้ `message` ของเซิร์ฟเวอร์ (ข้อความคงที่); ยาวผิดปกติ/ไม่ใช่ข้อความ = ใช้ข้อความของหน้า
    assert _fail(429, '{error:"throttled", message:"ล็อกอีก 7 นาที"}')["message"] == "ล็อกอีก 7 นาที"
    assert _fail(500, '{error:"server", message:"ผิดพลาด"}')["message"] == "ผิดพลาด"
    M = logic("L.M")
    assert _fail(500, '{error:"server", message:"x".repeat(401)}')["message"] == M["server"]
    assert _fail(500, '{error:"server", message:{a:1}}')["message"] == M["server"]


def test_logic_test_summary_distinguishes_warnings():
    M = logic("L.M")
    assert logic('L.summarizeTest(true, [{status:"ok"},{status:"ok"}])') == {"kind": "ok", "message": M["testOk"]}
    assert logic('L.summarizeTest(true, [{status:"ok"},{status:"warn"}])') == {"kind": "warn", "message": M["testWarn"]}
    assert "มีคำเตือน" in M["testWarn"] and M["testOk"] == "ทดสอบผ่านทุกขั้นตอน"
    assert logic('L.summarizeTest(false, [{status:"fail"}])') == {"kind": "fail", "message": M["testFail"]}


def test_logic_is_restarted_only_for_a_different_non_empty_boot_id():
    def r(page, health):
        return logic(f"L.isRestarted({page}, {health})")
    assert r('"aaaa1111"', '{boot_id:"bbbb2222", ok:false}') is True      # ok ไม่เกี่ยว
    assert r('"aaaa1111"', '{boot_id:"bbbb2222", ok:true}') is True
    assert r('"aaaa1111"', '{boot_id:"aaaa1111", ok:true}') is False      # worker เก่ายังตอบ
    assert r('"aaaa1111"', '{ok:true}') is False                          # ไม่มี boot_id
    assert r('"aaaa1111"', '{boot_id:"", ok:true}') is False
    assert r('"aaaa1111"', '{boot_id:123}') is False
    assert r('"aaaa1111"', "null") is False
    assert r('"aaaa1111"', '"bbbb2222"') is False
    assert r('"aaaa1111"', "[1]") is False
    assert r('""', '{boot_id:"bbbb2222"}') is False                       # หน้าไม่มี boot_id: ตัดสินไม่ได้
    assert r("null", '{boot_id:"bbbb2222"}') is False


def test_logic_confirm_text_says_which_keys_are_used():
    typed, stored = logic("[L.confirmSendText(true), L.confirmSendText(false)]")
    assert typed.startswith("ส่งข้อความทดสอบเข้ากลุ่ม LINE จริง 1 ข้อความ?") and "ใช้กุญแจที่กรอกไว้" in typed
    assert stored.startswith("ส่งข้อความทดสอบเข้ากลุ่ม LINE จริง 1 ข้อความ?") and "ใช้กุญแจที่ตั้งไว้" in stored


def test_settings_js_restart_poll_uses_boot_id_and_times_out_safely():
    js = js_source()
    assert "isRestarted(data.boot_id" in js and "ok === true" not in js
    assert "POLL_EVERY_MS = 2000" in js and "POLL_MAX_MS = 60000" in js and "POST_TIMEOUT_MS = 40000" in js
    m = re.search(r"if \(Date\.now\(\) - started >= POLL_MAX_MS\) \{(.*?)\n      \}", js, re.S)
    assert m and "M.restartTimeout" in m.group(1) and "reloadButton()" in m.group(1)
    assert "stopPolling()" in m.group(1) and "end()" in m.group(1)   # หยุดตัวจับเวลา + เปิดปุ่มคืน


DEFAULT_ITEMS = ITEMS.replace('DENGUE_START_FROM:{secret:false,value:"2026-11-09"}',
                              'DENGUE_START_FROM:{secret:false,value:null,source:"default",fallback:"2026-11-09"}'
                              ).replace('DENGUE_BASE_URL:{secret:false,value:"https://a.example/d"}',
                                        'DENGUE_BASE_URL:{secret:false,value:"https://a.example/d",source:"default"}'
                                        ).replace('DENGUE_DATA_AUDITED:{secret:false,value:"0"}',
                                                  'DENGUE_DATA_AUDITED:{secret:false,value:"0",source:"default"}')
EMPTY_START = '{DENGUE_START_FROM:""}'


def test_logic_default_source_empty_field_untouched_is_not_sent():
    # I-2: START_FROM ที่ยังไม่ตั้งแสดงช่องว่าง; ไม่ได้แตะ/ว่าง = ไม่ส่ง (ไม่ใช่ค่าว่างที่เซิร์ฟเวอร์ต้องปฏิเสธ)
    assert collect(EMPTY_START, items=DEFAULT_ITEMS, keys='["DENGUE_START_FROM"]')["values"] == {}
    r = logic(f"L.collect({DEFAULT_ITEMS}, Object.assign({RAW}, {EMPTY_START}), {{}}, {ALL}, {{DENGUE_START_FROM:true}})")
    assert r["values"] == {}   # แตะแล้วลบจนว่างก็ไม่มีอะไรให้ตั้ง


def test_logic_default_source_value_is_always_a_change_even_when_equal_to_fallback():
    # ตั้งวัน go-live = วันนี้ (เท่าค่าสำรอง "วันเริ่มระบบ") ต้องถูกส่ง
    r = collect('{DENGUE_START_FROM:"2026-11-09"}', items=DEFAULT_ITEMS)
    assert r["values"]["DENGUE_START_FROM"] == "2026-11-09"
    # DATA_AUDITED "0" ทับค่าเริ่มต้น "0" และ BASE_URL เท่าค่าสำรอง: ส่ง (ไม่ระบุว่าแตะ = ถือว่าผู้ใช้ส่งทั้งหมด)
    assert r["values"]["DENGUE_DATA_AUDITED"] == "0" and r["values"]["DENGUE_BASE_URL"] == "https://a.example/d"


def test_logic_default_source_only_touched_fields_are_sent_when_touched_is_given():
    def run(touched, raw="{DENGUE_START_FROM:'2026-11-09'}"):
        return logic(f"L.collect({DEFAULT_ITEMS}, Object.assign({RAW}, {raw}), {{}}, {ALL}, {touched})")["values"]
    assert run("{}") == {}                                              # ไม่แตะอะไรเลย: บันทึกข้ามฟิลด์อื่นไม่ตรึงค่าเริ่มต้น
    assert run("{DENGUE_DATA_AUDITED:true}") == {"DENGUE_DATA_AUDITED": "0"}   # เลือกกลับมาเป็น 0 = ตั้งค่า 0
    assert run("{DENGUE_BASE_URL:true}") == {"DENGUE_BASE_URL": "https://a.example/d"}
    assert run("{DENGUE_START_FROM:true}") == {"DENGUE_START_FROM": "2026-11-09"}
    assert run("{DENGUE_START_FROM:false}") == {}


def test_logic_overlay_or_env_source_equal_value_stays_a_noop_even_when_touched():
    items = (ITEMS.replace('DENGUE_START_FROM:{secret:false,value:"2026-11-09"}',
                           'DENGUE_START_FROM:{secret:false,value:"2026-11-09",source:"overlay"}')
             .replace('DENGUE_BASE_URL:{secret:false,value:"https://a.example/d"}',
                      'DENGUE_BASE_URL:{secret:false,value:"https://a.example/d",source:"env"}')
             .replace('DENGUE_DATA_AUDITED:{secret:false,value:"0"}',
                      'DENGUE_DATA_AUDITED:{secret:false,value:"0",source:"overlay"}'))
    r = logic(f"L.collect({items}, {RAW}, {{}}, {ALL}, "
              f"{{DENGUE_DATA_AUDITED:true, DENGUE_BASE_URL:true, DENGUE_START_FROM:true}})")
    assert r["values"] == {}
    r = logic(f"L.collect({items}, Object.assign({RAW}, {{DENGUE_START_FROM:'2026-11-10'}}), {{}}, {ALL}, "
              f"{{DENGUE_START_FROM:true}})")
    assert r["values"] == {"DENGUE_START_FROM": "2026-11-10"}


def test_logic_conflict_error_maps_to_reload_with_fixed_message():
    M = logic("L.M")
    assert M["changedByOther"] == "ค่าถูกเปลี่ยนโดยผู้ดูแลคนอื่นระหว่างบันทึก โหลดหน้าใหม่แล้วลองอีกครั้ง"
    got = _fail(409, '{error:"conflict", message:"ข้อความอื่นจากเซิร์ฟเวอร์"}')
    assert got["message"] == M["changedByOther"] and got["action"] == "reload" and got["field"] is None
    assert M["changedByOther"] not in (M["badRequest"], M["untested"], M["noPrev"])
    # 409 อื่นไม่เปลี่ยนพฤติกรรม
    assert _fail(409, '{error:"no_prev"}')["action"] == "hide_revert"
    assert _fail(409, '{error:"untested"}')["action"] == "offer_force"


def test_settings_js_tracks_touched_fields_and_renders_page_warnings():
    js = js_source()
    assert "touched" in js and "Logic.collect(items, raw, ticked, keys, touched)" in js
    assert "start_from_default" in js and "start_from_unset_warning" in js


def test_settings_js_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("ไม่มี node")
    for f in (JS, LOGIC, HARNESS):
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, (f.name, r.stderr)


def test_settings_page_has_no_inline_event_handlers(admin, path):
    settings_store.write_overlay(path, dict(SECRETS), "seed")
    html = get_page(admin)
    main = main_of(html)
    assert not re.search(r"\son[a-z]+\s*=", main)
    assert "javascript:" not in main
    assert not re.search(r"\sstyle\s*=", main)
    # JS inline เดียวในหน้านี้คือบล็อกข้อมูล JSON
    for m in re.finditer(r"<script(?![^>]*\bsrc=)([^>]*)>", main):
        assert 'type="application/json"' in m.group(1)


# ---------------------------------------------------------------- เมนู

def test_nav_link_only_for_admin_on_lan(client, store, login_as):
    store.add_user("boss", PW, "admin")
    login_as(client, "boss", "admin")
    html = client.get("/", headers=LAN).get_data(as_text=True)
    assert 'href="/settings"' in html and "ตั้งค่า" in html
    # admin แต่ไม่ได้เปิดจาก LAN
    assert 'href="/settings"' not in client.get("/", headers=PUBLIC).get_data(as_text=True)
    # ยังไม่ล็อกอิน (หน้า login ไม่มีแถบเมนูเลย)
    anon = client.application.test_client()
    assert 'href="/settings"' not in anon.get("/login", headers=LAN).get_data(as_text=True)
    # บทบาทอื่นใน LAN
    for role in ("srrt", "viewer"):
        login_as(client, f"u-{role}", role)
        assert 'href="/settings"' not in client.get("/", headers=LAN).get_data(as_text=True)


def test_nav_link_marked_active_on_settings_page(admin):
    html = get_page(admin)
    m = re.search(r'<a href="/settings" class="([^"]*)"', html)
    assert m and "on" in m.group(1).split()
    index = admin.get("/", headers=LAN).get_data(as_text=True)
    m = re.search(r'<a href="/settings" class="([^"]*)"', index)
    assert m and "on" not in m.group(1).split()


def test_admin_on_srrt_pages_unchanged(admin, store):
    for url in ("/", "/map", "/check"):
        r = admin.get(url, headers=LAN)
        assert r.status_code == 200, url
    html = admin.get("/", headers=LAN).get_data(as_text=True)
    assert "รายการเคส" in html and "แผนที่รวม" in html and "ตรวจก่อนใช้" in html
