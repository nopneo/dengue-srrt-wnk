from datetime import date

from config import load_settings


def test_defaults_match_spec():
    s = load_settings({})
    assert s.icds == ("A90", "A91")
    assert s.poll_seconds == 120
    assert s.cluster_min_cases == 2
    assert s.cluster_radius_m == 100
    assert s.cluster_days == 14
    assert s.realert_days == 21
    assert s.start_from == date.today()
    assert s.area_amphoe == ()
    assert s.base_url == "https://wnkhos.go.th/dengue"
    assert s.data_audited is False
    assert s.db_path == "data/dengue.db"


def test_env_overrides():
    s = load_settings({
        "DENGUE_ICDS": "A90",
        "DENGUE_POLL_SECONDS": "60",
        "DENGUE_START_FROM": "2026-10-01",
        "DENGUE_CLUSTER_MIN_CASES": "3",
        "DENGUE_CLUSTER_RADIUS_M": "150",
        "DENGUE_CLUSTER_DAYS": "7",
        "DENGUE_REALERT_DAYS": "30",
        "DENGUE_AREA_AMPHOE": "3014, 3015",
        "DENGUE_BASE_URL": "https://example.test/dengue",
        "DENGUE_TILE_URL": "https://tiles.test/{z}/{x}/{y}.png",
        "DENGUE_DATA_AUDITED": "1",
        "DENGUE_DB_PATH": "x/y.db",
    })
    assert s.icds == ("A90",)
    assert s.poll_seconds == 60
    assert s.start_from == date(2026, 10, 1)
    assert s.cluster_min_cases == 3
    assert s.cluster_radius_m == 150
    assert s.cluster_days == 7
    assert s.realert_days == 30
    assert s.area_amphoe == ("3014", "3015")
    assert s.base_url == "https://example.test/dengue"
    assert s.tile_url == "https://tiles.test/{z}/{x}/{y}.png"
    assert s.data_audited is True
    assert s.db_path == "x/y.db"


def test_icds_split_and_stripped():
    assert load_settings({"DENGUE_ICDS": " a90, A91 "}).icds == ("A90", "A91")


def test_lan_and_moph_defaults_and_overrides():
    s = load_settings({})
    assert s.lan_prefixes == (
        "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.",
        "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
        "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "192.168.",
        "127.", "30.25.",
    )
    assert all(p.endswith(".") for p in s.lan_prefixes)
    assert not "172.217.1.1".startswith(s.lan_prefixes)
    assert "172.20.5.9".startswith(s.lan_prefixes)
    assert s.secret_key == ""
    assert s.moph_base_url == "https://morpromt2f.moph.go.th"
    assert s.moph_client_key == ""
    assert s.moph_secret_key == ""

    s = load_settings({
        "DENGUE_LAN_PREFIXES": " 10. , 192.168.",
        "SECRET_KEY": "k",
        "MOPH_BASE_URL": "https://moph.test",
        "MOPH_CLIENT_KEY": "ck",
        "MOPH_SECRET_KEY": "sk",
    })
    assert s.lan_prefixes == ("10.", "192.168.")
    assert s.secret_key == "k"
    assert s.moph_base_url == "https://moph.test"
    assert s.moph_client_key == "ck"
    assert s.moph_secret_key == "sk"


def test_blank_or_bad_values_fall_back_to_defaults():
    s = load_settings({
        "DENGUE_POLL_SECONDS": "",
        "DENGUE_CLUSTER_MIN_CASES": "abc",
        "DENGUE_CLUSTER_RADIUS_M": "  ",
        "DENGUE_CLUSTER_DAYS": "1.5",
        "DENGUE_REALERT_DAYS": "",
        "DENGUE_ICDS": "",
        "DENGUE_START_FROM": "not-a-date",
        "DENGUE_LAN_PREFIXES": "",
    })
    assert s.poll_seconds == 120
    assert s.cluster_min_cases == 2
    assert s.cluster_radius_m == 100
    assert s.cluster_days == 14
    assert s.realert_days == 21
    assert s.icds == ("A90", "A91")
    assert s.start_from == date.today()
    assert "10." in s.lan_prefixes and "172.2" not in s.lan_prefixes


def test_start_from_note_normal_values():
    from datetime import timedelta
    today = date.today()
    # ยังไม่ตั้ง (ไม่มี/ว่าง) = "unset": ใช้วันนี้ทุกครั้งที่เริ่มระบบ (setup_check เตือน)
    assert load_settings({}).start_from_note == "unset"
    assert load_settings({"DENGUE_START_FROM": "   "}).start_from_note == "unset"
    assert load_settings({"DENGUE_START_FROM": "2026-10-01"}).start_from_note == ""
    soon = (today + timedelta(days=7)).isoformat()      # พอดี 7 วัน = ยังไม่ผิดปกติ
    s = load_settings({"DENGUE_START_FROM": soon})
    assert s.start_from_note == "" and s.start_from == today + timedelta(days=7)


def test_start_from_note_invalid_falls_back_to_today():
    for bad in ("not-a-date", "15/11/2026", "2026-13-40"):
        s = load_settings({"DENGUE_START_FROM": bad})
        assert s.start_from_note == "invalid" and s.start_from == date.today()


def test_start_from_note_future_keeps_the_parsed_date():
    from datetime import timedelta
    far = date.today() + timedelta(days=8)
    s = load_settings({"DENGUE_START_FROM": far.isoformat()})
    assert s.start_from_note == "future" and s.start_from == far
    s = load_settings({"DENGUE_START_FROM": "2569-11-15"})   # ปี พ.ศ. ที่พิมพ์ผิด
    assert s.start_from_note == "future" and s.start_from == date(2569, 11, 15)


def test_cancel_check_days_default_override_and_fallback():
    assert load_settings({}).cancel_check_days == 90
    assert load_settings({"DENGUE_CANCEL_CHECK_DAYS": "30"}).cancel_check_days == 30
    assert load_settings({"DENGUE_CANCEL_CHECK_DAYS": ""}).cancel_check_days == 90
    assert load_settings({"DENGUE_CANCEL_CHECK_DAYS": "abc"}).cancel_check_days == 90
