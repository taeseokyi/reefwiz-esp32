import { chromium } from "playwright-core";
const br = await chromium.launch({ executablePath: "/usr/bin/google-chrome", args:["--no-sandbox"] });
const p = await br.newPage({ viewport: { width: 420, height: 900 } });
p.on("dialog", d => d.accept());
// 페이지가 받는 상태 응답을 그대로 계측한다
await p.exposeFunction("logscan", (t, txt) => console.log(t, txt));
await p.goto("about:blank");
await p.goto("http://192.168.0.47/ops.html", { waitUntil: "networkidle" });
await p.evaluate(() => {
  const orig = window.fetch;
  window.fetch = async (...a) => {
    const r = await orig(...a);
    if (String(a[0]).includes("/api/ops/status")) {
      const c = r.clone();
      c.text().then(t => {
        let v = "PARSE-FAIL(len " + t.length + ")";
        try { const j = JSON.parse(t); v = "running=" + (j.scan && j.scan.running) + " passes=" + (j.scan && j.scan.passes); } catch (e) {}
        window.logscan(new Date().toISOString().slice(11,19), v);
      });
    }
    return r;
  };
});
await p.waitForTimeout(1500);
await p.locator("#btAddrSummary").click().catch(()=>{});
await p.locator("#scanBtBtn").click();
await p.waitForTimeout(75000);
console.log("btmsg:", (await p.locator("#btmsg").innerText()).trim().slice(0,60));
await p.locator("#scanStopBtn").click().catch(()=>{});
await br.close();
