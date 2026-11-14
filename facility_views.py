"""หมุด รพ./รพ.สต. — `GET /api/facilities.json` (ทุกบทบาท) และ `PUT /api/facilities/<hospcode>` (เฉพาะ admin).

ตอบเฉพาะที่ตั้งสถานพยาบาลและชื่อสถานที่ (ไม่มีข้อมูลผู้ป่วย). ข้อความผิดพลาดเป็นข้อความคงที่.
"""
from __future__ import annotations

import json

from flask import Flask, jsonify, request, session

from auth import api_write_guard, has_role, login_required, role_required

MAX_BODY = 64 * 1024
MESSAGES = {
    "not_found": ("ไม่พบสถานพยาบาลนี้", 404),
    "bad_coords": ("พิกัดไม่ถูกต้อง", 400),
    "bad_request": ("รูปแบบคำขอไม่ถูกต้อง", 400),
    "unavailable": ("ตรวจรายชื่อสถานพยาบาลไม่ได้ ลองใหม่ภายหลัง", 503),
    "too_large": ("คำขอใหญ่เกินไป", 413),
}


def _error(code: str):
    message, status = MESSAGES[code]
    return jsonify(error=code, message=message), status


def _valid_hospcode(code: str) -> bool:
    return len(code) == 5 and code.isascii() and code.isdigit()


def register_facility_routes(app: Flask) -> None:
    @app.get("/api/facilities.json")
    @login_required
    def facilities_json():
        svc = app.config["FACILITIES"]
        return jsonify(svc.snapshot(has_role(session.get("role"), "admin")))

    # ชั้นนอกสุด role_required (ไม่ login 401 -> ต่ำกว่า admin 403) แล้ว api_write_guard (415 ไม่ใช่ JSON -> 403 csrf)
    @app.put("/api/facilities/<hospcode>")
    @role_required("admin")
    @api_write_guard
    def facility_put(hospcode: str):
        if not _valid_hospcode(hospcode):
            return _error("not_found")
        if request.content_length is not None and request.content_length > MAX_BODY:
            return _error("too_large")
        raw = request.get_data(cache=True)
        if len(raw) > MAX_BODY:
            return _error("too_large")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _error("bad_request")
        if not isinstance(body, dict) or "lat" not in body or "lng" not in body:
            return _error("bad_request")
        result = app.config["FACILITIES"].place(hospcode, body["lat"], body["lng"], session["user"])
        if result.status == "ok":
            return jsonify(ok=True)
        return _error({"bad_coords": "bad_coords", "unknown": "not_found", "unavailable": "unavailable"}[result.status])
