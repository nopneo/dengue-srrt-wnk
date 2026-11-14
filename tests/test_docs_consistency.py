"""เอกสารและไฟล์ deploy ต้องตรงกับโค้ด (Task 11).

- ทุกตัวแปร env ที่โค้ดอ่านต้องอยู่ใน .env.example และ .env.example ต้องไม่มีค่าลับจริง
- INSTALL.md ต้องมีข้อกำหนดบังคับ (gunicorn, DENGUE_START_FROM, setup_check, X-Requested-With ...)
- unit systemd ต้องเป็น worker เดียวและไม่ใช้ preload
- bug_report.yml ต้องบังคับแนบผล setup_check และยืนยันว่าไม่มีข้อมูลผู้ป่วย
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# ชื่อ env ที่ตั้งใจไม่ใส่ใน .env.example (ชื่อ -> เหตุผล) — ว่างไว้: ทุกตัวที่โค้ดอ่านต้องมีเอกสาร
UNDOCUMENTED_OK: dict[str, str] = {}
# ชื่อใน .env.example ที่โค้ดไม่ได้อ่านจริง (ชื่อ -> เหตุผล) — ว่างไว้เช่นกัน
UNREAD_OK: dict[str, str] = {}

# ชื่อรูปแบบของแอปนี้ (พบเป็นสตริงในไฟล์ใดก็ถือว่าอ่านเป็น env)
PREFIXED_LITERAL = re.compile(r"""["']((?:DENGUE|MOPH|DB)_[A-Z0-9_]+|SECRET_KEY)["']""")
# อ่านจาก os.environ ตรง ๆ (environ[...] ที่ไม่มี os. คือ WSGI environ เช่น SCRIPT_NAME จึงไม่นับ)
STRICT_READ = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""")
# `from os import environ / getenv` -> อ่านแบบไม่มี os. ได้
FROM_OS_IMPORT = re.compile(r"(?m)^\s*from\s+os\s+import\s+[^\n]*\b(?:environ|getenv)\b")
BARE_READ = re.compile(
    r"""(?<![\w.])(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|(?<![\w.])environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""")
# ไฟล์ที่ตั้งชื่อแทน os.environ (เช่น `e = os.environ if ...`) -> `get("X")` / `e.get("X")` คือการอ่าน env
ENV_ALIAS = re.compile(r"=\s*(?:dict\(\s*)?os\.environ\b")
LOOSE_GET = re.compile(r"""\bget\(\s*["']([A-Z][A-Z0-9_]*)["']""")


def root_modules() -> list[Path]:
    """ทุกไฟล์ .py ที่ราก (ไม่รวม tests/) — เพิ่มโมดูลใหม่แล้วถูกสแกนอัตโนมัติ."""
    mods = sorted(ROOT.glob("*.py"))
    assert len(mods) >= 15, mods
    return mods


# บรรทัดใน .env.example: "NAME=value" หรือ "# NAME=value" (ค่าเริ่มต้นที่ comment ไว้)
ENV_LINE = re.compile(r"^(#\s?)?([A-Z][A-Z0-9_]*)=(.*)$")
SECRET_NAME = re.compile(r"SECRET|KEY|PASS|TOKEN")
PLACEHOLDER = re.compile(r"^<[^<>]+>$")
LONG_TOKEN = re.compile(r"^['\"]?[A-Za-z0-9+/_=-]{24,}['\"]?$")
SECRET_IN_URL = re.compile(r"(?i)(key|token|secret|pass(word)?)=|//[^/\s]*:[^/\s]*@")


def read(rel: str) -> str:
    path = ROOT / rel
    if not path.exists() and rel.startswith(("docs/superpowers/", "deploy/deploy_steps")):
        pytest.skip(f"{rel} ไม่อยู่ในชุดที่เผยแพร่ (เอกสารภายในของ รพ.)")
    return path.read_text(encoding="utf-8")


def env_names_in(text: str) -> set[str]:
    names = set(PREFIXED_LITERAL.findall(text))
    for m in STRICT_READ.finditer(text):
        names.add(m.group(1) or m.group(2))
    if FROM_OS_IMPORT.search(text):
        for m in BARE_READ.finditer(text):
            names.add(m.group(1) or m.group(2))
    if ENV_ALIAS.search(text):
        names.update(LOOSE_GET.findall(text))
    return names


def code_env_names() -> set[str]:
    names: set[str] = set()
    for path in root_modules():
        names |= env_names_in(path.read_text(encoding="utf-8"))
    return names


def env_example_lines() -> list[tuple[bool, str, str]]:
    """[(ถูก comment, ชื่อ, ค่า)] ของบรรทัด NAME=value ใน .env.example."""
    out = []
    for raw in read(".env.example").splitlines():
        m = ENV_LINE.match(raw.strip())
        if m:
            out.append((bool(m.group(1)), m.group(2), m.group(3).strip()))
    return out


# ---------- 1) ทุกตัวแปร env ที่โค้ดอ่านอยู่ใน .env.example ----------

def test_collector_finds_known_names():
    """กัน regex เก็บไม่ได้เงียบ ๆ: ชื่อหลักต้องถูกเก็บได้."""
    names = code_env_names()
    core = {"SECRET_KEY", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS",
            "MOPH_BASE_URL", "MOPH_CLIENT_KEY", "MOPH_SECRET_KEY", "DENGUE_ICDS",
            "DENGUE_POLL_SECONDS", "DENGUE_START_FROM", "DENGUE_REALERT_DAYS",
            "DENGUE_AREA_AMPHOE", "DENGUE_TILE_URL", "DENGUE_DATA_AUDITED",
            "DENGUE_DB_PATH", "DENGUE_LAN_PREFIXES", "DENGUE_SCHEDULER",
            "DENGUE_BASE_URL", "DENGUE_ENV_FILE"}
    assert core <= names, sorted(core - names)
    assert not {"SCRIPT_NAME", "HTTP_X_FORWARDED_PREFIX"} & names  # ตัวแปร WSGI ไม่ใช่ env


def test_collector_patterns_on_synthetic_sources():
    """ตรวจ regex กับตัวอย่างสังเคราะห์: ทุกรูปแบบการอ่านที่คาดไว้ต้องถูกเก็บ และ WSGI/config.get ไม่ถูกเก็บ."""
    src = (
        'import os\n'
        'a = os.environ.get("AAA_ONE")\n'
        'b = os.getenv("BBB_TWO", "x")\n'
        'c = os.environ["CCC_THREE"]\n'
    )
    assert env_names_in(src) == {"AAA_ONE", "BBB_TWO", "CCC_THREE"}
    src2 = 'from os import environ, getenv\nx = environ.get("DDD_FOUR")\ny = getenv("EEE_FIVE")\nz = environ["FFF_SIX"]\n'
    assert env_names_in(src2) == {"DDD_FOUR", "EEE_FIVE", "FFF_SIX"}
    src3 = 'import os\ne = os.environ if env is None else env\nv = e.get("GGG_SEVEN")\nw = get("HHH_EIGHT")\n'
    assert env_names_in(src3) == {"GGG_SEVEN", "HHH_EIGHT"}
    src4 = 'def f(environ):\n    return environ.get("HTTP_X_FORWARDED_PREFIX"), environ["SCRIPT_NAME"]\napp.config.get("SOURCE")\n'
    assert env_names_in(src4) == set()
    assert env_names_in('x = "DENGUE_FOO"') == {"DENGUE_FOO"}


def test_env_example_documents_every_env_the_code_reads():
    documented = {name for _, name, _ in env_example_lines()}
    names = code_env_names()
    assert len(names) >= 20, sorted(names)
    for name in sorted(names):                      # วนครบทุกชื่อที่เก็บได้ ไม่เลือกเฉพาะบางตัว
        if name in UNDOCUMENTED_OK:
            continue
        assert name in documented, f".env.example ขาดตัวแปรที่โค้ดอ่าน: {name}"


def test_every_env_example_name_is_read_by_the_code():
    """ชื่อใน .env.example ที่โค้ดไม่อ่านเลย = พิมพ์ผิดหรือค้างจากของเก่า."""
    names = code_env_names()
    for _, name, _ in env_example_lines():
        assert name in names or name in UNREAD_OK, f".env.example มีตัวแปรที่โค้ดไม่ใช้: {name}"


def test_env_example_realert_default_is_21():
    values = {name: value for _, name, value in env_example_lines()}
    assert values["DENGUE_REALERT_DAYS"] == "21"


# ---------- 2) .env.example ไม่มีค่าลับจริง ----------

def test_env_example_has_no_real_secrets():
    lines = env_example_lines()
    assert lines, "อ่านบรรทัดตัวแปรจาก .env.example ไม่ได้"
    for _, name, value in lines:
        if SECRET_NAME.search(name):
            assert value == "" or PLACEHOLDER.match(value), \
                f"{name} ต้องว่างหรือเป็น <...> (พบค่าที่ไม่ใช่ตัวแทน)"
        assert not LONG_TOKEN.match(value), f"{name}: ค่าดูเหมือนรหัส/กุญแจจริง"
        assert not SECRET_IN_URL.search(value), f"{name}: URL มีกุญแจหรือรหัสผ่านฝังอยู่"


def test_env_example_secret_placeholders_are_active_lines():
    """ค่าลับต้องมีบรรทัดตัวอย่างที่ไม่ถูก comment ไว้ให้ผู้ติดตั้งเห็นว่าต้องกรอก."""
    active = {name for commented, name, _ in env_example_lines() if not commented}
    for name in ("SECRET_KEY", "DB_PASS", "MOPH_CLIENT_KEY", "MOPH_SECRET_KEY"):
        assert name in active, f"{name} ควรเป็นบรรทัดที่ไม่ถูก comment"


# ---------- 3) INSTALL.md มีข้อกำหนดบังคับ ----------

def test_install_md_mandatory_callouts():
    text = read("INSTALL.md")
    for needle in ("--workers 1", "--timeout 120", "DENGUE_START_FROM", "setup_check.py",
                   "DENGUE_DATA_AUDITED", "X-Requested-With", "--preload", "--dry-run",
                   "add-user", "HC", "python3.11", "SELECT", "403",
                   "127.0.0.1:6800", "0.0.0.0:6800", "setsebool -P httpd_can_network_connect 1",
                   "grep -nE '^[A-Z_]+=<' .env", "--reference", "--exclude data --exclude .env",
                   "ค.ศ.", "ReadWritePaths", "502/503", "single quote", "DENGUE_ENV_FILE"):
        assert needle in text, f"INSTALL.md ต้องกล่าวถึง {needle!r}"
    assert "ก่อนนำไปใช้" in text
    assert text.index("## 0.") < text.index("## 1."), "ข้อ 0 ต้องมาก่อนขั้นตอนติดตั้ง"


def test_install_md_documents_every_env_in_example():
    text = read("INSTALL.md")
    for name in ("SECRET_KEY", "DENGUE_REALERT_DAYS", "DENGUE_LAN_PREFIXES", "DENGUE_SCHEDULER",
                 "DENGUE_TILE_URL", "DENGUE_DB_PATH", "DENGUE_AREA_AMPHOE", "MOPH_CLIENT_KEY"):
        assert name in text, f"INSTALL.md ควรอธิบาย {name}"


def test_readme_states_single_worker_rule():
    text = read("README.md")
    assert "## Deployment" in text
    assert "--workers 1" in text and "--preload" in text
    assert "INSTALL.md" in text


def test_license_is_mit_with_2026_holder():
    text = read("LICENSE")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026" in text and "Wang Nam Khiao Hospital" in text


def test_changelog_has_v1_entry():
    assert "## 1.0.0 (8 พ.ย. 2569)" in read("CHANGELOG.md")


def test_changelog_has_v1_0_1_entry_for_final_fix_wave():
    text = read("CHANGELOG.md")
    assert "## 1.0.1 (9 พ.ย. 2569)" in text
    entry = text.split("## 1.0.1")[1].split("\n## ")[0]
    for needle in ("absent_cycles", "DENGUE_CANCEL_CHECK_DAYS", "คืนสถานะ", "gthread",
                   "scheduler_expected", "set-password", "remove-user", "backup"):
        assert needle in entry, needle
    assert text.index("## 1.0.1") < text.index("## 1.0.0")


def test_docs_describe_cancel_debounce_guard_and_reactivation():
    for rel in ("INSTALL.md", "README.md"):
        text = read(rel)
        assert "ติดกัน 3 รอบ" in text, rel
        assert "คืนสถานะ" in text, rel
        assert "DENGUE_CANCEL_CHECK_DAYS" in text, rel
    install = read("INSTALL.md")
    assert "ข้ามการยกเลิกจำนวนมากผิดปกติ" in install
    spec = read("docs/superpowers/specs/2026-11-08-dengue-srrt-design.md")
    assert "ติดกัน 3 รอบ" in spec and "คืนสถานะ" in spec


def test_detector_message_matches_docs():
    from detector import CANCEL_AFTER_ABSENT_CYCLES, MASS_CANCEL_NOTICE
    assert CANCEL_AFTER_ABSENT_CYCLES == 3
    assert MASS_CANCEL_NOTICE in read("INSTALL.md")


def test_healthz_docs_match_fields_and_watchdog():
    install, readme, steps = read("INSTALL.md"), read("README.md"), read("deploy/deploy_steps.md")
    for name, text in (("INSTALL.md", install), ("README.md", readme), ("deploy_steps.md", steps)):
        assert "cases_active" not in text, name        # ไม่เปิดจำนวนเคสบน /healthz สาธารณะ
    fields = install.split("**ฟิลด์ใน `/healthz`:**")[1].split("**อาการ")[0]
    assert "scheduler_expected" in fields and "3 × `DENGUE_POLL_SECONDS`" in fields
    assert "watchdog" in install and "curl -s -m 10 http://127.0.0.1:6800/healthz" in install
    assert '"ok":true' in install


def test_docs_describe_account_commands_and_backup():
    install, readme, steps = read("INSTALL.md"), read("README.md"), read("deploy/deploy_steps.md")
    for needle in ("set-password", "remove-user", "อย่างน้อย 10 ตัวอักษร"):
        assert needle in install, needle
    assert "set-password" in readme and "remove-user" in readme
    for name, text in (("INSTALL.md", install), ("README.md", readme), ("deploy_steps.md", steps)):
        assert "run.py backup" in text, name
    for needle in ("/etc/cron.d/wnk-dengue-backup", "runuser -u dengue", "--keep 14",
                   "--exclude data", "ถูกส่งซ้ำ", "sqlite3 backup API"):
        assert needle in install, needle
    assert "backup" in steps and "cron" in steps
    run_py = read("run.py")
    for cmd in ("set-password", "remove-user", "backup"):
        assert cmd in run_py


def test_install_documents_secure_cookie_scope_and_session_recheck():
    text = read("INSTALL.md")
    assert "Path=/dengue" in text and "`Secure`" in text
    assert "ตรวจกับฐานทุกคำขอ" in text
    assert "cluster_check" in text


# ---------- 4) ไฟล์ systemd ----------

def test_service_file_single_worker_without_preload():
    text = read("deploy/wnk-dengue.service")
    exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
    assert re.search(r"--workers 1(?!\d)", exec_line), exec_line
    assert re.search(r"--timeout 120(?!\d)", exec_line), exec_line
    # R24/I1: worker เดียวแต่หลายเธรด — ตัวตั้งเวลาช้า (ส่ง LINE/ถาม HOSxP) ต้องไม่ทำให้เว็บค้างทั้งหมด
    assert re.search(r"--worker-class gthread(?!\w)", exec_line), exec_line
    assert re.search(r"--threads 4(?!\d)", exec_line), exec_line
    assert "--preload" not in text
    assert re.search(r"--bind 0\.0\.0\.0:6800(?!\d)", exec_line) and exec_line.endswith("wsgi:app")
    for line in ("User=dengue", "Group=dengue", "WorkingDirectory=/opt/dengue_srrt",
                 "EnvironmentFile=/opt/dengue_srrt/.env", "Restart=on-failure",
                 "NoNewPrivileges=true", "PrivateTmp=true",
                 "ReadWritePaths=/opt/dengue_srrt/data"):
        assert line in text, line


def test_apache_snippet_matches_service_port_and_order_rule():
    text = read("deploy/ssl-dengue.conf.snippet")
    assert "ProxyPass /dengue/ http://127.0.0.1:6800/" in text
    assert "ProxyPassReverse /dengue/ http://127.0.0.1:6800/" in text
    assert 'RequestHeader set X-Forwarded-Prefix "/dengue"' in text
    assert 'RequestHeader set X-Forwarded-Proto "https"' in text
    assert "httpd -t" in text and "ก่อน" in text and "ProxyPass / http://127.0.0.1:3000/" in text


def test_deploy_steps_marks_server_steps_for_confirmation():
    text = read("deploy/deploy_steps.md")
    assert text.count("ต้องให้ผู้ใช้ยืนยัน") >= 8
    assert "--workers 1" in text and "DENGUE_START_FROM" in text and "403" in text


# ---------- 5) แบบฟอร์มแจ้งปัญหา ----------

def _issue_items() -> dict[str, str]:
    """แยกแต่ละรายการ `- type:` ของ bug_report.yml ตาม id (ไม่พึ่งไลบรารี yaml)."""
    text = read(".github/ISSUE_TEMPLATE/bug_report.yml")
    items = re.split(r"(?m)^  - type:", text)[1:]
    out = {}
    for chunk in items:
        m = re.search(r"(?m)^    id: (\w+)", chunk)
        if m:
            out[m.group(1)] = chunk
    return out


def test_bug_report_requires_setup_check_output():
    items = _issue_items()
    chunk = items["setup_check"]
    assert chunk.strip().startswith("textarea")
    assert "required: true" in chunk
    assert "setup_check.py" in chunk


def test_bug_report_requires_no_patient_data_confirmation():
    chunk = _issue_items()["privacy"]
    assert chunk.strip().startswith("checkboxes")
    assert "ไม่มีข้อมูลผู้ป่วย (ชื่อ HN เบอร์โทร ที่อยู่)" in chunk
    assert "required: true" in chunk


def test_bug_report_requires_version_steps_expected_actual():
    items = _issue_items()
    for key in ("version", "steps", "expected", "actual"):
        assert "required: true" in items[key], key


def test_bug_report_is_valid_yaml():
    import yaml   # อยู่ใน requirements-dev.txt — ถ้าไม่มีให้เทสต์ล้ม (ไม่ข้ามเงียบ ๆ)
    data = yaml.safe_load(read(".github/ISSUE_TEMPLATE/bug_report.yml"))
    assert data["name"] and isinstance(data["body"], list)
    by_id = {b["id"]: b for b in data["body"] if "id" in b}
    assert by_id["setup_check"]["validations"]["required"] is True
    assert by_id["privacy"]["attributes"]["options"][0]["required"] is True


# ---------- แพ็กเกจ ----------

def test_requirements_pinned_and_dev_split():
    runtime = [l for l in read("requirements.txt").splitlines() if l.strip()]
    for line in runtime:
        assert "==" in line, f"requirements.txt ควรล็อกเวอร์ชัน: {line}"
    names = {l.split("==")[0].lower() for l in runtime}
    assert {"flask", "gunicorn", "pymysql", "requests"} <= names
    assert "pytest" not in names
    dev = read("requirements-dev.txt")
    assert "-r requirements.txt" in dev and "pytest" in dev and "PyYAML" in dev


def test_install_lists_setup_check_in_package_contents():
    assert "setup_check.py" in read("INSTALL.md").split("## 0.")[0]
    assert (ROOT / "setup_check.py").is_file()


def test_install_md_has_no_shell_sourcing_of_env_or_dengue_run():
    for rel in ("INSTALL.md", "README.md", "deploy/deploy_steps.md"):
        text = read(rel)
        assert "dengue_run" not in text, rel
        assert "set -a" not in text and ". ./.env" not in text, rel


def test_install_md_guards_env_creation_and_reload_chain():
    text = read("INSTALL.md")
    assert '[ -e .env ] && echo ".env มีอยู่แล้ว ห้ามเขียนทับ" || sudo install -m 640 -o root -g dengue .env.example .env' in text
    steps = read("deploy/deploy_steps.md")
    assert '[ -e .env ] && echo ".env มีอยู่แล้ว ห้ามเขียนทับ" ||' in steps
    for rel in ("INSTALL.md", "deploy/deploy_steps.md", "deploy/ssl-dengue.conf.snippet"):
        text = read(rel)
        assert "httpd -t && " in text and "reload httpd" in text, rel
        # ห้ามมีบรรทัดคำสั่ง reload ที่ไม่ผูกกับ httpd -t
        for line in text.splitlines():
            if line.strip().startswith(("sudo systemctl reload httpd", "systemctl reload httpd")):
                raise AssertionError(f"{rel}: reload ต้องผูกกับ httpd -t ด้วย &&: {line}")


def test_deploy_steps_header_and_cleanups():
    text = read("deploy/deploy_steps.md")
    assert "Task 12" not in text
    assert "เฉพาะเซิร์ฟเวอร์ของ รพ.วังน้ำเขียว" in text and "INSTALL.md" in text.split("## 0.")[0]
    assert not [l for l in text.splitlines() if l.startswith("+ ")]
    assert "## 11. ตรวจหลัง deploy — ต้องให้ผู้ใช้ยืนยัน" in text
    assert "rsync --delete" in text   # ข้อถอยกลับต้องเตือนเรื่อง rsync --delete
    assert "6800" in text and "127.0.0.1:6800" in text


def test_install_md_rollback_warns_about_database_revert():
    text = read("INSTALL.md")
    assert "rsync --delete" in text and "alert_log" in text and "ถูกส่งซ้ำ" in text
    assert "ลบบล็อก `/dengue/` ออกจาก `ssl.conf` ด้วยมือ" in text
    update = text[text.index("**อัปเดต:**"):text.index("**ถอยกลับ:**")]
    assert update.index("systemctl stop wnk-dengue") < update.index("chown -R root:root")
    assert "--reference" in update and "setup_check.py" in update


def test_env_example_notes_db_path_requires_unit_change_and_quoting():
    text = read(".env.example")
    assert "ReadWritePaths" in text
    assert "single quote" in text and "grep -nE '^[A-Z_]+=<' .env" in text


def test_install_quotes_real_secret_key_messages_and_alert_banner_docs():
    from config import secret_key_problem
    text = read("INSTALL.md")
    assert "ต้องตั้ง SECRET_KEY" not in text
    for problem in (secret_key_problem(""), secret_key_problem("<x>"),
                    secret_key_problem("short")):
        assert problem.split(" (")[0] in text, problem
    for needle in ("alerts_failing", "alerts_given_up", "หยุดส่งแล้ว", "alert_log",
                   "วันเริ่มแจ้งเตือน (DENGUE_START_FROM)"):
        assert needle in text, needle


def test_docs_describe_rearm_alerts_and_healthz_alert_fields():
    install, readme, steps = read("INSTALL.md"), read("README.md"), read("deploy/deploy_steps.md")
    for name, text in (("INSTALL.md", install), ("README.md", readme),
                       ("deploy_steps.md", steps)):
        assert "rearm-alerts" in text, name
        assert "alerts_given_up" in text, name
    assert "แถบหายไปเองเมื่อส่งสำเร็จ" in install and "ไม่หายเอง" in readme
    assert "ยังไม่มีการปิดรายการทีละรายการ" in install
    assert "alerts_failing" in install.split("**ฟิลด์ใน `/healthz`:**")[1].split("**อาการ")[0]
    run_py = read("run.py")
    assert "rearm-alerts" in run_py


# ---------- หน้าตั้งค่า /settings (v1.1.0) ----------

def _between(text: str, start: str, end: str | None = None) -> str:
    """ข้อความตั้งแต่หัวข้อ `start` ถึงก่อน `end` (หรือถึงท้ายไฟล์) — หัวข้อหายต้องล้ม ไม่ใช่คืนค่าว่าง"""
    assert start in text, f"ไม่พบหัวข้อ {start!r}"
    tail = text[text.index(start):]
    if end is None:
        return tail
    assert end in tail, f"ไม่พบหัวข้อ {end!r} หลัง {start!r}"
    return tail[:tail.index(end, len(start))]


def _install_settings_section() -> str:
    return _between(read("INSTALL.md"), "## 11. หน้าตั้งค่า (`/settings`)")


def test_no_doc_still_claims_two_roles():
    pattern = re.compile(r"(?i)(สอง|2)\s*บทบาท|two roles|2 roles")
    for rel in ("README.md", "INSTALL.md", "CHANGELOG.md", "deploy/deploy_steps.md", ".env.example",
                "docs/superpowers/specs/2026-11-08-dengue-srrt-design.md",
                "docs/superpowers/specs/2026-11-09-settings-page-design.md",
                ".github/ISSUE_TEMPLATE/bug_report.yml"):
        text = read(rel)
        m = pattern.search(text)
        assert not m, f"{rel} ยังบอกว่ามี 2 บทบาท: {m.group(0)!r}"
        # "บทบาท `srrt` และ `viewer`" ที่ไม่พูดถึง admin ในบรรทัดเดียวกัน = ยังนับแค่สองบทบาท
        for line in text.splitlines():
            if re.search(r"บทบาท\s*`srrt`\s*(และ|/|กับ)\s*`viewer`", line):
                assert "admin" in line, f"{rel}: {line[:80]!r}"


def test_docs_describe_three_roles_and_rank():
    from auth import ROLE_RANK
    assert set(ROLE_RANK) == {"viewer", "srrt", "admin"}     # ถ้าเพิ่มบทบาทใหม่ ต้องมาแก้เอกสารด้วย
    for rel in ("README.md", "INSTALL.md", "deploy/deploy_steps.md"):
        text = read(rel)
        for role in ("viewer", "srrt", "admin"):
            assert f"`{role}`" in text, f"{rel} ไม่กล่าวถึงบทบาท {role}"
    install = read("INSTALL.md")
    assert "viewer < srrt < admin" in install
    assert "ผู้ดู" in install and "ปักหมุด" in install and "ผู้ดูแล" in install
    assert "remove-user" in _install_settings_section()      # วิธีเลื่อนผู้ใช้เดิมเป็น admin
    assert "`admin`" in read("README.md").split("## เริ่มอย่างเร็ว")[0]


def test_install_new_flow_creates_admin_then_opens_settings_on_lan_address():
    install = read("INSTALL.md")
    assert "run.py add-user <ชื่อผู้ใช้> admin" in install
    flow = _between(install, "### 4.9 ").split("\n## ")[0]
    for needle in ("http://<ที่อยู่ LAN ของเซิร์ฟเวอร์>:6800/settings", "ไม่ใช่โดเมนสาธารณะ",
                   "บัญชี SELECT-only", "กลุ่มทดสอบ", "DENGUE_START_FROM", "ค.ศ.", "DENGUE_BASE_URL",
                   "ปุ่มทดสอบ", "เริ่มระบบใหม่เอง", "python setup_check.py", "/check",
                   "รหัสผ่านของตัวเอง", "อย่างน้อย 10 ตัวอักษร"):
        assert needle in flow, f"INSTALL.md ข้อ 4.9 ต้องกล่าวถึง {needle!r}"
    # ลำดับ: สร้าง admin ก่อน แล้วเปิดหน้า แล้วค่อยตรวจด้วย setup_check
    url = "http://<ที่อยู่ LAN ของเซิร์ฟเวอร์>:6800/settings"
    assert flow.index("add-user <ชื่อผู้ใช้> admin") < flow.index(url) < flow.index("python setup_check.py")
    assert "ไม่มีหน้าตั้งค่าบนเว็บ" not in install              # ข้อความเก่าของ v1.0 ต้องหายไป
    assert "set_env.py" in install                                # ทางเลือกเมื่อเปิดหน้าเว็บไม่ได้


def test_install_settings_section_explains_variables_precedence_and_secrets():
    import settings_store
    sec = _install_settings_section()
    assert len(settings_store.ALLOWED) == 11
    for key in settings_store.ALLOWED:
        assert f"`{key}`" in sec, f"INSTALL.md ข้อ 11 ต้องระบุ {key}"
    for needle in ("ซ้อนทับ", "ค่าจากหน้าตั้งค่าชนะ", "`SECRET_KEY`", "`.env` เท่านั้น",
                   "เว้นว่าง = คงค่าเดิม", "ล้างค่า", "ไม่แสดงค่าที่ตั้งไว้กลับ",
                   "ไม่ถูกเขียนลง `.env`", "แอปใช้ค่าเริ่มต้นของตัวเอง"):
        assert needle in sec, needle
    order = [sec.index(n) for n in ("`.env`", "ค่าจากหน้าตั้งค่าชนะ", "แอปใช้ค่าเริ่มต้นของตัวเอง")]
    assert order == sorted(order), "ต้องเรียง .env -> settings.json ชนะ -> ค่าเริ่มต้น"


def test_install_settings_section_documents_tests_limits_and_r8_rule():
    import settings_views
    assert settings_views.MAX_BODY_BYTES == 64 * 1024
    assert settings_views.SlidingWindowLimiter.LIMITS == {"test": 10, "send": 3}
    sec = _install_settings_section()
    for needle in ("ทดสอบ HOSxP", "อ่านอย่างเดียว", "ทดสอบ MOPH", "เครือข่ายและรูปแบบของกุญแจ",
                   "ส่งข้อความทดสอบ", "ข้อความเดียวที่กำหนดตายตัว", "ต้องกด**ยืนยัน**ก่อนส่ง",
                   "รหัสผ่านฐานข้อมูลว่างตั้งผ่านหน้านี้ไม่ได้", "ใช้โควตาเดียวกัน **10 ครั้งต่อนาที**",
                   "64 KB", "10 ครั้งต่อนาที", "3 ครั้งต่อนาที", "ผิด 5 ครั้งใน 10 นาที",
                   "ย้อนกลับค่าก่อนหน้า", "ย้อนได้**หนึ่งชุด**"):
        assert needle in sec, needle
    # กฎรหัสผ่านใหม่เมื่อเปลี่ยนปลายทางฐานข้อมูล: เปลี่ยนโฮสต์/พอร์ต/ผู้ใช้ต้องพิมพ์รหัสผ่านฐานข้อมูลใหม่ แม้ติ๊ก "บันทึกแม้ทดสอบไม่ผ่าน"
    r8 = _between(sec, "#### กฎรหัสผ่านใหม่เมื่อเปลี่ยนปลายทางฐานข้อมูล").split("\n### ")[0]
    for needle in ("`DB_HOST`", "`DB_PORT`", "`DB_USER`", "ต้องพิมพ์รหัสผ่านฐานข้อมูลใหม่",
                   "บันทึกแม้ทดสอบไม่ผ่าน", "รหัสผ่านที่เก็บไว้ต้องไม่ถูกส่งไปยังที่อยู่ใหม่"):
        assert needle in r8, needle
    # audit: ชื่อการกระทำต้องตรงกับที่โค้ดเขียนจริง และบอกว่าไม่เก็บค่า
    code_actions = set(re.findall(r'_audit\(app, "(\w+)"', read("settings_views.py")))
    assert code_actions == {"settings_change", "settings_revert", "settings_send_test"}, code_actions
    for action in code_actions:
        assert f"`{action}`" in sec, action
    assert "ชื่อตัวแปร" in sec and "ไม่เก็บค่า" in sec and "audit_log" in sec


def test_install_settings_section_is_honest_about_plaintext_secrets_and_backup():
    install = read("INSTALL.md")
    sec3 = _between(_install_settings_section(), "### 11.3 ").split("\n### 11.4")[0]
    for needle in ("ข้อความธรรมดา", "สิทธิ์ 600", "เจ้าของ `dengue`", "settings.json.prev",
                   "ผู้โจมตี", "ไม่สำรอง `settings.json`", "ต้องกรอกความลับใหม่",
                   "--exclude data", "เฉพาะ LAN", "ไม่เปิดพอร์ต 6800", "ผู้ดูแล (`admin`) ให้น้อยที่สุด",
                   "รหัสผ่านยาวและเดายาก", "ห้ามแนบ `settings.json`"):
        assert needle in sec3, f"INSTALL.md ข้อ 11.3 ต้องกล่าวถึง {needle!r}"
    # ข้อความเก่าที่ว่าความลับอยู่ใน .env เท่านั้นต้องถูกแก้ ไม่ขัดกับหน้าตั้งค่า
    assert "กุญแจ MOPH รหัสผ่านฐานข้อมูล และ `SECRET_KEY` อยู่เฉพาะใน `.env`" not in install
    assert "data/settings.json" in _between(install, "## 7. ", "## 8. ")
    # ข้อ 10 (backup) และ ข้อ 9 (อัปเดต/ถอยกลับ) ต้องบอกผลต่อ settings.json
    backup = _between(install, "## 10. ", "## 11. ")
    assert "settings.json" in backup and "ไม่รวม" in backup
    rollback = _between(install, "## 9. ", "## 10. ")
    assert "settings.json" in rollback and "--exclude data" in rollback


def test_run_py_backup_really_excludes_settings_json():
    """เอกสารบอกว่า backup ไม่รวม settings.json — ยืนยันว่าโค้ดสำรองไม่แตะไฟล์นี้"""
    src = read("run.py")
    start = src.index("def _backup")
    body = src[start:src.index("\ndef ", start + 1)]
    assert "settings.json" not in body and "settings_store" not in body
    assert "backup_database" in body


def test_install_recovery_steps():
    rec = _between(_install_settings_section(), "### 11.4 ").split("\n### 11.5")[0]
    for needle in ("เข้า `/settings` ได้", "ผู้ใช้เว็บอยู่ใน SQLite", "แม้ต่อ HOSxP ไม่ได้",
                   "sudo systemctl stop wnk-dengue", "sudo mv /opt/dengue_srrt/data/settings.json /root/",
                   "sudo systemctl start wnk-dengue", "กลับไปใช้ค่าจาก `.env`",
                   "deploy/set_env.py", "settings_error", "boot_id", "ไม่ใช่ความลับ",
                   "ข้อความเตือนคงที่", "แถบเตือน"):
        assert needle in rec, f"INSTALL.md ข้อ 11.4 ต้องกล่าวถึง {needle!r}"
    assert rec.index("systemctl stop") < rec.index("mv /opt/dengue_srrt/data/settings.json /root/") < rec.index("systemctl start")
    # .prev ต้องถูกย้ายออกด้วย (พร้อมเหตุผล) และมีขั้นคืนค่า + คืนเจ้าของ/สิทธิ์
    assert "sudo mv /opt/dengue_srrt/data/settings.json.prev /root/settings.json.prev.bak-" in rec
    assert "ปุ่ม \"ย้อนกลับค่าก่อนหน้า\" จะดึง `.prev` กลับมา" in rec
    assert "sudo mv /root/settings.json.bak-<เวลา> /opt/dengue_srrt/data/settings.json" in rec
    assert "sudo chown dengue:dengue /opt/dengue_srrt/data/settings.json" in rec
    assert "sudo chmod 600 /opt/dengue_srrt/data/settings.json" in rec
    assert rec.index("sudo mv /root/settings.json.bak-<เวลา>") < rec.index("sudo chown dengue:dengue")
    # ผลต่อการแจ้งเตือนเมื่อย้ายไฟล์ออก และกฎการแก้ไฟล์เอง
    for needle in ("ผลต่อการแจ้งเตือน", "ไม่มีค่าฐานข้อมูลและกุญแจ MOPH", "ตัวตรวจจับและการส่ง LINE **หยุด**",
                   "ยังไม่ได้ตั้งค่า DB_HOST / DB_USER", "**แก้ไฟล์เอง:** `sudo systemctl stop wnk-dengue` **ก่อน**",
                   "ไฟล์ที่ root สร้างหรือย้ายมาแล้วผู้ใช้ `dengue` อ่านไม่ได้ จะกลายเป็น `settings_error`"):
        assert needle in rec, needle
    from hosxp import MSG_DB_NOT_CONFIGURED
    assert MSG_DB_NOT_CONFIGURED == "ยังไม่ได้ตั้งค่า DB_HOST / DB_USER"


def test_install_healthz_field_table_lists_every_real_field(tmp_path):
    """ฟิลด์ที่ /healthz ตอบจริงทุกตัวต้องมีแถวในตารางของ INSTALL.md และกฎ ok ต้องครบ"""
    from app import create_app
    from config import load_settings
    from store import Store
    cfg = load_settings({"SECRET_KEY": "test-secret-key-0123456789", "DENGUE_DB_PATH": str(tmp_path / "d.db")})
    app = create_app(settings=cfg, store=Store(str(tmp_path / "d.db")))
    real = set(app.test_client().get("/healthz").get_json())
    assert real == {"ok", "last_cycle_at", "last_error", "alerts_failing", "alerts_given_up",
                    "scheduler_expected", "settings_error", "boot_id"}, real
    install = read("INSTALL.md")
    table = _between(install, "**ฟิลด์ใน `/healthz`:**", "**เฝ้าระวังจากภายนอก")
    for name in real:
        assert f"| `{name}` |" in table, f"ตารางฟิลด์ /healthz ขาด {name}"
    ok_row = next(l for l in table.splitlines() if l.startswith("| `ok` |"))
    for needle in ("settings_error", "last_error", "alerts_given_up", "3 × `DENGUE_POLL_SECONDS`"):
        assert needle in ok_row, needle
    # ที่อื่นที่เคยลิสต์ฟิลด์ (smoke test ข้อ 6, ข้อ 7) ต้องไม่ตกหล่น
    smoke = _between(install, "## 6. ", "## 7. ")
    sec7 = _between(install, "## 7. ", "## 8. ")
    for name in ("settings_error", "boot_id"):
        assert name in smoke, f"smoke test ข้อ 6 ต้องลิสต์ {name}"
        assert name in sec7, f"ข้อ 7 (/healthz เปิดสาธารณะ) ต้องลิสต์ {name}"
    assert "สุ่มต่อโปรเซส" in table and "ตรวจว่าเริ่มระบบใหม่" in table


def test_install_restart_note_and_gunicorn_condition():
    restart = _between(_install_settings_section(), "### 11.5 ")
    for needle in ("SIGTERM", "gunicorn", "เริ่ม worker ใหม่", "เฉพาะเมื่อรันภายใต้ gunicorn",
                   "ต้อง restart เอง", "boot_id", "ตรวจบนเซิร์ฟเวอร์จริงตอน deploy"):
        assert needle in restart, needle


def test_deploy_steps_step6_uses_settings_page_as_main_path():
    steps = read("deploy/deploy_steps.md")
    s6 = _between(steps, "## 6. ", "## 7. ")
    s7 = _between(steps, "## 7. ", "## 8. ")
    s8 = _between(steps, "## 8. ", "## 9. ")
    s9 = _between(steps, "## 9. ", "## 10. ")
    assert "ทางหลัก" in s6 and "ทางเลือก" in s6 and "deploy/set_env.py" in s6
    assert s6.index("ทางหลัก") < s6.index("ทางเลือก") < s6.index("deploy/set_env.py")
    assert "SECRET_KEY" in s6 and "/settings" in s6
    # เปิดหน้า /settings ได้ต้องมีบัญชี admin และ service ก่อน -> ขั้นตอนอยู่ในข้อ 7-9 ตามลำดับ
    assert "run.py add-user <ชื่อผู้ใช้> admin" in s7
    assert "systemctl enable --now wnk-dengue" in s8
    assert "http://<ที่อยู่ LAN ของเซิร์ฟเวอร์>:6800/settings" in s9
    assert s9.index("http://<ที่อยู่ LAN ของเซิร์ฟเวอร์>:6800/settings") < s9.index("setup_check.py")
    for needle in ("ไม่ใช่โดเมนสาธารณะ", "กลุ่มทดสอบ", "DENGUE_START_FROM"):
        assert needle in s9, needle


def test_deploy_steps_checklist_verifies_restart_on_real_server():
    steps = read("deploy/deploy_steps.md")
    s9 = _between(steps, "## 9. ", "## 10. ")
    for needle in ("boot_id", "เปลี่ยน", "last_cycle_at", "data/settings.json", "dengue", "-rw-------",
                   "SIGTERM", "ตรวจบนเซิร์ฟเวอร์จริง", "ส่งข้อความทดสอบ"):
        assert needle in s9, f"deploy_steps ข้อ 9 ต้องมีรายการตรวจ {needle!r}"
    s11 = _between(steps, "## 11. ", "## 12. ")
    assert "/dengue/settings" in s11 and "403" in s11      # โดเมนสาธารณะต้องเข้าหน้าตั้งค่าไม่ได้


def test_deploy_steps_documents_roles_and_secret_handling():
    steps = read("deploy/deploy_steps.md")
    s7 = _between(steps, "## 7. ", "## 8. ")
    assert "`admin`" in s7 and "`srrt`" in s7 and "`viewer`" in s7
    assert "settings.json" in steps.split("## 0.")[0]      # หัวไฟล์บอกว่าค่าลับอาจอยู่ใน settings.json ด้วย
    s13 = _between(steps, "## 13. ", "## ถอยกลับ")
    assert "settings.json" in s13 and "ไม่รวม" in s13


def test_changelog_has_v1_1_0_entry():
    text = read("CHANGELOG.md")
    assert "## 1.1.0 (9 พ.ย. 2569)" in text
    entry = _between(text, "## 1.1.0", "\n## 1.0.1")
    for needle in ("/settings", "`admin`", "ปุ่มทดสอบ", "settings_error", "boot_id", "รหัสผ่านฐานข้อมูลใหม่",
                   "`run.py backup` ไม่รวม", "settings.json", "viewer < srrt < admin", "ข้อความธรรมดา",
                   "set_env.py", "ไม่มีค่า env ใหม่"):
        assert needle in entry, f"CHANGELOG 1.1.0 ต้องกล่าวถึง {needle!r}"
    assert text.index("## 1.1.0") < text.index("## 1.0.1") < text.index("## 1.0.0")
    assert "login 2 บทบาท" not in text


def test_readme_lists_settings_feature_and_architecture_files():
    text = read("README.md")
    assert "เวอร์ชัน 1.5.1" in text   # README ระบุเวอร์ชันล่าสุดเสมอ (1.1.0 /settings, 1.2.0 /trend, 1.3.0 เส้นขอบ, 1.4.0 ตำบล+สถานพยาบาล, 1.5.0 หลายโรค)
    features = _between(text, "## ความสามารถ", "## เริ่มอย่างเร็ว")
    for needle in ("หน้าตั้งค่า", "/settings", "`admin`", "ข้อความธรรมดา", "settings_error", "boot_id"):
        assert needle in features, f"README ความสามารถต้องกล่าวถึง {needle!r}"
    arch = _between(text, "## สถาปัตยกรรม", "## ความเป็นส่วนตัว")
    for rel in ("settings_store.py", "settings_views.py", "settings_checks.py",
                "static/settings.js", "static/settings_logic.js", "templates/settings.html"):
        assert f"`{rel}`" in arch, f"README สถาปัตยกรรมขาด {rel}"
        assert (ROOT / rel).is_file(), rel
    quick = _between(text, "## เริ่มอย่างเร็ว", "## Deployment")
    assert "add-user <ชื่อผู้ใช้> admin" in quick


def test_first_spec_points_to_settings_spec():
    spec = read("docs/superpowers/specs/2026-11-08-dengue-srrt-design.md")
    assert "2026-11-09-settings-page-design.md" in spec
    assert (ROOT / "docs/superpowers/specs/2026-11-09-settings-page-design.md").is_file()


def test_env_example_header_explains_settings_overlay():
    import settings_store
    text = read(".env.example")
    header = text.split("# ส่วนที่ต้องตั้ง")[0]
    assert "/settings" in header and "ค่าที่ตั้งจากหน้าเว็บชนะ" in header
    for key in settings_store.ALLOWED:            # ตัวแปรทั้ง 11 ต้องถูกระบุชื่อในหมายเหตุหัวไฟล์
        assert key in header, f".env.example หัวไฟล์ไม่ระบุ {key}"
    assert "SECRET_KEY" in header and "เฉพาะใน .env" in header
    assert "ไม่ถูกเขียนลง .env" in header and "data/settings.json" in header
    # ตัวแปรที่ตั้งจากเว็บได้ทุกตัวยังมีบรรทัดตัวอย่างใน .env.example (ทางเลือก set_env.py / ติดตั้งแบบเดิม)
    names = {name for _, name, _ in env_example_lines()}
    assert set(settings_store.ALLOWED) <= names


def test_bug_report_forbids_attaching_settings_json_and_env():
    import yaml
    chunk = _issue_items()["privacy"]
    assert "settings.json" in chunk and ".env" in chunk
    assert "ไม่มีข้อมูลผู้ป่วย (ชื่อ HN เบอร์โทร ที่อยู่)" in chunk      # ข้อความเดิมที่เทสต์อื่นพึ่งพาต้องอยู่
    assert "required: true" in chunk
    data = yaml.safe_load(read(".github/ISSUE_TEMPLATE/bug_report.yml"))
    intro = next(b for b in data["body"] if b["type"] == "markdown")["attributes"]["value"]
    assert "settings.json" in intro
    assert "settings.json" in _between(read("INSTALL.md"), "**แจ้งปัญหา:**", "## 1. ")


# ---------- แก้รอบ 1: ตาราง/คำสั่ง curl, ข้อความ DENGUE_START_FROM, ทางเข้า /settings, ชื่อกฎ ----------

DOC_FILES = ("INSTALL.md", "README.md", "CHANGELOG.md", "deploy/deploy_steps.md", ".env.example")


def test_markdown_tables_have_no_broken_rows():
    """แถวตารางทุกแถวต้องจบด้วย | บนบรรทัดเดียว (เคยเกิด \\n กลายเป็นขึ้นบรรทัดจริงกลางแถว) และไม่มีบรรทัดเศษต่อท้ายแถว"""
    for rel in ("INSTALL.md", "README.md", "CHANGELOG.md", "deploy/deploy_steps.md"):
        lines = read(rel).splitlines()
        in_code = False
        for i, line in enumerate(lines):
            if line.lstrip().startswith("```"):
                in_code = not in_code
            if in_code or not line.startswith("|"):
                continue
            assert line.rstrip().endswith("|"), f"{rel}:{i + 1} แถวตารางไม่จบด้วย | (ถูกตัดบรรทัด?): {line[:70]!r}"
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            assert not (nxt.startswith("'") or nxt.startswith('"') or ("%{http_code}" in nxt and "|" not in nxt)), \
                f"{rel}:{i + 2} บรรทัดเศษต่อท้ายแถวตาราง: {nxt[:70]!r}"


def test_markdown_table_row_checker_catches_the_original_breakage():
    """ตัวตรวจแถวตารางต้องล้มกับรูปแบบที่เคยพัง (กันเทสต์ผ่านเพราะตรวจไม่เป็น)"""
    broken = "| ปิดจากสาธารณะ | `curl -w '%{http_code}\n' https://x/y` | **`403`** |"
    first = broken.splitlines()[0]
    assert not first.rstrip().endswith("|")
    assert broken.splitlines()[1].startswith("'")


def test_curl_http_code_commands_stay_on_one_line():
    """คำสั่ง curl -w '%{http_code}\\n' ต้องอยู่บรรทัดเดียว เครื่องหมายคำพูดของ -w ปิดครบ และ \\n เป็นอักขระ 2 ตัว"""
    bs = chr(92)
    for rel in ("INSTALL.md", "deploy/deploy_steps.md"):
        found = 0
        for i, line in enumerate(read(rel).splitlines()):
            if "http_code" not in line:
                continue
            found += 1
            m = re.search(r"""-w\s+('[^']*'|"[^"]*")""", line)
            assert m, f"{rel}:{i + 1} คำสั่ง curl -w ไม่ครบในบรรทัดเดียว: {line[:80]!r}"
            assert m.group(1)[-3:-1] == bs + "n", f"{rel}:{i + 1} ต้องลงท้าย -w ด้วย {bs}n (2 ตัวอักษร)"
        assert found >= 3, rel
    install_row = next(l for l in read("INSTALL.md").splitlines() if l.startswith("| `/settings` ปิดจากสาธารณะ |"))
    assert "/dengue/settings" in install_row and "**`403`**" in install_row
    assert any("/dengue/settings" in l and "http_code" in l for l in read("deploy/deploy_steps.md").splitlines())


def test_start_from_wording_matches_detector_behavior():
    """detector.run_cycle ค้นวินิจฉัยตั้งแต่ start_from ปัจจุบันทุกรอบ -> ย้อนวันกลับ = แจ้งเคสเก่า (ประโยคเก่า "ไม่ย้อนแจ้ง" ผิด)"""
    detector_src = read("detector.py")
    assert "since = datetime.combine(settings.start_from, time.min)" in detector_src
    assert "source.new_dx(since, rule.icds)" in detector_src   # ต่อโรค (v1.5.0) — ของ dengue = settings.icds
    old = "ไม่ย้อนแจ้งเคสเก่า"
    new = ("ย้อนวันเริ่มกลับ = เคสเก่าที่ยังไม่เป็นเคสจะถูกแจ้ง (ถ้ามีกุญแจ MOPH) · "
           "เลื่อนวันไปข้างหน้าไม่ลบเคสเดิม")
    for rel in (*DOC_FILES, "docs/superpowers/specs/2026-11-09-settings-page-design.md",
                "docs/superpowers/specs/2026-11-08-dengue-srrt-design.md"):
        assert old not in read(rel), f"{rel} ยังมีประโยคผิดเรื่อง DENGUE_START_FROM"
    assert new in read("INSTALL.md")
    assert new in read("docs/superpowers/specs/2026-11-09-settings-page-design.md")
    import settings_views
    assert "เคสที่สะสมไว้จะถูกส่งเข้ากลุ่มรวดเดียว" in settings_views.MSG_START_FROM_PAST


def test_install_49_tells_how_to_reach_settings_from_lan():
    flow = _between(read("INSTALL.md"), "### 4.9 ").split("\n## ")[0]
    step1 = _between(flow, "1. เปิดเบราว์เซอร์ในเครือข่าย", "2. กลุ่ม **ฐานข้อมูล HOSxP**")
    for needle in ("ssh -L 6800:127.0.0.1:6800 <เซิร์ฟเวอร์>", "http://127.0.0.1:6800/settings", "ข้อ 4.7",
                   "firewalld", "ถามผู้ดูแลเครือข่ายก่อนแก้", "DENGUE_LAN_PREFIXES", "ตั้งจากหน้าเว็บไม่ได้",
                   "ได้ 403 จาก LAN", "set-password"):
        assert needle in step1, needle
    # ข้อความอ้างอิงต้องมีอยู่จริงในข้อ 4.7 และตารางข้อ 5
    sec47 = _between(read("INSTALL.md"), "### 4.7 ", "### 4.8 ")
    assert "ssh -L 6800:127.0.0.1:6800 <เซิร์ฟเวอร์>" in sec47
    assert "| `DENGUE_LAN_PREFIXES` |" in read("INSTALL.md")


def test_install_user_promotion_and_set_env_instructions():
    install = read("INSTALL.md")
    s46 = _between(install, "### 4.6 ", "### 4.7 ")
    for needle in ("ต้องตั้งรหัสผ่านใหม่", "อย่าลบ `admin` คนสุดท้าย", "set-password"):
        assert needle in s46, needle
    # คำสั่ง set_env.py ต้องมีที่ INSTALL (ข้อ 4.4) พร้อมเงื่อนไขว่าต้องมี .env อยู่แล้ว
    s44 = _between(install, "### 4.4 ", "### 4.5 ")
    assert "sudo .venv/bin/python deploy/set_env.py" in s44 and "ต้องมีไฟล์ `.env` อยู่แล้ว" in s44
    assert "รหัสออก 2" in s44
    assert "ต้องมี `.env` อยู่แล้ว" in _between(read("deploy/deploy_steps.md"), "## 6. ", "## 7. ")


def test_docs_do_not_use_internal_r8_name_or_phantom_references():
    for rel in DOC_FILES:
        assert not re.search(r"\bR8\b", read(rel)), f"{rel} ใช้ชื่อภายใน R8"
    install = read("INSTALL.md")
    assert "`setup_check.py` ข้อ 7" not in install
    assert "กฎรหัสผ่านใหม่เมื่อเปลี่ยนปลายทางฐานข้อมูล" in install
    assert "กฎรหัสผ่านใหม่เมื่อเปลี่ยนปลายทางฐานข้อมูล" in read("CHANGELOG.md")


def test_docs_quote_real_last_error_text_when_db_not_configured():
    from hosxp import MSG_DB_NOT_CONFIGURED
    install, steps = read("INSTALL.md"), read("deploy/deploy_steps.md")
    s49 = _between(install, "### 4.9 ").split("\n## ")[0]
    assert MSG_DB_NOT_CONFIGURED in s49
    assert MSG_DB_NOT_CONFIGURED in _between(steps, "## 8. ", "## 9. ")
    assert "last_error` ต่อ HOSxP ไม่ได้" not in install and "last_error` ต่อ HOSxP ไม่ได้" not in steps


def test_env_example_header_is_consistent_with_minimal_env_flow():
    header = read(".env.example").split("# ส่วนที่ต้องตั้ง")[0]
    assert "แทนค่า <...> ทุกตัว" not in header
    assert "ใส่ SECRET_KEY (จำเป็นเสมอ)" in header and "ลบบรรทัด <...> เหล่านั้นทิ้ง" in header
    assert "พิมพ์ลง .env หรือที่หน้า /settings ก็ได้" in header


def test_deploy_step9_does_not_require_public_check_before_apache():
    steps = read("deploy/deploy_steps.md")
    s9 = _between(steps, "## 9. ", "## 10. ")
    assert "ต้องได้ 403" not in s9
    assert "ตรวจว่าโดเมนสาธารณะได้ 403 ในข้อ 11 หลังเปิด Apache ในข้อ 10" in s9
    assert "/dengue/settings" in _between(steps, "## 11. ", "## 12. ")


def test_restart_by_itself_claims_carry_gunicorn_caveat():
    install = read("INSTALL.md")
    step6 = next(l for l in install.splitlines() if l.startswith("6. หลังบันทึก **แอปเริ่มระบบใหม่เอง**"))
    assert "ใต้ gunicorn เท่านั้น" in step6 and "ข้อ 11.5" in step6
    entry = _between(read("CHANGELOG.md"), "## 1.1.0", "\n## 1.0.1")
    assert "เริ่มใหม่เองใต้ gunicorn (ตรวจจริงตามข้อ 11.5" in entry
    assert "เริ่มใหม่เองใต้ gunicorn (ตรวจจริงตาม INSTALL.md ข้อ 11.5)" in read("README.md")


def test_first_spec_notes_admin_role_and_settings_page_for_secrets():
    spec = read("docs/superpowers/specs/2026-11-08-dengue-srrt-design.md")
    assert "v1.1.0 เพิ่ม `admin`" in spec
    assert "ใน v1.1.0 ตั้งจากหน้า `/settings` ได้และเก็บใน `data/settings.json`" in spec


# ---------------- รอบแก้สุดท้าย (final fix wave) ----------------

TUNNEL_CMD = "ssh -L 6800:127.0.0.1:6800 <เซิร์ฟเวอร์>"


def test_docs_say_plain_http_leaks_secrets_and_recommend_ssh_tunnel():
    """I-1: เปิด /settings ผ่าน http://<LAN>:6800 = รหัสผ่านฐานข้อมูล/กุญแจ MOPH/รหัสผ่านผู้ดูแลวิ่งเป็นข้อความธรรมดา —
    เอกสารต้องบอกตรง ๆ และให้ SSH tunnel เป็นทางที่แนะนำ (ใช้คำสั่งเดียวกับข้อ 4.7)"""
    install, steps = read("INSTALL.md"), read("deploy/deploy_steps.md")
    sec47 = _between(install, "### 4.7 ", "### 4.8 ")
    assert TUNNEL_CMD in sec47
    s49 = _between(install, "### 4.9 ").split("\n## ")[0]
    step1 = _between(s49, "1. เปิดเบราว์เซอร์ในเครือข่าย", "2. กลุ่ม **ฐานข้อมูล HOSxP**")
    s113 = _between(_install_settings_section(), "### 11.3 ", "### 11.4 ")
    s9 = _between(steps, "## 9. ", "## 10. ")
    s6 = _between(steps, "## 6. ", "## 7. ")
    for name, text in (("INSTALL 4.9 ข้อ 1", step1), ("INSTALL 11.3", s113), ("deploy_steps 9", s9)):
        for needle in ("ไม่เข้ารหัส", "ข้อความธรรมดา", "แนะนำ", TUNNEL_CMD, "http://127.0.0.1:6800/settings"):
            assert needle in text, f"{name} ต้องมี {needle!r}"
        assert "สายที่เชื่อถือได้" in text or "สาย (wired)" in text, name   # LAN http ธรรมดาเฉพาะเครือข่ายสายที่เชื่อถือได้
    assert "ไม่เข้ารหัส" in s6 and "SSH tunnel" in s6     # ข้อ 6 ของ deploy_steps ชี้ไปข้อ 9 พร้อมคำเตือน
    # ลำดับ: ทางที่แนะนำ (tunnel) มาก่อนทาง LAN http ธรรมดา ในข้อ 4.9 ข้อ 1
    assert step1.index(TUNNEL_CMD) < step1.index("http://<ที่อยู่ LAN ของเซิร์ฟเวอร์>:6800/settings")
    # ข้อความบนแถบเตือนของหน้าอ้างข้อ 4.7 ที่มีคำสั่งนี้จริง
    import settings_views
    assert "ข้อ 4.7" in settings_views.MSG_INSECURE_TRANSPORT and "SSH tunnel" in settings_views.MSG_INSECURE_TRANSPORT


def test_docs_say_settings_page_accepts_go_live_even_when_equal_to_today():
    """I-2: เดิมค่าเริ่มต้น (วันนี้) ถูกถือเป็น "ค่าเดิม" จึงตั้ง go-live = วันนี้จากหน้าไม่ได้"""
    install, steps = read("INSTALL.md"), read("deploy/deploy_steps.md")
    s49 = _between(install, "### 4.9 ").split("\n## ")[0]
    step4 = _between(s49, "4. กลุ่ม **วัน go-live และลิงก์**", "5. ช่อง **รหัสผ่านของคุณ**")
    s9 = _between(steps, "## 9. ", "## 10. ")
    s111 = _between(_install_settings_section(), "### 11.1 ", "### 11.2 ")
    for name, text in (("INSTALL 4.9 ข้อ 4", step4), ("deploy_steps 9", s9), ("INSTALL 11.1", s111)):
        assert "แม้เท่ากับวันนี้" in text, name
    # ตาราง setup_check ในข้อ 8: ⚠ เมื่อไม่ได้ตั้ง DENGUE_START_FROM
    row = next(l for l in install.splitlines() if l.startswith("| วันเริ่มแจ้งเตือน (DENGUE_START_FROM) |"))
    assert "ยังไม่ได้ตั้ง" in row and "restart" in row
    import config
    assert "restart" in config.MSG_START_FROM_UNSET and "go-live" in config.MSG_START_FROM_UNSET


def test_install_db_test_wording_matches_what_the_code_does():
    """M-6: ปุ่มทดสอบ HOSxP ไม่รัน SELECT 1 — ต่อผ่านตัวต่อ READ ONLY แล้วอ่าน information_schema"""
    install = read("INSTALL.md")
    row = next(l for l in _between(install, "### 11.2 ").splitlines() if l.startswith("| **ทดสอบ HOSxP** |"))
    assert "SELECT 1" not in row and "information_schema" in row and "READ ONLY" in row
    assert "SELECT 1" not in _install_settings_section()
    code = read("settings_checks.py")
    assert "SELECT 1" not in code and "information_schema" in code


def test_every_restarts_itself_claim_in_install_carries_the_gunicorn_caveat():
    """M-6: "แอปเริ่มใหม่เอง" เป็นจริงเฉพาะใต้ gunicorn และต้องตรวจจริงบนเซิร์ฟเวอร์ (ข้อ 11.5)"""
    install = read("INSTALL.md")
    claims = [l for l in install.splitlines() if "เริ่มใหม่เอง" in l or "เริ่มระบบใหม่เอง" in l]
    assert len(claims) >= 2
    for line in claims:
        assert "gunicorn" in line, f"ประโยคนี้ไม่มีเงื่อนไข gunicorn: {line[:80]}"
    line77 = next(l for l in install.splitlines() if "เมื่อตรวจผ่านแล้วจึงเปลี่ยนเป็นกุญแจของกลุ่ม SRRT จริง" in l)
    assert "บันทึกที่หน้า `/settings` แอปเริ่มใหม่เองใต้ gunicorn (ตรวจจริงตามข้อ 11.5)" in line77


def test_install_recovery_says_settings_error_can_be_fixed_from_the_page():
    """M-6: settings_error แก้จากหน้าได้ (เปลี่ยนค่าใดแล้วบันทึกจะเขียนไฟล์ใหม่ที่สมบูรณ์) ไม่ใช่ต้องใช้ root เท่านั้น"""
    s114 = _between(_install_settings_section(), "### 11.4 ", "### 11.5 ")
    assert "แก้จากหน้าไม่ได้" not in s114
    step1 = _between(s114, "1. **ถ้ายัง login", "2. **ถ้าเข้าหน้าไม่ได้")
    for needle in ("settings_error", "เขียนไฟล์ใหม่", "เปลี่ยนค่า", "ส่งค่าเดิมซ้ำ"):
        assert needle in step1, needle
    step2 = _between(s114, "2. **ถ้าเข้าหน้าไม่ได้", "3. **ตั้งค่าผ่านไฟล์แทน")
    assert "root" in step2 and "settings_error" in step2
    # พฤติกรรมจริงที่เอกสารอ้าง: บันทึกค่าใหม่ทับไฟล์ที่เสีย -> ไฟล์สมบูรณ์ ไม่มี overlay_error
    # (ทดสอบ end-to-end ที่ tests/test_settings_views.py::test_saving_a_change_rewrites_a_corrupt_settings_file)


# ---------- หน้าเปรียบเทียบปี พ.ศ. /trend (v1.2.0) ----------

def test_trend_page_documented_in_changelog_readme_install():
    changelog = read("CHANGELOG.md")
    assert "## 1.2.0 (10 พ.ย. 2569)" in changelog
    entry = _between(changelog, "## 1.2.0", "\n## 1.1.0")
    for needle in ("/trend", "DENGUE_REALERT_DAYS", "เกณฑ์หยาบ", "trend_cache", "ไม่มีค่า env ใหม่"):
        assert needle in entry, f"CHANGELOG 1.2.0 ต้องกล่าวถึง {needle!r}"
    readme = read("README.md")
    assert "/trend" in readme and "21 วัน" in readme
    install = read("INSTALL.md")
    section = install[install.index("## 12."):]
    for needle in ("/trend", "21 วัน", "เกณฑ์หยาบ", "ปี พ.ศ.", "DENGUE_ICDS", "/check"):
        assert needle in section, f"INSTALL ข้อ 12 ต้องกล่าวถึง {needle!r}"


# ---------- เส้นขอบอำเภอและการระบาดปีก่อนบนแผนที่ (v1.3.0) ----------

def test_map_boundary_and_past_documented_in_changelog_readme_install():
    changelog = read("CHANGELOG.md")
    assert "## 1.3.0 (10 พ.ย. 2569)" in changelog
    entry = _between(changelog, "## 1.3.0", "\n## 1.2.0")
    for needle in ("/map", "boundaries/", "fetch_boundary.py", "ไม่มีค่า env ใหม่", "จุดกลางหมู่บ้าน"):
        assert needle in entry, f"CHANGELOG 1.3.0 ต้องกล่าวถึง {needle!r}"
    readme = read("README.md")
    assert "ODbL" in readme and "OpenStreetMap" in readme
    install = read("INSTALL.md")
    section = install[install.index("## 13."):]
    for needle in ("fetch_boundary.py", "--relation", "--query", "village_gis_location", "boundaries/"):
        assert needle in section, f"INSTALL ข้อ 13 ต้องกล่าวถึง {needle!r}"


# ---------- ตำบลแยกสีและหมุด รพ./รพ.สต. (v1.4.0) ----------

def test_tambon_and_facilities_documented_in_changelog_readme_install():
    changelog = read("CHANGELOG.md")
    assert "## 1.4.0 (10 พ.ย. 2569)" in changelog
    entry = _between(changelog, "## 1.4.0", "\n## 1.3.0")
    for needle in ("ตำบล", "import-facilities", "make_tambon_boundary.py", "facility", "ไม่มีค่า env ใหม่"):
        assert needle in entry, f"CHANGELOG 1.4.0 ต้องกล่าวถึง {needle!r}"
    readme = read("README.md")
    assert "เวอร์ชัน 1.5.1" in readme and "import-facilities" in readme
    install = read("INSTALL.md")
    section = install[install.index("## 14."):]
    for needle in ("import-facilities", "make_tambon_boundary.py", "HDX", "CC BY-IGO", "จัดตำแหน่งสถานพยาบาล",
                   "--dry-run", "8.89921E+12", "ชื่อ"):
        assert needle in section, f"INSTALL ข้อ 14 ต้องกล่าวถึง {needle!r}"
    assert "import-facilities" in read("deploy/deploy_steps.md")


# ---------- หลายโรคและแท็บเลือกโรค (v1.5.0) ----------

def test_multi_disease_documented_in_changelog_readme_install_deploy():
    changelog = read("CHANGELOG.md")
    assert "## 1.5.0 (10 พ.ย. 2569)" in changelog
    assert changelog.index("## 1.5.0") < changelog.index("## 1.4.0")
    entry = _between(changelog, "## 1.5.0", "\n## 1.4.0")
    for needle in ("แท็บเลือกโรค", "DENGUE_DISEASES", "disease", "เกณฑ์", "ไม่ลบข้อมูล", "1.6.0", "ทางเดินหายใจ",
                   "มือเท้าปาก", "ทีม SRRT"):
        assert needle in entry, f"CHANGELOG 1.5.0 ต้องกล่าวถึง {needle!r}"
    readme = read("README.md")
    assert "เวอร์ชัน 1.5.1" in readme and "DENGUE_DISEASES" in readme and "มือเท้าปาก" in readme
    install = read("INSTALL.md")
    section = install[install.index("## 15."):]
    for needle in ("DENGUE_DISEASES", "DENGUE_<KEY>_", "ทีม SRRT", "/check", "ทีละโรค", "A00", "visit เดียว",
                   "กลุ่มก้อน", "?d="):
        assert needle in section, f"INSTALL ข้อ 15 ต้องกล่าวถึง {needle!r}"
    step0 = _between(install, "## 0.", "## 1.")
    assert "เกณฑ์ตั้งต้นของโรคใหม่" in step0
    deploy = read("deploy/deploy_steps.md")
    assert "DENGUE_DISEASES" in deploy and "แคช" in deploy
    import diseases
    for d in diseases.REGISTRY:
        assert d.name_th in section, f"INSTALL ข้อ 15 ต้องระบุโรค {d.name_th}"


# ---------- เปรียบเทียบปี พ.ศ. ปฏิทิน (v1.5.1) ----------

def test_calendar_year_documented_in_changelog_readme_install():
    changelog = read("CHANGELOG.md")
    assert "## 1.5.1 (12 พ.ย. 2569)" in changelog and changelog.index("## 1.5.1") < changelog.index("## 1.5.0")
    entry = _between(changelog, "## 1.5.1", "\n## 1.5.0")
    for needle in ("ปี พ.ศ.", "ม.ค.–ธ.ค.", "ปีงบประมาณ", "year=", "แคช"):
        assert needle in entry, f"CHANGELOG 1.5.1 ต้องกล่าวถึง {needle!r}"
    install = read("INSTALL.md")
    section = install[install.index("## 12."):install.index("## 13.")]
    assert "ปีปฏิทิน" in section and "ม.ค.–ธ.ค." in section and "ปีงบ " not in section
    import trend
    assert trend.MONTH_LABELS[0] == "ม.ค." and trend.year_range(2569)[1].isoformat() == "2026-12-31"

