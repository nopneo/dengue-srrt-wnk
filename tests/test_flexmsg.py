import json
import re
from datetime import datetime

import pytest

from flexmsg import (cancel_message, cluster_message, new_case_message, reactivate_message,
                     thai_date)

BASE = "https://wnkhos.go.th/dengue"
HN = "0123456"
DX = datetime(2026, 11, 8, 9, 5)


def _case(**over):
    case = {
        "id": 42, "visit_type": "OPD", "hn": HN, "icd10": "A911", "dx_at": DX,
        "village_code": "30200307", "sex": "1", "age_years": 8, "in_area": True,
        "pin_state": "none",
    }
    case.update(over)
    return case


def _walk(node, found):
    if isinstance(node, dict):
        if node.get("type") is not None:
            found.append(node)
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


def _nodes(msg):
    out = []
    _walk(msg["contents"], out)
    return out


TOKENS = {"none", "xs", "sm", "md", "lg", "xl", "xxl"}
TEXT_SIZES = {"xxs", "xs", "sm", "md", "lg", "xl", "xxl", "3xl", "4xl", "5xl"}
LEN_RE = re.compile(r"^\d+(\.\d+)?(%|px)$")
PX_RE = re.compile(r"^\d+(\.\d+)?px$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
COLOR_KEYS = {"color", "backgroundColor", "startColor", "endColor"}
PAD_KEYS = {"paddingAll", "paddingTop", "paddingBottom", "paddingStart", "paddingEnd"}
CHILDREN = {
    "vertical": {"box", "text", "button", "separator", "filler", "image", "icon"},
    "horizontal": {"box", "text", "button", "separator", "filler", "image", "icon"},
    "baseline": {"text", "icon", "filler"},  # LINE: baseline ห้ามมี box/button/image ซ้อน
}


def _token_or_px(value):
    return value in TOKENS or bool(PX_RE.match(str(value)))


def _check_props(node):
    """ตรวจค่าของ property ที่ LINE รู้จัก (ชุดจำกัดเฉพาะที่โค้ดนี้ใช้)"""
    t = node["type"]
    for k, v in node.items():
        if k in COLOR_KEYS:
            assert isinstance(v, str) and COLOR_RE.match(v), (k, v)
        elif k in ("spacing", "margin") or k in PAD_KEYS:
            assert isinstance(v, str) and _token_or_px(v), (k, v)
        elif k == "cornerRadius":
            assert isinstance(v, str) and _token_or_px(v), (k, v)
        elif k == "flex":
            assert isinstance(v, int) and not isinstance(v, bool), (k, v)
        elif k == "wrap":
            assert isinstance(v, bool), (k, v)
        elif k == "weight":
            assert v in ("regular", "bold"), v
        elif k == "align":
            assert v in ("start", "end", "center"), v
        elif k == "gravity":
            assert v in ("top", "bottom", "center"), v
        elif k == "alignItems":
            assert v in ("flex-start", "flex-end", "center"), v
        elif k == "layout":
            assert v in ("horizontal", "vertical", "baseline"), v
        elif k == "size" and t == "text":
            assert v in TEXT_SIZES or PX_RE.match(str(v)), v
        elif k in ("width", "height") and t == "box":
            assert isinstance(v, str) and LEN_RE.match(v), (k, v)
        elif k == "height" and t == "button":
            assert v in ("sm", "md"), v


def _check_node(node, parent_layout):
    assert isinstance(node, dict) and "type" in node
    t = node["type"]
    assert t in CHILDREN[parent_layout], f"{t} ห้ามอยู่ใน {parent_layout}"
    _check_props(node)
    texts = []
    if t == "text":
        assert isinstance(node["text"], str) and node["text"].strip(), "text node ว่าง"
        texts.append(node["text"])
    elif t == "box":
        assert node["layout"] in CHILDREN
        assert isinstance(node["contents"], list) and node["contents"]
        bg = node.get("background")
        if bg is not None:
            assert bg["type"] == "linearGradient"
            assert re.match(r"^\d+deg$", bg["angle"])
            assert COLOR_RE.match(bg["startColor"]) and COLOR_RE.match(bg["endColor"])
        for child in node["contents"]:
            texts += _check_node(child, node["layout"])
    elif t == "button":
        act = node["action"]
        assert act["type"] == "uri" and act["label"] and act["uri"].startswith("http")
    return texts


def _check_flex(msgs):
    """ตรวจโครงสร้าง Flex แบบเรียกซ้ำตามกฎ LINE คืนข้อความของทุก text node"""
    assert isinstance(msgs, list) and len(msgs) == 1
    m = msgs[0]
    assert m["type"] == "flex"
    assert isinstance(m["altText"], str) and 0 < len(m["altText"]) <= 400
    bubble = m["contents"]
    assert bubble["type"] == "bubble"
    texts = []
    for part in ("header", "hero", "body", "footer"):
        if part in bubble:
            assert bubble[part]["type"] == "box"
            texts += _check_node(bubble[part], "vertical")
    return texts


def test_validator_rejects_box_inside_baseline():
    bad = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [
        {"type": "box", "layout": "baseline", "contents": [
            {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": "x"}]},
        ]},
    ]}}
    with pytest.raises(AssertionError):
        _check_flex([{"type": "flex", "altText": "a", "contents": bad}])


