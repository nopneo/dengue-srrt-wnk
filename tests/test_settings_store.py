"""settings_store — รายการอนุญาต ตัวตรวจ อ่าน/เขียนไฟล์ settings.json แบบ atomic และการซ้อนทับ env"""
import json
import os
import stat
from datetime import date, datetime
from pathlib import Path

import pytest

import settings_store as ss
from settings_store import ALLOWED, Overlay, SettingsError

TODAY = date(2026, 11, 9)
NOW = datetime(2026, 11, 9, 15, 0, 0)
posix_only = pytest.mark.skipif(os.name != "posix", reason="สิทธิ์ไฟล์ 600 ใช้ได้เฉพาะ POSIX")


def prev_of(path: Path) -> Path:
    return path.with_name(path.name + ".prev")


def leftovers(directory: Path) -> list[str]:
    """ไฟล์ในโฟลเดอร์ที่ไม่ใช่ settings.json / settings.json.prev (ต้องไม่มีไฟล์ชั่วคราวค้าง)"""
    return sorted(p.name for p in directory.iterdir() if p.name not in ("settings.json", "settings.json.prev"))


# ---------- รายการอนุญาต ----------

def test_allowed_has_exactly_the_eleven_keys_and_secret_flags():
    assert set(ALLOWED) == {
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS", "MOPH_CLIENT_KEY", "MOPH_SECRET_KEY",
        "DENGUE_START_FROM", "DENGUE_BASE_URL", "DENGUE_DATA_AUDITED", "DENGUE_DISEASES",
    }
    assert {k for k, f in ALLOWED.items() if f.secret} == {"DB_PASS", "MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"}
    assert "SECRET_KEY" not in ALLOWED
    assert all(f.label for f in ALLOWED.values())


# ---------- ตัวตรวจ ----------

@pytest.mark.parametrize("bad", ["2569-11-09", "2020-01-01", "09/11/2026", "2026-13-40", "20261109", "2026-W45-1", "", "٢٠٢٦-١١-٠٩"])
def test_validate_start_from_rejects_buddhist_year_and_far_dates(bad):
    with pytest.raises(SettingsError) as ei:
        ss.validate("DENGUE_START_FROM", bad, TODAY)
    assert ei.value.key == "DENGUE_START_FROM"
    with pytest.raises(SettingsError):
        ss.validate_start_from(bad, TODAY)


def test_validate_start_from_accepts_near_dates_and_strips():
    assert ss.validate("DENGUE_START_FROM", " 2026-11-15 ", TODAY) == "2026-11-15"
    assert ss.validate_start_from("2026-11-09", TODAY) == "2026-11-09"
    assert ss.validate_start_from("2025-11-09", TODAY) == "2025-11-09"


def test_validate_start_from_boundary_is_366_days():
    assert ss.validate_start_from("2025-11-08", TODAY) == "2025-11-08"
    with pytest.raises(SettingsError):
        ss.validate_start_from("2025-11-07", TODAY)
    assert ss.validate_start_from("2027-11-10", TODAY) == "2027-11-10"
    with pytest.raises(SettingsError):
        ss.validate_start_from("2027-11-11", TODAY)


def test_validate_db_pass_allows_quote_rejects_newline():
    assert ss.validate("DB_PASS", "p'a\"ss $&;#!") == "p'a\"ss $&;#!"
    assert ss.validate("DB_PASS", "x" * 128) == "x" * 128
    for bad in ["a\nb", "a\rb", "", "x" * 129, " " * 129, "a\x00b"]:
        with pytest.raises(SettingsError) as ei:
            ss.validate("DB_PASS", bad)
        assert ei.value.key == "DB_PASS"


@pytest.mark.parametrize("value,ok", [
    ("1", True), ("3306", True), ("65535", True), (" 3306 ", True),
    ("0", False), ("65536", False), ("-1", False), ("33.06", False), ("abc", False), ("", False),
    ("٣٣٠٦", False), ("+3306", False), ("3306;DROP", False),
])
def test_validate_db_port_range(value, ok):
    if ok:
        assert ss.validate("DB_PORT", value) == str(int(value))
    else:
        with pytest.raises(SettingsError):
            ss.validate("DB_PORT", value)


def test_validate_db_port_normalizes_leading_zeros():
    assert ss.validate("DB_PORT", "03306") == "3306"


@pytest.mark.parametrize("value,ok", [
    ("127.0.0.1", True), ("db.example.lan", True), ("my-host", True), ("a" * 253, True),
    ("a" * 254, False), ("", False), ("host name", False), ("host;rm", False), ("<DB host>", False),
    ("db_host", False), ("ฐานข้อมูล", False), ("host/path", False),
])
def test_validate_db_host(value, ok):
    if ok:
        assert ss.validate("DB_HOST", value) == value
    else:
        with pytest.raises(SettingsError):
            ss.validate("DB_HOST", value)


@pytest.mark.parametrize("key", ["DB_NAME", "DB_USER"])
@pytest.mark.parametrize("value,ok", [
    ("hos", True), ("ro_user.1-x", True), ("a" * 64, True), ("a" * 65, False), ("", False),
    ("a b", False), ("a'b", False), ("a;b", False), ("ผู้ใช้", False),
])
def test_validate_db_name_and_user(key, value, ok):
    if ok:
        assert ss.validate(key, value) == value
    else:
        with pytest.raises(SettingsError):
            ss.validate(key, value)


@pytest.mark.parametrize("key", ["MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"])
def test_validate_moph_key_rejects_placeholder_and_spaces(key):
    assert ss.validate(key, "") == ""  # ว่าง = ล้างค่า
    assert ss.validate(key, "   ") == ""
    assert ss.validate(key, " abcd1234 ") == "abcd1234"
    assert ss.validate(key, "k" * 256) == "k" * 256
    for bad in ["short", "k" * 257, "abcd 1234", "abcd\t1234", "<Client ID ของกลุ่ม>", "abcd\n1234"]:
        with pytest.raises(SettingsError) as ei:
            ss.validate(key, bad)
        assert ei.value.key == key


