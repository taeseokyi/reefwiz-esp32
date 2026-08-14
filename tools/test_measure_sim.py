#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP32 이식본(src/measure.py 등)을 원본 펌웨어 시뮬레이터로 검증한다 — 하드웨어 불요.

구조:
  firmware_sim.FirmwareSim (원본 bin/ 그대로 복사) — TCP 로 펌웨어 프로토콜을 흉내냄
      ↕ TCP
  mpshim: MicroPython 'machine.UART' 를 TCP 클라이언트로 구현 + time.sleep_ms 등 보강
      ↕ (src 코드는 자신이 CPython 위인지 모른다)
  src/link.py → src/measure.py → src/datalog.py  (검증 대상 — 무수정)

시나리오:
  A. 정상 calkh — 상수 pH 라 8회째 평탄 latch → dkh.dat/series/plateau 기록·정리 검증
  B. RF 순단 — tank 측정 중 소켓 강제 드롭 → 재연결 후 측정 완주 검증
  C. 에러 래치 — pH 누락(garble) → FAIL_MAX → 0.0 기록 → 다음 회차 측정 생략 →
     ops.clear_error_latch() → 측정 재개 검증. 비상정리 레시피(m2b→m1b→m3f)도 확인
  D. 조치 콘솔 — ops 의 cmd job: 일반 명령 수집 + 모터 명령 '[모터N] 완료' 대기 검증