def test_validator_rejects_bad_tokens():
    def wrap(node):
        return [{"type": "flex", "altText": "a", "contents": {
            "type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": [node]}}}]
    ok = {"type": "text", "text": "x", "size": "sm", "flex": 1, "wrap": True, "color": "#aabbcc"}
    assert _check_flex(wrap(ok)) == ["x"]
    for bad in ({"size": "huge"}, {"flex": "1"}, {"wrap": "true"}, {"color": "red"},
                {"weight": "heavy"}, {"align": "left"}, {"margin": "5"}, {"text": " "}):
        with pytest.raises(AssertionError):
            _check_flex(wrap({**ok, **bad}))
    for bad in ({"width": "50"}, {"height": "10"}, {"backgroundColor": "#fff"}):
        with pytest.raises(AssertionError):
            _check_flex(wrap({"type": "box", "layout": "vertical", "contents": [ok], **bad}))


def test_thai_date_buddhist_year():
    assert thai_date(datetime(2026, 11, 8, 9, 0)) == "8 พ.ย. 2569"
    assert thai_date(datetime(2026, 1, 31)) == "31 ม.ค. 2569"


def test_new_case_message_has_alttext_and_link():
    msgs = new_case_message(_case(), BASE)
    texts = _check_flex(msgs)
    m = msgs[0]
    assert m["altText"] == "สงสัยไข้เลือดออก (ในเขต) หมู่ 7 — เปิดดูรายละเอียด"
    s = json.dumps(m, ensure_ascii=False)
    assert f"{BASE}/case/42" in s
    assert "A91.1 · OPD" in texts
    assert "อายุ 8 ปี · ชาย" in texts
    assert "หมู่ 7" in texts
    assert "ในเขต" in texts
    assert "ยังไม่ปักหมุด" in texts
    assert "ปักที่บ้านผู้ป่วยเพื่อดูรัศมี 100 ม." in texts
    assert "8 พ.ย. 2569 · 09:05 น." in texts
    assert "#1440b8" in s and "#0b2e8a" in s and "#ffa62b" in s and "#1d6ff2" in s
    assert "SRRT WNK" in texts and "รพ.วังน้ำเขียว" in texts
    btn = [n for n in _nodes(m) if n["type"] == "button"][0]
    assert btn["action"] == {"type": "uri", "label": "เปิดแผนที่ / ปักหมุด", "uri": f"{BASE}/case/42"}


def test_new_case_message_never_contains_hn_or_name():
    msgs = new_case_message(_case(hn=HN, name="สมชาย ใจดี", phone="0812345678"), BASE)
    s = json.dumps(msgs, ensure_ascii=False)
    assert HN not in s
    assert "สมชาย" not in s and "0812345678" not in s
    assert "ชื่อ เบอร์โทร HN ดูในระบบ (ต้อง login)" in s


def test_new_case_message_handles_missing_fields():
    msgs = new_case_message(_case(age_years=None, sex=None, village_code=None), BASE)
    texts = _check_flex(msgs)
    assert "อายุ - · -" in texts
    assert "-" in texts  # พื้นที่ไม่ทราบหมู่
    assert "หมู่ -" not in texts
    assert msgs[0]["altText"].startswith("สงสัยไข้เลือดออก (ในเขต) - ")


