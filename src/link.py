# 측정 장비 링크 계층 — 원본 measure_kh_once.py 의 시리얼 헬퍼 이식.
# 무선 구간이 HC-06(장비)~HC-05(ESP32 유선) 이므로 RF 순단 대응 정책(keepalive,
# 송신 전 연결확인, 재연결-재송신, 모터 재시도 전 정지)은 원본 그대로 유지한다.
# 원본과 다른 점: COM close→open 대신 HC-05 리셋 핀 펄스로 라디오 자체를 재기동
# (Windows 에서 불가능했던 하드 복구). UART 송신은 라디오 사망과 무관하게 즉시
# 성공하므로 write_timeout 좀비 문제는 구조적으로 사라짐 — 판정은 항상 응답 기준.
import time
import re
from machine import UART, Pin

import config
import state


def _decode(b):
    """UTF-8 안전 디코드 — 순단으로 멀티바이트가 잘려도 죽지 않게."""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except (UnicodeError, ValueError):
        return "".join(chr(c) if c < 0x80 else "?" for c in b)


def _motor_index(cmd):
    """'m1f:70' → 1. 모터 구동 명령이 아니면 None."""
    m = re.match(r"m([1-4])[fb]:", cmd)
    return int(m.group(1)) if m else None


class Link:
    def __init__(self, uart_id, tx, rx, reset_pin=None, name="meas"):
        self.name = name
        self.uart = UART(uart_id, baudrate=config.BAUD, tx=tx, rx=rx,
                         timeout=200, timeout_char=50, rxbuf=2048)
        self.reset = Pin(reset_pin, Pin.OUT, value=1) if reset_pin is not None else None
        self.log = print   # measure.py 가 파일 로거로 교체

    # ── 저수준 ──

    def flush_input(self):
        while self.uart.any():
            self.uart.read()

    def write_line(self, s):
        self.uart.write(s.encode() + b"\r\n")

    def readline(self):
        return _decode(self.uart.readline()).strip()

    def _pulse_reset(self):
        """HC-05 하드 리셋(EN/전원 펄스) — 라디오 좀비 상태 복구. 핀 미배선이면 no-op."""
        if self.reset is None:
            return
        self.reset.value(0)
        time.sleep_ms(300)
        self.reset.value(1)

    # ── 원본 이식 ──

    def read_until(self, stop_pattern, timeout=60.0, keepalive=False):
        """stop_pattern 수신까지 읽기. keepalive=True 면 유휴 중 빈 줄 송신(HC-06 드롭 예방)."""
        lines = []
        deadline = time.time() + timeout
        next_ka = time.time() + config.KEEPALIVE_SECS
        while time.time() < deadline:
            if self.uart.any():
                line = self.readline()
                if line:
                    self.log("    " + line)
                    lines.append(line)
                    next_ka = time.time() + config.KEEPALIVE_SECS
                    if stop_pattern in line:
                        return lines
            else:
                time.sleep_ms(20)
                if keepalive and time.time() >= next_ka:
                    self.uart.write(b"\r\n")   # 펌웨어가 빈 줄 무시 — 링크만 깨움
                    next_ka = time.time() + config.KEEPALIVE_SECS
        self.log("    [TIMEOUT] '%s' 미수신" % stop_pattern)
        return lines

    def keepalive_sleep(self, secs):
        """유휴를 KEEPALIVE_SECS 청크로 나눠 자며 빈 줄 송신 — 링크 유휴 드롭 예방.
        중단 요청은 청크 경계에서 즉시 반응한다(사전폭기 25분 중에도 12초 내 중단)."""
        end = time.time() + secs
        while True:
            if state.abort_requested:
                raise state.Aborted("유휴 대기 중 중단 요청")
            remaining = end - time.time()
            if remaining <= 0:
                break
            time.sleep(min(config.KEEPALIVE_SECS, remaining))
            if time.time() < end:
                self.uart.write(b"\r\n")

    def _ping(self):
        """부작용 없는 status 핑 — 펌웨어 응답('============') 확인."""
        self.flush_input()
        self.write_line("status")
        deadline = time.time() + config.LINK_PING_TIMEOUT
        while time.time() < deadline:
            if self.uart.any():
                if "============" in self.readline():
                    self.flush_input()
                    return True
            else:
                time.sleep_ms(20)
        return False

    def reconnect(self, why):
        """HC-05 리셋 펄스로 재페어링 유도 + status 핑 확인. 원본 close→open 상당."""
        self.log("    [RF] 링크 끊김 — %s → 재연결 시도" % why)
        for i in range(1, config.RECONNECT_TRIES + 1):
            self._pulse_reset()
            backoff = config.RECONNECT_BACKOFF[min(i - 1, len(config.RECONNECT_BACKOFF) - 1)]
            time.sleep(backoff)
            self.flush_input()   # 사망 중 고인 스테일 바이트 폐기(원본 7/9 지연 배달 사고 대응)
            if self._ping():
                self.log("    [RF] 재연결 성공 (시도 %d) — 펌웨어 응답 확인" % i)
                return True
            self.log("    [RF] 재연결 시도 %d/%d 실패" % (i, config.RECONNECT_TRIES))
        self.log("    *[RF] 재연결 %d회 모두 실패 — 링크 복구 불가" % config.RECONNECT_TRIES)
        return False

    def ensure_link(self):
        """명령 송신 직전 링크 생존 확인 — 드롭은 대개 '보낼 때 이미 끊겨 있음'(원본 실측)."""
        if self._ping():
            return True
        return self.reconnect("송신 전 점검: 링크 무응답")

    def _stop_motor(self, idx):
        """모터 재시도 전 안전 정지(mNs) — 재송신 중복 구동 방지."""
        self.log("    [모터정지] m%ds (재시도 전 안전 정지)" % idx)
        self.flush_input()
        self.write_line("m%ds" % idx)
        self.read_until("[M%d] 정지" % idx, timeout=3)

    def send(self, cmd, stop_pattern=None, timeout=5.0, allow_reconnect=True, keepalive=False):
        """원본 send() 정책 그대로: 송신 전 연결확인 → 응답 미수신이면 재연결-재시도
        (SEND_RETRY_MAX회) → 모터는 재시도 전 정지. 소진 후 (부분/빈) 결과 반환."""
        self.log("\n-> " + cmd)
        motor_idx = _motor_index(cmd)
        lines = []
        for attempt in range(1, config.SEND_RETRY_MAX + 1):
            if allow_reconnect:
                self.ensure_link()
                if attempt > 1 and motor_idx is not None:
                    self._stop_motor(motor_idx)
            self.write_line(cmd)
            if not stop_pattern:
                time.sleep_ms(300)
                lines = []
                while self.uart.any():
                    line = self.readline()
                    if line:
                        self.log("    " + line)
                        lines.append(line)
                return lines
            lines = self.read_until(stop_pattern, timeout, keepalive=keepalive)
            if any(stop_pattern in ln for ln in lines):
                return lines
            if allow_reconnect and attempt < config.SEND_RETRY_MAX:
                self.log("    [RF] '%s' 응답 미수신 → 재연결 후 재시도 (%d/%d)"
                         % (cmd, attempt, config.SEND_RETRY_MAX))
                continue
            return lines
        return lines

    def send_motor(self, motor_idx, cmd):
        m = re.search(r":(\d+)$", cmd)
        duration = int(m.group(1)) if m else 60
        return self.send(cmd, stop_pattern="[모터%d] 완료" % motor_idx,
                         timeout=duration + 15, keepalive=True)
