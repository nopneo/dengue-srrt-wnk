"""ตัวช่วยตั้งค่า .env บนเซิร์ฟเวอร์ — ถามทีละช่อง ค่าลับพิมพ์แล้วไม่แสดงบนจอ

ใช้ครั้งแรกหลังวางไฟล์ (รันในเทอร์มินัลจริงเท่านั้น เพราะต้องพิมพ์รหัสผ่าน):

    ssh -t wnk-app 'cd /opt/dengue_srrt && .venv/bin/python deploy/set_env.py'

- สร้าง SECRET_KEY สุ่มให้เองเมื่อยังไม่มี/สั้น/เป็นค่าตัวอย่าง (ไม่แสดงค่า)
- แก้ไฟล์ "ในที่เดิม" จึงคงเจ้าของ/สิทธิ์เดิม (root:dengue 640) — ห้ามใช้ sed -i กับ .env
- ไม่พิมพ์ค่าลับใดออกหน้าจอ แสดงเฉพาะชื่อตัวแปรที่ตั้ง
- รันซ้ำได้ ค่าเดิมเป็นค่าตั้งต้น (กด Enter เพื่อคงไว้ ยกเว้นรหัสผ่านที่ต้องพิมพ์ใหม่หรือเว้นว่างเพื่อคงเดิม)
"""

from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import envfile  # noqa: E402
import settings_store  # noqa: E402
from config import looks_like_placeholder  # noqa: E402

SAFE_VALUE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]*$")


class InputError(ValueError):
    pass


def format_value(value: str) -> str:
    """คืนค่าที่เขียนลงไฟล์ได้ปลอดภัย (ครอบ single quote เมื่อมีอักขระพิเศษ)"""
    if "'" in value or "\n" in value or "\r" in value:
        raise InputError("ค่าห้ามมี ' หรือขึ้นบรรทัดใหม่")
    return value if SAFE_VALUE.match(value) else f"'{value}'"


def validate_start_from(value: str, today: date | None = None) -> str:
    """ตัวตรวจเดียวกับหน้า /settings (settings_store) — แปลง SettingsError เป็น InputError ให้โค้ดเดิมใช้ต่อได้"""
    try:
        return settings_store.validate_start_from(value, today)
    except settings_store.SettingsError as e:
        raise InputError(e.reason) from None


def check_digits(value: str) -> str:
    if not value.isdigit():
        raise InputError("ต้องเป็นตัวเลข")
    return value


def set_key(lines: list[str], key: str, value: str) -> list[str]:
    """แทนบรรทัด KEY=… (หรือ # KEY=… ที่ถูกคอมเมนต์) ไม่เจอก็ต่อท้าย"""
    new = f"{key}={format_value(value)}"
    active = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if active.match(line):
            lines[i] = new
            return lines
    for i, line in enumerate(lines):
        if commented.match(line):
            lines[i] = new
            return lines
    lines.append(new)
    return lines


def strong_secret_missing(current: str) -> bool:
    return len(current) < 16 or looks_like_placeholder(current)


