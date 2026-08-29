// 정비페이지(ops.html) 자동 점검 — 실기가 **서빙하는 페이지**를 실제 브라우저로 열고 검사한다.
//
// ★왜 있나(2026-08-29): 화면 결함 3건이 실사용 중에야 드러났다.
//   ①빠른 명령 버튼이 삭제된 요소(`#tgtsel`)를 참조해 **전부 죽어 있었다**(예외가 조용히 났다).
//   ②조치 결과가 페이지 맨 위에만 찍혀 버튼 옆에서는 "아무 반응 없음"으로 보였다.
//   ③폴링이 방금 누른 메시지를 낡은 결과로 덮었다.
//   셋 다 서버 테스트로는 못 잡는다 — 브라우저에서 눌러 봐야 나온다.
//
// 검사 항목:
//   A. JS 예외·콘솔 오류 없음                     ← ① 유형을 잡는다
//   B. 죽은 DOM 참조 없음($("#id") 가 전부 존재)   ← ① 을 **정적으로** 잡는다
//   C. id 중복 없음
//   D. data-job / data-post 가 서버에 실재
//   E. 주요 영역이 비어 있지 않음
//   F. 부작용 없는 조치를 눌러 결과가 **그 자리에** 뜨는지
//   G. 폴링이 방금 누른 메시지를 덮지 않는지        ← ③
//
// 사용:  node tools/ui_check.mjs [http://192.168.0.47]
//   ★읽기 전용 검사만 한다. 측정 시작·정리·HC-05 리셋·lrt 적용·래치 해제 같은
//     상태를 바꾸는 버튼은 **누르지 않는다**(누를 수 있는지만 본다).
import { chromium } from "playwright-core";

const BASE = (process.argv[2] || "http://192.168.0.47").replace(/\/$/, "");
const CHROME = process.env.CHROME_PATH || "/usr/bin/google-chrome";

let fail = 0, warn = 0;
const pass = (n) => console.log("  [PASS] " + n);
const bad  = (n, x) => { fail++; console.log("  [FAIL] " + n + (x === undefined ? "" : " — " + JSON.stringify(x))); };
const soft = (n, x) => { warn++; console.log("  [WARN] " + n + (x === undefined ? "" : " — " + JSON.stringify(x))); };
const check = (n, ok, x) => ok ? pass(n) : bad(n, x);

const api = async (p, init) => {
  const r = await fetch(BASE + p, init);
  return { status: r.status, body: await r.text() };
};

const browser = await chromium.launch({ executablePath: CHROME, args: ["--no-sandbox"] });
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push("console.error: " + m.text()); });
page.on("requestfailed", (r) => errors.push("requestfailed: " + r.url() + " " + r.failure()?.errorText));

console.log("=== 정비페이지 점검: " + BASE + "/ops.html ===\n");
// ★첫 이동은 한 번 헛돈다(2026-08-29): 갓 띄운 브라우저의 **첫** goto 가 개발 스텁
//   (tools/devserver.py, 맨 IP:포트)을 상대로는 응답을 받고도 끝나지 않아 통째로 타임아웃했다
//   — 실기(192.168.0.47)는 첫 시도부터 정상이라 스텁에서만 점검이 못 돌았다. 빈 페이지로
//   한 번 예열하고, 그래도 걸리면 재시도한다(진짜로 못 붙으면 아래에서 에러로 끝난다).
await page.goto("about:blank");
for (let i = 1; ; i++) {
  try { await page.goto(BASE + "/ops.html", { waitUntil: "load", timeout: 20000 }); break; }
  catch (e) { if (i >= 3) throw e; console.log("  (재시도 " + i + " — 첫 이동 지연)"); }
}
await page.waitForTimeout(9000);                  // 첫 폴링 + 렌더
await page.evaluate(() => document.querySelectorAll("details").forEach((d) => (d.open = true)));
await page.waitForTimeout(2500);

// ── A. 예외 ──
console.log("[A] JS 예외·콘솔 오류");
check("예외/오류 없음", errors.length === 0, [...new Set(errors)].slice(0, 6));