def test_validate_base_url_uses_config_rule(monkeypatch):
    import config
    samples = ["https://wnkhos.go.th/dengue", "http://10.0.0.1:5400/x", " https://a.b/c ", "ftp://x.y", "wnkhos.go.th",
               "<URL ของแอป>", "https://", "http://a b", "http://h:99999", "", "   "]
    for s in samples:
        problem = config.base_url_problem(s.strip())
        if problem is None:
            assert ss.validate("DENGUE_BASE_URL", s) == s.strip()
        else:
            with pytest.raises(SettingsError) as ei:
                ss.validate("DENGUE_BASE_URL", s)
            assert ei.value.key == "DENGUE_BASE_URL"
    # ตัวตรวจต้องเรียกกฎของ config จริง (ไม่ใช่เขียนซ้ำ)
    monkeypatch.setattr(config, "base_url_problem", lambda v: "x")
    with pytest.raises(SettingsError):
        ss.validate("DENGUE_BASE_URL", "https://ok.example/")


@pytest.mark.parametrize("value,ok", [("0", True), ("1", True), (" 1 ", True), ("2", False), ("true", False), ("", False), ("01", False), ("yes", False)])
def test_validate_data_audited_only_0_or_1(value, ok):
    if ok:
        assert ss.validate("DENGUE_DATA_AUDITED", value) == value.strip()
    else:
        with pytest.raises(SettingsError):
            ss.validate("DENGUE_DATA_AUDITED", value)


@pytest.mark.parametrize("key", ["SECRET_KEY", "DENGUE_ICDS", "db_host", "", "MOPH_BASE_URL", "PATH"])
def test_validate_unknown_key_rejected(key):
    with pytest.raises(SettingsError):
        ss.validate(key, "x")


def test_unknown_key_rejected_by_write(tmp_path):
    path = tmp_path / "data" / "settings.json"
    with pytest.raises(SettingsError):
        ss.write_overlay(path, {"SECRET_KEY": "x" * 40}, "admin", NOW)
    with pytest.raises(SettingsError):
        ss.write_overlay(path, {"DB_HOST": "h", "DENGUE_ICDS": "A90"}, "admin", NOW)
    assert not path.exists()  # ไม่เขียนอะไรเลย แม้คีย์แรกจะถูกต้อง
    assert not path.parent.exists() or leftovers(path.parent) == []


HOSTILE_MARK = "ZZZSECRETZZZ"
HOSTILE = [
    HOSTILE_MARK + "\n" + HOSTILE_MARK,
    "<" + HOSTILE_MARK + ">'; DROP TABLE x;--",
    "ftp://" + HOSTILE_MARK,
    HOSTILE_MARK * 80,
    "💥" + HOSTILE_MARK + "ภาษาไทย",
    "9" * 40 + HOSTILE_MARK,
    "2569-" + HOSTILE_MARK,
    "\x00" + HOSTILE_MARK,
    " " + HOSTILE_MARK + " " + HOSTILE_MARK,
]


def test_error_messages_never_contain_the_value():
    raised = 0
    for key in ALLOWED:
        for value in HOSTILE:
            try:
                ss.validate(key, value, TODAY)
            except SettingsError as e:
                raised += 1
                assert e.key == key
                for text in (str(e), repr(e), e.args[0], *map(str, e.args)):
                    assert HOSTILE_MARK not in text
                    assert value not in text
                assert str(e)  # มีข้อความ
                assert ALLOWED[key].label in str(e) or key in str(e)
    assert raised > 30
    # ผ่าน write_overlay ด้วย ต้องไม่ย้อนค่า
    with pytest.raises(SettingsError) as ei:
        ss.write_overlay(Path("never-written") / "settings.json", {"DB_PORT": HOSTILE[2]}, "u", NOW)
    assert HOSTILE_MARK not in str(ei.value)
    assert not Path("never-written").exists()
    # ชื่อคีย์แปลกที่ผู้ใช้ส่งมาก็ต้องไม่ย้อนในข้อความ
    with pytest.raises(SettingsError) as ei:
        ss.write_overlay(Path("never-written") / "settings.json", {HOSTILE_MARK: "v"}, "u", NOW)
    assert HOSTILE_MARK not in str(ei.value)
    assert not Path("never-written").exists()


# ---------- อ่านไฟล์ ----------

def test_read_overlay_missing_file_is_empty_no_error(tmp_path):
    o = ss.read_overlay(tmp_path / "nope" / "settings.json")
    assert isinstance(o, Overlay)
    assert o.values == {} and o.error is None and o.updated_at is None and o.updated_by is None and o.has_prev is False


@pytest.mark.parametrize("content", [
    b"", b'{"version": 1, "values": {"DB_HOST": "h', b"[]", b'{"version": 99, "values": {}}', b'{"values": {}}',
    b"null", b"123", b'"x"', b'{"version": 1, "values": []}', b'{"version": 1, "values": "x"}', b'{"version": 1}',
    b'{"version": "1", "values": {}}', b"\xff\xfe\x00garbage", b"not json at all ZZZSECRETZZZ",
])
def test_read_overlay_corrupt_json_returns_error_not_exception(tmp_path, content):
    p = tmp_path / "settings.json"
    p.write_bytes(content)
    o = ss.read_overlay(p)
    assert o.values == {}
    assert o.error and isinstance(o.error, str)
    assert HOSTILE_MARK not in o.error  # ไม่มีเนื้อไฟล์
    assert str(p) not in o.error


def test_read_overlay_unreadable_path_returns_error(tmp_path):
    d = tmp_path / "settings.json"
    d.mkdir()  # เป็นโฟลเดอร์ ไม่ใช่ไฟล์
    o = ss.read_overlay(d)
    assert o.values == {} and o.error


