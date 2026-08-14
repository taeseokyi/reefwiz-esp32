#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""디스플레이 시뮬레이터 — src/display.py(터치 UI)를 하드웨어 없이 눈으로 확인한다.

display.UI 가 그리는 모든 호출(fill/fill_rect/rect/text)을 기록하는 SVG 프레임버퍼를
드라이버 자리에 꽂고, 실데이터(data/) 기반 ops.snapshot() 으로 화면을 렌더링해
www/display_sim.html 로 저장한다 → devserver 로 브라우저에서 본다.

렌더링하는 화면:
  · 320x240 (임시 참조 하드웨어, 배율 1) — 메인 / 로그 / 확인 대기 상태
  · 800x480 (교체 예정 대형 화면 예시, 배율 2) — 메인
  ※ display.py 는 무수정 — 해상도 자동 적응(배율=가로/320)이 실제로 동작함을 보여준다.

실행:  python3 tools/display_sim.py   →  http://localhost:8123/display_sim.html
"""
import os
import sys
import time as _real_time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from test_measure_sim import _install_shims  # noqa: E402 — machine/network/time 심 재사용

_install_shims()

ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

import config                                  # noqa: E402
config.DATA_DIR = DATA
config.KFONT_PATH = os.path.join(ROOT, "src", "kfont.bin")   # 한글 비트맵 폰트

import datalog                                 # noqa: E402
import ops                                     # noqa: E402
import display                                 # noqa: E402


def _c565(c):
    """RGB565 → #rrggbb."""
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    return "#%02x%02x%02x" % (r * 255 // 31, g * 255 // 63, b * 255 // 31)


class SVGFB:
    """display.py 의 드라이버 계약을 만족하는 기록형 프레임버퍼 — 그리기 호출을 SVG 로 축적."""

    def __init__(self, w, h):
        self.width = w
        self.height = h
        self.ops = []

    def fill(self, c):
        self.ops = ['<rect x="0" y="0" width="%d" height="%d" fill="%s"/>'
                    % (self.width, self.height, _c565(c))]

    def fill_rect(self, x, y, w, h, c):
        self.ops.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s"/>'
                        % (x, y, w, h, _c565(c)))

    def rect(self, x, y, w, h, c):
        self.ops.append('<rect x="%.1f" y="%.1f" width="%d" height="%d" fill="none" '
                        'stroke="%s" stroke-width="1"/>' % (x + 0.5, y + 0.5, w - 1, h - 1, _c565(c)))

    def _esc(self, s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def text_scaled(self, s, x, y, c, scale):
        if not s:
            return
        w = len(s) * 8 * scale
        self.ops.append(
            '<text x="%d" y="%d" font-family="Courier New,monospace" font-size="%d" '
            'fill="%s" textLength="%d" lengthAdjust="spacingAndGlyphs">%s</text>'
            % (x, y + 7 * scale, 8 * scale, _c565(c), w, self._esc(s)))

    def text(self, s, x, y, c):
        self.text_scaled(s, x, y, c, 1)

    def show(self):
        pass

    def svg(self, scale=2):
        return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" '
                'xmlns="http://www.w3.org/2000/svg" style="image-rendering:pixelated">%s</svg>'
                % (self.width * scale, self.height * scale, self.width, self.height,
                   "".join(self.ops)))


def render(w, h, page="main", armed_label=None, msg=None, snap=None):
    fb = SVGFB(w, h)
    ui = display.UI(fb, touch=None)
    ui.page = page
    if msg:
        ui.msg = msg
        ui.msg_until = _real_time.time() + 60
    if armed_label:                            # 파괴적 조치 버튼의 '확인?' 상태 재현
        for b in ui.buttons:
            if b.label == armed_label:
                ui.armed = b
                ui.armed_until = _real_time.time() + 60
    ui.draw(snap or ops.snapshot())
    return fb


def main():
    # 로그 화면용 — 실제 measure_kh.log 가 없으므로 plateau 실데이터로 합성해 임시 파일에
    import json
    import tempfile
    log_path = os.path.join(tempfile.gettempdir(), "reefwiz_sim_measure.log")
    with open(os.path.join(DATA, "plateau.jsonl"), encoding="utf-8") as f:
        runs = [json.loads(ln) for ln in f if ln.strip()]
    with open(log_path, "w", encoding="utf-8") as f:
        run = runs[-1]
        f.write("===== AquaWiz KH V4 %s [%s] =====\n" % (run["run_started"], run["mode"]))
        for ph in ("tank", "ref"):
            for r in run.get(ph) or []:
                f.write("[%s] %d회 pH:%.3f (%ds)\n" % (ph, r["n"], r["ph"], r["elapsed"]))
            if run.get(ph + "_flat_n"):
                f.write("[평탄] %s %d회 → 평형\n" % (ph, run[ph + "_flat_n"]))
    datalog.LOG_FILE = log_path

    snap = ops.snapshot()
    shots = [
        ("320×240 — 메인 (임시 참조 하드웨어, 배율 1)", render(320, 240, "main"), 2),
        ("320×240 — 오터치 방지 확인 대기 (측정 정리 1탭 후)", render(320, 240, "main",
             armed_label="측정 정리", msg="측정 정리: 5초 안에 한 번 더 누르면 실행"), 2),
        ("320×240 — 로그 화면 (로그 버튼)", render(320, 240, "log"), 2),
        ("800×480 — 메인 (교체 예정 대형 화면 예시, 자동 배율 2)", render(800, 480, "main"), 1),
    ]
    cards = "".join(
        '<figure><figcaption>%s</figcaption>%s</figure>' % (title, fb.svg(scale))
        for title, fb, scale in shots)
    html = ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<title>reefwiz 디스플레이 시뮬레이터</title><style>"
            "body{background:#20242b;color:#dde;font:14px sans-serif;padding:20px}"
            "figure{display:inline-block;margin:0 24px 28px 0;vertical-align:top}"
            "figcaption{margin-bottom:8px;color:#9ab}"
            "svg{border:6px solid #000;border-radius:10px;box-shadow:0 8px 24px #0008}"
            "</style></head><body><h2>디스플레이 UI 미리보기 — display.py 실렌더링"
            "<small style=\"color:#9ab\"> (실데이터 · 무수정 코드 · 한글 비트맵 폰트 kfont.bin)"
            "</small></h2>"
            "<p style=\"color:#9ab\">버튼: 측정 / 측정 중단 / 측정 정리 / 래치해제 / "
            "BT 연결 점검 / 로그. 측정 정리·래치해제는 되돌리기 어려워 5초 내 두 번 탭해야 "
            "실행된다(오터치 방지 — 첫 탭에 '한번 더 탭' 표시). "
            "긴 줄은 화면 폭에 맞춰 자동 줄바꿈되어 잘리지 않는다.</p>"
            + cards + "</body></html>")
    out = os.path.join(ROOT, "www", "display_sim.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("saved:", out)
    print("스냅샷 기준 dKH:", (snap.get("latest") or {}).get("tank_kh"))


if __name__ == "__main__":
    main()
