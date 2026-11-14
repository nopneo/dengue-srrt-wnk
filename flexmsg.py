"""LINE Flex Message ของแอปแจ้งเตือนไข้เลือดออก (SRRT) สไตล์ DB_color

ชนิดข้อความ: เคสใหม่ · cluster · ยกเลิกเคส · คืนสถานะเคส — แต่ละฟังก์ชันคืนลิสต์ของ Flex เดียว (มี altText)
ไม่มีชื่อ เบอร์ HN หรือที่อยู่บ้านในข้อความ มีเพียงหมู่ อายุ เพศ
โมดูลนี้เป็นฟังก์ชันล้วน ไม่ต่อเครือข่าย/ฐานข้อมูล
ชื่อโรคและสีหัวการ์ดมาจากทะเบียนโรค (diseases.py) ตาม case["disease"] — ไม่มี/ไม่รู้จัก = ไข้เลือดออก (ข้อความเดิมคงตัวอักษร)
"""
from diseases import DISEASES

NAVY = ("#1440b8", "#0b2e8a")
NAVY_BAR = "#0b2e8a"
ORANGE = ("#ffa62b", "#f97316")
GREEN = ("#34c759", "#16a34a")
RED = ("#ff4d5e", "#dc2626")
GREY = ("#64748b", "#475569")
BLUE_BTN = "#1d6ff2"
INK = "#1e293b"
MUTED = "#64748b"
RED_TEXT = "#dc2626"
TRACK = "#e2e8f0"
PILL_IN = ("#ddf6e4", "#166534")
PILL_OUT = ("#ffe9cf", "#9a3412")

HOSPITAL_SHORT = "รพ.วังน้ำเขียว"
APP_TAG = "SRRT WNK"

_MONTHS = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
           "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
_SEX = {"1": "ชาย", "2": "หญิง"}


# ------------------------------------------------------------ วันที่

def thai_date(dt):
    """8 พ.ย. 2569 (พ.ศ. = ค.ศ. + 543)"""
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year + 543}"


def _short(dt):
    """8 พ.ย. (ไม่มีปี) ใช้ในแถว รายแรก → ล่าสุด"""
    return f"{dt.day} {_MONTHS[dt.month - 1]}"


# ------------------------------------------------------------ ชิ้นส่วนพื้นฐาน

def _grad(colors):
    a, b = colors
    return {"type": "linearGradient", "angle": "135deg", "startColor": a, "endColor": b}


def _text(text, **kw):
    return {"type": "text", "text": str(text) if text not in (None, "") else "-", "wrap": True, **kw}


def _pill(text, colors, size="xs"):
    bg, fg = colors
    return {
        "type": "box", "layout": "vertical", "flex": 0,
        "backgroundColor": bg, "cornerRadius": "20px",
        "paddingTop": "2px", "paddingBottom": "2px", "paddingStart": "10px", "paddingEnd": "10px",
        "contents": [_text(text, size=size, color=fg, weight="bold", align="center", wrap=False)],
    }


def _kv(label, value, pill=None):
    label_t = _text(label, size="xs", color=MUTED, flex=3, wrap=False)
    value_t = _text(value, size="sm", color=INK, weight="bold", flex=6)
    if pill is None:  # แถวที่มีแต่ text ใช้ baseline ได้
        return {"type": "box", "layout": "baseline", "spacing": "md",
                "contents": [label_t, value_t]}
    # LINE: box ซ้อนได้เฉพาะใน vertical/horizontal จึงใช้ horizontal เมื่อมี pill
    return {"type": "box", "layout": "horizontal", "alignItems": "center", "spacing": "sm",
            "contents": [label_t, value_t, pill]}


def _header(colors, title, subtitle=None):
    contents = [_text(title, size="lg", color="#ffffff", weight="bold")]
    if subtitle:
        contents.append(_text(subtitle, size="xs", color="#ffffffcc", margin="sm"))
    return {"type": "box", "layout": "vertical", "background": _grad(colors),
            "paddingAll": "16px", "contents": contents}


def _strip(colors, title, subtitle=None):
    contents = [_text(title, size="sm", color="#ffffff", weight="bold")]
    if subtitle:
        contents.append(_text(subtitle, size="xxs", color="#ffffffdd"))
    return {"type": "box", "layout": "vertical", "background": _grad(colors),
            "cornerRadius": "12px", "paddingAll": "10px", "spacing": "xs", "contents": contents}


def _button(label, uri):
    return {"type": "button", "style": "primary", "color": BLUE_BTN, "height": "sm",
            "action": {"type": "uri", "label": label, "uri": uri}}