def test_read_overlay_drops_unknown_keys_and_non_string_values(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "version": 1, "updated_at": "2026-11-09T15:00:00", "updated_by": "admin1",
        "values": {"DB_HOST": "h", "DB_PORT": 3306, "SECRET_KEY": "x" * 40, "DENGUE_ICDS": "A90", "DB_PASS": "",
                   "DB_USER": None, "DB_NAME": ["a"], "DENGUE_DATA_AUDITED": "1"},
    }), encoding="utf-8")
    o = ss.read_overlay(p)
    assert o.error is None
    assert o.values == {"DB_HOST": "h", "DB_PASS": "", "DENGUE_DATA_AUDITED": "1"}
    assert o.updated_at == "2026-11-09T15:00:00" and o.updated_by == "admin1"


def test_read_overlay_ignores_non_string_metadata(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "updated_at": 5, "updated_by": ["x"], "values": {}}), encoding="utf-8")
    o = ss.read_overlay(p)
    assert o.error is None and o.updated_at is None and o.updated_by is None


def test_read_overlay_has_prev(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"version": 1, "values": {}}', encoding="utf-8")
    assert ss.read_overlay(p).has_prev is False
    prev_of(p).write_text("{}", encoding="utf-8")
    assert ss.read_overlay(p).has_prev is True


def test_overlay_repr_and_str_never_contain_secret_values(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_PASS": "TOPSECRET-pw", "MOPH_SECRET_KEY": "TOPSECRET-moph", "DB_HOST": "db.example.lan"}, "admin", NOW)
    o = ss.read_overlay(p)
    for text in (repr(o), str(o), f"{o}", f"{o!r}"):
        assert "TOPSECRET" not in text
    assert "DB_PASS" in repr(o)  # แสดงชื่อคีย์ได้
    assert repr(ALLOWED["DB_PASS"])  # Field ไม่มีค่า
    assert o.values["DB_PASS"] == "TOPSECRET-pw"  # แต่ค่ายังอ่านได้ผ่านฟิลด์


# ---------- เขียนไฟล์ ----------

def test_write_overlay_format_and_merge(tmp_path):
    p = tmp_path / "data" / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "db.example.lan", "DB_PORT": " 3306 "}, "admin1", NOW)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw == {"version": 1, "updated_at": "2026-11-09T15:00:00", "updated_by": "admin1",
                   "values": {"DB_HOST": "db.example.lan", "DB_PORT": "3306"}}
    ss.write_overlay(p, {"DB_PORT": "3307", "DENGUE_DATA_AUDITED": "1"}, "admin2", datetime(2026, 11, 10, 8, 30, 5))
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["values"] == {"DB_HOST": "db.example.lan", "DB_PORT": "3307", "DENGUE_DATA_AUDITED": "1"}
    assert raw["updated_by"] == "admin2" and raw["updated_at"] == "2026-11-10T08:30:05"
    assert b"\r\n" not in p.read_bytes()
    assert leftovers(p.parent) == []


def test_write_overlay_default_now(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "h"}, "u")
    assert datetime.fromisoformat(json.loads(p.read_text(encoding="utf-8"))["updated_at"])


def test_write_overlay_stores_unicode_password_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    pw = "pä'ss\"\\ไทย💥 x"
    ss.write_overlay(p, {"DB_PASS": pw}, "u", NOW)
    assert ss.read_overlay(p).values["DB_PASS"] == pw


def test_write_overlay_invalid_key_writes_nothing(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "keep.me"}, "u", NOW)
    before = p.read_bytes()
    with pytest.raises(SettingsError) as ei:
        ss.write_overlay(p, {"DB_HOST": "new.host", "DB_PORT": "99999"}, "u", NOW)
    assert ei.value.key == "DB_PORT"
    assert p.read_bytes() == before
    assert not prev_of(p).exists()
    assert leftovers(p.parent) == []


def test_write_overlay_empty_allowed_only_for_secret_keys(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_PASS": "pw", "MOPH_CLIENT_KEY": "abcd1234", "MOPH_SECRET_KEY": "efgh5678"}, "u", NOW)
    ss.write_overlay(p, {"DB_PASS": "", "MOPH_CLIENT_KEY": "", "MOPH_SECRET_KEY": ""}, "u", NOW)
    assert ss.read_overlay(p).values == {"DB_PASS": "", "MOPH_CLIENT_KEY": "", "MOPH_SECRET_KEY": ""}
    for key in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DENGUE_START_FROM", "DENGUE_BASE_URL", "DENGUE_DATA_AUDITED"):
        with pytest.raises(SettingsError) as ei:
            ss.write_overlay(p, {key: ""}, "u", NOW)
        assert ei.value.key == key


def test_write_overlay_rejects_non_string_value(tmp_path):
    p = tmp_path / "settings.json"
    with pytest.raises(SettingsError):
        ss.write_overlay(p, {"DB_PORT": 3306}, "u", NOW)  # type: ignore[dict-item]
    assert not p.exists()


def test_write_overlay_treats_corrupt_current_as_empty_and_keeps_last_good_prev(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "good.one"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "good.two"}, "u", NOW)
    last_good_prev = prev_of(p).read_bytes()
    p.write_text("{broken", encoding="utf-8")
    ss.write_overlay(p, {"DB_PORT": "3307"}, "u", NOW)
    assert ss.read_overlay(p).values == {"DB_PORT": "3307"}  # ไฟล์เสียนับเป็นว่าง
    assert prev_of(p).read_bytes() == last_good_prev  # ไฟล์เสียไม่ถูกหมุนไปทับรุ่นดีล่าสุด


def test_write_overlay_does_not_rotate_when_current_has_invalid_keys(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "good.one"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "good.two"}, "u", NOW)
    last_good_prev = prev_of(p).read_bytes()
    p.write_text(json.dumps({"version": 1, "values": {"DB_PORT": "abc", "DB_HOST": "h"}}), encoding="utf-8")
    ss.write_overlay(p, {"DB_USER": "u1"}, "u", NOW)
    assert ss.read_overlay(p).values == {"DB_HOST": "h", "DB_USER": "u1"}
    assert prev_of(p).read_bytes() == last_good_prev


