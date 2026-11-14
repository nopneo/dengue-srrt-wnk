/* ตรรกะล้วน ๆ ของหน้า /settings (ไม่แตะ DOM — ทดสอบด้วย node จาก tests/test_settings_page.py)
   - collect: ช่องไหนถูกส่ง (เฉพาะที่ต่างจากค่าปัจจุบัน + ความลับไม่ว่าง + ล้างค่า; ช่องที่ source เป็น "default" =
     ค่าเริ่มต้นที่ยังไม่ได้ตั้ง จึงไม่เทียบกับค่าที่แสดง: ที่ผู้ใช้แตะ/ส่งมาคือการตั้งค่าจริงเสมอ)
   - needsDbPassword: เปลี่ยนโฮสต์/พอร์ต/ผู้ใช้ ต้องกรอกรหัสผ่านฐานข้อมูลใหม่ (เซิร์ฟเวอร์ตรวจซ้ำ)
   - describeFailure: ผลที่ไม่สำเร็จของ API -> ข้อความไทย + การกระทำที่ UI ต้องทำ (ข้อความจากเซิร์ฟเวอร์ใช้เฉพาะ `message` คงที่)
   - summarizeTest / isRestarted / confirmSendText
   โหลดก่อน settings.js; ในเบราว์เซอร์อยู่ที่ window.SettingsLogic, ใน node ใช้ module.exports */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module !== "undefined" && module.exports) { module.exports = api; }
  if (root) { root.SettingsLogic = api; }
})(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  var M = {
    noChange: "ไม่มีค่าที่เปลี่ยน",
    needDbPass: "เปลี่ยนโฮสต์/พอร์ต/ผู้ใช้ ต้องกรอกรหัสผ่านฐานข้อมูลใหม่",
    needPassword: "กรอกรหัสผ่านของคุณ (ยืนยันการแก้ไข) ก่อน",
    conflict: "ใส่ค่าใหม่และสั่งล้างค่าช่องเดียวกันพร้อมกันไม่ได้",
    changedByOther: "ค่าถูกเปลี่ยนโดยผู้ดูแลคนอื่นระหว่างบันทึก โหลดหน้าใหม่แล้วลองอีกครั้ง",
    wrongPassword: "รหัสผ่านของคุณไม่ถูกต้อง",
    network: "เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง",
    timeout: "เซิร์ฟเวอร์ตอบช้าเกินกำหนด (รอเกิน 40 วินาที) — ผลอาจยังไม่ชัดเจน รีเฟรชหน้านี้เพื่อตรวจค่าล่าสุด",
    session: "เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่",
    badRequest: "คำขอไม่ถูกต้อง โหลดหน้านี้ใหม่แล้วลองอีกครั้ง",
    csrf: "เซิร์ฟเวอร์ปฏิเสธคำขอ (ตรวจสอบต้นทางไม่ผ่าน) โหลดหน้านี้ใหม่แล้วลองอีกครั้ง",
    forbidden: "ไม่มีสิทธิ์ใช้งานส่วนนี้ (ต้องเป็นผู้ดูแลที่เปิดจากเครือข่ายภายใน รพ.)",
    noPrev: "ไม่มีค่าก่อนหน้าให้ย้อนกลับ",
    tooLarge: "คำขอใหญ่เกินกำหนด ไม่ได้ประมวลผล",
    unsupported: "ส่งข้อมูลผิดรูปแบบ (ต้องเป็น JSON) โหลดหน้านี้ใหม่แล้วลองอีกครั้ง",
    rateLimited: "ทำรายการถี่เกินไป กรุณารอสักครู่แล้วลองใหม่",
    throttled: "ใส่รหัสผ่านผิดหลายครั้ง บัญชีถูกล็อกชั่วคราว กรุณารอสักครู่แล้วลองใหม่",
    server: "เกิดข้อผิดพลาดภายในระบบ ไม่ได้บันทึกค่า กรุณาลองใหม่ หรือแจ้งผู้ดูแลระบบ",
    invalid: "ค่าที่กรอกไม่ถูกต้อง",
    untested: "ทดสอบฐานข้อมูลไม่ผ่าน จึงยังไม่ได้บันทึก — แก้ค่าแล้วลองใหม่ หรือติ๊ก \"บันทึกแม้ทดสอบไม่ผ่าน\" " +
      "แล้วกรอกรหัสผ่านของคุณอีกครั้งและกดบันทึก",
    unexpected: "เกิดข้อผิดพลาดที่ไม่คาดคิด",
    testOk: "ทดสอบผ่านทุกขั้นตอน",
    testWarn: "ทดสอบผ่าน (มีคำเตือน) — ดูรายละเอียดด้านล่างปุ่มทดสอบ",
    testFail: "ทดสอบไม่ผ่านบางขั้นตอน — ดูรายละเอียดด้านล่างปุ่มทดสอบ",
    restarting: "กำลังเริ่มระบบใหม่…",
    restartOk: "เริ่มระบบใหม่แล้ว กำลังโหลดหน้าใหม่…",
    restartManual: "ต้องรีสตาร์ตบริการเอง (ไม่ได้ทำงานภายใต้ gunicorn)",
    restartTimeout: "ระบบยังไม่เริ่มใหม่ — ลองรีเฟรชหน้านี้หรือแจ้งผู้ดูแลเซิร์ฟเวอร์",
    renderAfterSave: "บันทึกแล้ว แต่แสดงผลไม่สำเร็จ — รีเฟรชหน้า",
    render: "แสดงผลไม่สำเร็จ — รีเฟรชหน้านี้"
  };

  function hasKey(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }

  // ---- พอร์ต: เหมือน wiring._port (int(strip) ไม่งั้น 3306) เฉพาะรูปตัวเลขที่ Python int() รับ ----
  var NUMERIC = /^[+-]?[0-9]+(?:_[0-9]+)*$/;

  function portNorm(s) {
    var t = String(s === null || s === undefined ? "" : s).trim();
    return NUMERIC.test(t) ? String(parseInt(t.replace(/_/g, ""), 10)) : "3306";
  }

  function original(items, key) {
    var v = items[key] ? items[key].value : null;
    return v === null || v === undefined ? "" : String(v);
  }

  function differs(items, key, v) {
    var o = original(items, key);
    if (v === o) { return false; }
    if (key === "DB_PORT" && NUMERIC.test(v)) { return portNorm(v) !== portNorm(o); }
    return true;   // รวมพอร์ตว่าง/ไม่ใช่ตัวเลข: ส่งไปให้เซิร์ฟเวอร์ตรวจและแจ้ง
  }

  /* items: {key: {secret, value, source?}}, raw: {key: ข้อความในช่อง}, clears: {key: bool}, keys: ช่องที่พิจารณา,
     touched: {key: bool} ช่องที่ผู้ใช้แตะ (ไม่ระบุ = ถือว่าส่งทุกช่อง) -> {values, clear}
     ช่อง source "default" (ยังไม่เคยตั้ง): ไม่เทียบกับค่าที่แสดง — ที่ถูกแตะและไม่ว่างส่งเสมอ (ตั้ง go-live = วันนี้ / DATA_AUDITED 0
     ได้จริง); ไม่ได้แตะหรือว่าง = ไม่ส่ง (บันทึกช่องอื่นจึงไม่ตรึงค่าเริ่มต้นโดยไม่ตั้งใจ) */
  function collect(items, raw, clears, keys, touched) {
    var values = {}, clear = [];
    keys.forEach(function (key) {
      var item = items[key];
      if (!item || !hasKey(raw, key)) { return; }
      var v = String(raw[key]);
      if (item.secret) {
        if (clears && clears[key]) { clear.push(key); }
        // ช่องลับว่าง = ใช้ค่าเดิม; DB_PASS เก็บตรงตามที่พิมพ์ (เว้นวรรคหัวท้ายเป็นส่วนของรหัส) ตัวอื่นเว้นวรรคล้วน = ว่าง
        if (v !== "" && (key === "DB_PASS" || v.trim() !== "")) { values[key] = v; }
      } else {
        v = v.trim();
        if (item.source === "default") {
          if (v !== "" && (!touched || touched[key] === true)) { values[key] = v; }
        } else if (differs(items, key, v)) {
          values[key] = v;
        }
      }
    });
    return { values: values, clear: clear };
  }

  function needsDbPassword(values) {
    return (hasKey(values, "DB_HOST") || hasKey(values, "DB_PORT") || hasKey(values, "DB_USER")) &&
      !hasKey(values, "DB_PASS");
  }

  function findConflict(c) {
    for (var i = 0; i < c.clear.length; i++) {
      if (hasKey(c.values, c.clear[i])) { return c.clear[i]; }
    }
    return null;
  }

  function serverMessage(body, fallback) {
    var m = body && body.message;
    return (typeof m === "string" && m.length > 0 && m.length <= 400) ? m : fallback;
  }

  /* r = {network?, timeout?, status, body}; known = ชื่อช่องที่มีในฟอร์ม
     -> {message, field, action, steps}
     action: "none" | "login" (แสดงลิงก์เข้าสู่ระบบ) | "offer_force" (แสดงขั้นตอน + ช่อง force) |
             "hide_revert" | "reload" (แสดงปุ่มโหลดหน้าใหม่; เช่น หมดเวลา และ 409 conflict) */
  function describeFailure(r, known) {
    var out = { message: M.unexpected, field: null, action: "none", steps: null };
    var body = (r && r.body && typeof r.body === "object") ? r.body : {};
    var code = typeof body.error === "string" ? body.error : "";
    if (r && r.network) { out.message = M.network; return out; }
    if (r && r.timeout) { out.message = M.timeout; out.action = "reload"; return out; }
    var status = r ? r.status : 0;
    switch (status) {
      case 400:
        if (code === "invalid") {
          out.message = serverMessage(body, M.invalid);
          if (typeof body.key === "string" && (known || []).indexOf(body.key) >= 0) { out.field = body.key; }
        } else if (code === "password_required") {
          out.message = M.needDbPass;
          out.field = "DB_PASS";
        } else {   // "bad_request" และตัวที่ไม่ใช่ JSON
          out.message = M.badRequest;
        }
        return out;
      case 401:
        out.message = M.session;
        out.action = "login";
        return out;
      case 403:
        if (code === "password") { out.message = M.wrongPassword; out.field = "confirm"; }
        else if (code === "csrf") { out.message = M.csrf; }
        else { out.message = M.forbidden; }
        return out;
      case 409:
        if (code === "untested") {
          out.message = M.untested;
          out.action = "offer_force";
          out.steps = Array.isArray(body.steps) ? body.steps : null;
        } else if (code === "no_prev") {
          out.message = M.noPrev;
          out.action = "hide_revert";
        } else if (code === "conflict") {   // ผู้ดูแลอีกคนบันทึกแทรกระหว่างตรวจกับเขียน: ไม่เขียนอะไร
          out.message = M.changedByOther;
          out.action = "reload";
        } else {
          out.message = M.badRequest;
        }
        return out;
      case 413:   // "too_large"
        out.message = M.tooLarge;
        return out;
      case 415:
        out.message = M.unsupported;
        return out;
      case 429:
        out.message = serverMessage(body, code === "throttled" ? M.throttled : M.rateLimited);   // "rate_limited" | "throttled"
        return out;
      case 500:   // "server"
        out.message = serverMessage(body, M.server);
        return out;
      default:
        out.message = M.unexpected + " (HTTP " + status + ")";
        return out;
    }
  }

  /* ผลทดสอบ 200 -> {kind: "ok"|"warn"|"fail", message} */
  function summarizeTest(ok, steps) {
    if (!ok) { return { kind: "fail", message: M.testFail }; }
    var warned = (Array.isArray(steps) ? steps : []).some(function (s) { return s && s.status === "warn"; });
    return warned ? { kind: "warn", message: M.testWarn } : { kind: "ok", message: M.testOk };
  }

  /* เริ่มระบบใหม่แล้ว = /healthz ตอบ JSON ที่มี boot_id ไม่ว่างและต่างจากตอนโหลดหน้า (ไม่ดู ok) */
  function isRestarted(pageBootId, health) {
    if (typeof pageBootId !== "string" || pageBootId === "") { return false; }
    if (!health || typeof health !== "object" || Array.isArray(health)) { return false; }
    return typeof health.boot_id === "string" && health.boot_id !== "" && health.boot_id !== pageBootId;
  }

  function confirmSendText(typedKeys) {
    return "ส่งข้อความทดสอบเข้ากลุ่ม LINE จริง 1 ข้อความ?\n" +
      (typedKeys ? "(ใช้กุญแจที่กรอกไว้ ยังไม่ได้บันทึก)" : "(ใช้กุญแจที่ตั้งไว้)");
  }

  return {
    M: M, portNorm: portNorm, collect: collect, needsDbPassword: needsDbPassword, findConflict: findConflict,
    describeFailure: describeFailure, summarizeTest: summarizeTest, isRestarted: isRestarted,
    confirmSendText: confirmSendText
  };
});
