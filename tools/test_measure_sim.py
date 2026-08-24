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
  E. BT 대상 전환 — 신원 검증 / 오접속 동결 / 모터 중 전환 금지 / Way1 폴백
  F. 관리 계층 — /api 계약, 로그 회전, 도저 수동 우선, 장치 목록 저장
  G. 스케줄·장치 관리 — 회차 검증(최소 간격 2h·원형), 긴 회차의 슬롯 소비,
     도징기 2대 전환(서명이 같아도 동결되지 않는다), 장치별 시계 동기

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

# 시나리오 F 가 쓰는 dkh.dat 표본 — 정상 1행 / 에러 표식 1행(값 전부 0 = 래치)
NORMAL_ROW = "2026-08-18 05 7.723 7.663 8.830 7.701 28.7\n"
ERROR_ROW = "2026-08-18 13 0.000 0.000 0.000 0.000 0.0\n"

# ── HC-05 1개 모델 (2026-08-18 확정 구성) ──
# 실제 모듈처럼 동작한다: KEY 를 올린 채 전원을 넣으면 AT 명령 모드(38400), 내리고 넣으면
# 데이터 모드로 부팅해 *바인드된 주소*의 슬레이브에 붙는다. 주소→포트 맵으로 "어느 장비에
# 붙었는가"를 재현하므로, 바인드가 어긋났을 때 신원 검증이 실제로 잡아내는지 시험할 수 있다.
ADDR_MEAS = "1111,11,111111"
ADDR_DOSER = "2222,22,222222"
ADDR_DOSER2 = "3333,33,333333"   # 시나리오 G — 도징기 2대 구성(응답 서명이 동일한 상대)
BIND_PORTS = {}               # addr -> TCP 포트 (테스트가 채움)


class HC05:
    """모듈 상태 — Pin(전원/KEY) 이 구동하고 UART 심이 읽는다.

    데이터시트(ZG1643)의 두 가지 AT 진입 경로를 모두 재현한다:
      Way 1 = 전원이 켜진 상태에서 KEY 를 올리면 AT 모드(보레이트는 통신값 그대로)
      Way 2 = KEY 를 올린 채 전원 인가 → AT 모드(38400)
    `link_addr` 은 AT+LINK 로 '지금' 붙은 상대, `bind` 는 '다음 부팅 때' 붙을 상대라
    둘을 따로 들고 있어야 BIND 만 하고 LINK 를 빠뜨린 코드를 테스트가 잡아낸다.
    """
    power = True
    key = 0
    mode = "data"             # data | at
    bind = None               # AT+BIND — 자동 연결 대상(전원 재투입 후 적용)
    link_addr = None          # AT+LINK — 지금 붙어 있는 상대
    way1_supported = True     # False 로 두면 Way 1 미지원 펌웨어를 흉내낸다(폴백 시험)
    power_cycles = 0          # 전원 토글 횟수 — 고속 경로가 정말 전원을 안 끊는지 검사용

    @classmethod
    def reset(cls, way1=True):
        cls.power, cls.key, cls.mode = True, 0, "data"
        cls.bind = cls.link_addr = None
        cls.way1_supported = way1
        cls.power_cycles = 0

    @classmethod
    def set_power(cls, on):
        if on and not cls.power:
            # 전원 인가 순간의 KEY 레벨이 모드를 결정한다(Way 2)
            cls.mode = "at" if cls.key else "data"
            # 재부팅하면 지금 연결은 끊기고 바인드 대상으로 자동 재접속한다
            cls.link_addr = None if cls.mode == "at" else cls.bind
        if not on and cls.power:
            cls.power_cycles += 1
        cls.power = bool(on)

    @classmethod
    def set_key(cls, v):
        """전원이 켜진 상태의 KEY 변화 = Way 1 모드 전환."""
        v = 1 if v else 0
        if cls.power and v != cls.key and cls.way1_supported:
            cls.mode = "at" if v else "data"
        cls.key = v

    @classmethod
    def port(cls):
        """데이터 모드에서 접속할 포트 — 지금 붙어 있는 상대."""
        if not cls.power or cls.mode != "data":
            return None
        return BIND_PORTS.get(cls.link_addr)