def test_write_overlay_creates_missing_parent_dir(tmp_path):
    p = tmp_path / "a" / "b" / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    assert p.is_file()


@posix_only
def test_write_overlay_sets_600_and_rotates_prev(tmp_path):
    p = tmp_path / "data" / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "first.host", "DB_PASS": "pw1"}, "u1", NOW)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert not prev_of(p).exists()  # ครั้งแรกไม่มีของเดิมให้หมุน
    first = p.read_bytes()
    ss.write_overlay(p, {"DB_HOST": "second.host"}, "u2", NOW)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(prev_of(p).stat().st_mode) == 0o600
    assert prev_of(p).read_bytes() == first
    assert json.loads(p.read_text(encoding="utf-8"))["values"]["DB_HOST"] == "second.host"
    assert leftovers(p.parent) == []


def test_write_overlay_rotates_prev_content(tmp_path):  # ไม่ขึ้นกับ POSIX
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "first.host"}, "u1", NOW)
    first = p.read_bytes()
    ss.write_overlay(p, {"DB_HOST": "second.host"}, "u2", NOW)
    assert prev_of(p).read_bytes() == first
    ss.write_overlay(p, {"DB_HOST": "third.host"}, "u3", NOW)
    assert json.loads(prev_of(p).read_text(encoding="utf-8"))["values"]["DB_HOST"] == "second.host"  # .prev มีรุ่นเดียว


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_write_overlay_is_atomic_when_replace_fails(tmp_path, monkeypatch, fail_on_call):
    p = tmp_path / "data" / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "old.host"}, "u", NOW)
    before = p.read_bytes()
    real_replace = os.replace
    calls = {"n": 0}

    def flaky(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == fail_on_call:
            raise OSError("disk full ZZZSECRETZZZ")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        ss.write_overlay(p, {"DB_HOST": "new.host", "DB_PASS": "NEWSECRET"}, "u", NOW)
    monkeypatch.setattr(os, "replace", real_replace)
    assert p.read_bytes() == before  # ไฟล์เดิมไม่เปลี่ยน
    assert leftovers(p.parent) == []  # ไม่เหลือไฟล์ชั่วคราว
    if fail_on_call == 1:
        assert not prev_of(p).exists()  # ล้มตอนหมุน .prev: ยังไม่มีอะไรถูกสร้าง
    else:
        assert prev_of(p).read_bytes() == before  # หมุน .prev สำเร็จแล้ว (= ไฟล์เดิม) ล้มตอนทับไฟล์ใหม่


def test_write_overlay_cleans_temp_when_write_fails(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "old.host"}, "u", NOW)
    before = p.read_bytes()
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        ss.write_overlay(p, {"DB_HOST": "new.host"}, "u", NOW)
    monkeypatch.undo()
    assert p.read_bytes() == before
    assert leftovers(p.parent) == []


def test_write_overlay_does_not_log_values(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_PASS": "TOPSECRET-pw", "DB_HOST": "db.secret.lan"}, "admin", NOW)
    ss.read_overlay(p)
    ss.revert_overlay(p, "admin", NOW)
    p.write_text("TOPSECRET-garbage", encoding="utf-8")
    ss.read_overlay(p)
    ss.effective_env({"DENGUE_DB_PATH": str(p.parent / "dengue.db")})
    for r in caplog.records:
        assert "TOPSECRET" not in r.getMessage() and "db.secret.lan" not in r.getMessage()


# ---------- ย้อนกลับ ----------

def test_revert_without_prev_returns_false(tmp_path):
    p = tmp_path / "settings.json"
    assert ss.revert_overlay(p, "u", NOW) is False
    ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    before = p.read_bytes()
    assert ss.revert_overlay(p, "u", NOW) is False
    assert p.read_bytes() == before


def test_revert_twice_returns_to_latest(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "one.host", "DB_PASS": "pw-one"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "two.host", "DB_PASS": "pw-two"}, "u", NOW)
    latest = p.read_bytes()
    older = prev_of(p).read_bytes()
    assert ss.revert_overlay(p, "u", NOW) is True
    assert p.read_bytes() == older and prev_of(p).read_bytes() == latest
    assert ss.read_overlay(p).values["DB_HOST"] == "one.host"
    assert ss.revert_overlay(p, "u", NOW) is True
    assert p.read_bytes() == latest and prev_of(p).read_bytes() == older
    assert ss.read_overlay(p).values["DB_PASS"] == "pw-two"
    assert leftovers(p.parent) == []


def test_revert_when_current_missing_moves_prev_into_place(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "one.host"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "two.host"}, "u", NOW)
    older = prev_of(p).read_bytes()
    p.unlink()
    assert ss.revert_overlay(p, "u", NOW) is True
    assert p.read_bytes() == older
    assert not prev_of(p).exists()
    assert leftovers(p.parent) == []


def _two_versions(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "one.host"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "two.host"}, "u", NOW)
    return p, p.read_bytes(), prev_of(p).read_bytes()  # path, latest, older


def _watch_replace(monkeypatch, p, fail_calls):
    """ฉีดความล้มเหลวที่ os.replace ครั้งที่ระบุ และจดว่า settings.json มีอยู่ก่อนทุกการเรียกหรือไม่"""
    real_replace = os.replace
    state = {"n": 0, "present": []}

    def flaky(src, dst, *a, **kw):
        state["n"] += 1
        state["present"].append(p.exists())
        if state["n"] in fail_calls:
            raise OSError("nope ZZZSECRETZZZ")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", flaky)
    return state, real_replace


def test_revert_fails_before_any_rename_changes_nothing(tmp_path, monkeypatch):
    p, latest, older = _two_versions(tmp_path)
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("boom")))  # ขั้น 1 (สำเนา) ล้ม
    with pytest.raises(OSError):
        ss.revert_overlay(p, "u", NOW)
    monkeypatch.undo()
    assert p.read_bytes() == latest and prev_of(p).read_bytes() == older
    assert leftovers(p.parent) == []


