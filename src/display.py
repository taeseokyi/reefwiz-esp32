# 장비 디스플레이 + 터치 UI — 화면에서 상태 확인과 조치를 할 수 있게 한다.
#
# ★하드웨어 교체 예정(사용자 2026-08-14: 더 고성능 보드 + 더 큰 화면). 드라이버는
#   config.DISPLAY_DRIVER 모듈로 분리했고, 레이아웃은 해상도에 자동 적응한다
#   (배율 = 가로폭/320). 드라이버가 없거나 import 실패면 NullDisplay 로 헤드리스 운전.
#
# ★한글 UI(사용자 지시 2026-08-14): 버튼·메시지 전부 한국어. 내장 8x8 폰트에 한글이
#   없으므로 자체 비트맵 폰트(kfont.bin — tools/gen_kfont 로 생성, 소스에 등장하는
#   글자만 수록)를 로드해 fill_rect 수평 런(run)으로 직접 그린다. kfont.bin 이 없으면
#   ASCII 폴백(한글은 '?')으로 내려가 시스템은 계속 동작한다.
#
# ★줄 잘림 금지(사용자 지시 2026-08-14): 긴 줄은 화면 폭에 맞춰 픽셀 단위로 자동
#   줄바꿈한다 — 화면 크기와 무관하게 내용이 잘리지 않는다(세로로 넘치면 이후 줄만 생략).
#
#   드라이버 계약 — display_driver.get_display() -> (fb, touch)
#     fb.width, fb.height, fill(c), fill_rect(x,y,w,h,c), rect(x,y,w,h,c), show()
#     (한글 렌더링은 fill_rect 만 쓰므로 드라이버는 텍스트 기능이 없어도 된다.
#      kfont 폴백용으로 text(s,x,y,c) 가 있으면 좋다.)
#     touch.get_touch() -> (x, y) | None   ※화면 좌표계
import time

import config
import ops
import state

# RGB565
BLACK = 0x0000
WHITE = 0xFFFF
GREY = 0x8410
DGREY = 0x2124
RED = 0xF800
GREEN = 0x07E0
YELLOW = 0xFFE0
BLUE = 0x001F
CYAN = 0x07FF
ORANGE = 0xFC00

CELL_H = 16          # kfont 글리프 셀 높이(px). ASCII 폭 8, 한글·기호 폭 16

# 액체 위치 표시 용어 통일(사용자 지시 2026-08-14): 측정 챔버·홀딩 챔버·위즈 수조.
# 내부 상태값(KCL/EMPTY/TANK/REF/UNKNOWN)은 원본 코드와의 정합을 위해 그대로 두고 표시만 번역.
LIQ_KO = {"KCL": "KCl", "EMPTY": "비움", "TANK": "수조수", "REF": "참조수",
          "UNKNOWN": "불명", None: "-"}


class KFont:
    """kfont.bin 로더 — 코드 정렬 배열을 이진 탐색(문자당 객체를 만들지 않아 힙 절약).
    형식: b'KF1' + count(2B LE) + codes(count*2B LE, 정렬) + widths(count*1B)
          + glyphs(count*32B — 16행 x 2B, 상위비트가 왼쪽)."""

    def __init__(self, path):
        with open(path, "rb") as f:
            data = f.read()
        if data[:3] != b"KF1":
            raise ValueError("kfont 형식 오류")
        n = data[3] | (data[4] << 8)
        self.n = n
        mv = memoryview(data)
        self.codes = mv[5:5 + 2 * n]
        self.widths = mv[5 + 2 * n:5 + 3 * n]
        self.glyphs = mv[5 + 3 * n:]
        self._data = data          # memoryview 수명 유지

    def _find(self, code):
        lo, hi = 0, self.n - 1
        while lo <= hi:
            mid = (lo + hi) >> 1
            c = self.codes[2 * mid] | (self.codes[2 * mid + 1] << 8)
            if c == code:
                return mid
            if c < code:
                lo = mid + 1
            else:
                hi = mid - 1
        return -1

    def get(self, ch):
        """(width, glyph 32B memoryview) | None."""
        i = self._find(ord(ch))
        if i < 0:
            return None
        return self.widths[i], self.glyphs[32 * i:32 * (i + 1)]


def _load_font():
    for path in (getattr(config, "KFONT_PATH", None), "kfont.bin", "/kfont.bin"):
        if not path:
            continue
        try:
            return KFont(path)
        except (OSError, ValueError):
            continue
    return None


class NullDisplay:
    """디스플레이 없음 — 모든 호출이 no-op. 헤드리스 운전용 기본값."""
    active = False

    def tick(self):
        pass


class Button:
    def __init__(self, label, action, color=DGREY, confirm=False):
        self.label = label
        self.action = action
        self.color = color
        self.confirm = confirm      # True 면 첫 탭은 확인 대기, 두 번째 탭에 실행
        self.x = self.y = self.w = self.h = 0

    def hit(self, x, y):
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h


