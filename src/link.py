# 측정 장비 링크 계층 — 원본 measure_kh_once.py 의 시리얼 헬퍼 이식.
# 무선 구간이 HC-06(장비)~HC-05(ESP32 유선) 이므로 RF 순단 대응 정책(keepalive,
# 송신 전 연결확인, 재연결-재송신, 모터 재시도 전 정지)은 원본 그대로 유지한다.
# 원본과 다른 점: COM close→open 대신 HC-05 전원 재투입으로 라디오 자체를 재기동
# (Windows 에서 불가능했던 하드 복구). UART 송신은 라디오 사망과 무관하게 즉시
# 성공하므로 write_timeout 좀비 문제는 구조적으로 사라짐 — 판정은 항상 응답 기준.
#
# ★HC-05 1개로 두 장비를 번갈아 쓴다(사용자 확정 2026-08-18). 측정기와 도저는 main 루프가
#   순차 실행하므로 동시 연결이 필요 없다. 전환 경로는 둘이다(config.BT_SWITCH_MODE):
#     고속(key)   = 전원 유지, KEY↑ → AT+DISC → AT+BIND → AT+LINK → KEY↓. 회당 1~2초.
#     폴백(power) = KEY↑ 상태로 전원 재투입 → AT(38400) → AT+BIND → 전원 재투입. 8~12초.
#   고속 경로는 데이터시트(ZG1643) 'AT 모드 진입 Way 1'(전원 켠 채 PIN34 를 올리면 AT 모드)
#   에 기댄다. ★진입 후 AT 콘솔은 38400 이다 — ZS-040 실측(펌웨어 3.0-20170601)·데이터시트
#   모두 풀 AT 모드는 38400 고정이고, '통신 보레이트 그대로'의 미니 AT 는 명령이 일부만 먹는
#   버그라 안 쓴다. 그래도 못 먹는 리비전이 있어 전원 폴백을 남긴다(_rebind_key 상세 주석).
#
# ★★전환의 핵심 안전 레일 — 신원 검증:
#   AT+BIND 가 OK 를 돌려줘도 *실제로 누구에게 붙었는지*는 알 수 없다(바인드 주소 오타,
#   상대 모듈 전원 꺼짐, 이전 연결 잔류 등). 바인드가 어긋난 채 명령을 보내면 도저 명령이
#   측정기로 가거나 그 반대가 되는 사고가 난다. 그래서 전환 후 첫 구동 명령 이전에
#   무해한 조회로 상대의 응답 서명을 확인하고, ★다른 장비의 서명이 오면 즉시 동결한다.
#   원본의 "거짓 성공은 있으면 안 된다"(이송 전 airoff·ton 검증) 원칙을 링크 계층에 적용한 것.
#
# ★★전환 전 모터 정지 확인:
#   전환 중에는 장비에 정지 명령을 보낼 수단이 없다 — 고속 경로는 KEY 를 올린 동안 데이터
#   채널이 AT 콘솔로 바뀌고 AT+DISC 로 RF 링크를 끊으며, 폴백 경로는 아예 전원을 끊는다.
#   모터가 도는 중에 그러면 시약이 계속 주입되므로, 진입 조건에 "구동 중인 모터 없음"을 건다.
import time
import re
from machine import UART, Pin

import config
import devices
import rwtime
import state
import version
import watchdog


def _decode(b):
    """UTF-8 안전 디코드 — 순단으로 멀티바이트가 잘려도 죽지 않게."""
    if not b:
        return ""
    try:
        return b.decode("utf-8")
    except (UnicodeError, ValueError):
        return "".join(chr(c) if c < 0x80 else "?" for c in b)


def _parse_bind(line):
    """'+BIND:98DA:60:56895' → '98da,60,056895'. 실패하면 None.

    ★펌웨어가 각 구간의 **앞 0 을 떼고** 답한다(실측): 실제 주소가 `98da,60,056895` 인데
      조회 응답은 `98DA:60:56895` 다. 그대로 정규화하면 16진수 11자리라 형식 오류가 난다.
      구간별로 자릿수를 채워 복원한다."""
    if ":" not in line:
        return None
    parts = line.split(":", 1)[1].strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return "%04x,%02x,%06x" % (int(parts[0], 16), int(parts[1], 16), int(parts[2], 16))
    except ValueError:
        return None


def _motor_index(cmd):
    """'m1f:70' → 1. 모터 구동 명령이 아니면 None."""
    m = re.match(r"m([1-4])[fb]:", cmd)
    return int(m.group(1)) if m else None


class LinkFrozen(Exception):
    """신원 검증 실패로 링크가 동결됨 — 어느 장비에 붙었는지 모르는 상태.
    이 상태에서는 어떤 구동 명령도 내보내지 않는다(오장비 명령 사고 방지).
    해제는 운영자가 정비페이지의 'BT 대상 전환'/'래치해제'로 명시적으로 한다."""


# ── 장치 레지스트리 연결 (BIND 주소·이름·종류) ──
# ★2026-08-19: 종전에는 config.py 를 고쳐 다시 올려야 주소를 넣을 수 있었다(실장 전 필수
#   작업인데 소스 수정이 필요했다). 정비페이지에서 넣으면 파일에 저장되고 그 값이 우선한다.
# ★2026-08-21: 주소·이름·시계 동기 시각을 **devices.py**(`/data/devices.json`)로 옮겼다 —
#   종전 bt.json 의 {meas, doser} 두 칸 구조로는 도징기를 2대 이상 둘 수 없었다. 여기서는
#   그 레지스트리를 '전환 대상 표(TARGETS)'로 펼쳐 쓴다. 표의 구조는 종전과 같아서
#   호출부(ops.py·measure.py·테스트)는 무변경이다.
normalize_addr = devices.normalize_addr      # 종전 API 재노출(link.normalize_addr 호출부 보존)


def bind_addr(key):
    """대상의 BIND 주소 — 레지스트리 조회. 없거나 미설정이면 빈 문자열."""
    d = devices.get(key)
    return ((d["addr"] if d else "") or "").strip()


def dev_pswd(key):
    """대상의 접속 암호(상대 HC-06 의 PIN) — 없으면 빈 문자열 = '지금 값 그대로 둔다'."""
    d = devices.get(key)
    return ((d.get("pswd") if d else "") or "").strip()


def bind_source(key):
    """주소의 출처 — 정비페이지가 '어디서 온 값인지'를 보여 준다."""
    return devices.source() if bind_addr(key) else "none"


# ── 전환 대상 표 (장치 레지스트리에서 파생) ──
# probe = 부작용 없는 조회 명령, sig = 그 **종류**만 내는 응답 조각.
# eol   = 줄 종단(도저 펌웨어는 CR 이 붙으면 명령을 실행하지 않는다 — 원본 확인).
TARGETS = {}
_link = None                        # 싱글턴 Link(모듈 하단 get() 참조) — refresh 가 만진다


