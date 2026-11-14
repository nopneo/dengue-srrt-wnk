"""ตัวอ่านไฟล์ `.env` แบบไม่รันอะไร (ไม่ใช้ shell, ไม่ขยายตัวแปร) สำหรับคำสั่งบรรทัดคำสั่งเท่านั้น.

ใช้จาก `run.py` และ `setup_check.py` (รันมือจากโฟลเดอร์แอป) — **ไม่เรียกจาก wsgi/app**: ตอนรันเป็น service
systemd ส่งค่าจาก `EnvironmentFile` ให้อยู่แล้ว

กติกาไฟล์ (ตรงกับที่ INSTALL.md บอก):
- หนึ่งบรรทัดต่อหนึ่งค่า `NAME=value` · บรรทัดว่างและบรรทัดที่ขึ้นต้นด้วย `#` ถูกข้าม · ขึ้นต้นด้วย `export ` ได้
- ชื่อต้องเป็น `[A-Za-z_][A-Za-z0-9_]*` ไม่งั้นข้ามบรรทัดนั้น
- ค่าที่ครอบด้วย single quote หรือ double quote คู่เดียวกันจะถูกเอาเครื่องหมายครอบออก เนื้อในเก็บตามตัวอักษร
  (ไม่มี escape, ไม่มีการขยาย `$VAR`, backtick หรือ `$(...)`, ไม่รันคำสั่ง) — **ค่าที่ในตัวมี `'` ไม่รองรับ**
- `#` กลางบรรทัด **ไม่ใช่คอมเมนต์** (เป็นส่วนของค่า) จึงห้ามเขียนคอมเมนต์ต่อท้ายบรรทัดค่า
- ค่าที่มีอักขระพิเศษ (ช่องว่าง `$` `#` `!` `&` `;` ...) ให้ครอบด้วย single quote
"""
from __future__ import annotations

import os
import re
from typing import MutableMapping

ENV_FILE_VAR = "DENGUE_ENV_FILE"
DEFAULT_ENV_FILE = ".env"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_env_file(path: str) -> dict[str, str]:
    """อ่านไฟล์ env เป็น dict (ค่าหลังๆ ทับค่าก่อนหน้า). ไฟล์ไม่มี -> FileNotFoundError."""
    values: dict[str, str] = {}
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export ") or line.startswith("export\t"):
                line = line[len("export"):].lstrip()
            name, sep, value = line.partition("=")
            name = name.strip()
            if not sep or not _NAME.match(name):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[name] = value
    return values


def load_dotenv_if_present(path: str | None = None,
                           environ: MutableMapping[str, str] | None = None) -> list[str]:
    """โหลดไฟล์ `DENGUE_ENV_FILE` (หรือ `./.env`) เข้า environ โดย**ไม่ทับ**ตัวแปรที่ตั้งไว้แล้ว.

    คืนรายชื่อตัวแปรที่โหลด (ไม่คืนค่า). ไม่มีไฟล์หรืออ่านไม่ได้ -> [] (ไม่ล้ม)."""
    env = os.environ if environ is None else environ
    target = path or env.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE
    try:
        values = parse_env_file(target)
    except (OSError, UnicodeDecodeError):
        return []
    loaded = []
    for name, value in values.items():
        if name not in env:
            env[name] = value
            loaded.append(name)
    return loaded
