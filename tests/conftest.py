import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """pbkdf2 ค่าเริ่มต้นของ werkzeug ช้า (~1 วินาทีต่อแฮช) — เทสต์ใช้ 1000 รอบ (รูปแบบแฮชเดิม
    `pbkdf2:sha256:...` การตรวจรหัสอ่านจำนวนรอบจากตัวแฮชเอง จึงตรวจแฮชจริงของ production ได้เหมือนเดิม)."""
    import store as store_module
    real = store_module.generate_password_hash

    def quick(password, method="pbkdf2:sha256", *a, **kw):
        if method == "pbkdf2:sha256":
            method = "pbkdf2:sha256:1000"
        return real(password, method, *a, **kw)

    monkeypatch.setattr(store_module, "generate_password_hash", quick)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "data" / "dengue.db"))


@pytest.fixture
def login_as():
    """ตั้ง session ตรง ๆ (ไม่ผ่าน pbkdf2) สำหรับเทสต์ที่ไม่ได้ทดสอบขั้นตอน login เอง.
    สร้าง/อัปเดตแถวผู้ใช้ในฐานด้วย (แฮชปลอม ใช้ล็อกอินจริงไม่ได้) เพราะ login_required
    ตรวจ session กับฐานทุกคำขอ."""
    def _login_as(client, username="u", role="srrt"):
        store = client.application.config["STORE"]
        with store._connect() as con:
            con.execute('INSERT INTO "user" (username, password_hash, role) VALUES (?,?,?)'
                        " ON CONFLICT(username) DO UPDATE SET role=excluded.role",
                        (username, "x", role))
        with client.session_transaction() as s:
            s["user"] = username
            s["role"] = role
    return _login_as