def refresh_targets():
    """레지스트리 → TARGETS 재구축. 장치 목록을 저장한 뒤 부른다.

    ★**제자리 갱신**(clear+update)인 이유: `link.TARGETS` 를 그대로 들고 있는 코드가
    여럿이라(ops.py, 테스트) 새 dict 으로 바꾸면 낡은 표를 보게 된다."""
    new = {}
    for d in devices.all_devices():
        kind = devices.KINDS[d["kind"]]
        new[d["id"]] = {
            "bind": (lambda i=d["id"]: bind_addr(i)),
            "pswd": (lambda i=d["id"]: dev_pswd(i)),
            "probe": kind["probe"], "sig": kind["sig"], "eol": kind["eol"],
            "name": d["name"], "kind": d["kind"],
            "sync_hours": list(d.get("sync_hours") or ()),
            "primary": devices.is_primary_doser(d["id"]),
        }
    TARGETS.clear()
    TARGETS.update(new)
    # 목록에서 사라진 장치에 '붙어 있다고 검증됨' 상태로 남아 있으면 안 된다 — 재검증시킨다.
    if _link is not None and _link.target is not None and _link.target not in TARGETS:
        _link.target = None
    return TARGETS


refresh_targets()


def _other_sigs(target):
    """대상과 **다른 종류**의 장비 서명 — 교차 검출용(엉뚱한 장비에 붙었는지).

    ★같은 종류의 서명은 제외한다(2026-08-21). 도저 펌웨어 응답은 모든 도징기가 동일해서
    (`ls` → "왼쪽 동작") 이걸 '남의 서명'으로 넣으면 정상 응답이 곧바로 오접속 판정이 된다
    — `_ask()` 는 theirs 를 mine 보다 **먼저** 보므로 도저2 를 등록하는 순간 도저 전환이
    매번 동결된다. 즉 신원 검증이 보장하는 것은 "요청한 **종류**가 응답했다"까지이고,
    도징기끼리의 구분은 원리적으로 불가능하다 → 오장비 위험이 있는 명령(lrt)은 기본 도저
    에만 허용한다(ops._require_primary_doser)."""
    mine = TARGETS[target]["kind"] if target in TARGETS else None
    out = []
    for spec in TARGETS.values():
        if spec["kind"] == mine:
            continue
        for s in spec["sig"]:
            if s not in out:
                out.append(s)
    return out


