import pytest
import requests

import moph_notify
from moph_notify import NotifyError, send_messages

BASE = "https://notify.example.test"
CK = "CLIENT-KEY-TEST"
SK = "SECRET-KEY-TEST"
MSGS = [{"type": "text", "text": "ทดสอบ"}]


class FakeResp:
    def __init__(self, body=None, status_code=200, text=None):
        self._body = body
        self.status_code = status_code
        self.text = text if text is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _patch_post(monkeypatch, resp=None, exc=None):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, allow_redirects=True):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout,
                      "allow_redirects": allow_redirects})
        if exc is not None:
            raise exc
        return resp

    monkeypatch.setattr(moph_notify.requests, "post", fake_post)
    return calls


def test_send_messages_posts_keys_and_returns_body(monkeypatch):
    body = {"message_code": 200, "message": "success"}
    calls = _patch_post(monkeypatch, FakeResp(body))
    out = send_messages(MSGS, BASE + "/", CK, SK)
    assert out == body
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == BASE + "/api/notify/send"
    assert c["headers"]["client-key"] == CK
    assert c["headers"]["secret-key"] == SK
    assert c["json"] == {"messages": MSGS}
    assert c["timeout"] == 15
    assert c["allow_redirects"] is False  # ไม่ส่งคีย์ต่อไปยังปลายทาง redirect


def test_http_200_with_error_body_raises(monkeypatch):
    _patch_post(monkeypatch, FakeResp({"status": 401, "message": "Unauthorized"}))
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    assert "client-key" in str(ei.value)  # คำอธิบายไทยจาก ERROR_HINTS
    assert "401" in str(ei.value)


def test_message_code_not_200_raises(monkeypatch):
    _patch_post(monkeypatch, FakeResp({"message_code": 500, "message": "Send messages error"}))
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    assert "หมอพร้อม" in str(ei.value)


def test_missing_code_keys_raises(monkeypatch):
    _patch_post(monkeypatch, FakeResp({"message": "hello"}))
    with pytest.raises(NotifyError):
        send_messages(MSGS, BASE, CK, SK)


def test_non_json_body_raises(monkeypatch):
    _patch_post(monkeypatch, FakeResp(None, status_code=502, text="<html>bad gateway</html>"))
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    assert "502" in str(ei.value)


def test_non_dict_json_raises(monkeypatch):
    _patch_post(monkeypatch, FakeResp(["x"]))
    with pytest.raises(NotifyError):
        send_messages(MSGS, BASE, CK, SK)


def test_network_exception_wrapped_without_keys(monkeypatch):
    _patch_post(monkeypatch, exc=requests.ConnectionError(f"boom {CK} {SK}"))
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    text = str(ei.value)
    assert CK not in text and SK not in text
    assert "เชื่อมต่อ" in text


def test_timeout_wrapped(monkeypatch):
    _patch_post(monkeypatch, exc=requests.Timeout("slow"))
    with pytest.raises(NotifyError):
        send_messages(MSGS, BASE, CK, SK)


def test_missing_config_raises_without_calling_network(monkeypatch):
    calls = _patch_post(monkeypatch, FakeResp({"message_code": 200}))
    with pytest.raises(NotifyError):
        send_messages(MSGS, "", CK, SK)
    with pytest.raises(NotifyError):
        send_messages(MSGS, BASE, "", SK)
    with pytest.raises(NotifyError):
        send_messages(MSGS, BASE, CK, "")
    assert calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_error_and_keys_not_leaked(monkeypatch, status):
    resp = FakeResp({"message_code": 200}, status_code=status)  # แม้ body ดูสำเร็จก็ต้องถือเป็น error
    _patch_post(monkeypatch, resp)
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    text = str(ei.value)
    assert str(status) in text and "เปลี่ยนเส้นทาง" in text
    assert CK not in text and SK not in text


def test_error_text_without_api_message_has_no_leading_dash(monkeypatch):
    _patch_post(monkeypatch, FakeResp({"status": 500}))
    with pytest.raises(NotifyError) as ei:
        send_messages(MSGS, BASE, CK, SK)
    assert str(ei.value) == "ไม่ทราบสาเหตุ (code 500)"