def test_revert_failure_on_first_replace_changes_nothing(tmp_path, monkeypatch):
    p, latest, older = _two_versions(tmp_path)
    state, real = _watch_replace(monkeypatch, p, {1})  # ขั้น 2 ล้ม
    with pytest.raises(OSError):
        ss.revert_overlay(p, "u", NOW)
    monkeypatch.setattr(os, "replace", real)
    assert all(state["present"])
    assert p.read_bytes() == latest and prev_of(p).read_bytes() == older
    assert leftovers(p.parent) == []  # ลบไฟล์ชั่วคราวแล้ว


def test_revert_failure_on_last_replace_still_counts_as_reverted_and_retries(tmp_path, monkeypatch):
    p, latest, older = _two_versions(tmp_path)
    state, real = _watch_replace(monkeypatch, p, {2})  # ขั้น 3 ล้มครั้งเดียว: ลองซ้ำแล้วสำเร็จ
    assert ss.revert_overlay(p, "u", NOW) is True
    monkeypatch.setattr(os, "replace", real)
    assert all(state["present"])  # settings.json ไม่เคยหาย
    assert p.read_bytes() == older and prev_of(p).read_bytes() == latest
    assert leftovers(p.parent) == []


def test_revert_failure_on_last_replace_keeps_recoverable_temp(tmp_path, monkeypatch):
    p, latest, older = _two_versions(tmp_path)
    state, real = _watch_replace(monkeypatch, p, {2, 3})  # ขั้น 3 ล้มทั้งครั้งแรกและครั้งลองซ้ำ
    assert ss.revert_overlay(p, "u", NOW) is True  # การย้อนมีผลแล้ว
    monkeypatch.setattr(os, "replace", real)
    assert all(state["present"])
    assert p.read_bytes() == older  # ไม่ขาด ไม่ครึ่งๆ กลางๆ
    kept = leftovers(p.parent)
    assert len(kept) == 1 and (p.parent / kept[0]).read_bytes() == latest  # รุ่นก่อนย้อนกู้ได้จากไฟล์ชั่วคราว
    assert not prev_of(p).exists() or prev_of(p).read_bytes() == older


def test_revert_refuses_unparseable_prev(tmp_path):
    p, latest, older = _two_versions(tmp_path)
    prev_of(p).write_text("{broken", encoding="utf-8")
    assert ss.revert_overlay(p, "u", NOW) is False
    assert p.read_bytes() == latest and prev_of(p).read_text(encoding="utf-8") == "{broken"
    prev_of(p).write_text(json.dumps({"version": 1, "values": {"DB_PORT": "abc"}}), encoding="utf-8")
    assert ss.revert_overlay(p, "u", NOW) is False  # มีคีย์ไม่ผ่านตรวจ = อ่านไม่ผ่าน
    assert p.read_bytes() == latest


@posix_only
def test_revert_keeps_600(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "one.host"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "two.host"}, "u", NOW)
    ss.revert_overlay(p, "u", NOW)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(prev_of(p).stat().st_mode) == 0o600


# ---------- เส้นทางไฟล์ ----------

def test_settings_path_follows_db_path_dir():
    assert ss.settings_path({"DENGUE_DB_PATH": "/opt/dengue_srrt/data/dengue.db"}) == Path("/opt/dengue_srrt/data/settings.json")
    assert ss.settings_path({}) == Path("data") / "settings.json"
    assert ss.settings_path({"DENGUE_DB_PATH": ""}) == Path("data") / "settings.json"
    assert ss.settings_path({"DENGUE_DB_PATH": "dengue.db"}) == Path("settings.json")


# ---------- effective_env / describe ----------

def env_with_overlay(tmp_path, overlay_values, base=None):
    p = tmp_path / "data" / "settings.json"
    if overlay_values:
        ss.write_overlay(p, overlay_values, "u", NOW)
    env = dict(base or {})
    env["DENGUE_DB_PATH"] = str(tmp_path / "data" / "dengue.db")
    return env


def test_effective_env_precedence_overlay_over_env(tmp_path):
    base = env_with_overlay(tmp_path, {"DB_HOST": "overlay.host", "DB_PORT": "3307"},
                            {"DB_HOST": "env.host", "DB_USER": "env_user", "SECRET_KEY": "k" * 40})
    out = ss.effective_env(base)
    assert out["DB_HOST"] == "overlay.host" and out["DB_PORT"] == "3307"
    assert out["DB_USER"] == "env_user"  # ไม่อยู่ใน overlay คงค่า env
    assert out["SECRET_KEY"] == "k" * 40
    assert base["DB_HOST"] == "env.host"  # ไม่แก้ mapping ที่ส่งเข้า
    assert out is not base


def test_effective_env_empty_string_clears_env_value(tmp_path):
    base = env_with_overlay(tmp_path, {"MOPH_CLIENT_KEY": ""}, {"MOPH_CLIENT_KEY": "from-env-value", "MOPH_SECRET_KEY": "other-env-value"})
    out = ss.effective_env(base)
    assert out["MOPH_CLIENT_KEY"] == ""
    assert out["MOPH_SECRET_KEY"] == "other-env-value"


def test_effective_env_ignores_corrupt_file(tmp_path):
    base = {"DB_HOST": "env.host", "DENGUE_DB_PATH": str(tmp_path / "data" / "dengue.db")}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text("{corrupt", encoding="utf-8")
    assert ss.effective_env(base) == base
    (tmp_path / "data" / "settings.json").write_bytes(b"\xff\xfe\x00\x01")
    assert ss.effective_env(base) == base


def test_effective_env_missing_file_equals_base(tmp_path):
    base = {"DB_HOST": "env.host", "DENGUE_DB_PATH": str(tmp_path / "data" / "dengue.db")}
    assert ss.effective_env(base) == base