class Link:
    def __init__(self, uart_id=None, tx=None, rx=None, power_pin=None, key_pin=None):
        self.uart = UART(uart_id if uart_id is not None else config.BT_UART_ID,
                         baudrate=config.BAUD,
                         tx=tx if tx is not None else config.BT_TX,
                         rx=rx if rx is not None else config.BT_RX,
                         timeout=200, timeout_char=50, rxbuf=2048)
        pp = config.BT_POWER_PIN if power_pin is None else power_pin
        kp = config.BT_KEY_PIN if key_pin is None else key_pin
        # 전원 제어 극성 — ZS-040 은 EN 핀이 LDO enable 이라 HIGH=ON 이다(기본값).
        # 별도 MOSFET 스위치를 만든 경우에만 config.BT_POWER_ACTIVE_HIGH 로 뒤집는다.
        # 부팅 직후부터 켜진 상태로 시작한다(전원 인가 시 KEY=LOW → 데이터 모드).
        self._pol = 1 if config.BT_POWER_ACTIVE_HIGH else 0
        self.power = Pin(pp, Pin.OUT, value=self._pol) if pp is not None else None
        self.key = Pin(kp, Pin.OUT, value=0) if kp is not None else None
        # STATE(코어 PIN32) — 배선했으면 연결 여부를 하드웨어로 즉시 안다.
        sp = getattr(config, "BT_STATE_PIN", None)
        self.state = Pin(sp, Pin.IN) if sp is not None else None
        # ★부팅 보호(2026-08-28): 직전 실행이 KEY 를 HIGH 로 남긴 채 죽었으면 HC-05 가 AT 모드에
        #   갇혀 있다(EN 전원차단이 듣지 않는 배선에서는 재부팅으로도 안 풀린다). 링크를 만들 때
        #   한 번 확실히 내려 데이터 모드로 되돌린다.
        if self.key is not None:
            self.key.value(0)
        self.target = None          # 현재 붙어 있다고 *검증된* 대상. None=미확인
        self.frozen = None          # 동결 사유(문자열) 또는 None
        self.motor_running = None   # 구동 중인 모터 번호(전환 금지 조건)
        self.log = print            # measure.py 가 파일 로거로 교체
        # 정비페이지 표시용 흔적 — status() 는 웹 스레드에서 불리므로 UART 를 만질 수 없다.
        # 그래서 "마지막으로 응답을 확인한 시각"과 "마지막 RF 이벤트"를 여기에 남겨 둔다.
        self.last_ok_at = None      # 신원 서명/핑 응답을 마지막으로 확인한 시각
        # 대상 id → {"ver","model","version","serial","at"} — 상대 펌웨어의 판(1회 캐시).
        self.dev_ver = {}
        self.last_event = None      # {"kind","detail","at"} — 전환·재연결 이력의 최신 1건

    # ── 저수준 ──

    @property
    def eol(self):
        spec = TARGETS.get(self.target)
        return spec["eol"] if spec else b"\r\n"

    def flush_input(self):
        while self.uart.any():
            self.uart.read()

    def write_line(self, s):
        self.uart.write(s.encode() + self.eol)

    def readline(self):
        return _decode(self.uart.readline()).strip()

    def _event(self, kind, detail=""):
        """RF 이벤트 기록 — 전환·재연결 이력을 남겨 사후 진단·백오프 튜닝에 쓴다.
        ★SD 원장을 뺐으므로(2026-08-18) 측정 로그(measure_kh.log)로 간다 — `[rf] ` 로 검색.
        기록이 실패해도 링크 동작에는 영향이 없다.
        ★최신 1건은 메모리에도 남긴다 — 정비페이지가 로그를 뒤지지 않고 바로 보여 준다."""
        self.last_event = {"kind": kind, "detail": detail, "at": rwtime.stamp()}
        try:
            self.log("[rf] %s target=%s%s"
                     % (kind, self.target, (" " + detail) if detail else ""))
        except Exception:
            pass

    def _set_baud(self, baud):
        self.uart.init(baudrate=baud, bits=8, parity=None, stop=1,
                       tx=config.BT_TX, rx=config.BT_RX,
                       timeout=200, timeout_char=50, rxbuf=2048)

    def _power(self, on):
        if self.power is not None:
            self.power.value(self._pol if on else (1 - self._pol))

    def _power_cycle(self, key_high=False, boot_secs=None):
        """전원 재투입 — HC-05 하드 복구 겸 모드 전환의 유일한 수단.
        key_high=True 면 KEY 를 올린 채 전원을 넣어 AT 명령 모드로 부팅시킨다.
        전원 핀이 배선되지 않았으면 no-op(그 경우 전환 기능은 못 쓴다)."""
        if self.power is None:
            return False
        if self.key is not None:
            self.key.value(1 if key_high else 0)
        self._power(False)
        time.sleep(config.BT_POWER_OFF_SECS)
        self._power(True)
        if boot_secs is None:
            boot_secs = config.BT_AT_BOOT_SECS if key_high else config.BT_DATA_BOOT_SECS
        time.sleep(boot_secs)
        return True

    def at_reset(self):
        """★소프트 리셋 — `AT+RESET` 으로 모듈을 재시작한다(2026-08-28 실측 도입).

        전원 재투입의 **대체 수단**이다. 이 보드의 헤더 `EN` 은 전원 차단이 아니라 KEY(PIN34)
        여서(실측) VCC 를 끊을 방법이 없는데, `AT+RESET` 이 같은 일을 해 준다:
            AT+RESET → 'OK / +DISC:SUCCESS / OK' → STATE 가 1→0 으로 **실제로 링크가 끊긴다**.
        ★`AT+DISC` 만으로는 안 끊긴다(CMODE 와 무관하게 STATE 가 1 로 유지됨 — 실측). 대상 전환·
        좀비 복구처럼 '지금 붙어 있는 것을 확실히 떼야' 할 때는 반드시 이 경로를 쓴다.

        ★AT 콘솔 보레이트가 상태에 따라 다르다(실측): **연결 중이면 통신 보레이트(9600)**,
        미연결이면 38400. 그래서 두 값을 모두 시도한다. 반환: 리셋 명령이 먹었는가."""
        if self.key is None:
            return False
        self.key.value(1)
        time.sleep(config.BT_KEY_SETTLE_SECS)
        done = False
        try:
            for baud in (config.BAUD, config.BT_AT_BAUD):
                self._set_baud(baud)
                ok, _lines = self._at("AT")
                if not ok:
                    continue
                ok, lines = self._at("AT+RESET")
                done = ok or any("DISC" in ln for ln in lines)
                break
        finally:
            self._set_baud(config.BAUD)
            self.key.value(0)
            time.sleep(config.BT_KEY_SETTLE_SECS)
        if done:
            # 재시작 후 데이터 모드로 올라올 시간(실측: 1초로는 부족)
            time.sleep(config.BT_RESET_WAIT_SECS * 2)
        return done

    def _wait_state(self, secs):
        """STATE 핀이 '연결'로 올라올 때까지 기다린다. 반환: 걸린 초 또는 None(시간 초과).

        ★고정 대기로 재면 안 된다(2026-08-29 실측): 같은 절차인데 회차마다 1.2~5.5초로
          흔들려서, 8초 고정 대기 뒤 한 번만 확인하는 방식은 붙었는데도 '실패'로 오판했다."""
        if self.state is None:
            time.sleep(secs)
            return None
        end = rwtime.deadline_ms(secs)
        t0 = time.ticks_ms()
        while rwtime.before(end):
            watchdog.feed()
            if self.state.value():
                return time.ticks_diff(time.ticks_ms(), t0) / 1000.0
            time.sleep_ms(200)
        return None

    def _pulse_reset(self):
        """라디오 좀비 상태 복구 — ★AT+RESET(소프트) 우선, 전원 핀이 있으면 폴백으로 전원 재투입.
        종전에는 전원 재투입만 썼는데, 이 보드는 EN 이 전원 차단이 아니라 그 경로가 무효였다."""
        if self.at_reset():
            return True
        return self._power_cycle(key_high=False)

    # ── AT 모드 / 대상 전환 ──

    def _at(self, cmd, timeout=None):
        """AT 명령 1개 송신 후 응답 줄 수집. 'OK' 를 받았는지와 응답을 함께 돌려준다."""
        if timeout is None:
            timeout = config.BT_AT_TIMEOUT
        self.flush_input()
        self.uart.write(cmd.encode() + b"\r\n")
        lines, deadline = [], rwtime.deadline_ms(timeout)
        while rwtime.before(deadline):
            watchdog.feed()
            if self.uart.any():
                ln = self.readline()
                if ln:
                    lines.append(ln)
                    if ln == "OK" or ln.startswith("ERROR"):
                        break
            else:
                time.sleep_ms(20)
        return ("OK" in lines), lines

    def _apply_pswd(self, pswd):
        """붙기 직전에 마스터의 PIN 을 대상에 맞춘다(빈 값이면 기본 PIN).

        ★대상마다 다시 넣어야 한다: `AT+PSWD` 는 HC-05 **자기 값**이라 모듈에 저장은 되지만
          (리셋에도 남는다 — 실측), 상대가 바뀌면 그 값은 더 이상 맞지 않는다.
        ★따옴표 형식이다(펌웨어 3.0-20170601 실측): `AT+PSWD="1234"` 가 먹고 조회는 `+PIN:"1234"`.
        ★★빈 값은 '건드리지 않는다'가 아니라 **기본 PIN**(config.BT_DEFAULT_PSWD)이다:
          PIN 을 넣은 장비에 붙은 뒤 안 넣은 장비로 가면 마스터에 남은 PIN 이 그대로 쓰여
          조용히 못 붙는다. 대상마다 PIN 을 항상 확정한다."""
        pswd = pswd or getattr(config, "BT_DEFAULT_PSWD", "")
        if not pswd:
            return True, ""
        ok, lines = self._at('AT+PSWD="%s"' % pswd)
        if not ok:
            return False, "AT+PSWD 실패: %s" % (lines or "(응답 없음)")
        return True, ""

    def _rebind_key(self, addr, pswd=""):
        """★고속 경로 — 전원을 끊지 않고 KEY 만으로 대상을 바꾼다(회당 1~2초).

        데이터시트(ZG1643) AT 모드 진입 Way 1: 전원이 켜진 상태에서 PIN34(KEY)를 HIGH 로
        올리면 AT 모드로 들어간다. 주 (3) "When PIN34 keeps high level, all commands can
        be used" — 설정을 넣는 동안 KEY 를 계속 올려 두고, 마지막 리셋 때만 내린다.

        ★보레이트 주의(2026-08-26 실측): 데이터시트는 Way1 에서 '통신값 그대로'(9600)라고
        하지만, 실장 모듈(펌웨어 VERSION:3.0-20170601)은 KEY↑ 만으로도 **AT 콘솔이 38400**
        이었다. 종전 코드는 여기서 9600 으로 AT 를 보내 매번 무응답 → 8~12초 전원 폴백으로
        떨어졌다("고속 전환이 안 먹는다"의 정체). 리비전마다 다를 수 있어 38400→9600 순서로
        진입 보레이트를 탐색하고, 빠져나갈 때 반드시 데이터 보레이트(9600)로 되돌린다.

        ★★실측으로 확정한 전환 절차(2026-08-29, 측정기↔도저1↔도저2 3방향 성공):
            KEY↑ → AT+RESET(기존 연결 해제) → AT+RMAAD → ROLE=1 → CMODE=0 → PSWD → BIND=<주소>
            → AT+RESET → **부팅 중 KEY↓** → 데이터 모드로 부팅하며 자동연결 → 신원검증
        - `AT+RMAAD` 가 없으면 BIND 를 무시하고 예전 본딩 상대로 붙는다(실측: 도저1 을 바인드했는데
          측정기에 붙어 신원검증이 'wrong' 으로 차단).
        - 이 펌웨어는 `AT+LINK` 가 항상 FAIL 이라 '지금 즉시 붙이기'가 안 된다. 실제 연결은
          **데이터 모드로 부팅할 때의 BIND 자동연결**로만 성립한다.
        - ★그 부팅을 `AT+RESET` 으로 만든다(2026-08-29 실측): 리셋을 보낸 **직후 부팅되는 동안
          KEY 를 내리면** 데이터 모드로 부팅해 자동연결이 걸린다. **전원 차단이 필요 없다** —
          MOSFET 전원 게이팅 없이 측정기↔도저1↔도저2 3방향 전환을 1.2~1.6초에 확인했다."""
        if self.key is None:
            return False, "KEY 핀(BT_KEY_PIN) 미배선 — 고속 전환 불가"
        self.key.value(1)
        time.sleep(config.BT_KEY_SETTLE_SECS)
        try:
            # ★진입 보레이트 탐색(실측 2026-08-28): AT 콘솔 보레이트가 **연결 상태에 따라 다르다** —
            #   연결 중이면 통신 보레이트(9600), 미연결이면 38400. 전환은 대개 '붙어 있는 상태'에서
            #   시작하므로 9600 을 먼저 본다.
            ok = False
            lines = []
            for baud in (config.BAUD, config.BT_AT_BAUD):
                self._set_baud(baud)
                ok, lines = self._at("AT")
                if ok:
                    break
            if not ok:
                return False, "KEY AT 모드 무응답(KEY↑ 안 됨/보레이트 불일치?): %s" % (
                    lines or "(없음)")
            # ★기존 연결 해제는 AT+RESET 으로 한다(실측): `AT+DISC` 는 SUCCESS 를 돌려줘도
            #   STATE 가 1 로 남아 **실제로 안 끊긴다**. 붙은 채로 BIND 를 바꾸면 다음 연결이
            #   엉뚱해지므로, 여기서 확실히 떼고 시작한다.
            self._at("AT+RESET")
            # ★리셋 후 모듈이 다시 올라올 때까지 기다린다(실측: 1초로는 부족해 AT 가 무응답이었다).
            #   콘솔 보레이트도 미연결 기준(38400)으로 바뀌므로 두 값을 번갈아 재시도한다.
            ok = False
            for _try in range(config.BT_RESET_TRIES):
                time.sleep(config.BT_RESET_WAIT_SECS)
                for baud in (config.BT_AT_BAUD, config.BAUD):
                    self._set_baud(baud)
                    ok, lines = self._at("AT")
                    if ok:
                        break
                if ok:
                    break
            if not ok:
                return False, "리셋 후 AT 무응답: %s" % (lines or "(없음)")
            # ★AT+RMAAD 가 맨 앞이다(2026-08-29 실측): 저장된 본딩을 지우지 않으면 이 펌웨어는
            #   **BIND 주소를 무시하고 예전 본딩 상대로 붙는다**(도저1 을 바인드했는데 측정기에
            #   붙어 신원검증이 'wrong' 으로 잡아낸 실측 사례). 본딩을 비우면 BIND 대상에만 붙는다.
            for cmd in ("AT+RMAAD", "AT+ROLE=1", "AT+CMODE=0"):
                ok, lines = self._at(cmd)
                if not ok:
                    return False, "%s 실패: %s" % (cmd, lines or "(응답 없음)")
            # ★PIN 은 BIND 앞에 넣는다 — 틀린 PIN 이면 자동연결이 조용히 실패한다(실측: 20초 무연결).
            ok, err = self._apply_pswd(pswd)
            if not ok:
                return False, err
            ok, lines = self._at("AT+BIND=%s" % addr)
            if not ok:
                return False, "AT+BIND 실패: %s" % (lines or "(응답 없음)")
            # ★AT+LINK 는 쓰지 않는다(실측): 펌웨어 3.0-20170601 에서 항상 FAIL 이다.
            # ★★전원 재투입을 대신하는 마지막 한 걸음(2026-08-29 실측): `AT+RESET` 을 보낸 뒤
            #   **부팅되는 동안 KEY 를 내려** 데이터 모드로 부팅시키면 CMODE=0 의 BIND 자동연결이
            #   그대로 걸린다. 종전에는 BIND 만 넣고 KEY 를 내려 끝냈는데, 그러면 모듈이 재부팅을
            #   하지 않으므로 자동연결이 **아예 발동하지 않았다** — 그래서 전원 차단이 필요했던 것이다
            #   (원인은 '연결 정보가 안 지워져서'가 아니라 '자동연결을 발동시킬 부팅이 없어서'였다).
            #   측정기↔도저1↔도저2 3방향 전환을 1.2~1.6초에 확인했다. 전원 게이팅 없이 완결된다.
            self._at("AT+RESET", timeout=config.BT_AT_TIMEOUT)
            self.key.value(0)               # ★부팅 중에 내린다 — 순서가 핵심(먼저 내리면 AT 를 못 보낸다)
            self._set_baud(config.BAUD)
            time.sleep(config.BT_RESET_WAIT_SECS)
            el = self._wait_state(config.BT_CONNECT_SECS)
            if el is None:
                # STATE 가 안 올라와도 실패로 단정하지 않는다 — 신원 검증(_probe_identity)이
                # 최종 판정이고, STATE 핀이 미배선인 설치도 있다.
                self.log("  [BT] 리셋 후 %g초 내 자동연결 미확인 — 신원 검증으로 판정"
                         % config.BT_CONNECT_SECS)
            else:
                self.log("  [BT] 자동연결 %.1f초" % el)
        finally:
            self._set_baud(config.BAUD)    # ★데이터 보레이트로 복귀(빠져나가는 모든 경로)
            self.key.value(0)              # ★어느 경로로 빠져나가든 데이터 모드로 되돌린다
            time.sleep(config.BT_KEY_SETTLE_SECS)
        return True, ""

    def _rebind_power(self, addr, pswd=""):
        """폴백 경로 — AT 모드로 부팅해 바인드 주소를 바꾼 뒤 데이터 모드로 복귀시킨다.

        Way 1(고속 경로)이 안 먹는 펌웨어 리비전과, 라디오가 좀비라 AT 조차 응답하지 않는
        상태를 위해 남긴다. ROLE/CMODE/UART 를 매번 다시 넣는 이유는 전원 이상으로 설정이
        날아간 모듈을 조용히 잘못된 역할로 쓰는 것보다 매번 확정하는 편이 안전해서다.

        ★★KEY 잔류 버그 수정(2026-08-28 실측): 종전에는 실패 경로가 KEY 를 **HIGH 로 둔 채**
        return 했다. EN 전원차단이 듣지 않는 배선(실측: 이 보드)에서는 그러면 HC-05 가 **AT 모드에
        갇혀** 이후 모든 데이터 통신이 죽는다(연결은 살아 있는데 응답이 없어 '전환 에러'가 반복된다).
        어느 경로로 빠져나가든 KEY 를 내리고 데이터 보레이트로 되돌린다."""
        if not self._power_cycle(key_high=True):
            return False, "전원 제어 핀(BT_POWER_PIN) 미배선 — 전원 경로 전환 불가"
        try:
            self._set_baud(config.BT_AT_BAUD)
            ok, lines = self._at("AT")
            if not ok:
                return False, "AT 모드 무응답(KEY/전원 배선 확인): %s" % (lines or "(없음)")
            # ★AT+RMAAD 선행 — 고속 경로와 같은 이유(저장된 본딩이 남아 있으면 BIND 를 무시하고
            #   예전 상대로 붙는다). 상세는 _rebind_key 주석 참조.
            for cmd in ("AT+RMAAD", "AT+ROLE=1", "AT+CMODE=0"):
                ok, lines = self._at(cmd)
                if not ok:
                    return False, "%s 실패: %s" % (cmd, lines or "(응답 없음)")
            ok, err = self._apply_pswd(pswd)
            if not ok:
                return False, err
            for cmd in ("AT+BIND=%s" % addr, "AT+UART=%d,0,0" % config.BAUD):
                ok, lines = self._at(cmd)
                if not ok:
                    return False, "%s 실패: %s" % (cmd, lines or "(응답 없음)")
        finally:
            # ★어느 경로로 빠져나가든 데이터 모드로 되돌린다(KEY↓ + 데이터 보레이트).
            self._set_baud(config.BAUD)
            if self.key is not None:
                self.key.value(0)
            time.sleep(config.BT_KEY_SETTLE_SECS)
        # 데이터 모드 복귀 — 전원 재투입(EN 이 듣지 않는 배선이면 위 KEY↓ 만으로도 복귀된다)
        self._power_cycle(key_high=False)
        return True, ""

    def _rebind(self, addr, pswd=""):
        """설정된 방식으로 재바인드. auto 면 고속 경로 시도 후 실패 시 전원 경로로 폴백."""
        mode = getattr(config, "BT_SWITCH_MODE", "auto")
        if mode == "power":
            return self._rebind_power(addr, pswd)
        ok, err = self._rebind_key(addr, pswd)
        if ok or mode == "key":
            return ok, err
        self.log("  [BT] 고속 전환 실패(%s) → 전원 경로로 폴백" % err)
        self._event("rebind_key_fail", err)
        return self._rebind_power(addr, pswd)

    def _ask(self, probe, eol, mine, theirs, timeout):
        """조회를 보내고 서명을 기다린다. 반환: ("ok"|"wrong"|"silent", 줄들).

        ★응답이 없으면 주기적으로 다시 묻는다. 모드 전환(KEY↓) 직후나 재연결 직후에는
        첫 바이트가 유실되기 쉬운데, 한 번만 던지고 기다리면 그 유실이 곧바로 '무응답'
        판정이 되어 불필요한 재전환을 부른다. 조회는 양쪽 다 부작용이 없으므로(status/ls)
        다시 물어도 안전하다."""
        self.flush_input()
        deadline = rwtime.deadline_ms(timeout)
        next_ask = rwtime.deadline_ms(0)      # 첫 바퀴에 즉시 한 번 묻는다
        lines = []
        while rwtime.before(deadline):
            watchdog.feed()
            if not rwtime.before(next_ask):
                self.uart.write(probe.encode() + eol)
                next_ask = rwtime.deadline_ms(config.LINK_PING_TIMEOUT)
            if self.uart.any():
                ln = self.readline()
                if ln:
                    lines.append(ln)
                    if any(s in ln for s in theirs):
                        return "wrong", lines
                    if any(s in ln for s in mine):
                        return "ok", lines
            else:
                time.sleep_ms(20)
        return "silent", lines

    def _probe_identity(self, target):
        """부작용 없는 조회를 보내고 응답 서명으로 상대를 판별한다.
        반환: ("ok" | "wrong" | "silent", 수집한 줄들)
        ★'wrong' 은 다른 장비의 서명이 확인된 경우 — 절대 명령을 보내면 안 되는 상태.

        ★교차 프로브: 대상의 조회에 침묵이 오면 *다른 **종류**의 조회 형식*으로 한 번 더 묻는다.
        장비마다 줄 종단 규약이 달라(측정기 CRLF / 도저 LF only) 엉뚱한 장비에 붙으면 그쪽이
        아예 응답하지 않는 경우가 많은데, 그러면 '무응답'과 '오접속'이 구분되지 않는다.
        둘 다 명령을 안 보내니 안전하기는 같지만, 원인이 '상대 전원 꺼짐'인지 'BIND 주소가
        뒤바뀜'인지에 따라 운영자가 할 일이 완전히 다르다. 조회는 양쪽 다 부작용이 없으므로
        (status / ls) 한 번 더 물어보고 정확한 사유를 남긴다."""
        spec = TARGETS[target]
        theirs = _other_sigs(target)
        verdict, lines = self._ask(spec["probe"], spec["eol"], spec["sig"], theirs,
                                   config.BT_CONNECT_SECS)
        if verdict != "silent":
            return verdict, lines
        # ★종류당 한 번만 묻는다 — 도징기가 여러 대여도 조회 형식은 하나다(중복 발송 방지).
        asked = [spec["kind"]]
        for ospec in TARGETS.values():
            if ospec["kind"] in asked:
                continue
            asked.append(ospec["kind"])
            v, olines = self._ask(ospec["probe"], ospec["eol"], ospec["sig"], (),
                                  config.LINK_PING_TIMEOUT)
            if v == "ok":
                self.log("    [BT] 교차 프로브 응답 — 실제로 붙은 상대는 '%s' 쪽이다"
                         % devices.KINDS[ospec["kind"]]["label"])
                return "wrong", olines
        return "silent", lines

    def _capture_ver(self, target, force=False):
        """검증 직후 상대의 `ver` 한 줄을 읽어 둔다 — 대상당 1회 캐시.

        ★신원 '게이트'가 아니다: 옛 펌웨어에는 `ver` 이 없어 **무응답이 정상**이고, 그걸로
          연결을 막으면 멀쩡한 장비가 죽는다 — 펌웨어를 올리는 중이거나 한 대만 구형이어도
          그 장비가 통째로 못 쓰게 된다.
          지금은 '무엇이 붙어 있는지' 기록·표시까지만 하고, 서명(종류)+BIND 주소(개체)라는
          기존 게이트는 건드리지 않는다. 게이트를 이름·개체로 옮기는 것은 4종이 모두 `ver` 을
          내고 장치 목록에 기대 개체(#)를 등록한 뒤다(README '장비 펌웨어 ver 규약').
        ★대상당 한 번만 묻는다: 펌웨어는 도는 중에 바뀌지 않는다. 매 전환마다 물으면 전환이
          그만큼 느려진다(전환은 측정 회차마다 일어난다). 정비페이지의 '장비 판 조회'가
          force=True 로 다시 읽는다(펌웨어를 올린 직후 확인용)."""
        if not force and target in self.dev_ver:
            return self.dev_ver[target]
        spec = TARGETS.get(target)
        if spec is None:
            return None
        verdict, lines = self._ask("ver", spec["eol"], (version.BRAND,), (),
                                   config.LINK_PING_TIMEOUT)
        info = version.parse_ver(lines) if verdict == "ok" else None
        if info is None:
            # 사실을 남겨 두고 다시 묻지 않는다 — 없는 명령을 전환마다 던질 이유가 없다.
            info = {"ver": None, "model": None, "version": None, "serial": None}
            self.log("  [BT] %s 판 조회 — 응답 없음(`ver` 이 없는 펌웨어)" % spec["name"])
        else:
            expect = devices.KINDS[spec["kind"]].get("models") or ()
            odd = "  ← 이 종류에서 기대하지 않은 이름" if expect and info["model"] not in expect else ""
            self.log("  [BT] %s 판: %s%s" % (spec["name"], info["ver"], odd))
        info["at"] = rwtime.stamp()
        self.dev_ver[target] = info
        return info

    def bound_addr(self):
        """지금 바인드된 주소를 읽는다 — `AT+BIND?` 조회. 실패하면 None.

        ★연결을 끊지 않는다(실측 2026-08-29): KEY↑ 로 AT 콘솔에 들어가 질의하고 KEY↓ 로 나올
          뿐이다. `AT+RESET` 을 보내지 않으므로 붙어 있는 링크가 그대로 유지된다.
        ★왜 필요한가: 도징기가 여러 대면 **응답 서명이 같아** '몇 번 도저'인지 구분되지 않는다.
          BIND 주소가 그 구분을 준다 — 서명(종류) + BIND(개체) 를 함께 봐야 대상이 확정된다.
          어느 한쪽만으로 확정하지 않는 것이 핵심이다: 서명만 보면 도저1/도저2 를 못 가리고,
          BIND 만 보면 '바인드는 됐지만 실제로 그쪽에 붙었는지'를 모른다."""
        if self.key is None:
            return None
        self.key.value(1)
        time.sleep(config.BT_KEY_SETTLE_SECS)
        try:
            for baud in (config.BAUD, config.BT_AT_BAUD):
                self._set_baud(baud)
                ok, _lines = self._at("AT")
                if not ok:
                    continue
                ok, lines = self._at("AT+BIND?")
                for ln in lines:
                    if ln.startswith("+BIND:"):
                        return _parse_bind(ln)
                return None
            return None
        finally:
            self._set_baud(config.BAUD)
            self.key.value(0)
            time.sleep(config.BT_KEY_SETTLE_SECS)

    def identify(self, timeout=None):
        """지금 붙어 있는 상대의 **종류**를 읽는다 — 부작용 없는 조회 1회씩, 아무것도 안 바꾼다.

        반환: 'meas' | 'doser' | None(무응답).
        ★종류까지만 알 수 있다: 도저 응답 서명은 모든 도징기가 동일해서 '몇 번 도저'인지는
          구분되지 않는다. 그래서 여기서 self.target 을 세우지 않는다 — 판단은 호출부 몫이다
          (종류가 유일한 장치 하나뿐이면 그때는 대상이 확정된다).
        ★대상 미확정 상태를 위한 것이다: 부팅 자동연결을 끄면 Link 는 '누구에게 붙었는지'를
          모르지만 라디오는 직전 상대를 그대로 물고 있다(HC-05 는 자체 전원). 그 간극을 메운다."""
        if timeout is None:
            timeout = config.LINK_PING_TIMEOUT
        asked = []
        for spec in TARGETS.values():
            if spec["kind"] in asked:
                continue          # 종류당 한 번만 — 도징기가 여러 대여도 조회 형식은 하나다
            asked.append(spec["kind"])
            verdict, _lines = self._ask(spec["probe"], spec["eol"], spec["sig"], (), timeout)
            if verdict == "ok":
                return spec["kind"]
        return None

    def select_target(self, target, force=False, allow_measuring=False):
        """대상 장비로 전환하고 신원을 검증한다. 이미 검증된 대상이면 즉시 True.

        ★전제조건 ①측정 중이 아닐 것 ②구동 중인 모터가 없을 것.
        ②전환은 라디오 차단이라 모터가 도는 중이면 정지 명령을 보낼 수단이 사라진다
          (시약 계속 주입). force=True 는 운영자가 장비를 눈으로 확인했을 때만 쓴다.
        ①측정 중 전환은 측정 자체를 조용히 망친다 — 측정 명령(tank/ref/calkh)이 도저로
          가거나, 폭기·이송이 끊긴 채 회차가 이어진다. 그래서 **force 로도 못 뚫는다**:
          측정을 먼저 중단(정비페이지 '측정 중단')하고 전환해야 한다. 측정 흐름 자신이
          부르는 경로(measure.make_link)만 allow_measuring=True 로 지나간다."""
        if target not in TARGETS:
            return False, "알 수 없는 대상: %s" % target
        if self.frozen and not force:
            return False, "링크 동결됨(%s) — 정비페이지에서 해제 후 재시도" % self.frozen
        if self.target == target and not force:
            return True, ""      # 이미 그 대상 = 라디오를 건드리지 않는다(측정 중에도 안전)
        if state.measuring and not allow_measuring:
            return False, ("측정 중 — BT 대상 전환 금지(측정 명령이 다른 장비로 갈 수 있다). "
                           "'측정 중단' 후 다시 시도하세요")
        if self.motor_running is not None and not force:
            return False, ("모터 %d 구동 중 — 전환 금지(전환 중에는 정지 명령을 보낼 수 "
                           "없다). 정지 후 재시도" % self.motor_running)

        spec = TARGETS[target]
        addr = spec["bind"]()
        pswd = spec["pswd"]()
        if not addr:
            return False, ("%s의 BIND 주소가 비어 있음 — 오접속 방지를 위해 중단"
                           "(정비페이지 'BT 연결 → 장치 목록'에서 넣으세요)" % spec["name"])
        # ★전원 핀은 더 이상 전제가 아니다(2026-08-28): 이 보드의 헤더 EN 은 KEY 였고 전원 차단
        #   수단이 없다. 전환은 KEY(AT 모드) + AT+RESET + BIND 자동연결로 이뤄지므로 KEY 만 있으면 된다.
        if self.key is None:
            return False, "KEY 핀(BT_KEY_PIN) 미배선 — 대상 전환 불가"

        self.target = None      # 검증 전까지는 '어느 장비인지 모름'

        # ★재바인드 전에 '이미 붙어 있는지' 먼저 본다(2026-08-28 실측 교훈).
        #   self.target 은 메모리 값이라 재부팅·수동 조작 뒤에는 None 이지만, **라디오는 이미
        #   원하는 상대에 붙어 있을 수 있다**(BIND 자동연결). 그 상태에서 곧바로 재바인드하면
        #   AT+RESET 이 멀쩡한 연결을 끊고, 이 펌웨어(3.0-20170601)는 AT+LINK 가 듣지 않아
        #   **다시 붙지 못한다** — 전환 시도가 오히려 링크를 파괴한다. 그래서 STATE 가 붙었다고
        #   말하면 무해한 조회로 신원부터 확인하고, 맞으면 라디오를 건드리지 않는다.
        # ★같은 종류가 여러 대면 이 지름길을 쓰면 안 된다(2026-08-29 실측): 도저 펌웨어의
        #   응답 서명은 모든 도징기가 동일해서(`ls` → "왼쪽 동작") 신원 검증은 '도저인가'까지만
        #   안다. 도저2 에 붙은 채 도저1 을 요청하면 'ok' 가 나와 **재바인드 없이 통과**하고,
        #   그 뒤 도징량(lrt) 명령이 도저2 로 간다. 재바인드가 2초면 끝나므로(전원 재투입이
        #   필요했을 때와 달리) 모호하면 그냥 다시 붙는 편이 싸고 안전하다.
        ambiguous = sum(1 for v in TARGETS.values() if v["kind"] == spec["kind"]) > 1
        if ambiguous:
            self.log("  [BT] %s 는 같은 종류가 여러 대라 신원 서명으로 구분되지 않는다 — 재바인드"
                     % spec["name"])
        if self.state is not None and self.state.value() and not ambiguous:
            verdict, lines = self._probe_identity(target)
            if verdict == "ok":
                self.target = target
                self.last_ok_at = rwtime.stamp()
                self._capture_ver(target)
                self.log("  [BT] %s 이미 연결됨 — 신원 서명 일치(재바인드 생략)" % spec["name"])
                self._event("switch_ok", "already-connected")
                self.flush_input()
                return True, ""
            if verdict == "wrong":
                # ★여기서 동결하면 안 된다(2026-08-29 실측 버그): 이 지점의 '다른 종류가 응답'은
                #   **전환 전이라 당연한 상태**다 — 측정기에 붙어 있다가 도저를 요청하면 측정기가
                #   답하는 게 정상이다. 종전 코드는 이걸 오접속으로 보고 재바인드도 못 해 본 채
                #   얼어붙어서, 측정기↔도저 전환이 첫 시도부터 실패했다.
                #   오접속의 진짜 판정 지점은 **재바인드 이후**다(아래 루프) — 요청한 주소로 바인드
                #   했는데도 다른 종류가 답하면 그때가 주소가 뒤바뀐 것이다.
                self.log("  [BT] 지금은 다른 장비에 붙어 있다 — 재바인드로 전환한다")
                self._event("switch_from_other", " | ".join(lines[:2]))
            # silent = 붙어 있긴 한데 응답이 없다 → 아래 재바인드 경로로 진행

        for attempt in range(1, config.BT_SWITCH_TRIES + 1):
            self.log("  [BT] %s로 전환 시도 %d/%d (bind %s)"
                     % (spec["name"], attempt, config.BT_SWITCH_TRIES, addr))
            ok, err = self._rebind(addr, pswd)
            if not ok:
                self.log("  [BT] 재바인드 실패: %s" % err)
                self._event("rebind_fail", err)
                continue
            verdict, lines = self._probe_identity(target)
            if verdict == "ok":
                self.target = target
                self.last_ok_at = rwtime.stamp()
                self._capture_ver(target)
                self.log("  [BT] %s 연결 확인 — 신원 서명 일치" % spec["name"])
                self._event("switch_ok", "attempt=%d" % attempt)
                self.flush_input()
                return True, ""
            if verdict == "wrong":
                # ★엉뚱한 장비에 붙었다 — 재시도하지 않고 즉시 동결한다. 바인드 주소가
                #   뒤바뀌었을 가능성이 높고, 재시도는 같은 오접속을 반복할 뿐이다.
                self.frozen = ("%s를 요청했는데 다른 종류의 장비가 응답 — BIND 주소가 뒤바뀐 "
                               "것으로 보임(장치 목록의 주소 확인)" % spec["name"])
                self.log("  *[BT] 신원 불일치! %s" % self.frozen)
                self.log("       수신: %s" % " | ".join(lines[:4]))
                self._event("identity_mismatch", " | ".join(lines[:4]))
                return False, self.frozen
            self.log("  [BT] 무응답 — 상대 전원/거리 확인 (시도 %d)" % attempt)
            self._event("switch_silent", "attempt=%d" % attempt)
        self._event("switch_fail", "tries=%d" % config.BT_SWITCH_TRIES)
        return False, "%s 연결 실패(%d회) — 상대 전원·거리·BIND 주소 확인" % (
            spec["name"], config.BT_SWITCH_TRIES)

    def unfreeze(self):
        """운영자 확인 후 동결 해제 — 다음 select_target 이 다시 신원을 검증한다."""
        was, self.frozen, self.target = self.frozen, None, None
        return was

    # ── 원본 이식 ──

    def read_until(self, stop_pattern, timeout=60.0, keepalive=False):
        """stop_pattern 수신까지 읽기. keepalive=True 면 유휴 중 빈 줄 송신(HC-06 드롭 예방)."""
        lines = []
        deadline = rwtime.deadline_ms(timeout)
        next_ka = rwtime.deadline_ms(config.KEEPALIVE_SECS)
        while rwtime.before(deadline):
            watchdog.feed()
            if self.uart.any():
                line = self.readline()
                if line:
                    self.log("    " + line)
                    lines.append(line)
                    next_ka = rwtime.deadline_ms(config.KEEPALIVE_SECS)
                    if stop_pattern in line:
                        return lines
            else:
                time.sleep_ms(20)
                if keepalive and not rwtime.before(next_ka):
                    self.uart.write(self.eol)   # 펌웨어가 빈 줄 무시 — 링크만 깨움
                    next_ka = rwtime.deadline_ms(config.KEEPALIVE_SECS)
        self.log("    [TIMEOUT] '%s' 미수신" % stop_pattern)
        return lines

    def keepalive_sleep(self, secs):
        """유휴를 KEEPALIVE_SECS 청크로 나눠 자며 빈 줄 송신 — 링크 유휴 드롭 예방.
        중단 요청은 청크 경계에서 즉시 반응한다(사전폭기 25분 중에도 12초 내 중단)."""
        end = rwtime.deadline_ms(secs)
        while True:
            watchdog.feed()
            if state.abort_requested:
                raise state.Aborted("유휴 대기 중 중단 요청")
            remaining = rwtime.remaining_s(end)
            if remaining <= 0:
                break
            time.sleep(min(config.KEEPALIVE_SECS, remaining))
            if rwtime.before(end):
                self.uart.write(self.eol)

    def _ping(self):
        """부작용 없는 조회 핑 — 현재 대상의 펌웨어 응답 확인."""
        if self.target is None:
            return False
        # STATE 가 배선돼 있고 LOW 면 RF 링크 자체가 끊긴 것 — 3초 핑 타임아웃을 기다릴
        # 이유가 없다. ★HIGH 라고 해서 검증을 건너뛰지는 않는다: STATE 는 '붙었다'만 알려주고
        # '누구와'는 모르므로, 신원 판정은 언제나 응답 서명으로 한다.
        if self.state is not None and not self.state.value():
            return False
        spec = TARGETS[self.target]
        self.flush_input()
        self.write_line(spec["probe"])
        deadline = rwtime.deadline_ms(config.LINK_PING_TIMEOUT)
        while rwtime.before(deadline):
            watchdog.feed()
            if self.uart.any():
                ln = self.readline()
                if any(s in ln for s in spec["sig"]):
                    self.last_ok_at = rwtime.stamp()
                    self.flush_input()
                    return True
                # ★핑 응답으로 다른 장비 서명이 오면 즉시 동결 — 조용한 오접속 차단
                if any(s in ln for s in _other_sigs(self.target)):
                    self.frozen = "핑 응답이 다른 장비 — 링크가 엉뚱한 곳에 붙어 있음"
                    self.log("    *[BT] %s" % self.frozen)
                    self._event("identity_mismatch", "ping: " + ln)
                    return False
            else:
                time.sleep_ms(20)
        return False

    def reconnect(self, why):
        """전원 재투입으로 재페어링 유도 + 신원 확인. 원본 close→open 상당.
        ★재연결 후에도 신원을 다시 본다 — 순단 사이에 다른 슬레이브가 붙을 수 있다."""
        if self.frozen:
            self.log("    [RF] 링크 동결 상태 — 재연결하지 않음(%s)" % self.frozen)
            return False
        self.log("    [RF] 링크 끊김 — %s → 재연결 시도" % why)
        self._event("reconnect_start", why)
        for i in range(1, config.RECONNECT_TRIES + 1):
            watchdog.feed()
            t0 = time.time()
            # ★모터 구동 중에는 라디오 전원을 끊지 않는다(대상 전환·HC-05 리셋과 같은 규칙):
            #   전원을 끊으면 정지 명령(mNs)을 보낼 수단이 사라진다. 이 구간에선 전원 펄스
            #   없이 재페어링을 기다려 보고, 그래도 안 되면 마지막 시도에서만 펄스를 준다
            #   (그때는 모터 타이머도 끝났고, 펄스 말고는 되살릴 방법이 없다).
            if self.motor_running is not None and i < config.RECONNECT_TRIES:
                self.log("    [RF] 모터 %d 구동 중 — 전원 펄스 생략(정지 명령 경로 보존)"
                         % self.motor_running)
            else:
                self._pulse_reset()
            backoff = config.RECONNECT_BACKOFF[min(i - 1, len(config.RECONNECT_BACKOFF) - 1)]
            time.sleep(backoff)
            self.flush_input()   # 사망 중 고인 스테일 바이트 폐기(원본 7/9 지연 배달 사고 대응)
            if self._ping():
                self.log("    [RF] 재연결 성공 (시도 %d) — 펌웨어 응답 확인" % i)
                self._event("reconnect_ok", "try=%d secs=%.1f" % (i, time.time() - t0))
                return True
            if self.frozen:
                return False
            self.log("    [RF] 재연결 시도 %d/%d 실패" % (i, config.RECONNECT_TRIES))
            self._event("reconnect_retry", "try=%d secs=%.1f" % (i, time.time() - t0))
        self.log("    *[RF] 재연결 %d회 모두 실패 — 링크 복구 불가" % config.RECONNECT_TRIES)
        self._event("reconnect_fail", "tries=%d" % config.RECONNECT_TRIES)
        return False

    def ensure_link(self):
        """명령 송신 직전 링크 생존 확인 — 드롭은 대개 '보낼 때 이미 끊겨 있음'(원본 실측)."""
        if self.frozen:
            return False
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
        (SEND_RETRY_MAX회) → 모터는 재시도 전 정지. 소진 후 (부분/빈) 결과 반환.
        ★동결 상태에서는 아무것도 보내지 않는다(오장비 명령 방지)."""
        if self.frozen:
            raise LinkFrozen(self.frozen)
        self.log("\n-> " + cmd)
        motor_idx = _motor_index(cmd)
        lines = []
        for attempt in range(1, config.SEND_RETRY_MAX + 1):
            if allow_reconnect:
                self.ensure_link()
                if self.frozen:
                    raise LinkFrozen(self.frozen)
                if attempt > 1 and motor_idx is not None:
                    self._stop_motor(motor_idx)
            if motor_idx is not None:
                self.motor_running = motor_idx     # 전환 금지 구간 진입
            try:
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
            except OSError as e:
                # ★원본 send() 정책 3)+4): 송신/수신 중 통신 오류도 재시도 대상이다
                #   (다음 루프의 ensure_link 가 재연결). 마지막 시도에서만 올린다 —
                #   종전 이식본은 여기서 곧바로 전파해 재시도 한 번을 잃었다.
                self.log("    [RF] '%s' 통신 오류: %r" % (cmd, e))
                if allow_reconnect and attempt < config.SEND_RETRY_MAX:
                    continue
                raise
            finally:
                if motor_idx is not None:
                    self.motor_running = None
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


# ── 모듈 싱글턴 ──
# HC-05 가 1개뿐이므로 Link 도 1개다. 종전에는 measure.make_link() 가 호출마다 새 UART 를
# 잡았는데, 이제는 전환 상태(현재 대상·동결 여부)를 들고 있어야 하므로 공유해야 한다.
# (선언은 refresh_targets 가 참조하므로 파일 위쪽 TARGETS 옆에 있다.)

def get():
    """싱글턴 Link — 없으면 생성."""
    global _link
    if _link is None:
        _link = Link()
    return _link


def get_if_created():
    """이미 만들어진 Link 만 돌려준다(없으면 None) — 부팅 중 UART 를 먼저 잡지 않기 위해."""
    return _link


def acquire(target, log=None, force=False, allow_measuring=False):
    """대상 장비로 전환된(=신원 검증된) 링크를 돌려준다. 실패 시 (None, 사유).

    호출부는 반드시 반환값을 확인해야 한다 — 링크를 못 잡았는데 명령을 보내면
    엉뚱한 장비가 받을 수 있다.
    allow_measuring=True 는 측정 흐름 자신만 쓴다(측정이 자기 장비를 잡는 경로)."""
    lk = get()
    if log is not None:
        lk.log = log
    ok, err = lk.select_target(target, force=force, allow_measuring=allow_measuring)
    return (lk, "") if ok else (None, err)


_state_pin = None            # STATE 입력 핀 — 링크가 없을 때도 읽으려고 모듈에 둔다


def state_pin_value():
    """STATE 핀 값 — 미배선이면 None. ★Link 가 아직 없어도 읽을 수 있어야 한다.

    부팅 자동연결을 끄고 **명시적으로 붙이는 운영**(2026-08-29 사용자 확정)에서는 부팅 직후
    Link 가 생성되지 않는다. 종전 status() 는 그 구간에서 state_pin 을 무조건 None 으로
    돌려줬고, 정비페이지는 그걸 '미배선'으로 그렸다 — 배선돼 있는데 매 부팅마다 거짓말을 했다.
    입력 핀 읽기는 UART 를 건드리지 않으므로 웹 스레드에서 불러도 안전하다."""
    global _state_pin
    sp = getattr(config, "BT_STATE_PIN", None)
    if sp is None:
        return None
    try:
        if _state_pin is None:
            _state_pin = Pin(sp, Pin.IN)
        return bool(_state_pin.value())
    except (OSError, ValueError):
        return None


def status():
    """현재 링크 상태 — 정비페이지 표시용.

    ★UART 를 만지지 않는다: 이 함수는 웹 스레드(상태 폴링)에서 불리는데, 여기서 핑을
    보내면 측정 중인 메인 스레드의 응답과 뒤섞인다. 그래서 '지금 살아 있나'를 새로 묻지
    않고, 링크가 남긴 흔적(마지막 응답 확인 시각·최신 RF 이벤트·STATE 핀 레벨)만 읽는다.
    실제 생존 확인이 필요하면 정비페이지의 'BT 연결 점검'(큐 작업)을 쓴다."""
    # ★UI 가 이 표로 전환 버튼과 장치 목록을 그린다 — 순서가 흔들리면 안 되므로 ids 를
    #   따로 준다(레지스트리 순서: 측정기 → 기본 도저 → 도저2..). MicroPython dict 은
    #   JSON 으로 나가면 순서를 보장하지 않는다.
    ids = [d["id"] for d in devices.all_devices()]
    # ★pswd 를 그대로 실어 보내는 이유: 정비페이지의 장치 목록이 이 표로 입력칸을 채운다
    #   (주소도 같은 방식이다). BT 페어링 PIN 이고 주소가 이미 공개되는 화면이라 별도로
    #   가리지 않는다 — 다만 **로그에는 남기지 않는다**(devices.set_devices 참조).
    binds = {k: {"name": v["name"], "kind": v["kind"], "addr_set": bool(v["bind"]()),
                 "addr": v["bind"](), "source": bind_source(k),
                 "pswd": v["pswd"](), "pswd_set": bool(v["pswd"]()),
                 "sync_hours": v["sync_hours"], "primary": v["primary"]}
             for k, v in TARGETS.items()}
    if _link is None:
        return {"target": None, "target_name": "미연결(부팅 후 아직 안 잡음)", "frozen": None,
                "verified": False, "motor_running": None,
                "state_pin": state_pin_value(),
                "last_ok_at": None, "last_event": None, "dev_ver": {},
                "switch_locked": bool(state.measuring), "targets": binds, "ids": ids}
    lk = _link
    spec = TARGETS.get(lk.target)
    return {"target": lk.target,
            "target_name": spec["name"] if spec else "미확정",
            "frozen": lk.frozen,
            # 신원 검증을 통과한 대상이 있고 동결도 아니면 '명령을 보내도 되는 상태'
            "verified": bool(lk.target and not lk.frozen),
            "motor_running": lk.motor_running,
            # STATE 핀은 배선했을 때만 의미가 있다(미배선 None). ★연결 여부만 알려줄 뿐
            # '누구와' 붙었는지는 모르므로 신원 검증을 대체하지 않는다.
            "state_pin": (bool(lk.state.value()) if lk.state is not None else None),
            "last_ok_at": lk.last_ok_at,
            "last_event": lk.last_event,
            # 상대 펌웨어의 판 — 대상 id → {"ver","model","version","serial","at"}.
            # 읽어 둔 것만 담긴다(미접속 대상은 없음). 게이트가 아니라 표시·기록용이다.
            "dev_ver": dict(lk.dev_ver),
            "switch_locked": bool(state.measuring),
            "targets": binds, "ids": ids}
