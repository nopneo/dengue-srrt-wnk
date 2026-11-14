"""deploy/set_env.py — ตัวช่วยตั้งค่า .env: ไม่พิมพ์ค่าลับ คงเจ้าของไฟล์ ตรวจรูปแบบ"""

import importlib.util
import os
import stat
from datetime import date
from pathlib import Path

import pytest

import envfile

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("set_env", ROOT / "deploy" / "set_env.py")
set_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(set_env)

TODAY = date(2026, 11, 9)


def feeder(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


def make_env(tmp_path):
    p = tmp_path / ".env"
    p.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    return p


def run_main(path, plain, secret, capsys=None):
    return set_env.main(["--env", str(path)], ask=feeder(plain), ask_secret=feeder(secret))


def test_sets_values_and_generates_strong_secret_key(tmp_path, capsys):
    p = make_env(tmp_path)
    # DB_HOST, DB_PORT, DB_NAME, DB_USER, DENGUE_BASE_URL, DENGUE_START_FROM
    plain = ["db.example.lan", "", "", "ro_user", "", date.today().isoformat()]
    secret = ["P@ss w0rd&$x", "", ""]  # DB_PASS, MOPH id, MOPH secret (blank = not set)
    rc = run_main(p, plain, secret)
    out = capsys.readouterr().out
    assert rc == 0, out
    env = envfile.parse_env_file(str(p))
    assert env["DB_HOST"] == "db.example.lan"
    assert env["DB_USER"] == "ro_user"
    assert env["DB_PASS"] == "P@ss w0rd&$x"  # อักขระพิเศษคงเดิมหลังครอบ single quote
    assert len(env["SECRET_KEY"]) >= 32 and "<" not in env["SECRET_KEY"]
    assert env["DENGUE_BASE_URL"] == "https://wnkhos.go.th/dengue"
    assert env["MOPH_CLIENT_KEY"] == "" and env["MOPH_SECRET_KEY"] == ""  # ค่าตัวอย่าง <...> ถูกล้างเป็นว่าง
    assert "<" not in "".join(l for l in p.read_text(encoding="utf-8").splitlines() if l and not l.startswith("#"))


def test_never_prints_secret_values(tmp_path, capsys):
    p = make_env(tmp_path)
    run_main(p, ["h", "", "", "u", "", date.today().isoformat()], ["TopSecretPw123", "moph-id-AAA", "moph-secret-BBB"])
    out = capsys.readouterr().out
    for s in ("TopSecretPw123", "moph-id-AAA", "moph-secret-BBB"):
        assert s not in out
    env = envfile.parse_env_file(str(p))
    secret_key = env["SECRET_KEY"]
    assert secret_key not in out
    assert env["MOPH_CLIENT_KEY"] == "moph-id-AAA"


def test_keeps_existing_strong_secret_key_and_db_pass_on_rerun(tmp_path):
    p = make_env(tmp_path)
    run_main(p, ["h", "", "", "u", "", date.today().isoformat()], ["pw-one", "", ""])
    first = envfile.parse_env_file(str(p))
    run_main(p, ["", "", "", "", "", ""], ["", "", ""])  # Enter ทุกช่อง = คงค่าเดิม
    second = envfile.parse_env_file(str(p))
    assert second["SECRET_KEY"] == first["SECRET_KEY"]
    assert second["DB_PASS"] == "pw-one"
    assert second["DB_HOST"] == "h" and second["DB_USER"] == "u"


def test_weak_or_placeholder_secret_key_is_replaced(tmp_path):
    p = make_env(tmp_path)
    text = p.read_text(encoding="utf-8").replace("SECRET_KEY=<สุ่มยาว 32 ตัวอักษรขึ้นไป>", "SECRET_KEY=short")
    p.write_text(text, encoding="utf-8")
    run_main(p, ["h", "", "", "u", "", date.today().isoformat()], ["pw", "", ""])
    assert len(envfile.parse_env_file(str(p))["SECRET_KEY"]) >= 32


@pytest.mark.parametrize("bad", ["2569-11-09", "09/11/2026", "2026-13-40", "2020-01-01"])
def test_start_from_rejects_buddhist_year_and_far_dates(bad):
    with pytest.raises(set_env.InputError):
        set_env.validate_start_from(bad, TODAY)


def test_start_from_accepts_today_and_near_dates():
    assert set_env.validate_start_from("2026-11-15", TODAY) == "2026-11-15"


def test_value_with_single_quote_is_rejected_and_reprompted(tmp_path, capsys):
    p = make_env(tmp_path)
    run_main(p, ["h", "", "", "u", "", date.today().isoformat()], ["bad'pw", "good-pw", "", ""])
    out = capsys.readouterr().out
    assert "ไม่ถูกต้อง" in out
    assert envfile.parse_env_file(str(p))["DB_PASS"] == "good-pw"


def test_missing_file_exits_2(tmp_path, capsys):
    assert set_env.main(["--env", str(tmp_path / "nope.env")], ask=feeder([]), ask_secret=feeder([])) == 2


def test_eof_cancels_without_modifying_file(tmp_path):
    p = make_env(tmp_path)
    before = p.read_bytes()

    def eof(prompt=""):
        raise EOFError

    assert set_env.main(["--env", str(p)], ask=eof, ask_secret=eof) == 2
    assert p.read_bytes() == before


def test_edits_in_place_keeping_inode_and_mode(tmp_path):
    p = make_env(tmp_path)
    if os.name == "posix":
        os.chmod(p, 0o640)
    inode = p.stat().st_ino
    mode = stat.S_IMODE(p.stat().st_mode)
    run_main(p, ["h", "", "", "u", "", date.today().isoformat()], ["pw", "", ""])
    assert p.stat().st_ino == inode
    assert stat.S_IMODE(p.stat().st_mode) == mode


def test_set_key_replaces_active_then_commented_then_appends():
    lines = ["A=1", "# B=2", "C=3"]
    set_env.set_key(lines, "A", "9")
    set_env.set_key(lines, "B", "8")
    set_env.set_key(lines, "D", "7")
    assert lines == ["A=9", "B=8", "C=3", "D=7"]
