"""คำสั่งดูแลบัญชีและสำรองข้อมูลของ run.py (set-password · remove-user · backup) — ออฟไลน์ทั้งหมด."""
import sqlite3
import stat
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

import run
from store import Store


def env_for(db):
    return {"DENGUE_DB_PATH": str(db)}


def passwords(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(run.getpass, "getpass", lambda prompt="": next(it))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data" / "dengue.db"
    Store(str(path)).add_user("nurse1", "old-password-1", "srrt")
    return path


# ---------------------------------------------------------------- set-password

def test_set_password_changes_password(db, monkeypatch, capsys):
    passwords(monkeypatch, "brand-new-pw-9", "brand-new-pw-9")
    assert run.main(["set-password", "nurse1"], env=env_for(db)) == 0
    store = Store(str(db))
    assert store.check_user("nurse1", "brand-new-pw-9") == "srrt"
    assert store.check_user("nurse1", "old-password-1") is None
    out = capsys.readouterr()
    assert "brand-new-pw-9" not in out.out + out.err


def test_set_password_unknown_user_exit_2(db, monkeypatch, capsys):
    passwords(monkeypatch, "brand-new-pw-9", "brand-new-pw-9")
    assert run.main(["set-password", "ghost"], env=env_for(db)) == 2
    assert "ghost" in capsys.readouterr().err
    assert Store(str(db)).user_role("ghost") is None


def test_set_password_mismatch_exit_2_and_unchanged(db, monkeypatch):
    passwords(monkeypatch, "brand-new-pw-9", "brand-new-pw-8")
    assert run.main(["set-password", "nurse1"], env=env_for(db)) == 2
    assert Store(str(db)).check_user("nurse1", "old-password-1") == "srrt"


@pytest.mark.parametrize("short", ["", "123456789"])
def test_set_password_too_short_exit_2_and_unchanged(db, monkeypatch, capsys, short):
    passwords(monkeypatch, short, short)
    assert run.main(["set-password", "nurse1"], env=env_for(db)) == 2
    err = capsys.readouterr().err
    assert err and (not short or "10" in err)
    assert Store(str(db)).check_user("nurse1", "old-password-1") == "srrt"


def test_set_password_accepts_exactly_min_length(db, monkeypatch):
    passwords(monkeypatch, "1234567890", "1234567890")
    assert run.main(["set-password", "nurse1"], env=env_for(db)) == 0


# ---------------------------------------------------------------- add-user min length

def test_add_user_rejects_password_shorter_than_ten(tmp_path, monkeypatch, capsys):
    path = tmp_path / "data" / "dengue.db"
    passwords(monkeypatch, "short-pw1", "short-pw1")      # 9 ตัวอักษร
    assert run.main(["add-user", "bob", "viewer"], env=env_for(path)) == 2
    assert "10" in capsys.readouterr().err
    assert Store(str(path)).user_role("bob") is None


# ---------------------------------------------------------------- remove-user

def test_remove_user_requires_yes_confirmation(db, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "yes")
    assert run.main(["remove-user", "nurse1"], env=env_for(db)) == 0
    assert prompts == ["พิมพ์ yes เพื่อยืนยัน: "]
    assert Store(str(db)).user_role("nurse1") is None
    assert "nurse1" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "y", "no", "ใช่"])