def test_effective_env_never_leaks_non_allowed_keys_from_file(tmp_path):
    base = env_with_overlay(tmp_path, {"DB_HOST": "h"})
    p = tmp_path / "data" / "settings.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["values"].update({"SECRET_KEY": "evil" * 10, "DENGUE_DB_PATH": "/etc/elsewhere", "PATH": "/evil"})
    p.write_text(json.dumps(raw), encoding="utf-8")
    out = ss.effective_env(base)
    assert out["DB_HOST"] == "h"
    assert "SECRET_KEY" not in out and out["DENGUE_DB_PATH"] == base["DENGUE_DB_PATH"] and "PATH" not in out


def test_effective_env_uses_os_environ_without_modifying_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DENGUE_DB_PATH", str(tmp_path / "data" / "dengue.db"))
    monkeypatch.setenv("DB_HOST", "env.host")
    ss.write_overlay(tmp_path / "data" / "settings.json", {"DB_HOST": "overlay.host", "DB_PASS": "pw"}, "u", NOW)
    out = ss.effective_env()
    assert out["DB_HOST"] == "overlay.host" and out["DB_PASS"] == "pw"
    assert os.environ["DB_HOST"] == "env.host"
    assert "DB_PASS" not in os.environ
    assert out is not os.environ


def test_describe_never_returns_secret_values(tmp_path):
    base = env_with_overlay(tmp_path, {"DB_PASS": "TOPSECRET-pw", "MOPH_CLIENT_KEY": "TOPSECRET-id", "DB_HOST": "overlay.host"},
                            {"MOPH_SECRET_KEY": "TOPSECRET-env-secret"})
    d = ss.describe(base)
    assert set(d) == set(ALLOWED)
    for key, info in d.items():
        assert set(info) == {"label", "secret", "state", "source", "value"}
        assert info["label"] == ALLOWED[key].label and info["secret"] is ALLOWED[key].secret
        if info["secret"]:
            assert info["value"] is None
    assert "TOPSECRET" not in repr(d)
    assert d["DB_PASS"]["state"] == "set" and d["DB_PASS"]["source"] == "overlay"
    assert d["MOPH_SECRET_KEY"]["state"] == "set" and d["MOPH_SECRET_KEY"]["source"] == "env"
    assert d["DB_HOST"] == {"label": ALLOWED["DB_HOST"].label, "secret": False,
                            "state": "set", "source": "overlay", "value": "overlay.host"}


def test_describe_source_and_state_rules(tmp_path):
    base = env_with_overlay(tmp_path, {"MOPH_CLIENT_KEY": "", "DENGUE_DATA_AUDITED": "1"},
                            {"MOPH_CLIENT_KEY": "env-has-it", "DB_USER": "env_user", "DB_NAME": ""})
    d = ss.describe(base)
    assert (d["MOPH_CLIENT_KEY"]["source"], d["MOPH_CLIENT_KEY"]["state"]) == ("overlay", "unset")  # "" ใน overlay ล้าง env
    assert (d["DB_USER"]["source"], d["DB_USER"]["state"], d["DB_USER"]["value"]) == ("env", "set", "env_user")
    assert (d["DB_NAME"]["source"], d["DB_NAME"]["state"], d["DB_NAME"]["value"]) == ("default", "unset", None)
    assert (d["DB_HOST"]["source"], d["DB_HOST"]["state"], d["DB_HOST"]["value"]) == ("default", "unset", None)
    assert d["DENGUE_DATA_AUDITED"]["value"] == "1"


def test_describe_ignores_corrupt_overlay(tmp_path):
    base = {"DB_HOST": "env.host", "DENGUE_DB_PATH": str(tmp_path / "dengue.db")}
    (tmp_path / "settings.json").write_text("{corrupt", encoding="utf-8")
    d = ss.describe(base)
    assert d["DB_HOST"]["source"] == "env" and d["DB_HOST"]["value"] == "env.host"
    info = ss.overlay_info(base)
    assert info.error and info.values == {}


def test_overlay_info_reads_from_settings_path(tmp_path):
    base = env_with_overlay(tmp_path, {"DB_HOST": "h"})
    info = ss.overlay_info(base)
    assert info.values == {"DB_HOST": "h"} and info.updated_by == "u" and info.error is None


# ---------- fix round 1 ----------

SPACEY_PW = "  pw with\ttab and spaces \t "


def test_db_pass_is_stored_exactly_as_typed_through_write_read_effective_env(tmp_path):
    p = tmp_path / "data" / "settings.json"
    assert ss.validate("DB_PASS", SPACEY_PW) == SPACEY_PW
    ss.write_overlay(p, {"DB_PASS": SPACEY_PW}, "u", NOW)
    assert ss.read_overlay(p).values["DB_PASS"] == SPACEY_PW
    out = ss.effective_env({"DENGUE_DB_PATH": str(p.parent / "dengue.db")})
    assert out["DB_PASS"] == SPACEY_PW


def test_all_space_db_pass_is_accepted_but_other_keys_are_still_stripped():
    assert ss.validate("DB_PASS", "   ") == "   "
    assert ss.validate("DB_PASS", "\t") == "\t"
    assert ss.validate("DB_HOST", " h ") == "h"
    with pytest.raises(SettingsError):
        ss.validate("DB_HOST", "   ")


def test_empty_db_pass_means_clear_only_via_write_overlay(tmp_path):
    with pytest.raises(SettingsError):
        ss.validate("DB_PASS", "")
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_PASS": "x"}, "u", NOW)
    ss.write_overlay(p, {"DB_PASS": ""}, "u", NOW)
    assert ss.read_overlay(p).values == {"DB_PASS": ""}


@pytest.mark.parametrize("key", sorted(ALLOWED))
@pytest.mark.parametrize("bad", ["a\ud800b", "\udfff", "abcdefgh\ud800", "2026-11-0\ud800", "https://h.example/\ud800"])
def test_validators_reject_unencodable_strings_without_echo(key, bad):
    with pytest.raises(SettingsError) as ei:
        ss.validate(key, bad, TODAY)
    assert ei.value.key == key
    assert "\ud800" not in str(ei.value) and "\udfff" not in str(ei.value)
    with pytest.raises(SettingsError):
        ss.write_overlay(Path("never-written") / "settings.json", {key: bad}, "u", NOW)
    assert not Path("never-written").exists()


