"""โรคที่เลือกดู (`?d=<key>`) และแท็บเลือกโรคด้านขวา — สเปก 2026-11-10-multi-disease ข้อ 4.6.

ค่า `d` ที่ไม่รู้จัก/ว่าง/โรคที่ปิดอยู่ = โรคแรกที่เปิด (ไม่ใช่ error). แท็บแสดงเฉพาะเมื่อเปิดมากกว่าหนึ่งโรค
บนหน้าที่กรองตามโรค (รายการเคส แผนที่ เปรียบเทียบปี พ.ศ.) — เปิดโรคเดียวหน้าตาเหมือน 1.4.0.
"""
from __future__ import annotations

from flask import Flask, request, session, url_for

from config import Settings
from diseases import DiseaseRule

TAB_ENDPOINTS = frozenset({"index", "map_view", "trend_page"})


def current_rule(settings: Settings, args, store=None) -> DiseaseRule:
    """กฎของโรคที่เลือกจาก query `d` (รับ mapping ใด ๆ ที่มี .get).

    ไม่มี `d` ที่ใช้ได้แต่มี `cluster=<id>` (ลิงก์จากข้อความ LINE) และส่ง store มา = โรคของกลุ่มนั้น (ถ้าเปิดอยู่)."""
    rules = settings.enabled_rules()
    key = str(args.get("d") or "").strip().lower()
    for rule in rules:
        if rule.key == key:
            return rule
    raw = str(args.get("cluster") or "").strip()
    if store is not None and raw.isascii() and raw.isdigit():
        owner = store.cluster_disease(int(raw))
        for rule in rules:
            if rule.key == owner:
                return rule
    return rules[0]


def register_disease_context(app: Flask) -> None:
    @app.context_processor
    def inject_disease():
        settings: Settings = app.config["SETTINGS"]
        rules = settings.enabled_rules()
        store = app.config["STORE"]
        rule = current_rule(settings, request.args, store)
        multi = len(rules) > 1
        explicit = str(request.args.get("d") or "").strip().lower() in {r.key for r in rules}
        tabs: list[dict] = []
        if multi and request.endpoint in TAB_ENDPOINTS and session.get("user"):
            counts = store.active_counts(settings.start_from)
            tabs = [{"key": r.key, "name": r.disease.name_th, "color": r.disease.color[0],
                     "badge": counts.get(r.key, 0), "active": r.key == rule.key,
                     "url": url_for(request.endpoint, d=r.key)} for r in rules]
        return {"current_disease": rule.disease, "disease_tabs": tabs,
                # ใส่ใน url_for ของลิงก์ตัวกรอง: None = ไม่ต่อ ?d= (เปิดโรคเดียว URL เหมือนเดิม)
                "disease_param": rule.key if multi else None,
                # เมนูซ้าย: พก ?d= เฉพาะเมื่อผู้ใช้เลือกโรคไว้ในหน้านี้ (หน้าอื่นไม่ล็อกโรค)
                "menu_param": rule.key if multi and explicit else None}