def test_remove_user_other_answers_exit_2_and_keep_user(db, monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert run.main(["remove-user", "nurse1"], env=env_for(db)) == 2
    assert Store(str(db)).user_role("nurse1") == "srrt"


def test_remove_user_eof_exit_2(db, monkeypatch):
    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert run.main(["remove-user", "nurse1"], env=env_for(db)) == 2
    assert Store(str(db)).user_role("nurse1") == "srrt"


def test_remove_user_yes_flag_skips_prompt(db, monkeypatch):
    def fail(prompt=""):
        raise AssertionError("ต้องไม่ถามเมื่อใช้ --yes")
    monkeypatch.setattr("builtins.input", fail)
    assert run.main(["remove-user", "nurse1", "--yes"], env=env_for(db)) == 0
    assert Store(str(db)).user_role("nurse1") is None


def test_remove_user_unknown_exit_2_even_with_yes(db, capsys):
    assert run.main(["remove-user", "ghost", "--yes"], env=env_for(db)) == 2
    assert "ghost" in capsys.readouterr().err
    assert Store(str(db)).user_role("nurse1") == "srrt"


# ---------------------------------------------------------------- backup (I6)

T0 = datetime(2026, 11, 9, 10, 30, 15)


def seeded(db):
    store = Store(str(db))
    cid = store.add_case("VN:1", "OPD", "HN1", "A90", datetime(2026, 11, 8, 9, 0),
                         "40010103", "12", "1", 30, True)
    return store, cid


def backups_dir(db):
    return Path(db).parent / "backups"


def test_backup_writes_consistent_copy_named_by_time(db, capsys):
    store, cid = seeded(db)
    assert run._backup(14, env_for(db), now=T0) == 0
    target = backups_dir(db) / "dengue-20261109-1030.db"
    assert target.is_file()
    assert str(target) in capsys.readouterr().out
    copy = Store(str(target))
    assert copy.get_case(cid)["visit_key"] == "VN:1"
    assert copy.check_user("nurse1", "old-password-1") == "srrt"
    with closing(sqlite3.connect(target)) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # ฐานจริงยังเขียนได้ (ไม่มีมือจับค้าง) และสำเนาไม่เปลี่ยนตามฐานจริง
    store.add_case("VN:2", "OPD", "HN2", "A90", datetime(2026, 11, 8, 10, 0),
                   "40010103", "1", "1", 5, True)
    assert Store(str(target)).get_case(2) is None


def test_backup_cli_default_keep_and_exit_code(db, capsys):
    seeded(db)
    assert run.main(["backup"], env=env_for(db)) == 0
    [f] = list(backups_dir(db).glob("dengue-*.db"))
    assert str(f) in capsys.readouterr().out


def test_backup_prunes_older_beyond_keep_and_ignores_other_files(db):
    seeded(db)
    bdir = backups_dir(db)
    bdir.mkdir()
    for stamp in ("20261101-0100", "20261102-0100", "20261103-0100", "20261104-0100"):
        (bdir / f"dengue-{stamp}.db").write_bytes(b"old")
    (bdir / "notes.txt").write_text("keep me")
    assert run._backup(3, env_for(db), now=T0) == 0
    names = sorted(p.name for p in bdir.iterdir())
    assert names == ["dengue-20261103-0100.db", "dengue-20261104-0100.db",
                     "dengue-20261109-1030.db", "notes.txt"]


def test_backup_keep_one_keeps_only_newest(db):
    seeded(db)
    run._backup(14, env_for(db), now=datetime(2026, 11, 9, 10, 0))
    run._backup(1, env_for(db), now=datetime(2026, 11, 9, 11, 0))
    assert [p.name for p in backups_dir(db).glob("dengue-*.db")] == ["dengue-20261109-1100.db"]


def test_backup_same_minute_twice_overwrites_cleanly(db):
    store, _ = seeded(db)
    run._backup(14, env_for(db), now=T0)
    store.add_user("viewer2", "password-vvv-1", "viewer")
    assert run._backup(14, env_for(db), now=T0) == 0
    [f] = list(backups_dir(db).glob("dengue-*.db"))
    assert Store(str(f)).user_role("viewer2") == "viewer"
    assert not list(backups_dir(db).glob("*.tmp"))


@pytest.mark.parametrize("keep", [0, -1])
def test_backup_rejects_keep_below_one(db, capsys, keep):
    seeded(db)
    assert run.main(["backup", "--keep", str(keep)], env=env_for(db)) == 2
    assert not backups_dir(db).exists()
    assert capsys.readouterr().err


def test_backup_missing_database_is_friendly_and_creates_nothing(tmp_path, capsys):
    path = tmp_path / "nodata" / "dengue.db"
    assert run._backup(14, env_for(path), now=T0) == 2
    assert "ไม่พบ" in capsys.readouterr().err
    assert not (path.parent / "backups").exists()


@pytest.mark.skipif(__import__("os").name != "posix", reason="สิทธิ์ไฟล์ POSIX")
def test_backup_dir_and_files_are_private(db):
    seeded(db)
    run._backup(14, env_for(db), now=T0)
    bdir = backups_dir(db)
    assert stat.S_IMODE(bdir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(f.stat().st_mode) & 0o077 == 0 for f in bdir.glob("*.db"))


# ---------------------------------------------------------------- import-facilities
from types import SimpleNamespace

from fakes import FakeSource, HosxpError

RPST = "โรงพยาบาลส่งเสริมสุขภาพตำบล"
CATALOG = [
    {"hospcode": "02846", "name": RPST + "ระเริง", "kind": "rpst", "tmb": "302503"},
    {"hospcode": "02844", "name": RPST + "ยุบอีปูน", "kind": "rpst", "tmb": "302502"},
    {"hospcode": "10894", "name": "โรงพยาบาลวังน้ำเขียว", "kind": "hospital", "tmb": "302505"},
]
CSV_HEAD = "รหัสหน่วยงาน,กระทรวง,กรม,หน่วยงาน,หมุดที่ไม่พบความผิดปกติ,ที่อยู่จุดบริการ,ละติจูด,ลองติจูด"


def csv_line(name, lat, lng):
    return f"8.89921E+12,กระทรวงสาธารณสุข,สำนักงานปลัดกระทรวงสาธารณสุข,{name} จังหวัดนครราชสีมา,Y,ที่อยู่,{lat},{lng}"


@pytest.fixture
def fac_env(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "data" / "fac.db"))
    source = FakeSource(facilities=CATALOG, area=("3025",))
    rt = SimpleNamespace(settings=SimpleNamespace(area_amphoe=("3025",)), store=store, source=source)
    monkeypatch.setattr(run, "build_runtime", lambda env=None: rt)
    return rt