def collect(existing: dict[str, str], ask=input, ask_secret=getpass.getpass, today: date | None = None) -> dict[str, str]:
    """ถามค่าทั้งหมด คืน dict ของค่าที่จะตั้ง (ไม่เขียนไฟล์ ไม่พิมพ์ค่า)"""
    out: dict[str, str] = {}

    def text(key: str, label: str, default: str = "", check=None) -> None:
        cur = existing.get(key, "")
        if looks_like_placeholder(cur):
            cur = ""
        shown = cur or default
        while True:
            raw = ask(f"{label} [{shown}]: ").strip() if shown else ask(f"{label}: ").strip()
            val = raw or shown
            if not val:
                print("  ต้องกรอก")
                continue
            try:
                format_value(val)
                if check:
                    val = check(val)
            except InputError as e:
                print(f"  ไม่ถูกต้อง: {e}")
                continue
            out[key] = val
            return

    def secret(key: str, label: str, required: bool) -> None:
        has = bool(existing.get(key)) and not looks_like_placeholder(existing.get(key, ""))
        hint = "เว้นว่าง = คงค่าเดิม" if has else ("ต้องกรอก" if required else "เว้นว่าง = ยังไม่ตั้ง")
        while True:
            val = ask_secret(f"{label} ({hint}): ")
            if not val:
                if has:
                    return
                if not required:
                    if looks_like_placeholder(existing.get(key, "")):
                        out[key] = ""  # แทนค่าตัวอย่าง <...> ด้วยค่าว่าง = ยังไม่ตั้ง
                    return
                print("  ต้องกรอก")
                continue
            try:
                format_value(val)
            except InputError as e:
                print(f"  ไม่ถูกต้อง: {e}")
                continue
            out[key] = val
            return

    text("DB_HOST", "DB_HOST (ที่อยู่ฐานข้อมูล HOSxP)", "127.0.0.1")
    text("DB_PORT", "DB_PORT", "3306", check=check_digits)
    text("DB_NAME", "DB_NAME", "hos")
    text("DB_USER", "DB_USER (บัญชี SELECT-only ห้ามใช้บัญชีที่แก้ตารางได้)")
    secret("DB_PASS", "DB_PASS", required=True)
    text("DENGUE_BASE_URL", "DENGUE_BASE_URL", "https://wnkhos.go.th/dengue")
    text("DENGUE_START_FROM", "DENGUE_START_FROM (วัน go-live ปี ค.ศ. YYYY-MM-DD)", (today or date.today()).isoformat(),
         check=lambda v: validate_start_from(v, today))
    secret("MOPH_CLIENT_KEY", "MOPH_CLIENT_KEY ของกลุ่ม LINE", required=False)
    secret("MOPH_SECRET_KEY", "MOPH_SECRET_KEY ของกลุ่ม LINE", required=False)
    if strong_secret_missing(existing.get("SECRET_KEY", "")):
        out["SECRET_KEY"] = secrets.token_urlsafe(48)
    return out


def apply(path: Path, values: dict[str, str]) -> list[str]:
    """เขียนในที่เดิม (r+ + truncate) เพื่อคงเจ้าของ/สิทธิ์ของไฟล์"""
    with open(path, "r+", encoding="utf-8", newline="") as f:
        lines = f.read().replace("\r\n", "\n").split("\n")
        while lines and lines[-1] == "":
            lines.pop()
        for key, val in values.items():
            lines = set_key(lines, key, val)
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()
    return list(values)


def main(argv: list[str] | None = None, ask=input, ask_secret=getpass.getpass) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env", default=str(Path(__file__).resolve().parent.parent / ".env"), help="ไฟล์ .env (ต้องมีอยู่แล้ว)")
    args = ap.parse_args(argv)
    path = Path(args.env)
    if not path.is_file():
        print(f"ไม่พบ {path} — สร้างจาก .env.example ก่อน (ดู INSTALL.md ข้อ 4.4)")
        return 2
    existing = envfile.parse_env_file(str(path))
    print(f"ตั้งค่า {path} — พิมพ์รหัสผ่านแล้วจะไม่แสดงบนจอ (กด Enter เพื่อคงค่าในวงเล็บ)\n")
    try:
        values = collect(existing, ask=ask, ask_secret=ask_secret)
    except (EOFError, KeyboardInterrupt):
        print("\nยกเลิก — ไม่ได้แก้ไฟล์")
        return 2
    names = apply(path, values)
    print("\nตั้งค่าแล้ว:", ", ".join(names))
    left = [ln for ln in path.read_text(encoding="utf-8").splitlines() if re.match(r"^[A-Z_]+=<", ln)]
    if left:
        print(f"\n!! ยังมี {len(left)} บรรทัดที่เป็นค่าตัวอย่าง <...> ให้ลบบรรทัดที่ไม่ใช้ หรือกรอกค่าจริง:")
        for ln in left:
            print("  ", ln.split("=", 1)[0])
        return 1
    print("ไม่มีค่าตัวอย่าง <...> ค้าง — รัน python setup_check.py ต่อได้")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