def test_new_case_message_out_of_area_pill():
    msgs = new_case_message(_case(in_area=False), BASE)
    texts = _check_flex(msgs)
    s = json.dumps(msgs)
    assert "นอกเขต" in texts and "ในเขต" not in texts
    assert "(นอกเขต)" in msgs[0]["altText"]
    assert "#ffe9cf" in s and "#9a3412" in s


def test_new_case_in_area_pill_colors():
    s = json.dumps(new_case_message(_case(in_area=True), BASE))
    assert "#ddf6e4" in s and "#166534" in s


def test_new_case_pin_states():
    est = _check_flex(new_case_message(_case(pin_state="estimated"), BASE))
    assert "หมุดประมาณ ระดับหมู่บ้าน" in est
    conf_msgs = new_case_message(_case(pin_state="confirmed"), BASE)
    assert "ปักหมุดแล้ว" in _check_flex(conf_msgs)
    s = json.dumps(conf_msgs)
    assert "#34c759" in s and "#16a34a" in s


def test_new_case_area_label_overrides_and_female():
    msgs = new_case_message(_case(area_label="ต.วังน้ำเขียว หมู่ 3", sex="2"), BASE)
    texts = _check_flex(msgs)
    assert "ต.วังน้ำเขียว หมู่ 3" in texts
    assert "อายุ 8 ปี · หญิง" in texts
    assert "ต.วังน้ำเขียว หมู่ 3" in msgs[0]["altText"]


def test_new_case_icd_without_dot_when_short():
    texts = _check_flex(new_case_message(_case(icd10="A91", visit_type="IPD"), BASE))
    assert "A91 · IPD" in texts


def test_cluster_message_shows_size_and_updated_flag():
    first, last = datetime(2026, 10, 28, 8), datetime(2026, 11, 8, 9)
    kw = dict(size=3, region_label="ต.วังน้ำเขียว หมู่ 7", first_dx_at=first, last_dx_at=last,
              min_cases=2, cluster_id=9, base_url=BASE)
    msgs = cluster_message(**kw)
    texts = _check_flex(msgs)
    s = json.dumps(msgs, ensure_ascii=False)
    assert "พบกลุ่มผู้ป่วยไข้เลือดออก" in texts
    assert "3" in texts and "ราย" in texts
    assert "ในรัศมี 100 ม. ภายใน 14 วัน" in texts
    assert "ถึงเกณฑ์เตือน 2 / 2 ราย" in texts
    assert "28 ต.ค. → 8 พ.ย." in texts
    assert "ต.วังน้ำเขียว หมู่ 7" in texts
    assert f"{BASE}/map?cluster=9" in s
    btn = [n for n in _nodes(msgs[0]) if n["type"] == "button"][0]
    assert btn["action"]["label"] == "ดูแผนที่ cluster"
    assert "#ff4d5e" in s and "#dc2626" in s
    assert msgs[0]["altText"] == "พบกลุ่มผู้ป่วยไข้เลือดออก 3 ราย (ต.วังน้ำเขียว หมู่ 7)"
    upd = cluster_message(**kw, updated=True, radius_m=150, days=21)
    utexts = _check_flex(upd)
    assert "กลุ่มผู้ป่วยไข้เลือดออกเพิ่มขึ้น" in utexts
    assert "พบกลุ่มผู้ป่วยไข้เลือดออก" not in utexts
    assert "ในรัศมี 150 ม. ภายใน 21 วัน" in utexts


def test_cluster_progress_below_threshold():
    kw = dict(size=2, region_label="x", first_dx_at=DX, last_dx_at=DX, min_cases=3,
              cluster_id=1, base_url=BASE)
    msgs = cluster_message(**kw)
    texts = _check_flex(msgs)
    assert "ถึงเกณฑ์เตือน 2 / 3 ราย" in texts
    widths = [n.get("width") for n in _nodes(msgs[0]) if n.get("type") == "box"]
    assert "67%" in widths