def write_fac_csv(tmp_path, *lines, name="fac.csv"):
    p = tmp_path / name
    p.write_bytes(("\ufeff" + "\n".join([CSV_HEAD, *lines]) + "\n").encode("utf-8"))
    return p


GOOD = [csv_line(RPST + "ระเริง", "14.42899329", "101.8211519"),
        csv_line("โรงพยาบาลวังน้ำเขียว", "14.41768128", "101.8630457"),
        csv_line("ชื่อที่ไม่มีใน HOSxP", "14.4", "101.8")]


def test_import_facilities_writes_matched_rows_and_prints_summary_without_coordinates(fac_env, tmp_path, capsys):
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p)], env={}) == 0
    rows = {f["hospcode"]: f for f in fac_env.store.facility_all()}
    assert set(rows) == {"02846", "10894"} and all(f["source"] == "csv" for f in rows.values())
    assert rows["02846"]["tmb"] == "302503" and rows["02846"]["kind"] == "rpst"
    out = capsys.readouterr().out
    assert "นำเข้า 2" in out and "ชื่อที่ไม่มีใน HOSxP" in out and "ยุบอีปูน" in out
    for coord in ("14.42899", "101.8211", "14.4176", "101.8630"):
        assert coord not in out


def test_import_facilities_dry_run_writes_nothing(fac_env, tmp_path, capsys):
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p), "--dry-run"], env={}) == 0
    assert fac_env.store.facility_all() == []
    assert "dry-run" in capsys.readouterr().out


def test_import_facilities_keeps_manual_without_force(fac_env, tmp_path, capsys):
    fac_env.store.facility_upsert("02846", RPST + "ระเริง", "rpst", "302503", 14.5, 101.9, "manual", "admin",
                                  datetime(2026, 11, 10, 9, 0))
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p)], env={}) == 0
    got = fac_env.store.facility_get("02846")
    assert got["lat"] == 14.5 and got["source"] == "manual"
    assert "manual" in capsys.readouterr().out
    assert run.main(["import-facilities", str(p), "--force"], env={}) == 0
    got = fac_env.store.facility_get("02846")
    assert got["lat"] == pytest.approx(14.42899329) and got["source"] == "csv"


def test_import_facilities_warns_when_outside_district_but_still_imports(fac_env, tmp_path, capsys):
    p = write_fac_csv(tmp_path, csv_line(RPST + "ระเริง", "14.0", "101.0"))
    assert run.main(["import-facilities", str(p)], env={}) == 0
    out = capsys.readouterr().out
    assert "นอกอำเภอ" in out and RPST + "ระเริง" in out
    assert fac_env.store.facility_get("02846") is not None


def test_import_facilities_hosxp_failure_exit_1(fac_env, tmp_path, capsys):
    fac_env.source.failure, fac_env.source.fail_methods = HosxpError("down 10.1.2.3"), {"facilities"}
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p)], env={}) == 1
    assert fac_env.store.facility_all() == []
    assert "10.1.2.3" not in capsys.readouterr().err


def test_import_facilities_no_catalog_exit_2(fac_env, tmp_path):
    fac_env.source.facility_catalog = []
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p)], env={}) == 2


def test_import_facilities_bad_csv_exit_1(fac_env, tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert run.main(["import-facilities", str(bad)], env={}) == 1
    assert "คอลัมน์" in capsys.readouterr().err
    assert run.main(["import-facilities", str(tmp_path / "missing.csv")], env={}) == 1
    assert fac_env.store.facility_all() == []


def test_import_facilities_idempotent(fac_env, tmp_path):
    p = write_fac_csv(tmp_path, *GOOD)
    assert run.main(["import-facilities", str(p)], env={}) == 0
    first = [(f["hospcode"], f["lat"], f["lng"], f["source"]) for f in fac_env.store.facility_all()]
    assert run.main(["import-facilities", str(p)], env={}) == 0
    assert [(f["hospcode"], f["lat"], f["lng"], f["source"]) for f in fac_env.store.facility_all()] == first