def test_base_url_length_cap_and_control_chars():
    ok = "https://wnkhos.go.th/" + "a" * (2048 - len("https://wnkhos.go.th/"))
    assert len(ok) == 2048 and ss.validate("DENGUE_BASE_URL", ok) == ok
    with pytest.raises(SettingsError):
        ss.validate("DENGUE_BASE_URL", ok + "a")
    huge = "https://wnkhos.go.th/" + "a" * 1_100_000  # ~1.1 MB
    with pytest.raises(SettingsError) as ei:
        ss.validate("DENGUE_BASE_URL", huge)
    assert "aaaaaaaa" not in str(ei.value)
    for c in ["\x00", "\x01", "\x1b", "\x1f", "\x7f", "\n", "\r", "\t"]:
        with pytest.raises(SettingsError):
            ss.validate("DENGUE_BASE_URL", f"https://wnkhos.go.th/a{c}b")


@pytest.mark.parametrize("key", ["MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"])
def test_moph_keys_reject_control_chars(key):
    for c in ["\x00", "\x01", "\x1b", "\x1f", "\x7f"]:
        with pytest.raises(SettingsError):
            ss.validate(key, f"abcd{c}efgh")


def test_huge_base_url_write_is_rejected_and_file_stays_readable(tmp_path):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    before = p.read_bytes()
    with pytest.raises(SettingsError):
        ss.write_overlay(p, {"DENGUE_BASE_URL": "https://wnkhos.go.th/" + "a" * 1_100_000}, "u", NOW)
    assert p.read_bytes() == before and ss.read_overlay(p).error is None


def test_write_overlay_backstop_when_serialized_file_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "MAX_FILE_BYTES", 60)
    p = tmp_path / "data" / "settings.json"
    with pytest.raises(SettingsError) as ei:
        ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    assert ei.value.key == "file" and str(ei.value)
    assert not p.parent.exists()  # ไม่เขียนอะไรเลย (ไม่แม้แต่สร้างโฟลเดอร์)


def test_backstop_leaves_existing_file_and_prev_untouched(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    ss.write_overlay(p, {"DB_HOST": "h2"}, "u", NOW)
    before, prev = p.read_bytes(), prev_of(p).read_bytes()
    monkeypatch.setattr(ss, "MAX_FILE_BYTES", len(before) - 1)
    with pytest.raises(SettingsError):
        ss.write_overlay(p, {"DB_HOST": "h3"}, "u", NOW)
    assert p.read_bytes() == before and prev_of(p).read_bytes() == prev
    assert leftovers(p.parent) == []


# ---------- ตรวจค่าตอนอ่าน ----------

def raw_overlay(tmp_path, values):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"version": 1, "updated_at": "2026-11-09T15:00:00", "updated_by": "a", "values": values}), encoding="utf-8")
    return p


def test_read_overlay_drops_invalid_values_and_names_only_keys(tmp_path):
    p = raw_overlay(tmp_path, {
        "DB_HOST": "good.host", "DB_PORT": "abc", "DENGUE_START_FROM": "not-a-date-ZZZSECRETZZZ",
        "DENGUE_BASE_URL": "https://wnkhos.go.th/" + "a" * 3000, "DB_PASS": "pw\nbad", "MOPH_CLIENT_KEY": "short",
        "DENGUE_DATA_AUDITED": "1", "DB_USER": "", "DB_NAME": "<hos>",
    })
    o = ss.read_overlay(p)
    assert o.values == {"DB_HOST": "good.host", "DENGUE_DATA_AUDITED": "1"}
    assert o.error
    for key in ("DB_PORT", "DENGUE_START_FROM", "DENGUE_BASE_URL", "DB_PASS", "MOPH_CLIENT_KEY", "DB_USER", "DB_NAME"):
        assert key in o.error
    assert "DB_HOST" not in o.error and "DENGUE_DATA_AUDITED" not in o.error
    assert "ZZZSECRETZZZ" not in o.error and "abc" not in o.error and "pw" not in o.error and "aaaa" not in o.error


def test_read_overlay_valid_file_has_no_error_and_keeps_empty_secrets(tmp_path):
    p = raw_overlay(tmp_path, {"DB_PASS": "", "MOPH_CLIENT_KEY": "", "MOPH_SECRET_KEY": "", "DB_PORT": "3306"})
    o = ss.read_overlay(p)
    assert o.error is None and o.values == {"DB_PASS": "", "MOPH_CLIENT_KEY": "", "MOPH_SECRET_KEY": "", "DB_PORT": "3306"}


def test_read_overlay_checks_only_date_format_not_window(tmp_path):
    p = raw_overlay(tmp_path, {"DENGUE_START_FROM": "2020-01-01"})  # go-live เก่า (เกิน 366 วัน) ต้องไม่ถูกทิ้ง
    o = ss.read_overlay(p)
    assert o.error is None and o.values == {"DENGUE_START_FROM": "2020-01-01"}
    with pytest.raises(SettingsError):  # แต่ตอนเขียนยังตรวจช่วง
        ss.validate("DENGUE_START_FROM", "2020-01-01", TODAY)
    for bad in ("2026-13-45", "20261109", "09/11/2026", ""):
        assert ss.read_overlay(raw_overlay(tmp_path, {"DENGUE_START_FROM": bad})).values == {}


def test_read_overlay_normalizes_values_it_keeps(tmp_path):
    o = ss.read_overlay(raw_overlay(tmp_path, {"DB_PORT": " 03306 ", "DB_PASS": SPACEY_PW}))
    assert o.values == {"DB_PORT": "3306", "DB_PASS": SPACEY_PW}


def test_read_overlay_drops_surrogate_escape_from_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"version": 1, "values": {"DB_HOST": "\\ud800", "DB_NAME": "ok"}}', encoding="utf-8")
    o = ss.read_overlay(p)
    assert o.values == {"DB_NAME": "ok"} and "DB_HOST" in o.error