def test_cancel_message_text_fields_and_link():
    case = _case(village_code="30200307", area_label="ต.ทดสอบ ม.7", pin_state="confirmed")
    msgs = cancel_message(case, BASE)
    texts = _check_flex(msgs)
    s = json.dumps(msgs, ensure_ascii=False)
    assert msgs[0]["altText"].startswith("ยกเลิกเคสไข้เลือดออก")
    assert "ต.ทดสอบ ม.7" in msgs[0]["altText"]
    assert "ยกเลิกเคสไข้เลือดออก" in texts
    assert "แพทย์แก้รหัสวินิจฉัย ไม่ใช่ไข้เลือดออกแล้ว" in texts
    # ฟิลด์ไม่ระบุตัวตนชุดเดียวกับข้อความเคสใหม่ เพื่อให้ผู้รับระบุได้ว่าเคสไหนถูกยกเลิก
    assert "8 พ.ย. 2569 · 09:05 น." in texts
    assert "A91.1 · OPD" in texts
    assert "อายุ 8 ปี · ชาย" in texts
    assert "ต.ทดสอบ ม.7" in texts and "ในเขต" in texts
    assert "#64748b" in s and "#475569" in s
    assert HN not in s
    [button] = [n for n in _nodes(msgs[0]) if n["type"] == "button"]
    assert button["action"]["uri"] == f"{BASE}/case/42"


def test_cancel_message_falls_back_to_moo_and_out_of_area():
    texts = _check_flex(cancel_message(_case(in_area=False), BASE))
    assert "หมู่ 7" in texts and "นอกเขต" in texts


def test_reactivate_message_fields_link_and_no_identity():
    case = _case(area_label="ต.ทดสอบ ม.7")
    msgs = reactivate_message(case, BASE)
    texts = _check_flex(msgs)
    s = json.dumps(msgs, ensure_ascii=False)
    assert msgs[0]["altText"].startswith("คืนสถานะเคสไข้เลือดออก")
    assert "คืนสถานะเคสไข้เลือดออก" in texts
    assert "8 พ.ย. 2569 · 09:05 น." in texts
    assert "A91.1 · OPD" in texts
    assert "อายุ 8 ปี · ชาย" in texts and "ต.ทดสอบ ม.7" in texts
    assert HN not in s
    [button] = [n for n in _nodes(msgs[0]) if n["type"] == "button"]
    assert button["action"]["uri"] == f"{BASE}/case/42"


def test_reactivate_message_header_is_blue_not_grey_or_red():
    s = json.dumps(reactivate_message(_case(), BASE), ensure_ascii=False)
    assert "#1440b8" in s and "#475569" not in s and "#dc2626" not in s


def test_cancel_and_reactivate_alttext_clamped():
    long = "ก" * 1000
    for fn in (cancel_message, reactivate_message):
        msgs = fn(_case(area_label=long), BASE)
        assert len(msgs[0]["altText"]) <= 400
        _check_flex(msgs)


def test_area_row_pill_is_in_horizontal_box_not_baseline():
    m = new_case_message(_case(), BASE)[0]
    rows = [n for n in _nodes(m) if n["type"] == "box" and any(
        c["type"] == "box" for c in n["contents"] if isinstance(c, dict))]
    for r in rows:
        assert r["layout"] != "baseline"
    pill_rows = [r for r in rows if r["layout"] == "horizontal" and r.get("alignItems") == "center"]
    assert pill_rows and pill_rows[0]["spacing"] == "sm"


def test_alttext_clamped_to_400_with_ellipsis():
    long = "ก" * 1000
    a = new_case_message(_case(area_label=long), BASE)[0]["altText"]
    b = cluster_message(size=3, region_label=long, first_dx_at=DX, last_dx_at=DX, min_cases=2,
                        cluster_id=1, base_url=BASE)[0]["altText"]
    for alt in (a, b):
        assert len(alt) == 400 and alt.endswith("…")
    # ข้อความสั้นไม่ถูกแตะ
    assert not new_case_message(_case(), BASE)[0]["altText"].endswith("…")
    _check_flex(new_case_message(_case(area_label=long), BASE))


def test_cluster_progress_width_never_below_one_percent():
    msgs = cluster_message(size=1, region_label="x", first_dx_at=DX, last_dx_at=DX,
                           min_cases=500, cluster_id=1, base_url=BASE)
    widths = [n.get("width") for n in _nodes(msgs[0]) if n.get("type") == "box"]
    assert "1%" in widths
    _check_flex(msgs)