실행:  python3 tools/test_measure_sim.py     (원본과 같은 CPython 3.6+)
"""
import os
import select
import shutil
import socket
import sys
import tempfile
import time as _real_time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                          # firmware_sim
sys.path.insert(0, os.path.join(HERE, "..", "src"))  # 검증 대상

from firmware_sim import FirmwareSim, DEFAULT_REF_DKH, TANK_PH, REF_PH  # noqa: E402

SIM_ADDR = ["127.0.0.1", 0]   # UART 심이 접속할 곳 — 테스트가 포트를 채움


# ─────────────────────────────────────────────
# MicroPython 심(shim) — src 코드가 요구하는 것만
# ─────────────────────────────────────────────

class _TimeShim:
    """sys.modules['time'] 대체 — 실제 time 에 sleep_ms/ticks_ms/ticks_diff 를 얹는다."""
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep_ms(self, ms):
        self._real.sleep(ms / 1000.0)

    def ticks_ms(self):
        return int(self._real.time() * 1000)

    def ticks_diff(self, a, b):
        return a - b


class Pin:
    OUT = 1
    IN = 0

    def __init__(self, n, mode=0, value=None):
        self.n = n
        self._v = value

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = v


class UART:
    """machine.UART 의 TCP 구현 — 무선 링크의 성질을 흉내낸다:
    · write 는 절대 실패하지 않는다(라디오가 죽어도 UART 송신은 성공 — 데이터만 유실)
    · 링크가 죽으면 읽을 게 없다. HC-05 처럼 백그라운드에서 재접속을 계속 시도한다."""
    RECONNECT_GAP = 0.3

    def __init__(self, uid, baudrate=9600, tx=None, rx=None,
                 timeout=200, timeout_char=50, rxbuf=2048):
        self.timeout = timeout / 1000.0
        self.sock = None
        self.buf = b""
        self._last_try = 0.0
        self._ensure()

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _ensure(self):
        if self.sock:
            return
        now = _real_time.time()
        if now - self._last_try < self.RECONNECT_GAP:
            return
        self._last_try = now
        try:
            s = socket.create_connection(tuple(SIM_ADDR), timeout=0.5)
            s.setblocking(False)
            self.sock = s
        except OSError:
            self.sock = None

    def _fill(self):
        self._ensure()
        if not self.sock:
            return
        while True:
            try:
                r, _, _ = select.select([self.sock], [], [], 0)
            except OSError:
                self._close()
                return
            if not r:
                return
            try:
                d = self.sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._close()
                return
            if not d:                # 서버가 닫음 = 링크 드롭
                self._close()
                return
            self.buf += d

    def any(self):
        self._fill()
        return len(self.buf)

    def read(self, n=None):
        self._fill()
        out, self.buf = (self.buf, b"") if n is None else (self.buf[:n], self.buf[n:])
        return out or None

    def readline(self):
        deadline = _real_time.time() + self.timeout
        while True:
            self._fill()
            if b"\n" in self.buf:
                ln, self.buf = self.buf.split(b"\n", 1)
                return ln + b"\n"
            if _real_time.time() >= deadline:
                return None
            _real_time.sleep(0.005)

    def write(self, b):
        self._ensure()
        if self.sock:
            try:
                self.sock.sendall(b)
            except OSError:
                self._close()        # 라디오 사망 — 송신은 '성공'(데이터 유실)
        return len(b)


class _WLANStub:
    def __init__(self, _if):
        pass

    def active(self, v=None):
        return False

    def isconnected(self):
        return False

    def ifconfig(self):
        return ("0.0.0.0",) * 4

    def config(self, *a, **k):
        return ""

    def scan(self):
        return []

    def connect(self, *a):
        pass

    def disconnect(self):
        pass

    def status(self, *a):
        return None


def _install_shims():
    import types
    sys.modules["time"] = _TimeShim(_real_time)
    machine = types.ModuleType("machine")
    machine.UART = UART
    machine.Pin = Pin
    sys.modules["machine"] = machine
    network = types.ModuleType("network")   # ops→wifinet 이 import — 테스트에선 미접속 스텁
    network.WLAN = _WLANStub
    network.STA_IF = 0
    network.AP_IF = 1
    network.AUTH_WPA_WPA2_PSK = 4
    sys.modules["network"] = network


# ─────────────────────────────────────────────
# 테스트 하네스
# ─────────────────────────────────────────────

def _shrink_config(config, data_dir):
    """실측 타이밍(사전폭기 25분 등)을 초 단위로 압축 — 판정 로직은 그대로."""
    config.DATA_DIR = data_dir
    config.PREAERATE_SECS = {"tank": 1.0, "ref": 0.5}
    config.FIRST_POINT_AERATE_SECS = 0.5
    config.SETTLE_SECS = 0.1
    config.MEAS_INTERVAL = 0.3
    config.KEEPALIVE_SECS = 0.3
    config.MEAS_READ_TIMEOUT = 4
    config.LINK_PING_TIMEOUT = 2
    config.LINK_RETRY_INTERVAL = 0.5
    config.PHASE_MAX_SECS = 60
    config.MEAS_MAX = 40
    config.RECONNECT_BACKOFF = (0.2, 0.2, 0.3, 0.3, 0.5)
    config.CLEANUP_RECOVERY_SECS = 2
    config.MOVE_PRECOND_RECOVERY_SECS = 2


_FAILS = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (tag, name, (" — " + str(detail)) if detail and not cond else ""))
    if not cond:
        _FAILS.append(name)


def run():
    _install_shims()
    data_dir = tempfile.mkdtemp(prefix="reefwiz_test_")
    import config
    _shrink_config(config, data_dir)
    import datalog
    import measure
    import ops
    import state  # noqa: F401
    datalog.log = lambda msg: None          # 콘솔 소음 억제(파일 로그도 생략)
    measure.p = datalog.log
    expected_dkh = DEFAULT_REF_DKH * 10.0 ** (TANK_PH - REF_PH)

    # ── A. 정상 calkh ──────────────────────────────────────
    print("\n[A] 정상 calkh — 상수 pH, 8회째 평탄 latch")
    sim = FirmwareSim()
    SIM_ADDR[1] = sim.start()
    r = measure.run_once()
    check("측정 성공(5값 모두)", r is not None and all(v is not None for v in r), r)
    if r:
        check("dKH = ref×10^ΔpH (%.3f)" % expected_dkh, abs(r[3] - expected_dkh) < 0.01, r[3])
        check("평탄 도달 = 양수 기록", r[3] > 0)
    lines = datalog.read_dat_lines()
    check("dkh.dat 1행 기록", len(lines) == 1 and len(lines[0]) == 6, lines)
    check("에러 래치 아님", not datalog.last_dat_is_error())
    last = datalog.last_plateau()
    check("plateau tank_flat_n=8", last.get("tank_flat_n") == 8, last.get("tank_flat_n"))
    check("plateau ref_flat_n=8", last.get("ref_flat_n") == 8, last.get("ref_flat_n"))
    check("plateau 첫점 n=0 포함", last.get("tank") and last["tank"][0]["n"] == 0)
    check("CO₂ 미의심(ref 상수)", last.get("co2_suspect") is False, last.get("co2_suspect"))
    series = datalog._read_json(datalog.SERIES_FILE, [])
    check("series 1행·is_flat", len(series) == 1 and series[0]["is_flat"] is True, series)
    rc = sim.received
    for cmd in ("m3b:68", "m1f:70", "m2f:60", "m2b:68", "m4f:60", "m4b:70", "m1b:82", "m3f:60"):
        check("이송 시퀀스 %s" % cmd, cmd in rc)
    check("정리 후 챔버=KCL", measure._liquid == {"chamber": "KCL", "holding": "EMPTY"},
          measure._liquid)
    check("재연결 없음(순단 미발생)", sim.connection_count == 1, sim.connection_count)
    sim.stop()

    # ── B. RF 순단 — tank 3회째 read 직전 드롭 → 재연결 후 완주 ──
    print("\n[B] RF 순단 — tank 3회째 응답 직전 소켓 드롭")
    for f in os.listdir(data_dir):
        os.remove(os.path.join(data_dir, f))
    sim = FirmwareSim()
    sim.drops = [{"pat": "tank", "nth": 4, "when": "before"}]   # 첫점 포함 4번째 tank
    SIM_ADDR[1] = sim.start()
    r = measure.run_once()
    check("드롭에도 측정 완주", r is not None and all(v is not None for v in r), r)
    if r:
        check("dKH 값 동일(%.3f)" % expected_dkh, abs(abs(r[3]) - expected_dkh) < 0.01, r[3])
    check("재연결 발생", sim.connection_count >= 2, sim.connection_count)
    check("에러 래치 아님", not datalog.last_dat_is_error())
    sim.stop()

    # ── C. 에러 래치 — pH 누락 → 0.0 기록 → 생략 → 해제 → 재개 ──
    print("\n[C] 에러 래치 — tank pH 누락(garble) → FAIL_MAX → 래치 → 해제")
    for f in os.listdir(data_dir):
        os.remove(os.path.join(data_dir, f))
    sim = FirmwareSim()
    sim.garble = {"tank"}
    SIM_ADDR[1] = sim.start()
    r = measure.run_once()
    check("측정 실패 반환", r is None)
    check("0.0 에러 표식 기록", datalog.last_dat_is_error())
    rc = sim.received
    i_fail = len(rc)
    check("비상정리 m2b(챔버→홀딩)", "m2b:68" in rc)
    check("비상정리 m1b(홀딩→본수조)", "m1b:82" in rc)
    check("비상정리 m3f(KCl 소크)", "m3f:60" in rc)
    # 래치 상태 — 측정 없이 에러 표식만 재기록
    n_before = len(datalog.read_dat_lines())
    r = measure.run_once()
    check("래치 중 측정 생략", r is None and len(sim.received) == i_fail,
          "명령 %d→%d" % (i_fail, len(sim.received)))
    check("에러 표식 재기록", len(datalog.read_dat_lines()) == n_before + 1)
    # 해제 → garble 제거 → 재개
    ok, msg = ops.clear_error_latch()
    check("clear_error_latch 1회", ok, msg)
    ok2, _ = ops.clear_error_latch()            # 아직 한 줄 남음(2연속 에러 표식)
    check("clear_error_latch 2회", ok2)
    check("래치 해제됨", not datalog.last_dat_is_error())
    sim.garble = set()
    r = measure.run_once()
    check("해제 후 측정 재개", r is not None and all(v is not None for v in r), r)
    sim.stop()

    # ── D. 조치 콘솔 — 일반 명령 수집 + 모터 완료 대기 ──
    print("\n[D] 조치 콘솔 — cmd job (status 수집 / 모터 완료 대기 / 완료 누락 감지)")
    sim = FirmwareSim()
    SIM_ADDR[1] = sim.start()
    ok, msg = ops._job_cmd({"cmd": "status", "target": "meas", "timeout": 1})
    check("status 응답 수집", ok and "refKH" in msg, msg[:60])
    ok, msg = ops._job_cmd({"cmd": "m3f:60", "target": "meas", "timeout": 5})
    check("모터 완료 대기·성공", ok and "[모터3] 완료" in msg, msg)
    sim.no_done = {"m3f"}
    ok, msg = ops._job_cmd({"cmd": "m3f:60", "target": "meas", "timeout": 5})
    check("완료 누락 → 실패 보고", (not ok) and "성공 불명" in msg, msg)
    sim.stop()

    shutil.rmtree(data_dir, ignore_errors=True)
    print("\n%s — 실패 %d건%s" % ("ALL PASS" if not _FAILS else "FAILURES",
                                  len(_FAILS), (": " + ", ".join(_FAILS)) if _FAILS else ""))
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    sys.exit(run())