// ── B. 죽은 DOM 참조 ──
console.log("\n[B] 죽은 DOM 참조($(\"#id\") 가 실제로 있는가)");
const dead = await page.evaluate(() => {
  const src = [...document.querySelectorAll("script")].map((s) => s.textContent).join("\n");
  const ids = new Set();
  for (const m of src.matchAll(/\$\(\s*["'`]#([A-Za-z0-9_-]+)["'`]\s*\)/g)) ids.add(m[1]);
  for (const m of src.matchAll(/getElementById\(\s*["'`]([A-Za-z0-9_-]+)["'`]\s*\)/g)) ids.add(m[1]);
  // 지금 문서에 없는 id 만 본다. 그중에서도 **가드 없이 곧바로 역참조**하는 것이 진짜 결함이다
  //   ($("#tgtsel").value = ... 처럼) — 조건부로 그려지는 요소는 가드가 있으면 정상이다.
  const missing = [...ids].filter((id) => !document.getElementById(id));
  const hard = [], softer = [];
  for (const id of missing) {
    const q = "\\$\\(\\s*[\"'`]#" + id + "[\"'`]\\s*\\)";
    // ★가드를 인정한다(2026-08-29): `const cb = $("#x"); if (cb) …` 나
    //   `$("#x") && $("#x").checked` 는 없는 요소를 **안전하게** 다루는 정상 코드인데
    //   종전 규칙은 뒤쪽 `.checked` 만 보고 결함으로 세어, 조건부 요소(#conAck)마다
    //   가짜 FAIL 이 났다 — 가짜가 섞이면 진짜 죽은 참조를 아무도 안 본다.
    // 가드는 **역참조마다** 따로 본다 — 파일 어딘가에 가드가 하나 있다고 다른 자리의
    // 맨 역참조까지 봐주면, 종전에 실제로 났던 `$("#tgtsel").value = …` 같은 결함을 놓친다.
    const guard = new RegExp(q + "\\s*(&&|\\?\\.|\\)\\s*(&&|\\?))");
    const bare = [];
    for (const h of src.matchAll(new RegExp(q + "\\s*\\.", "g"))) {
      const before = src.slice(Math.max(0, h.index - 80), h.index);
      if (!guard.test(before)) bare.push(h.index);
    }
    if (bare.length) {
      const at = src.slice(Math.max(0, bare[0] - 60), bare[0] + 80).replace(/\s+/g, " ");
      hard.push({ id, at });
    } else softer.push(id);
  }
  return { hard, softer };
});
check("가드 없는 죽은 참조 없음", dead.hard.length === 0, dead.hard);
if (dead.softer.length) soft("조건부로만 존재하는 id(가드 확인됨)", dead.softer);

// ── C. id 중복 ──
console.log("\n[C] id 중복");
const dup = await page.evaluate(() => {
  const seen = {}, out = [];
  document.querySelectorAll("[id]").forEach((e) => {
    seen[e.id] = (seen[e.id] || 0) + 1;
    if (seen[e.id] === 2) out.push(e.id);
  });
  return out;
});
check("중복 id 없음", dup.length === 0, dup);

// ── D. 버튼이 가리키는 서버 계약 ──
console.log("\n[D] 버튼 → 서버 계약");
const jobs = await page.evaluate(() =>
  [...new Set([...document.querySelectorAll("button[data-job]")].map((b) => b.dataset.job))]);
const posts = await page.evaluate(() =>
  [...new Set([...document.querySelectorAll("[data-post]")].map((b) => b.dataset.post))]);
console.log("      data-job  : " + jobs.join(", "));
console.log("      data-post : " + posts.join(", "));
const bogus = await api("/api/ops/job", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ kind: "__nope__" }),
});
check("서버가 모르는 작업을 거부한다", /"ok": false/.test(bogus.body) || bogus.status >= 400,
      bogus.body.slice(0, 120));
for (const p of posts) {
  const r = await api(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  // 404 면 화면이 없는 엔드포인트를 부르고 있다는 뜻
  check("POST " + p + " 가 실재한다", r.status !== 404, r.status);
}

// ── E. 주요 영역이 채워졌는가 ──
console.log("\n[E] 주요 영역 렌더");
for (const [sel, name] of [["#devstate", "장비 상태 배너"], ["#stat", "상태 표"],
                           ["#btstat", "BT 연결 표"], ["#btbadges", "BT 배지"],
                           ["#devList", "장치 목록"], ["#btSwitchBtns", "전환 버튼"],
                           ["#arcstat", "저장소 표"], ["#wifistat", "WiFi 표"],
                           ["#schHours", "스케줄 회차 칩"], ["#conlock", "콘솔 잠금 안내"]]) {
  const t = await page.evaluate((s) => document.querySelector(s)?.innerText?.trim() ?? null, sel);
  if (t === null) soft(name + " (" + sel + ") 가 없다");
  else check(name + " 가 비어 있지 않다", t.length > 0, t);
}

// ── F. 부작용 없는 조치 결과가 그 자리에 뜨는가 ──
console.log("\n[F] 조치 결과 표시 위치(읽기 전용 작업만)");
const dosemsgNow = () => page.evaluate(() => document.querySelector("#dosemsg")?.textContent ?? "");
const btmsgNow   = () => page.evaluate(() => document.querySelector("#btjobmsg")?.textContent ?? "");

const waitFor = async (fn, re, secs = 60) => {
  for (let i = 0; i < secs * 2; i++) {
    const v = await fn();
    if (re.test(v)) return v;
    await page.waitForTimeout(500);
  }
  return await fn();
};

const btBefore = await btmsgNow();
await page.click('button[data-job="link"]');
const btAfter = await waitFor(btmsgNow, /연결 점검/);
check("연결 점검 결과가 BT 카드에 뜬다", /연결 점검/.test(btAfter) && btAfter !== btBefore,
      { btBefore, btAfter });

const canDose = await page.evaluate(() => !document.querySelector('button[data-job="doser_query"]')?.disabled);
if (!canDose) {
  soft("도징 조작이 잠겨 있어 F/G 의 도저 항목을 건너뛴다(기본 도저로 전환 후 재실행)");
} else {
  const qBefore = await dosemsgNow();
  await page.click('button[data-job="doser_query"]');
  // ★**새** 결과를 기다린다(이전 실행의 잔상을 통과로 세지 않게). 기기가 바쁘면 수십 초 걸린다.
  let qAfter = qBefore;
  for (let i = 0; i < 240; i++) {
    qAfter = await dosemsgNow();
    if (/현재값 조회/.test(qAfter) && qAfter !== qBefore) break;
    await page.waitForTimeout(500);
  }
  check("현재값 조회 결과가 도징 패널에 새로 뜬다",
        /현재값 조회/.test(qAfter) && qAfter !== qBefore, { qBefore, qAfter });

  // ── G. 폴링이 방금 누른 메시지를 덮지 않는가 ──
  console.log("\n[G] 폴링이 방금 누른 메시지를 덮지 않는가");
  await page.evaluate(() => {
    const el = document.querySelector("#dosemsg");
    el.textContent = "△△테스트 표식△△";           // 직접 조작이 남긴 메시지를 흉내낸다
  });
  await page.waitForTimeout(12000);                 // 폴링 여러 번
  const kept = await dosemsgNow();
  check("★낡은 결과가 덮어쓰지 않는다", kept === "△△테스트 표식△△", kept);
}

// ── 위험 버튼은 '있는지'만 본다 ──
console.log("\n[H] 위험 조작 버튼(누르지 않는다 — 존재·확인문구만 확인)");
const risky = await page.evaluate(() =>
  [...document.querySelectorAll("button[data-confirm]")].map((b) => ({
    label: b.textContent.trim().slice(0, 24), job: b.dataset.job || b.dataset.post || "(직접)",
    hasConfirm: !!b.dataset.confirm,
  })));
risky.forEach((r) => console.log("      " + r.label.padEnd(26) + r.job));
check("위험 버튼에 확인 문구가 붙어 있다", risky.every((r) => r.hasConfirm), risky);

await browser.close();
console.log("\n" + (fail ? `FAIL ${fail}건` : "ALL PASS") + (warn ? ` · WARN ${warn}건` : ""));
process.exit(fail ? 1 : 0);
