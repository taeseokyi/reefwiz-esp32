import { chromium } from "playwright-core";
const br = await chromium.launch({ executablePath: "/usr/bin/google-chrome", args:["--no-sandbox"] });
const p = await br.newPage({ viewport: { width: 420, height: 900 } });
p.on("dialog", d => d.accept());
await p.goto("about:blank");
await p.goto("http://192.168.0.47/ops.html", { waitUntil: "networkidle" });
await p.waitForTimeout(2000);
await p.locator("#btAddrSummary").click().catch(()=>{});
await p.selectOption("#scanSecs", "120").catch(()=>{});
await p.locator("#scanBtBtn").click();
const t0 = Date.now();
let last = "";
for (let i = 0; i < 34; i++) {
  await p.waitForTimeout(5000);
  const sec = Math.round((Date.now()-t0)/1000);
  const on = await p.locator("#scanStopBtn").isVisible();
  const m = (await p.locator("#btmsg").innerText()).trim();
  const head = (await p.locator("#scanout").innerText()).split("\n")[0];
  if (head !== last) { console.log(`${sec}s on=${on} | ${head}`); last = head; }
  if (!on && m) { console.log(`>> 종료 ${sec}s · btmsg: ${m.slice(0,70)}`); break; }
}
await br.close();
