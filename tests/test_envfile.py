"""envfile: ตัวอ่าน .env แบบไม่รันอะไร + การเรียกจาก CLI เท่านั้น."""
import pytest

import envfile
import run
import setup_check
from envfile import load_dotenv_if_present, parse_env_file


def write(tmp_path, text, name=".env"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_parse_basic_comments_blank_and_export(tmp_path):
    path = write(tmp_path, "# คอมเมนต์\n\nA=1\n  B = two  \nexport C=3\nexport\tD=4\n# E=5\n")
    assert parse_env_file(path) == {"A": "1", "B": "two", "C": "3", "D": "4"}


def test_parse_strips_matching_quotes_only(tmp_path):
    path = write(tmp_path, "A='single'\nB=\"double\"\nC='mismatch\"\nD='\nE=''\nF=\"a b\"\n")
    assert parse_env_file(path) == {"A": "single", "B": "double", "C": "'mismatch\"",
                                    "D": "'", "E": "", "F": "a b"}


def test_parse_preserves_special_characters_literally(tmp_path):
    value = "p&ss;w$ord `whoami` $(id) ${HOME} a b !x"
    path = write(tmp_path, f"DB_PASS='{value}'\nPLAIN={value}\n")
    got = parse_env_file(path)
    assert got["DB_PASS"] == value and got["PLAIN"] == value


def test_parse_does_not_strip_inline_hash_or_expand(tmp_path):
    path = write(tmp_path, "A=abc # not a comment\nB=$A\nC=\"x # y\"\n")
    assert parse_env_file(path) == {"A": "abc # not a comment", "B": "$A", "C": "x # y"}


def test_parse_skips_bad_lines_and_keeps_last_duplicate(tmp_path):
    path = write(tmp_path, "no equals sign\n1BAD=x\nbad-name=y\nA=1\nA=2\n=v\n")
    assert parse_env_file(path) == {"A": "2"}


def test_parse_reads_utf8_with_bom_and_crlf(tmp_path):
    p = tmp_path / ".env"
    p.write_bytes("\ufeffA=ไทย\r\nB=2\r\n".encode("utf-8"))
    assert parse_env_file(str(p)) == {"A": "ไทย", "B": "2"}


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_env_file(str(tmp_path / "nope"))


def test_load_does_not_override_existing_and_returns_names_only(tmp_path):
    path = write(tmp_path, "A=file\nB=file\nC=\n")
    env = {"A": "already"}
    loaded = load_dotenv_if_present(path, env)
    assert loaded == ["B", "C"]
    assert env == {"A": "already", "B": "file", "C": ""}
    assert "file" not in " ".join(loaded)


def test_load_existing_empty_value_is_not_overridden(tmp_path):
    env = {"A": ""}
    assert load_dotenv_if_present(write(tmp_path, "A=x\n"), env) == []
    assert env == {"A": ""}


def test_load_missing_file_returns_empty(tmp_path):
    env = {}
    assert load_dotenv_if_present(str(tmp_path / "nope"), env) == []
    assert env == {}


def test_load_directory_or_unreadable_returns_empty(tmp_path):
    assert load_dotenv_if_present(str(tmp_path), {}) == []
    bad = tmp_path / "bad.env"
    bad.write_bytes(b"A=\xff\xfe\xfa\n")
    assert load_dotenv_if_present(str(bad), {}) == []


def test_load_uses_dengue_env_file_then_cwd_dotenv(tmp_path, monkeypatch):
    other = write(tmp_path, "FROM_VAR=1\n", name="other.env")
    write(tmp_path, "FROM_CWD=1\n")
    monkeypatch.chdir(tmp_path)
    env = {"DENGUE_ENV_FILE": other}
    assert load_dotenv_if_present(environ=env) == ["FROM_VAR"]
    env2 = {}
    assert load_dotenv_if_present(environ=env2) == ["FROM_CWD"]


def test_load_default_target_is_real_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("ZZ_TEST_LOADED", raising=False)
    monkeypatch.delenv("DENGUE_ENV_FILE", raising=False)
    path = write(tmp_path, "ZZ_TEST_LOADED=yes\n")
    monkeypatch.setenv("DENGUE_ENV_FILE", path)
    assert load_dotenv_if_present() == ["ZZ_TEST_LOADED"]
    import os
    assert os.environ["ZZ_TEST_LOADED"] == "yes"
    monkeypatch.delenv("ZZ_TEST_LOADED")


def test_does_not_execute_anything(tmp_path):
    marker = tmp_path / "pwned"
    path = write(tmp_path, f"A=$(touch {marker})\nB=`touch {marker}`\nC=x; touch {marker}\n")
    parse_env_file(path)
    assert not marker.exists()


# ---------- เรียกจาก CLI เท่านั้น ----------

def test_run_main_loads_dotenv_only_when_env_not_given(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(run, "load_dotenv_if_present", lambda *a, **k: calls.append(1) or [])
    with pytest.raises(SystemExit):
        run.main([])                      # ไม่ระบุคำสั่ง -> parser.error
    assert calls == [1]
    db = tmp_path / "d.db"
    run.main(["add-user", "x y", "viewer"], env={"DENGUE_DB_PATH": str(db)})
    assert calls == [1]                   # ส่ง env มาเอง = ไม่โหลดไฟล์


def test_setup_check_main_loads_dotenv_only_for_real_build(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup_check, "load_dotenv_if_present",
                        lambda *a, **k: calls.append(1) or [])
    import wiring

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(wiring, "build_runtime", boom)
    assert setup_check.main([]) == 1
    assert calls == [1]
    setup_check.main([], build=boom)
    assert calls == [1]


def test_app_and_wsgi_do_not_call_the_loader():
    from pathlib import Path
    root = Path(envfile.__file__).parent
    for name in ("app.py", "wsgi.py", "wiring.py", "config.py"):
        assert "envfile" not in (root / name).read_text(encoding="utf-8"), name