def test_invalid_overlay_keys_are_not_applied_by_effective_env_or_describe(tmp_path):
    raw_overlay(tmp_path, {"DB_PORT": "abc", "DB_HOST": "overlay.host"})
    base = {"DENGUE_DB_PATH": str(tmp_path / "dengue.db"), "DB_PORT": "3306"}
    out = ss.effective_env(base)
    assert out["DB_PORT"] == "3306" and out["DB_HOST"] == "overlay.host"
    d = ss.describe(base)
    assert d["DB_PORT"]["source"] == "env" and d["DB_HOST"]["source"] == "overlay"


# ---------- ขอบเขตการอ่านไฟล์ ----------

def test_read_overlay_size_cap_boundary(tmp_path):
    p = tmp_path / "settings.json"
    body = b'{"version": 1, "values": {"DB_HOST": "h"}}'
    p.write_bytes(body + b" " * (ss.MAX_FILE_BYTES - len(body)))  # พอดีเพดาน ยังอ่านได้
    assert ss.read_overlay(p).values == {"DB_HOST": "h"}
    p.write_bytes(body + b" " * (ss.MAX_FILE_BYTES - len(body) + 1))  # เกินไป 1 ไบต์
    o = ss.read_overlay(p)
    assert o.values == {} and o.error


def test_read_overlay_nul_byte_inside_json(tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(b'{"version": 1, "values": {"DB_HOST": "h\x00x"}}')  # NUL ดิบในสตริง JSON = ไม่ใช่ JSON
    o = ss.read_overlay(p)
    assert o.values == {} and o.error
    p.write_bytes(b'{"version": 1, "values": {"DB_PASS": "a\\u0000b", "DB_HOST": "h"}}')  # \\u0000 เป็น JSON ถูกต้อง
    o = ss.read_overlay(p)
    assert o.values == {"DB_HOST": "h"} and "DB_PASS" in o.error  # ตัวตรวจ DB_PASS ทิ้งเฉพาะคีย์นั้น


def test_symlinked_settings_json_is_read_but_write_replaces_link_not_target(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"version": 1, "values": {"DB_HOST": "linked.host"}}), encoding="utf-8")
    link = tmp_path / "data" / "settings.json"
    link.parent.mkdir()
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("สร้าง symlink ไม่ได้ (ต้องมีสิทธิ์พิเศษบน Windows)")
    assert ss.read_overlay(link).values == {"DB_HOST": "linked.host"}
    before = target.read_bytes()
    ss.write_overlay(link, {"DB_PORT": "3307"}, "u", NOW)
    assert target.read_bytes() == before  # ไม่เขียนทะลุ symlink ไปไฟล์ปลายทาง
    assert not link.is_symlink() and ss.read_overlay(link).values == {"DB_HOST": "linked.host", "DB_PORT": "3307"}
    assert not prev_of(link).exists()  # ไม่ใช่ไฟล์ปกติ จึงไม่หมุนเป็น .prev
    assert leftovers(link.parent) == []


# ---------- POSIX: สิทธิ์ไฟล์ / fsync โฟลเดอร์ ----------

@posix_only
def test_read_overlay_warns_once_when_group_or_other_can_access(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    p = tmp_path / "settings.json"
    ss.write_overlay(p, {"DB_PASS": "TOPSECRET-pw"}, "u", NOW)
    ss.read_overlay(p)
    assert not [r for r in caplog.records if "permissions" in r.getMessage()]  # 600 ไม่เตือน
    os.chmod(p, 0o644)
    caplog.clear()
    ss.read_overlay(p)
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "permissions" in r.getMessage()]
    assert len(warns) == 1
    assert "settings.json" in warns[0].getMessage() and "TOPSECRET" not in warns[0].getMessage()
    assert str(tmp_path) not in warns[0].getMessage()


def test_permission_warning_is_logged_once_per_process_per_path(tmp_path, caplog, monkeypatch):
    """ENV_PROVIDER อ่านไฟล์ทุกคำขอ: คำเตือนสิทธิ์ต้องไม่ท่วม log — หนึ่งครั้งต่อไฟล์ต่อโปรเซส (ปลอดภัยต่อหลายเธรด)"""
    import logging
    import threading
    a, b = tmp_path / "a" / "settings.json", tmp_path / "b" / "settings.json"
    for p in (a, b):
        ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
        os.chmod(p, 0o644)                       # Windows: st_mode ของไฟล์ปกติมีบิต group/other อยู่แล้ว
    monkeypatch.setattr(ss, "_posix", lambda: True)
    caplog.set_level(logging.DEBUG)

    def warns():
        return [r for r in caplog.records if r.levelno == logging.WARNING and "permissions" in r.getMessage()]
    for _ in range(5):
        ss.read_overlay(a)
    assert len(warns()) == 1
    ss.read_overlay(b)                           # คนละไฟล์ = เตือนของมันเอง
    assert len(warns()) == 2
    ss.read_overlay(b)
    ss.read_overlay(a)
    assert len(warns()) == 2
    # หลายเธรดอ่านพร้อมกันไฟล์ใหม่ = เตือนครั้งเดียว
    c = tmp_path / "c" / "settings.json"
    ss.write_overlay(c, {"DB_HOST": "h"}, "u", NOW)
    os.chmod(c, 0o644)
    threads = [threading.Thread(target=ss.read_overlay, args=(c,)) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(warns()) == 3


@posix_only
def test_directory_is_fsynced_after_replace_and_failure_is_ignored(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    real_fsync = os.fsync
    synced = []

    def spy(fd):
        synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    ss.write_overlay(p, {"DB_HOST": "h"}, "u", NOW)
    assert True in synced  # มี fsync ของโฟลเดอร์

    def dir_fails(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("x")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", dir_fails)
    ss.write_overlay(p, {"DB_HOST": "h2"}, "u", NOW)  # fsync โฟลเดอร์ล้มต้องไม่ทำให้การเขียนล้ม
    assert ss.read_overlay(p).values == {"DB_HOST": "h2"}
