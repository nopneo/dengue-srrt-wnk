// ตัวรันเทสต์ตรรกะของหน้า /settings (static/settings_logic.js) ด้วย node: อ่านนิพจน์ JS จาก stdin
// (ตัวแปร L = โมดูลตรรกะ) แล้วพิมพ์ผลเป็น JSON — ไม่มี DOM ใช้ออบเจ็กต์ธรรมดา
const path = require("path");
const L = require(path.join(__dirname, "..", "static", "settings_logic.js"));
let src = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (c) => { src += c; });
process.stdin.on("end", () => {
  const out = new Function("L", "return (" + src + ");")(L);
  process.stdout.write(JSON.stringify(out === undefined ? null : out));
});