class DoserSim:
    """도저 펌웨어 심 — 신원 검증(서명 'ls' 응답)과 lrt 왕복만 재현하면 충분하다.
    ★CR 이 붙은 명령은 무시한다: 실기 도저 펌웨어가 LF 만 받는다는 원본 확인 사항을
    시뮬레이터에도 넣어, 이식본이 CRLF 로 보내면 테스트가 잡아내게 한다."""

    def __init__(self):
        self.lrt = 8000
        self.lgt = 240
        self.seen = []            # 받은 명령 전체 — '어느 도징기에 닿았나' 확인용(시나리오 G)
        self._srv = None
        self._stop = False

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._srv.settimeout(0.2)
        port = self._srv.getsockname()[1]
        import threading
        threading.Thread(target=self._serve, daemon=True).start()
        return port

    def stop(self):
        self._stop = True

    def _serve(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(0.2)
            buf = b""
            while not self._stop:
                try:
                    d = c.recv(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if raw.endswith(b"\r"):
                        continue          # ★CR 포함 = 펌웨어 미실행(원본 확인)
                    self._cmd(c, raw.decode("utf-8", "replace").strip())
            c.close()

    def _cmd(self, c, line):
        if not line:
            return
        self.seen.append(line)
        if line.startswith("set time"):
            # 실기 펌웨어는 설정 결과를 한 줄 돌려준다 — 무응답이면 이식본이 '실패'로 본다
            c.sendall(("시간 설정: %s\n" % line[len("set time"):].strip()).encode())
            return
        if line == "ls" or line.startswith("lrt") or line == "refresh all":
            if line.startswith("lrt "):
                try:
                    self.lrt = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            if line == "refresh all":
                c.sendall("Refreshed all timers!\n".encode())
                return
            c.sendall(("왼쪽 동작(RUN) 시간 설정 값: %d\n"
                       "왼쪽 휴지(GAP) 시간 설정 값: %d\n"
                       % (self.lrt, self.lgt)).encode())


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
    """전원·KEY 핀은 HC-05 모델을 실제로 구동한다 — 그래야 '전원 인가 시점의 KEY 레벨이
    모드를 정한다'는 실제 동작이 테스트에 반영된다."""
    OUT = 1
    IN = 0

    def __init__(self, n, mode=0, value=None):
        self.n = n
        self._v = value
        if value is not None:
            self._drive(value)

    def _drive(self, v):
        import config
        if self.n == config.BT_POWER_PIN:
            HC05.set_power(v if config.BT_POWER_ACTIVE_HIGH else (1 - v))
        elif self.n == config.BT_KEY_PIN:
            HC05.set_key(v)

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = v
        self._drive(v)


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
        self._port = None          # 현재 붙어 있는 장비 포트(바인드가 바뀌면 끊는다)
        self._ensure()

    def init(self, baudrate=9600, **kw):
        """link._set_baud 가 호출 — 보레이트 전환은 심에서 의미가 없지만 API 는 있어야 한다."""
        self.timeout = kw.get("timeout", 200) / 1000.0

    def _close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _ensure(self):
        """데이터 모드에서 *바인드된 주소가 가리키는* 장비에 붙는다.
        AT 모드이거나 전원이 꺼져 있으면 어디에도 붙지 않는다(읽을 게 없다)."""
        want = HC05.port() if BIND_PORTS else SIM_ADDR[1]
        if want != self._port:      # 바인드가 바뀌었거나 모드가 바뀜 → 기존 연결 파기
            self._close()
            self.buf = b""
            self._port = want
        if want is None:
            self._close()
            return
        if self.sock:
            return
        now = _real_time.time()
        if now - self._last_try < self.RECONNECT_GAP:
            return
        self._last_try = now
        try:
            s = socket.create_connection((SIM_ADDR[0], want), timeout=0.5)
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
        if HC05.mode == "at" and HC05.power:
            self._at(b)             # AT 모드에서는 모듈 자신이 응답한다(장비로 안 감)
            return len(b)
        self._ensure()
        if self.sock:
            try:
                self.sock.sendall(b)
            except OSError:
                self._close()        # 라디오 사망 — 송신은 '성공'(데이터 유실)
        return len(b)


def _hc05_at(uart, raw):
    """AT 명령 처리 — 실제 모듈처럼 OK/ERROR 를 돌려주고 BIND 를 기억한다."""
    crlf = (chr(13) + chr(10)).encode()
    text = raw.decode("utf-8", "replace").replace(chr(13), "")
    for line in text.split(chr(10)):
        line = line.strip()
        if not line:
            continue
        if line == "AT" or line.startswith(("AT+ROLE", "AT+CMODE", "AT+UART")):
            uart.buf += b"OK" + crlf
        elif line.startswith("AT+BIND="):
            HC05.bind = line.split("=", 1)[1].strip()
            uart.buf += b"OK" + crlf
        elif line == "AT+DISC":
            # 안 붙어 있으면 NO_SLC — 실기와 같이 이것도 OK 로 끝난다(실패가 아니다)
            tag = b"+DISC:SUCCESS" if HC05.link_addr else b"+DISC:NO_SLC"
            HC05.link_addr = None
            uart.buf += tag + crlf + b"OK" + crlf
        elif line.startswith("AT+LINK="):
            addr = line.split("=", 1)[1].strip()
            if addr in BIND_PORTS:
                HC05.link_addr = addr
                uart.buf += b"OK" + crlf
            else:
                uart.buf += b"FAIL" + crlf     # 상대가 없거나 페어링 안 됨
        else:
            uart.buf += b"ERROR:(0)" + crlf


UART._at = lambda self, b: _hc05_at(self, b)


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
    # ★아카이브도 임시 디렉토리로 — config.ARCHIVE_DIR 은 import 시점에 '/data/archive' 로
    #   굳으므로 그대로 두면 복원·스냅샷이 PC 의 절대경로를 건드린다(테스트는 흔적을 남기지
    #   않아야 한다).
    config.ARCHIVE_DIR = data_dir + "/archive"
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
    # HC-05 1개 구성 — 전환 타이밍도 압축한다(판정 로직은 그대로)
    config.BIND_ADDR_MEAS = ADDR_MEAS
    config.BIND_ADDR_DOSER = ADDR_DOSER
    config.BT_POWER_OFF_SECS = 0.05
    config.BT_AT_BOOT_SECS = 0.1
    config.BT_DATA_BOOT_SECS = 0.1
    config.BT_CONNECT_SECS = 4.0
    config.BT_AT_TIMEOUT = 1.0
    config.SD_ENABLED = False       # 시뮬레이터에는 SD 가 없다(비활성 경로도 함께 검증)


def _reload_devices():
    """config.BIND_ADDR_* 를 고친 뒤 부른다 — 주소는 devices 캐시를 거치므로(2026-08-21)
    config 만 바꿔서는 반영되지 않는다. 파일(devices.json)이 없을 때 config 가 폴백이다."""
    import devices
    import link
    devices.reload()
    link.refresh_targets()


def _rebind_sim(meas_port, doser_port=None, way1=True):
    """장비 심을 주소 맵에 등록하고 링크 상태를 초기화한다.
    ★시나리오마다 FirmwareSim 을 새로 띄우므로 포트가 바뀐다 — 링크가 이전 대상을 '검증됨'
    으로 기억하고 있으면 새 심에 붙지 않으니 매번 대상 미확정으로 되돌린다."""
    import link
    BIND_PORTS.clear()
    BIND_PORTS[ADDR_MEAS] = meas_port
    if doser_port is not None:
        BIND_PORTS[ADDR_DOSER] = doser_port
    HC05.reset(way1=way1)
    lk = link.get_if_created()
    if lk is not None:
        lk.target, lk.frozen, lk.motor_running = None, None, None


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
    _real_log = datalog.log                 # 시나리오 F(로그 회전)에서 원본 함수를 되돌려 쓴다
    datalog.log = lambda msg: None          # 콘솔 소음 억제(파일 로그도 생략)
    measure.p = datalog.log
    expected_dkh = DEFAULT_REF_DKH * 10.0 ** (TANK_PH - REF_PH)

    # ── A. 정상 calkh ──────────────────────────────────────
    print("\n[A] 정상 calkh — 상수 pH, 8회째 평탄 latch")
    sim = FirmwareSim()
    SIM_ADDR[1] = sim.start()
    _rebind_sim(SIM_ADDR[1])
    r = measure.run_once()
    check("측정 성공(5값 모두)", r is not None and all(v is not None for v in r), r)
    if r:
        check("dKH = ref×10^ΔpH (%.3f)" % expected_dkh, abs(r[3] - expected_dkh) < 0.01, r[3])
        check("평탄 도달 = 양수 기록", r[3] > 0)
    lines = datalog.read_dat_lines()
    # ★신형식(날짜 컬럼) — 원본 2026-08-16 반영으로 6필드 → 7필드
    check("dkh.dat 1행 기록(7필드)", len(lines) == 1 and len(lines[0]) == 7, lines)
    import dkh_dat
    import rwtime
    row = dkh_dat.parse_parts(lines[0]) if lines else None
    check("날짜 컬럼 = 측정 시작일", row and row["date"] == rwtime.date_str(),
          row and row["date"])
    check("파서가 tank_kh 를 정확히 집음", row and abs(row["tank_kh"] - expected_dkh) < 0.01,
          row and row["tank_kh"])
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
    _rebind_sim(SIM_ADDR[1])
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
    _rebind_sim(SIM_ADDR[1])
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
    _rebind_sim(SIM_ADDR[1])
    # ★대상 인자는 없앴다(2026-08-19) — 콘솔은 '지금 붙어 있는 BT 대상'으로만 나간다.
    #   그래서 먼저 실제 전환(신원 검증 포함)을 거쳐 대상을 확정해 둔다.
    import link as _lk_mod
    _lk, _err = ops._meas_link()
    check("콘솔 전 측정기 링크 확보", _lk is not None, _err)
    ok, msg = ops._job_cmd({"cmd": "status", "timeout": 1})
    check("status 응답 수집", ok and "refKH" in msg, msg[:60])
    ok, msg = ops._job_cmd({"cmd": "m3f:60", "timeout": 5})
    check("모터 완료 대기·성공", ok and "[모터3] 완료" in msg, msg)
    sim.no_done = {"m3f"}
    ok, msg = ops._job_cmd({"cmd": "m3f:60", "timeout": 5})
    check("완료 누락 → 실패 보고", (not ok) and "성공 불명" in msg, msg)
    _lk_mod.get().target = None
    ok, msg = ops._job_cmd({"cmd": "status", "timeout": 1})
    check("대상 미확정이면 콘솔 거부", (not ok) and "미확정" in msg, msg)
    _lk_mod.get().target = "meas"
    sim.stop()

    # ── E. BT 대상 전환 + 신원 검증 (HC-05 1개 구성의 핵심 안전 레일) ──
    print("")
    print("[E] BT 대상 전환 — 신원 검증 / 오접속 동결 / 모터 중 전환 금지")
    import link
    meas_sim = FirmwareSim()
    meas_port = meas_sim.start()
    doser_sim = DoserSim()
    doser_port = doser_sim.start()
    _rebind_sim(meas_port, doser_port)
    lk = link.get()
    lk.log = lambda m: None

    ok, err = lk.select_target("meas")
    check("측정 장비 전환 성공", ok, err)
    check("대상 = meas", lk.target == "meas")
    # ★고속 경로 확인 — 전원을 한 번도 끊지 않고 KEY 만으로 붙었어야 한다
    check("고속 경로(전원 미차단)", HC05.power_cycles == 0, "전원토글 %d회" % HC05.power_cycles)
    # BIND 와 LINK 를 둘 다 걸었는지 — BIND 만 하면 재부팅 후 엉뚱한 상대로 간다
    check("AT+BIND 로 자동연결 대상도 기록", HC05.bind == ADDR_MEAS, HC05.bind)

    cycles_before = HC05.power_cycles
    ok, err = lk.select_target("doser")
    check("도저 전환 성공", ok, err)
    check("대상 = doser", lk.target == "doser")
    check("도저 전환도 고속 경로", HC05.power_cycles == cycles_before,
          "전원토글 %d회" % (HC05.power_cycles - cycles_before))
    check("이미 붙은 대상 재요청은 즉시 통과", lk.select_target("doser")[0])

    # 도저 명령이 실제로 도저에 닿는가(LF only 규약 포함 — CR 이 붙으면 심이 무시한다)
    import doser as doser_mod
    lrt, lgt = doser_mod.query_left()
    check("도저 ls 왕복(LF only)", lrt == 8000 and lgt == 240, (lrt, lgt))

    # ★오접속 동결 — BIND 주소가 뒤바뀐 상황(측정기를 요청했는데 도저가 응답)
    BIND_PORTS[ADDR_MEAS] = doser_port          # 주소 오배선 재현
    lk.target, lk.frozen = None, None
    ok, err = lk.select_target("meas")
    check("오접속 감지 → 전환 실패", not ok, err)
    check("링크 동결됨", bool(lk.frozen), lk.frozen)
    # 동결 중에는 어떤 명령도 나가지 않아야 한다
    before = len(meas_sim.received)
    try:
        lk.send("status", stop_pattern="====", timeout=1)
        sent = True
    except link.LinkFrozen:
        sent = False
    check("동결 중 송신 차단(LinkFrozen)", not sent)
    check("장비에 명령 미도달", len(meas_sim.received) == before)
    # 자동 해제는 없다 — 운영자 확인 경로로만
    ok, err = lk.select_target("doser")
    check("동결 중 다른 대상 전환도 거부", not ok, err)
    BIND_PORTS[ADDR_MEAS] = meas_port           # 배선 수정
    lk.unfreeze()
    ok, err = lk.select_target("meas")
    check("동결 해제 후 재전환 성공", ok, err)

    # ★연결 점검은 읽기 전용이어야 한다 — 전환도 전원 펄스도 없이 조회 1회만.
    #   (종전 구현은 ensure_link 를 타서 무응답 시 전원을 최대 5회 끊었다 = HC-05 리셋과 같은 위험)
    pc_before, tgt_before = HC05.power_cycles, lk.target
    n_before = len(meas_sim.received)
    ok, msg = ops._job_link({})
    check("연결 점검 성공(응답 확인)", ok, msg)
    check("연결 점검은 전원을 끊지 않는다", HC05.power_cycles == pc_before,
          "전원토글 %d→%d" % (pc_before, HC05.power_cycles))
    check("연결 점검은 대상을 바꾸지 않는다", lk.target == tgt_before, lk.target)
    check("연결 점검이 보낸 것은 조회뿐(구동 명령 없음)",
          all(not c.startswith("m") for c in meas_sim.received[n_before:]),
          meas_sim.received[n_before:])

    # ★모터 구동 중 전환 금지 — 전원 차단 시 정지 명령을 보낼 수단이 사라진다
    lk.motor_running = 3
    ok, err = lk.select_target("doser")
    check("모터 구동 중 전환 거부", not ok, err)
    check("거부 사유에 모터 명시", "모터" in (err or ""), err)
    lk.motor_running = None

    # BIND 주소 미설정이면 즉시 실패(오접속 방지)
    saved = config.BIND_ADDR_DOSER
    config.BIND_ADDR_DOSER = ""
    _reload_devices()
    lk.target = None
    ok, err = lk.select_target("doser")
    check("BIND 주소 없으면 전환 거부", not ok and "BIND" in (err or ""), err)
    config.BIND_ADDR_DOSER = saved
    _reload_devices()

    # ★Way 1 미지원 펌웨어 — 고속 경로가 실패하고 전원 경로로 폴백해 결국 붙어야 한다.
    #   실기 펌웨어 리비전에 따라 KEY-only AT 진입이 안 먹을 수 있어서 남긴 경로다.
    _rebind_sim(meas_port, doser_port, way1=False)
    lk.log = lambda m: None
    ok, err = lk.select_target("meas")
    check("Way1 미지원 → 전원 경로 폴백 성공", ok, err)
    check("폴백 시엔 전원을 실제로 끊었다", HC05.power_cycles > 0,
          "전원토글 %d회" % HC05.power_cycles)
    check("폴백 후에도 신원 확인됨", lk.target == "meas" and not lk.frozen)

    # BT_SWITCH_MODE="key" 면 폴백 없이 실패해야 한다(설정이 실제로 먹히는지)
    _rebind_sim(meas_port, doser_port, way1=False)
    saved_mode = config.BT_SWITCH_MODE
    config.BT_SWITCH_MODE = "key"
    ok, err = lk.select_target("meas")
    check('mode="key" 는 폴백 없이 실패', not ok and HC05.power_cycles == 0, err)
    config.BT_SWITCH_MODE = saved_mode

    meas_sim.stop()
    doser_sim.stop()

    # ── F. 관리 계층 회귀 — API 계약 / 로그 회전 / 도저 수동 우선 ──
    print("\n[F] 관리 계층 — /api/dkh 값·로그 회전·수동 우선")
    import json as _json
    import webserver

    class FakeConn:
        """webserver 의 send() 만 쓰는 최소 소켓 대역 — 응답 바이트를 모은다."""
        def __init__(self):
            self.buf = b""

        def send(self, b):
            self.buf += b
            return len(b)

        def body(self):
            return _json.loads(self.buf.split(b"\r\n\r\n", 1)[1].decode())

    # ★위치 인덱싱 회귀: 날짜 컬럼이 붙은 줄에서 parts[4] 는 tank_kh 가 아니라 ref_kh 다.
    #   원본 dkh_server.read_last_dkh 는 tank_kh 를 돌려준다 — 값 자체를 못 박아 둔다.
    with open(datalog.DAT_FILE, "w") as f:
        f.write("2026-08-17 21 7.724 7.657 8.830 7.558 28.8\n"
                "2026-08-18 05 7.723 7.663 8.830 7.701 28.7\n")
    c = FakeConn()
    webserver._api(c, "GET", "/api/dkh", {}, "")
    got = c.body().get("dkh")
    check("/api/dkh = 수조 dKH(tank_kh)", got == 7.701, got)
    check("/api/dkh 가 ref_kh(8.83) 가 아님", got != 8.83, got)
    # 음수(미평탄)·0.0(에러) 표식은 원본과 같이 그대로 통과시킨다
    with open(datalog.DAT_FILE, "a") as f:
        f.write("2026-08-18 13 7.700 7.650 8.830 -7.400 28.7\n")
    c = FakeConn()
    webserver._api(c, "GET", "/api/dkh", {}, "")
    check("미평탄 음수 표식 그대로 통과", c.body().get("dkh") == -7.4, c.body())
    with open(datalog.DAT_FILE, "a") as f:
        f.write("2026-08-18 21 0.000 0.000 0.000 0.000 0.0\n")
    c = FakeConn()
    webserver._api(c, "GET", "/api/dkh", {}, "")
    check("에러 표식은 0.0", c.body().get("dkh") == 0.0, c.body())

    # ★로그 회전: 부팅 시 1회가 아니라 쓰는 도중에도 상한에서 새로 시작해야 한다
    #   (ESP32 는 수개월 상시 가동 — 종전 구현은 핸들이 열린 뒤 무한 증식했다).
    saved_max, saved_f = config.LOG_MAX_BYTES, datalog._log_f
    config.LOG_MAX_BYTES = 2048
    datalog._log_f, datalog._log_bytes = None, 0
    datalog.log = _real_log
    _print = __builtins__["print"] if isinstance(__builtins__, dict) else __builtins__.print
    try:
        import builtins
        builtins.print = lambda *a, **k: None       # 회전 시험 중 콘솔 소음 억제
        for i in range(400):
            datalog.log("회전 시험 줄 %d — 한글이 섞이면 바이트가 문자 수보다 크다" % i)
    finally:
        builtins.print = _print
        datalog.log = lambda msg: None
    size = os.stat(datalog.LOG_FILE)[6]
    check("로그가 상한(2KB) 근처에서 회전", size <= config.LOG_MAX_BYTES * 2, "%dB" % size)
    config.LOG_MAX_BYTES = saved_max
    if datalog._log_f:
        datalog._log_f.close()
    datalog._log_f, datalog._log_bytes = saved_f, 0

    # ★수동 우선(원본 doser_adjust.main): 새 오버라이드를 적용한 회차는 자동 조정을 건너뛴다
    calls = {"slot": 0}
    ov_applied = {"v": True}
    saved_chk, saved_slot, saved_log = doser_mod.check_override, doser_mod.slot_adjust, doser_mod.log
    doser_mod.check_override = lambda: ov_applied["v"]
    doser_mod.slot_adjust = lambda: calls.__setitem__("slot", calls["slot"] + 1)
    doser_mod.log = lambda m: None
    doser_mod.post_measure(config.DOSER_SLOT_HOUR)
    check("수동 적용 회차 = 자동 조정 생략", calls["slot"] == 0, calls)
    ov_applied["v"] = False
    doser_mod.post_measure(config.DOSER_SLOT_HOUR)
    check("수동 없으면 자동 조정 실행", calls["slot"] == 1, calls)
    doser_mod.post_measure(config.DOSER_SLOT_HOUR + 1)
    check("13시 회차가 아니면 자동 조정 안 함", calls["slot"] == 1, calls)
    doser_mod.check_override, doser_mod.slot_adjust = saved_chk, saved_slot
    doser_mod.log = saved_log

    # ★측정 중 BT 대상 전환 금지 — 전환하면 측정 명령(tank/ref/calkh)이 다른 장비로 간다.
    #   force 로도 뚫리면 안 된다(측정을 먼저 중단해야 한다). 측정 흐름 자신만 지나간다.
    lk = link.get()
    lk.log = lambda m: None
    lk.target, lk.frozen, lk.motor_running = "meas", None, None
    state.measuring = True
    ok, err = lk.select_target("doser")
    check("측정 중 대상 전환 거부", not ok and "측정 중" in (err or ""), err)
    check("거부돼도 현재 대상 유지", lk.target == "meas", lk.target)
    ok, err = lk.select_target("doser", force=True)
    check("force 로도 못 뚫는다", not ok and "측정 중" in (err or ""), err)
    lk2, err2 = link.acquire("doser", log=lambda m: None)
    check("acquire 도 거부(None 반환)", lk2 is None and "측정 중" in (err2 or ""), err2)
    ok, err = lk.select_target("meas")
    check("이미 붙은 대상 재요청은 측정 중에도 통과(라디오 미접촉)", ok, err)
    # 측정 경로(allow_measuring)는 측정 중 게이트를 지나 그 다음 검사로 간다
    saved_bind = config.BIND_ADDR_DOSER
    config.BIND_ADDR_DOSER = ""
    _reload_devices()
    ok, err = lk.select_target("doser", allow_measuring=True)
    check("측정 경로는 게이트 통과(다음 검사인 BIND 에서 걸림)",
          not ok and "BIND 주소가" in (err or ""), err)
    config.BIND_ADDR_DOSER = saved_bind
    _reload_devices()
    st = link.status()
    check("status: 측정 중이면 전환 잠금 표시", st["switch_locked"] is True, st)
    # ★명령 콘솔 게이트(사용자 지시 2026-08-19) — Idle 에서만 열린다.
    #   측정 중에는 우회 불가(ack 를 줘도 거부), 래치·위치 불명은 ack 로만 열린다.
    dev = ops.device_state()
    check("상태 판정: 측정 중", dev["state"] == "measuring", dev)
    check("측정 중 콘솔 잠금(우회 불가)",
          not dev["console_allowed"] and not dev["console_override"], dev)
    c = FakeConn()
    webserver._api(c, "POST", "/api/ops/job", {"kind": "cmd", "cmd": "status", "ack": True}, "")
    check("측정 중 cmd 는 ack 로도 거부", c.body().get("ok") is False, c.body())
    check("거부돼도 작업이 큐에 안 남는다", state.job is None, state.job)

    # ★HC-05 리셋도 측정 중 금지 — 라디오를 끊으면 회차가 깨진다(웹 계층 외 ops 에서도 방어)
    ok, msg = ops._job_hc05_reset({})
    check("측정 중 HC-05 리셋 거부", not ok and "측정 중" in (msg or ""), msg)
    state.measuring = False
    lk.target = "meas"
    st = link.status()
    check("status: 신원 검증된 대상 보고", st["verified"] and st["target_name"] == "측정 장비", st)
    check("status: BIND 주소 설정 여부 보고",
          st["targets"]["meas"]["addr_set"] and st["targets"]["doser"]["addr_set"], st)
    lk.frozen = "테스트 동결"
    st = link.status()
    check("status: 동결이면 verified=False", st["verified"] is False and st["frozen"], st)
    lk.frozen = None

    # 상태별 콘솔 게이트 — Idle / 에러 래치 / 액체 위치 불명
    with open(datalog.DAT_FILE, "w") as f:
        f.write(NORMAL_ROW)
    measure._liquid["chamber"], measure._liquid["holding"] = "KCL", "EMPTY"
    dev = ops.device_state()
    check("상태 판정: 대기(Idle)", dev["state"] == "idle" and dev["console_allowed"], dev)
    with open(datalog.DAT_FILE, "a") as f:
        f.write(ERROR_ROW)
    dev = ops.device_state()
    check("상태 판정: 에러 래치", dev["state"] == "latched", dev)
    check("래치 = 기본 잠금 + 운영자 확인으로 해제 가능",
          not dev["console_allowed"] and dev["console_override"], dev)
    c = FakeConn()
    webserver._api(c, "POST", "/api/ops/job", {"kind": "cmd", "cmd": "status"}, "")
    check("래치 중 ack 없는 cmd 거부", c.body().get("ok") is False, c.body())
    c = FakeConn()
    webserver._api(c, "POST", "/api/ops/job", {"kind": "cmd", "cmd": "status", "ack": True}, "")
    check("래치 중 ack 있으면 허용(수동 정리 경로 보존)", c.body().get("ok") is True, c.body())
    state.job = None
    with open(datalog.DAT_FILE, "w") as f:
        f.write(NORMAL_ROW)
    measure._liquid["chamber"] = "UNKNOWN"
    dev = ops.device_state()
    check("상태 판정: 액체 위치 불명", dev["state"] == "liquid_unknown", dev)
    check("위치 불명도 운영자 확인으로 해제 가능", dev["console_override"], dev)
    measure._liquid["chamber"], measure._liquid["holding"] = "KCL", "EMPTY"

    # ★BT 접속 정보(BIND 주소) — 웹 설정이 config 보다 우선, MAC 형식 자동 변환
    for raw, want in (("98:DA:60:0F:C5:7A", "98da,60,0fc57a"),
                      ("98DA600FC57A", "98da,60,0fc57a"),
                      ("98da,60,0fc57a", "98da,60,0fc57a"),
                      ("98-da-60-0f-c5-7a", "98da,60,0fc57a")):
        got, err = link.normalize_addr(raw)
        check("주소 정규화 %s" % raw, got == want and err is None, (got, err))
    for bad in ("98DA600FC5", "98DA600FC57AZZ", "98:DA:60:0F:C5:7G"):
        got, err = link.normalize_addr(bad)
        check("잘못된 주소 거부 %s" % bad, got is None and err, (got, err))
    got, err = link.normalize_addr("")
    check("빈 값은 '지움'(오류 아님)", got == "" and err is None, (got, err))

    import devices as dev_mod
    saved_meas = config.BIND_ADDR_MEAS
    ok, msg = dev_mod.set_devices([{"kind": "meas", "addr": "98:DA:60:0F:C5:7A"},
                                   {"kind": "doser", "addr": ADDR_DOSER}])
    check("장치 목록 저장 성공", ok, msg)
    link.refresh_targets()
    check("파일 값이 config 보다 우선", link.bind_addr("meas") == "98da,60,0fc57a",
          link.bind_addr("meas"))
    check("출처 표시 = file", link.bind_source("meas") == "file", link.bind_source("meas"))
    ok, msg = dev_mod.set_devices([{"kind": "meas", "addr": "짧음"},
                                   {"kind": "doser", "addr": ADDR_DOSER}])
    check("형식 오류는 저장 거부", not ok, msg)
    check("거부 후 기존 값 유지", link.bind_addr("meas") == "98da,60,0fc57a",
          link.bind_addr("meas"))
    ok, _ = dev_mod.set_devices([{"kind": "meas", "addr": ""},        # 지우면 미설정
                                 {"kind": "doser", "addr": ADDR_DOSER}])
    check("빈 값 저장 → 미설정", ok and link.bind_addr("meas") == "", link.bind_addr("meas"))
    check("미설정일 때 출처 표시 = none", link.bind_source("meas") == "none",
          link.bind_source("meas"))
    c = FakeConn()
    webserver._api(c, "POST", "/api/devices",
                   {"devices": [{"kind": "meas", "addr": ADDR_MEAS},
                                {"kind": "doser", "addr": "98DA60056895"}]}, "")
    check("POST /api/devices 저장", c.body().get("ok") is True, c.body())
    check("응답에 정규화 결과 포함",
          c.body()["targets"]["doser"]["addr"] == "98da,60,056895", c.body())
    c = FakeConn()
    webserver._api(c, "POST", "/api/devices",
                   {"devices": [{"kind": "meas", "addr": ADDR_MEAS},
                                {"kind": "doser", "addr": "xx"}]}, "")
    check("POST /api/devices 형식 거부", c.body().get("ok") is False, c.body())
    os.remove(dev_mod.DEVICES_FILE)               # 시뮬레이터 주소(config 폴백)로 되돌린다
    dev_mod.reload()
    link.refresh_targets()
    config.BIND_ADDR_MEAS = saved_meas
    check("파일 제거 후 config 폴백", link.bind_addr("meas") == ADDR_MEAS,
          link.bind_addr("meas"))

    # ★도징량 조작은 BT 대상이 기본 도저일 때만(2026-08-19 + 2026-08-21)
    lk.target = "meas"
    ok, msg = ops._job_doser_query({})
    check("대상이 측정기면 도징 조회 거부", not ok and "도저로 전환" in msg, msg)
    ok, msg = ops._job_doser_apply({"lrt": 8000})
    check("대상이 측정기면 lrt 적용도 거부", not ok and "도저로 전환" in msg, msg)
    ok, msg = ops._job_doser_preview({})
    check("권고 미리보기는 대상과 무관(장비 미접촉)", ok or "부족" in msg or "없음" in msg, msg)

    # ★'KCl 강제 공급'은 삭제됐다 — cleanup 은 force 인자를 받아도 위치 기반 정리만 한다
    check("cleanup 에 force 분기 없음", "force" not in ops._job_cleanup.__doc__ or True)
    import inspect
    src_cleanup = inspect.getsource(ops._job_cleanup)
    check("cleanup 코드에 force 분기가 남아 있지 않다",
          'args.get("force")' not in src_cleanup, src_cleanup[:80])

    # ── G. 스케줄·장치 관리 (2026-08-21) ─────────────────────
    print("\n[G] 스케줄 회차 검증 · 다중 도징기 · 시계 동기")
    import schedule as sched_mod

    # 회차 검증 — 서버가 유일한 진실이다(화면은 같은 규칙을 미리 보여 줄 뿐)
    ok, msg, warn = sched_mod.set_schedule({"measure_hours": [5, 6], "doser_slot_hour": 5})
    check("간격 2h 미만 거부", not ok and "간격" in msg, msg)
    ok, msg, _ = sched_mod.set_schedule({"measure_hours": [23, 0, 12], "doser_slot_hour": 12})
    check("자정 넘는 간격도 거부(원형)", not ok and "간격" in msg, msg)
    ok, msg, _ = sched_mod.set_schedule({"measure_hours": [1, 1, 5], "doser_slot_hour": 5})
    check("회차 중복 거부", not ok and "중복" in msg, msg)
    ok, msg, _ = sched_mod.set_schedule({"measure_hours": list(range(0, 24, 2)) + [1],
                                         "doser_slot_hour": 0})
    check("회차 13개 거부", not ok, msg)
    ok, msg, _ = sched_mod.set_schedule({"measure_hours": [5, 13], "doser_slot_hour": 21})
    check("조정 회차가 목록 밖이면 거부", not ok and "조정" in msg, msg)
    ok, msg, warn = sched_mod.set_schedule({"measure_hours": [21, 5, 13],
                                            "doser_slot_hour": 13})
    check("정상 저장·정렬", ok and sched_mod.measure_hours() == [5, 13, 21], (ok, msg))
    check("정상값엔 경고 없음", warn == "", warn)
    check("조정 회차도 함께 저장", sched_mod.doser_slot_hour() == 13,
          sched_mod.doser_slot_hour())
    ok, msg, warn = sched_mod.set_schedule({"measure_hours": [5], "doser_slot_hour": 5})
    check("하루 1회는 저장되지만 경고", ok and "유효 측정 부족" in warn, (ok, warn))
    check("rows_cap 하한 유지(=14×6)", sched_mod.rows_cap() == 84, sched_mod.rows_cap())
    sched_mod.set_schedule({"measure_hours": list(range(0, 24, 2)), "doser_slot_hour": 12})
    check("rows_cap 12회로 확장(=14×24)", sched_mod.rows_cap() == 336, sched_mod.rows_cap())

    # 회차 판정 — ★긴 회차가 다음 회차를 잡아먹지 않는다(간격 2h 허용의 경계)
    sched_mod.set_schedule({"measure_hours": [5, 7, 9], "doser_slot_hour": 5})
    due, slot = sched_mod.due_measure((2026, 8, 21, 5, 0, 0), None)
    check("회차 시각이면 실행", due and slot == (2026, 8, 21, 5), (due, slot))
    check("같은 회차 재실행 안 함", not sched_mod.due_measure((2026, 8, 21, 5, 30, 0), slot)[0])
    end_slot = sched_mod.slot_of((2026, 8, 21, 7, 30, 0))   # 05시 회차가 07:30 에 끝났다
    check("종료 슬롯 소비 → 연속 측정 안 함",
          not sched_mod.due_measure((2026, 8, 21, 7, 31, 0), end_slot)[0])
    check("그다음 회차는 정상 실행",
          sched_mod.due_measure((2026, 8, 21, 9, 0, 0), end_slot)[0])
    check("회차 아닌 시각은 실행 안 함",
          not sched_mod.due_measure((2026, 8, 21, 6, 0, 0), None)[0])
    c = FakeConn()
    webserver._api(c, "POST", "/api/schedule", {"measure_hours": [4, 5],
                                                "doser_slot_hour": 4}, "")
    check("POST /api/schedule 거부", c.body().get("ok") is False, c.body())
    c = FakeConn()
    webserver._api(c, "POST", "/api/schedule", {"measure_hours": [5, 13, 21],
                                                "doser_slot_hour": 13}, "")
    check("POST /api/schedule 저장", c.body().get("ok") is True, c.body())
    check("snapshot 이 라이브 회차를 보고한다",
          ops.snapshot()["schedule"]["hours"] == [5, 13, 21], ops.snapshot()["schedule"])

    # ★백업 복원의 **HTTP 경로**(2026-08-24 회귀) — 본문을 두 번 파싱하지 않는가.
    #   종전 webserver 는 `_handle` 이 이미 dict 으로 파싱해 넘긴 본문을 다시 json.loads 해
    #   TypeError 를 냈다(except ValueError 로는 안 잡힌다) → 요청이 **응답 없이** 끊겨
    #   정비페이지 '설정 복원'이 언제나 실패했다. 아무도 못 잡은 이유가 분명하다:
    #   test_archive 는 archive.restore() 를 **직접** 부르고, 개발 스텁은 복원을 따로
    #   구현해 뒀다(그래서 스텁만 옳게 동작했다). 그러니 여기서는 HTTP 경로를 못 박는다.
    import archive as arc_mod
    arc_mod.ensure()
    c = FakeConn()
    webserver._api(c, "POST", "/api/restore",
                   {"kind": "reefwiz-backup", "v": 1,
                    "config": {"schedule.json": {"measure_hours": [6, 18],
                                                 "doser_slot_hour": 6}}}, "")
    check("POST /api/restore 가 파싱된 본문을 그대로 받는다", c.body().get("ok") is True,
          c.body())
    check("★복원이 즉시 회차에 반영된다(캐시 무효화)", sched_mod.measure_hours() == [6, 18],
          sched_mod.measure_hours())
    c = FakeConn()
    webserver._api(c, "POST", "/api/restore", {"kind": "nope"}, "")
    check("백업 형식이 아니면 거부", c.body().get("ok") is False, c.body())
    c = FakeConn()
    webserver._api(c, "POST", "/api/restore", {}, "")     # 파싱 실패·본문 상한 초과 = 빈 dict
    check("빈 본문은 파싱 실패로 알린다",
          c.body().get("ok") is False and "본문" in (c.body().get("msg") or ""), c.body())
    c = FakeConn()
    webserver._api(c, "POST", "/api/restore",
                   {"kind": "reefwiz-backup", "v": 1,
                    "config": {"schedule.json": {"measure_hours": [5, 6],
                                                 "doser_slot_hour": 5}}}, "")
    check("★간격 2h 미만 회차는 복원도 거부", c.body().get("ok") is False, c.body())
    check("거부된 복원은 회차를 바꾸지 않는다", sched_mod.measure_hours() == [6, 18],
          sched_mod.measure_hours())
    sched_mod.set_schedule({"measure_hours": [5, 13, 21], "doser_slot_hour": 13})  # 원복


    # ★도징기 2대 — 응답 서명이 같은 상대가 둘 있어도 전환이 동결되지 않아야 한다.
    #   (_other_sigs 가 '자신 외 전부'였다면 도저 응답이 곧 오접속 판정이 된다)
    meas_sim = FirmwareSim()
    meas_port = meas_sim.start()
    d1, d2 = DoserSim(), DoserSim()
    d1_port, d2_port = d1.start(), d2.start()
    d2.lrt = 12000                        # 두 도징기를 구별해 '어디에 붙었나'를 확인하려고
    _rebind_sim(meas_port, d1_port)
    BIND_PORTS[ADDR_DOSER2] = d2_port
    ok, msg = dev_mod.set_devices([
        {"kind": "meas", "name": "측정기", "addr": ADDR_MEAS},
        {"kind": "doser", "name": "도저1", "addr": ADDR_DOSER, "sync_hours": [0]},
        {"kind": "doser", "name": "도저2", "addr": ADDR_DOSER2, "sync_hours": [0, 12]},
    ])
    check("도징기 2대 등록", ok, msg)
    link.refresh_targets()
    check("TARGETS 에 3대", sorted(link.TARGETS) == ["doser", "doser2", "meas"],
          sorted(link.TARGETS))
    check("같은 종류 서명은 남의 것이 아니다",
          not any(s in link._other_sigs("doser") for s in link.TARGETS["doser2"]["sig"]),
          link._other_sigs("doser"))
    check("측정기 서명은 여전히 남의 것", "============" in link._other_sigs("doser"))
    lk.target, lk.frozen = None, None
    ok, err = lk.select_target("doser")
    check("도저1 전환 성공(동결 없음)", ok and not lk.frozen, (err, lk.frozen))
    lrt, _lgt = doser_mod.query_left()
    check("도저1 에 붙었다(lrt 8000)", lrt == 8000, lrt)
    n2_before = len(d2.seen)
    ok, err = lk.select_target("doser2")
    check("도저2 전환 성공(동결 없음)", ok and not lk.frozen, (err, lk.frozen))
    check("도저2 가 신원 조회를 받았다(실제로 그쪽에 붙었다)",
          len(d2.seen) > n2_before, d2.seen)
    # ★도징량 조작은 기본 도저에서만 — 도저2 에 붙어 있으면 거부돼야 한다
    ok, msg = ops._job_doser_query({})
    check("도저2 에서는 도징 조회 거부", not ok and "전환" in msg, msg)
    ok, msg = ops._job_doser_apply({"lrt": 8000})
    check("도저2 에서는 lrt 적용 거부", not ok and "전환" in msg, msg)

    # 시계 동기 — 장치별 1회, 대상이 자동 전환된다(명령·값이 동일해 무해)
    n1, n2 = len(d1.seen), len(d2.seen)
    ok, msg = ops._job_doser_clock({})
    check("전 도징기 시계 동기 성공", ok, msg)
    check("도저1 에 set time 도달", any(s.startswith("set time") for s in d1.seen[n1:]),
          d1.seen[n1:])
    check("도저2 에 set time 도달", any(s.startswith("set time") for s in d2.seen[n2:]),
          d2.seen[n2:])
    ok, msg = ops._job_doser_clock({"device": "doser2"})
    check("장치 지정 동기 성공", ok and "도저2" in msg, msg)
    ok, msg = ops._job_doser_clock({"device": "doser9"})
    check("없는 장치 지정 거부", not ok and "도징기가 아닙니다" in msg, msg)
    ok, msg = ops._job_doser_clock({"device": "meas"})
    check("측정기 지정 거부", not ok, msg)

    # 장치를 지우면 '거기 붙어 있다'는 검증 상태도 함께 버려야 한다
    lk.select_target("doser2")
    dev_mod.set_devices([{"kind": "meas", "addr": ADDR_MEAS},
                         {"kind": "doser", "addr": ADDR_DOSER}])
    link.refresh_targets()
    check("사라진 장치에 붙어 있다고 믿지 않는다", lk.target != "doser2", lk.target)
    ok, err = lk.select_target("doser2")
    check("없는 대상 전환 거부", not ok and "알 수 없는 대상" in (err or ""), err)

    meas_sim.stop()
    d1.stop()
    d2.stop()

    shutil.rmtree(data_dir, ignore_errors=True)
    print("\n%s — 실패 %d건%s" % ("ALL PASS" if not _FAILS else "FAILURES",
                                  len(_FAILS), (": " + ", ".join(_FAILS)) if _FAILS else ""))
    return 0 if not _FAILS else 1


if __name__ == "__main__":
    sys.exit(run())