class UI:
    """상태 표시 + 터치 조치. 드라이버가 있을 때만 생성된다."""
    active = True

    def __init__(self, fb, touch):
        self.fb = fb
        self.touch = touch
        self.w = fb.width
        self.h = fb.height
        self.sc = max(1, self.w // 320)          # 배율 — 큰 화면이면 자동 확대
        self.font = _load_font()
        self.fh = (CELL_H if self.font else 8) * self.sc     # 줄 높이(픽셀, 여백 제외)
        self.lh = self.fh + 3                                # 줄 간격 포함
        self.msg = ""
        self.msg_until = 0
        self.armed = None                        # confirm 대기 중인 버튼
        self.armed_until = 0
        self.page = "main"                       # main | log
        self.last_draw = 0
        self.last_touch = 0
        self._sig = None                         # 직전 화면 내용 — 같으면 다시 그리지 않음
        self.buttons = [
            Button("측정", "measure", BLUE),
            Button("측정 중단", "abort", ORANGE),
            Button("측정 정리", "cleanup", RED, confirm=True),
            Button("래치해제", "clear_latch", RED, confirm=True),
            Button("BT 전환", "bt_target", DGREY),
            Button("로그", "log", DGREY),
        ]
        self._layout()

    # ── 배치 ──

    def _layout(self):
        """하단 2행 x 3열 버튼. 손가락 터치를 감안해 최소 40px(또는 화면의 1/7) 높이."""
        cols, rows = 3, 2
        bh = max(40, self.h // 7)
        bw = self.w // cols
        top = self.h - bh * rows
        for i, b in enumerate(self.buttons):
            b.x = (i % cols) * bw
            b.y = top + (i // cols) * bh
            b.w = bw - 2
            b.h = bh - 2
        self.body_h = top

    # ── 텍스트(한글 렌더러 + 픽셀 줄바꿈) ──

    def _char_w(self, ch):
        if self.font:
            g = self.font.get(ch)
            if g is None:
                g = self.font.get("?")
            return (g[0] if g else 8) * self.sc
        return 8 * self.sc

    def _tw(self, s):
        return sum(self._char_w(ch) for ch in s)

    def _wrap(self, s, max_w=None):
        """픽셀 폭 기준 자동 줄바꿈 — 잘리지 않는다(화면 크기 무관, 사용자 지시)."""
        if max_w is None:
            max_w = self.w - 8
        lines = []
        cur = ""
        cur_w = 0
        for ch in s:
            cw = self._char_w(ch)
            if cur and cur_w + cw > max_w:
                lines.append(cur)
                cur, cur_w = ch.lstrip(), self._char_w(ch)
            else:
                cur += ch
                cur_w += cw
        lines.append(cur)
        return lines or [""]

    def _draw_char(self, ch, x, y, c):
        """kfont 글리프를 수평 런(run) 단위 fill_rect 로 그린다 — 드라이버에 텍스트 기능 불요."""
        g = self.font.get(ch)
        if g is None:
            g = self.font.get("?")
        if g is None:
            return 8 * self.sc
        w, gl = g
        sc = self.sc
        for ry in range(CELL_H):
            bits = (gl[2 * ry] << 8) | gl[2 * ry + 1]
            if not bits:
                continue
            run = 0
            for rx in range(w + 1):
                on = rx < w and (bits >> (15 - rx)) & 1
                if on:
                    run += 1
                elif run:
                    self.fb.fill_rect(x + (rx - run) * sc, y + ry * sc, run * sc, sc, c)
                    run = 0
        return w * sc

    def _text(self, s, x, y, c=WHITE):
        """한 줄 그리기(줄바꿈 없음 — 호출자가 _wrap 으로 나눠 넘긴다)."""
        if self.font:
            for ch in s:
                if x >= self.w:
                    break
                x += self._draw_char(ch, x, y, c)
            return
        # 폴백: 내장 ASCII 폰트(한글은 '?')
        s = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)
        try:
            self.fb.text_scaled(s, x, y, c, self.sc)
        except AttributeError:
            self.fb.text(s, x, y, c)

    def _badge(self, s, x, y, color):
        w = self._tw(s) + 6
        self.fb.fill_rect(x, y, w, self.fh + 6, color)
        self._text(s, x + 3, y + 3, BLACK)
        return x + w + 4

    # ── 그리기 ──

    def draw(self, snap):
        """화면 내용이 바뀌었을 때만 실제로 그린다(깜빡임·SPI 낭비 방지)."""
        rows = self._compose_rows(snap)          # [(text|None, color)] — 이미 줄바꿈 완료
        sig = (self.page, tuple(t for t, _ in rows), self._armed_label())
        if sig == self._sig:
            return
        self._sig = sig
        self.fb.fill(BLACK)
        y = 4
        for text, color in rows:
            if y + self.fh > self.body_h:        # 세로로 넘치면 이후 줄 생략(가로 잘림은 없음)
                break
            if text is None:                     # 배지 행 자리
                self._draw_badges(snap, y)
                y += self.fh + 9
                continue
            self._text(text, 4, y, color)
            y += self.lh
        self._draw_buttons()
        self.fb.show()

    def _armed_label(self):
        return self.armed.label if (self.armed and time.time() < self.armed_until) else None

    def _draw_badges(self, snap, y):
        latest = snap.get("latest") or {}
        liq = snap.get("liquid") or {}
        x = 4
        if snap.get("measuring"):
            x = self._badge("측정중", x, y, GREEN)
        if snap.get("error_latch"):
            x = self._badge("에러래치", x, y, RED)
        lk = snap.get("link") or {}
        if lk.get("frozen"):
            x = self._badge("BT불일치", x, y, RED)
        elif lk.get("target") == "meas":
            x = self._badge("BT측정기", x, y, GREEN)
        elif lk.get("target") == "doser":
            x = self._badge("BT도저", x, y, GREEN)
        else:
            x = self._badge("BT미확정", x, y, ORANGE)
        if "UNKNOWN" in (liq.get("chamber"), liq.get("holding")):
            x = self._badge("위치불명", x, y, RED)
        if latest.get("co2_suspect"):
            x = self._badge("CO2?", x, y, YELLOW)
        if snap.get("job_busy") or snap.get("job_pending"):
            x = self._badge("작업중", x, y, ORANGE)
        if x == 4:
            self._badge("정상 대기", x, y, GREY)

    def _compose_rows(self, snap):
        """논리 줄 구성 → 픽셀 줄바꿈까지 끝낸 물리 행 목록. 변경 감지에도 이 값을 쓴다."""
        if self.page == "log":
            # 로그: 최신이 반드시 보이도록 뒤에서부터 채운다(긴 줄은 줄바꿈)
            cap = max(1, (self.body_h - 4) // self.lh)
            out = []
            for ln in reversed(ops.log_tail(cap)):
                for seg in reversed(self._wrap(ln)):
                    out.append((seg, GREY))
                    if len(out) >= cap:
                        break
                if len(out) >= cap:
                    break
            out.reverse()
            return out

        latest = snap.get("latest") or {}
        run = snap.get("last_run") or {}
        dos = snap.get("doser") or {}
        liq = snap.get("liquid") or {}
        dkh = latest.get("tank_kh")
        logical = [
            ("dKH %s%s  %s도  %s" % ("%.2f" % dkh if isinstance(dkh, float) else "--",
                                     "" if latest.get("is_flat") else "*",
                                     latest.get("temp", "--"), latest.get("date", "")), CYAN),
            (None, None),                        # 배지 행
        ]
        # ★조치 결과·안내는 배지 바로 아래(상단) — 작은 화면에서 세로로 밀려 안 보이면 안 됨
        res = snap.get("job_result")
        if self.msg and time.time() < self.msg_until:
            logical.append((self.msg, YELLOW))
        elif res:
            logical.append(("%s %s %s" % (res.get("kind"), "성공" if res.get("ok") else "실패",
                                          (res.get("msg") or "").split("\n")[0]),
                            GREEN if res.get("ok") else RED))
        lk = snap.get("link") or {}
        if lk.get("frozen"):
            logical.append(("BT 동결: %s" % lk["frozen"], RED))
        logical += [
            ("측정 %s %s · 평탄 t%s/r%s · 판독 %s/%s"
             % (run.get("mode") or "-", "완료" if run.get("completed") else "미완료",
                run.get("tank_flat_n"), run.get("ref_flat_n"),
                run.get("tank_reads"), run.get("ref_reads")),
             GREEN if run.get("completed") else ORANGE),
            ("측정 챔버:%s · 홀딩 챔버:%s" % (LIQ_KO.get(liq.get("chamber"), liq.get("chamber")),
                                              LIQ_KO.get(liq.get("holding"), liq.get("holding"))),
             RED if "UNKNOWN" in (liq.get("chamber"), liq.get("holding")) else WHITE),
            ("도저 %sms %smL/일 %s" % (dos.get("lrt_new"), dos.get("ml_day_new"),
                                       "자동" if dos.get("auto_apply") else "권고"), WHITE),
            ("%s · 기록 %s행 · 힙 %sk · SD %s"
             % (snap.get("now", "")[-8:], snap.get("dat_rows"),
                (snap.get("heap_free") or 0) // 1024,
                "기록중" if (snap.get("sd") or {}).get("ok") else "없음"), GREY),
        ]
        wf = snap.get("wifi") or {}
        if wf.get("connected"):
            logical.append(("WiFi %s" % wf.get("ip"), GREY))
        else:
            logical.append(("설정AP %s 비번 %s → http://%s/ops.html"
                            % (wf.get("ap_ssid"), wf.get("ap_pass"), wf.get("ap_ip")), YELLOW))

        rows = []
        for text, color in logical:
            if text is None:
                rows.append((None, None))
                continue
            for seg in self._wrap(text):         # ★줄 잘림 금지 — 픽셀 폭 기준 줄바꿈
                rows.append((seg, color))
        return rows

    def _draw_buttons(self):
        armed_label = self._armed_label()
        for b in self.buttons:
            armed = (armed_label is not None and self.armed is b)
            label = "한번 더 탭" if armed else b.label     # 오터치 방지 확인 단계(5초)
            self.fb.fill_rect(b.x, b.y, b.w, b.h, YELLOW if armed else b.color)
            self.fb.rect(b.x, b.y, b.w, b.h, WHITE)
            tw = self._tw(label)
            self._text(label, b.x + max(2, (b.w - tw) // 2),
                       b.y + (b.h - self.fh) // 2, BLACK if armed else WHITE)

    # ── 조치 실행 ──

    def _flash(self, msg, secs=6):
        self.msg = msg
        self.msg_until = time.time() + secs

    def _do(self, action):
        if action == "log":
            self.page = "log" if self.page == "main" else "main"
            return
        if action == "abort":
            ok, msg = ops.request_abort()
        elif action == "clear_latch":
            ok, msg = ops.clear_error_latch()
        elif action == "measure" and state.measuring:
            ok, msg = False, "이미 측정 중"
        elif action == "bt_target":
            # ★HC-05 1개 — 화면에서는 두 장비를 번갈아 전환한다(지금 붙은 쪽의 반대편으로).
            #   동결 상태는 화면에서 풀지 않는다: 배선·BIND 주소 확인이 선행돼야 하므로
            #   정비페이지의 명시적 '동결 해제' 경로로만 푼다.
            cur = (ops.snapshot().get("link") or {}).get("target")
            nxt = "doser" if cur == "meas" else "meas"
            ok = state.put_job("bt_target", target=nxt)
            msg = ("%s 로 전환 요청됨" % ("도저" if nxt == "doser" else "측정 장비")
                   ) if ok else "다른 작업 대기 중"
        else:
            ok = state.put_job(action)           # cleanup / measure / link
            names = {"cleanup": "측정 정리", "measure": "측정", "link": "BT 연결 점검"}
            msg = ("%s 요청됨" % names.get(action, action)) if ok else "다른 작업 대기 중"
        self._flash(("성공: " if ok else "실패: ") + msg)

    def _on_touch(self, x, y):
        for b in self.buttons:
            if not b.hit(x, y):
                continue
            if b.confirm and not (self.armed is b and time.time() < self.armed_until):
                self.armed = b
                self.armed_until = time.time() + 5      # 5초 안에 다시 누르면 실행
                self._flash("%s: 5초 안에 한 번 더 누르면 실행" % b.label, 5)
                return
            self.armed = None
            self._do(b.action)
            return

    # ── 주기 호출 ──

    def tick(self):
        """디스플레이 스레드가 자주 호출. 터치는 즉시, 상태 갱신은 1초 간격.
        ★ticks_ms 사용: ESP32 의 time.time() 은 초 단위 정수라 300ms 디바운스가 안 된다."""
        now = time.ticks_ms()
        try:
            pos = self.touch.get_touch() if self.touch else None
        except Exception:
            pos = None
        if pos and time.ticks_diff(now, self.last_touch) > 300:      # 디바운스
            self.last_touch = now
            self._on_touch(pos[0], pos[1])
            self.last_draw = 0                                      # 즉시 반영
        if self.last_draw == 0 or time.ticks_diff(now, self.last_draw) >= 1000:
            self.last_draw = now
            try:
                self.draw(ops.snapshot())
            except Exception as e:
                print("[disp] draw error: %r" % e)


def create():
    """드라이버가 있으면 UI, 없으면 NullDisplay. 실패해도 시스템은 계속 돌아간다."""
    name = getattr(config, "DISPLAY_DRIVER", None)
    if not name:
        return NullDisplay()
    try:
        mod = __import__(name)
        fb, touch = mod.get_display()
        ui = UI(fb, touch)
        print("[disp] %s %dx%d 배율%d 한글폰트%s" % (name, ui.w, ui.h, ui.sc,
              "O" if ui.font else "X(ASCII 폴백 — kfont.bin 업로드 필요)"))
        return ui
    except Exception as e:
        print("[disp] 드라이버 '%s' 사용 불가(%r) — 헤드리스로 계속" % (name, e))
        return NullDisplay()