def _brand_bar():
    return {
        "type": "box", "layout": "horizontal", "backgroundColor": NAVY_BAR,
        "paddingAll": "8px", "contents": [
            _text(APP_TAG, size="xxs", color="#ffffff", weight="bold", flex=1, wrap=False),
            _text(HOSPITAL_SHORT, size="xxs", color="#ffffffcc", align="end", flex=1, wrap=False),
        ],
    }


def _bubble(header, body_contents, action_contents, alt_text):
    """ประกอบ bubble: header · body · footer (ปุ่ม/หมายเหตุ ถ้ามี + แถบแบรนด์ท้ายสุด)"""
    footer = []
    if action_contents:
        footer.append({"type": "box", "layout": "vertical", "paddingAll": "12px",
                       "spacing": "sm", "contents": action_contents})
    footer.append(_brand_bar())
    bubble = {
        "type": "bubble", "size": "mega",
        "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "14px", "spacing": "md",
                 "contents": body_contents},
        "footer": {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": footer},
    }
    return [{"type": "flex", "altText": _alt(alt_text), "contents": bubble}]


# ------------------------------------------------------------ ค่าที่แสดง

ALT_MAX = 400


def _alt(text):
    """altText ต้อง <= 400 ตัวอักษร (LINE ปฏิเสธถ้าเกิน)"""
    return text if len(text) <= ALT_MAX else text[:ALT_MAX - 1] + "…"


def _disease(case):
    return DISEASES.get(case.get("disease") or "dengue") or DISEASES["dengue"]


def _icd_display(icd10):
    code = (icd10 or "").strip().upper()
    if not code:
        return "-"
    return f"{code[:3]}.{code[3:]}" if len(code) > 3 else code


def _area_text(case):
    label = case.get("area_label")
    if label:
        return str(label)
    vc = case.get("village_code")
    if isinstance(vc, str) and len(vc) == 8 and vc.isdigit() and int(vc[6:8]) > 0:
        return f"หมู่ {int(vc[6:8])}"
    return "-"


def _patient_text(case):
    age = case.get("age_years")
    age_txt = f"อายุ {age} ปี" if age is not None else "อายุ -"
    return f"{age_txt} · {_SEX.get(case.get('sex'), '-')}"


def _pin_strip(pin_state):
    if pin_state == "confirmed":
        return _strip(GREEN, "ปักหมุดแล้ว")
    if pin_state == "estimated":
        return _strip(ORANGE, "หมุดประมาณ ระดับหมู่บ้าน")
    return _strip(ORANGE, "ยังไม่ปักหมุด", "ปักที่บ้านผู้ป่วยเพื่อดูรัศมี 100 ม.")


# ------------------------------------------------------------ ข้อความ 3 ชนิด

def new_case_message(case, base_url):
    """แจ้งเคสสงสัยไข้เลือดออกใหม่ — ไม่ใส่ HN/ชื่อ/เบอร์ (case['hn'] ไม่ถูกใช้)"""
    dx_at = case["dx_at"]
    d = _disease(case)
    rows, area, area_state = _case_rows(case)
    body = [_pin_strip(case.get("pin_state")), *rows]
    footer = [
        _button("เปิดแผนที่ / ปักหมุด", f"{base_url.rstrip('/')}/case/{case['id']}"),
        _text("ชื่อ เบอร์โทร HN ดูในระบบ (ต้อง login)", size="xxs", color=MUTED, align="center"),
    ]
    header = _header(d.color, f"สงสัย{d.name_th}", f"{thai_date(dx_at)} · {dx_at:%H:%M} น.")
    alt = f"สงสัย{d.name_th} ({area_state}) {area} — เปิดดูรายละเอียด"
    return _bubble(header, body, footer, alt)


