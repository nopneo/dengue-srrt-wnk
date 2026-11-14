"""ไคลเอนต์ MOPH Notify (หมอพร้อม) — ส่ง LINE message เข้ากลุ่มของหน่วยบริการ

POST {base_url}/api/notify/send หัว client-key / secret-key
รับค่าทั้งหมดจากพารามิเตอร์ (ไม่อ่าน/เขียนไฟล์ตั้งค่า ไม่ใส่คีย์ในข้อความ error หรือ log)
"""

import requests

TIMEOUT_S = 15

# คำอธิบาย error ตามตาราง Response ในคู่มือ API
ERROR_HINTS = {
    "Unauthorized": "client-key หรือ secret-key ไม่ถูกต้อง",
    "messages error": "รูปแบบข้อความไม่ถูกต้องตามที่ LINE กำหนด",
    "Send messages error": "ระบบหมอพร้อมส่งข้อความไม่สำเร็จ",
    "require messages": "ไม่ได้ส่ง messages มาใน body",
}


class NotifyError(Exception):
    """ส่งข้อความผ่าน MOPH Notify ไม่สำเร็จ (ข้อความเป็นภาษาไทย ไม่มีคีย์)"""


def send_messages(messages, base_url, client_key, secret_key):
    """ส่ง LINE message objects เข้ากลุ่มที่ผูกกับ client-key นี้ คืน JSON ตอบกลับเมื่อสำเร็จ"""
    if not (base_url and client_key and secret_key):
        raise NotifyError("ยังไม่ได้ตั้งค่า MOPH Notify (base_url / client-key / secret-key)")

    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/notify/send",
            headers={
                "Content-Type": "application/json",
                "client-key": client_key,
                "secret-key": secret_key,
            },
            json={"messages": messages},
            timeout=TIMEOUT_S,
            allow_redirects=False,  # กันส่ง client-key/secret-key ต่อไปยังปลายทางที่ redirect ไป
        )
    except requests.RequestException as exc:
        # ใช้ชื่อชนิด exception เท่านั้น ไม่ดึงข้อความเดิม เพื่อไม่ให้คีย์หลุดลง log
        raise NotifyError(f"เชื่อมต่อ MOPH Notify ไม่ได้ ({type(exc).__name__})") from None

    if 300 <= resp.status_code < 400:
        raise NotifyError(f"MOPH Notify ตอบกลับเป็นการเปลี่ยนเส้นทาง (HTTP {resp.status_code}) "
                          "ไม่ส่งคีย์ต่อ — ตรวจ base_url ให้ถูกต้อง")

    try:
        data = resp.json()
    except ValueError:
        raise NotifyError(f"MOPH Notify ตอบกลับไม่ใช่ JSON (HTTP {resp.status_code})") from None
    if not isinstance(data, dict):
        raise NotifyError(f"MOPH Notify ตอบกลับรูปแบบไม่ถูกต้อง (HTTP {resp.status_code})")

    # API ตอบ HTTP 200 แม้ผิดพลาด จึงต้องเช็ครหัสใน body: คู่มือใช้ message_code
    # แต่ของจริงกรณี key ผิดตอบ {"status":401,...} จึงรับทั้งสองชื่อ
    raw = data.get("message_code", data.get("status"))
    try:
        code = int(raw)
    except (TypeError, ValueError):
        raise NotifyError("MOPH Notify ตอบกลับไม่มีรหัสสถานะ (message_code/status)") from None
    if code != 200:
        msg = str(data.get("message", ""))
        if not msg:
            raise NotifyError(f"ไม่ทราบสาเหตุ (code {code})")
        raise NotifyError(f"{msg} — {ERROR_HINTS.get(msg, 'ไม่ทราบสาเหตุ')} (code {code})")
    return data