def cluster_message(size, region_label, first_dx_at, last_dx_at, min_cases, cluster_id,
                    base_url, updated=False, radius_m=100, days=14,
                    disease_key="dengue", group_by="pin_radius"):
    """แจ้ง cluster ใหม่ (หรือ cluster ที่ขนาดเพิ่มขึ้นเมื่อ updated=True).
    group_by="village" = กลุ่มก้อนในหมู่บ้านเดียวกัน (ไม่มีรัศมี)"""
    d = DISEASES.get(disease_key) or DISEASES["dengue"]
    title = f"กลุ่มผู้ป่วย{d.name_th}เพิ่มขึ้น" if updated else f"พบกลุ่มผู้ป่วย{d.name_th}"
    scope = (f"ในหมู่บ้านเดียวกัน ภายใน {days} วัน" if group_by == "village"
             else f"ในรัศมี {radius_m} ม. ภายใน {days} วัน")
    reached = min(size, min_cases)
    pct = 100 if size >= min_cases or min_cases <= 0 else max(1, round(100 * size / min_cases))
    progress = {
        "type": "box", "layout": "vertical", "backgroundColor": TRACK, "cornerRadius": "6px",
        "height": "10px", "contents": [
            {"type": "box", "layout": "vertical", "width": f"{pct}%", "height": "10px",
             "background": _grad(d.cluster_color), "cornerRadius": "6px", "contents": [{"type": "filler"}]},
        ],
    }
    body = [
        {"type": "box", "layout": "baseline", "spacing": "sm", "contents": [
            _text(size, size="3xl", weight="bold", color=RED_TEXT, flex=0, wrap=False),
            _text("ราย", size="md", color=INK, weight="bold", flex=0, wrap=False),
        ]},
        _text(scope, size="xs", color=MUTED),
        progress,
        _text(f"ถึงเกณฑ์เตือน {reached} / {min_cases} ราย", size="xxs", color=MUTED),
        _kv("บริเวณ", region_label),
        _kv("รายแรก → ล่าสุด", f"{_short(first_dx_at)} → {_short(last_dx_at)}"),
    ]
    footer = [_button("ดูแผนที่ cluster", f"{base_url.rstrip('/')}/map?cluster={cluster_id}")]
    header = _header(d.cluster_color, title, f"รายล่าสุด {thai_date(last_dx_at)}")
    alt = f"พบกลุ่มผู้ป่วย{d.name_th} {size} ราย ({region_label})"
    return _bubble(header, body, footer, alt)


def _case_rows(case):
    """แถวข้อมูลไม่ระบุตัวตนของเคส (รหัสโรค/ผู้ป่วย/พื้นที่) ใช้ร่วมกันทุกข้อความรายเคส + ข้อความสถานะ"""
    in_area = bool(case.get("in_area"))
    area = _area_text(case)
    area_state = "ในเขต" if in_area else "นอกเขต"
    icd_line = f"{_icd_display(case.get('icd10'))} · {case.get('visit_type') or '-'}"
    rows = [
        _kv("รหัสโรค", icd_line),
        _kv("ผู้ป่วย", _patient_text(case)),
        _kv("พื้นที่", area, pill=_pill(area_state, PILL_IN if in_area else PILL_OUT)),
    ]
    return rows, area, area_state


def _status_message(case, base_url, colors, title, note, alt_prefix):
    """ข้อความเปลี่ยนสถานะของเคสเดิม (ยกเลิก/คืนสถานะ): ข้อความอธิบาย + ฟิลด์เคส + ปุ่มเปิดเคส (ต้อง login)"""
    dx_at = case["dx_at"]
    rows, area, area_state = _case_rows(case)
    body = [_text(note, size="sm", color=INK), *rows]
    footer = [
        _button("เปิดดูเคส", f"{base_url.rstrip('/')}/case/{case['id']}"),
        _text("ชื่อ เบอร์โทร HN ดูในระบบ (ต้อง login)", size="xxs", color=MUTED, align="center"),
    ]
    header = _header(colors, title, f"{thai_date(dx_at)} · {dx_at:%H:%M} น.")
    alt = f"{alt_prefix} ({area_state}) {area} — วินิจฉัย {thai_date(dx_at)} {dx_at:%H:%M} น."
    return _bubble(header, body, footer, alt)


def cancel_message(case, base_url):
    """แจ้งยกเลิกเคส (แพทย์แก้รหัสวินิจฉัย) — ฟิลด์ไม่ระบุตัวตนชุดเดียวกับข้อความเคสใหม่ +
    ปุ่มเปิดเคส (ต้อง login) ไม่มีชื่อ/HN"""
    name = _disease(case).name_th
    return _status_message(case, base_url, GREY, f"ยกเลิกเคส{name}",
                           f"แพทย์แก้รหัสวินิจฉัย ไม่ใช่{name}แล้ว", f"ยกเลิกเคส{name}")


def reactivate_message(case, base_url):
    """แจ้งคืนสถานะเคสที่เคยยกเลิก (รหัสวินิจฉัยกลับมาใน HOSxP) — ไม่มีชื่อ/HN"""
    d = _disease(case)
    return _status_message(case, base_url, d.color, f"คืนสถานะเคส{d.name_th}",
                           f"รหัสวินิจฉัย{d.name_th}กลับมาแล้ว เคสนี้ใช้งานอีกครั้ง",
                           f"คืนสถานะเคส{d.name_th}")
