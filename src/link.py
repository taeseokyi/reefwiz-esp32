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
#   고속 경로는 데이터시트(ZG1643) 'AT 모드 진입 Way 1'(전원 켠 채 PIN34 를 올리면 통신
#   보레이트 그대로 AT 모드)에 기댄다. 펌웨어 리비전에 따라 안 먹을 수 있어 폴백을 남긴다.
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
import rwtime
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


class LinkFrozen(Exception):
    """신원 검증 실패로 링크가 동결됨 — 어느 장비에 붙었는지 모르는 상태.
    이 상태에서는 어떤 구동 명령도 내보내지 않는다(오장비 명령 사고 방지).
    해제는 운영자가 정비페이지의 'BT 대상 전환'/'래치해제'로 명시적으로 한다."""


# ── BT 접속 정보(BIND 주소) — 웹 설정 우선, config 는 폴백 ──
# ★2026-08-19: 종전에는 config.py 를 고쳐 다시 올려야 주소를 넣을 수 있었다(실장 전 필수
#   작업인데 소스 수정이 필요했다). 이제 정비페이지에서 넣으면 /data/bt.json 에 저장되고
#   그 값이 우선한다. 파일에 없으면 config 값으로 떨어진다(기존 배포·테스트 호환).
BT_FILE = config.DATA_DIR + "/bt.json"
_binds = None                       # {"meas": "...", "doser": "..."} 캐시. None=아직 안 읽음


def _load_binds():
    global _binds
    if _binds is None:
        try:
            import json
            with open(BT_FILE) as f:
                d = json.load(f)
            _binds = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _binds = {}
    return _binds


def normalize_addr(s):
    """입력 주소 → HC-05 AT+BIND 형식 'nnnn,nn,nnnnnn'. 반환 (주소, 오류사유).

    ★사람이 아는 형태를 그대로 받는다: 콜론 MAC(98:DA:60:0F:C5:7A), 붙여쓴 MAC
    (98DA600FC57A), 이미 콤마 3구간인 값 모두 같은 결과가 된다(원본 bt_config.json 은
    MAC 을 붙여쓰기로 적어 뒀다). 빈 값은 '지움'이라 오류가 아니다."""
    s = (s or "").strip()
    if not s:
        return "", None
    hexs = ""
    for ch in s:
        if ch in ":-. ,":
            continue
        c = ch.lower()
        if not (("0" <= c <= "9") or ("a" <= c <= "f")):
            return None, "16진수·구분자 외 문자가 있습니다: %r" % ch
        hexs += c
    if len(hexs) != 12:
        return None, "16진수 12자리여야 합니다(입력 %d자리)" % len(hexs)
    return "%s,%s,%s" % (hexs[0:4], hexs[4:6], hexs[6:12]), None


def bind_addr(key):
    """대상(meas/doser)의 BIND 주소 — 웹 설정 우선, 없으면 config 폴백. 없으면 빈 문자열."""
    v = (_load_binds().get(key) or "").strip()
    if v:
        return v
    return (config.BIND_ADDR_MEAS if key == "meas" else config.BIND_ADDR_DOSER) or ""


def bind_source(key):
    """주소의 출처 — 정비페이지가 '어디서 온 값인지'를 보여 준다."""
    if (_load_binds().get(key) or "").strip():
        return "file"
    return "config" if bind_addr(key) else "none"


def set_binds(d, log=None):
    """웹에서 온 주소 저장 — 정규화·검증을 모두 통과해야 하나라도 쓴다. (ok, 메시지).
    빈 문자열은 그 대상의 설정을 지운다(= config 폴백으로 돌아간다)."""
    import json
    if not isinstance(d, dict):
        return False, "형식 오류"
    cur = dict(_load_binds())
    changes = []
    for key in TARGETS:
        if key not in d:
            continue
        addr, err = normalize_addr(d[key])
        if err:
            return False, "%s: %s" % (TARGETS[key]["name"], err)
        if addr:
            cur[key] = addr
            changes.append("%s=%s" % (key, addr))
        else:
            cur.pop(key, None)
            changes.append("%s=지움" % key)
    if not changes:
        return False, "저장할 항목이 없습니다"
    try:
        tmp = BT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f)
        import os as _os
        _os.rename(tmp, BT_FILE)
    except OSError as e:
        return False, "저장 실패: %r" % e
    global _binds
    _binds = cur                     # 캐시 갱신 — 다음 전환부터 새 주소로 붙는다
    msg = "BT 접속 정보 저장 — " + ", ".join(changes)
    if log:
        log("[조치] " + msg)
    return True, msg


# ── 장비별 신원 서명 ──
# probe = 부작용 없는 조회 명령, sig = 그 장비만 내는 응답 조각.
# eol   = 줄 종단(도저 펌웨어는 CR 이 붙으면 명령을 실행하지 않는다 — 원본 확인).
TARGETS = {
    "meas": {
        "bind": lambda: bind_addr("meas"),
        "probe": "status",
        "sig": ("============",),
        "eol": b"\r\n",
        "name": "측정 장비",
    },
    "doser": {
        "bind": lambda: bind_addr("doser"),
        "probe": "ls",
        "sig": ("왼쪽 동작", "왼쪽 휴지"),
        "eol": b"\n",
        "name": "도저",
    },
}


def _other_sigs(target):
    """대상 외 장비들의 서명 — 교차 검출용(엉뚱한 장비에 붙었는지)."""
    out = []
    for k, spec in TARGETS.items():
        if k != target:
            out.extend(spec["sig"])
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
        self.target = None          # 현재 붙어 있다고 *검증된* 대상. None=미확인
        self.frozen = None          # 동결 사유(문자열) 또는 None
        self.motor_running = None   # 구동 중인 모터 번호(전환 금지 조건)
        self.log = print            # measure.py 가 파일 로거로 교체
        # 정비페이지 표시용 흔적 — status() 는 웹 스레드에서 불리므로 UART 를 만질 수 없다.
        # 그래서 "마지막으로 응답을 확인한 시각"과 "마지막 RF 이벤트"를 여기에 남겨 둔다.
        self.last_ok_at = None      # 신원 서명/핑 응답을 마지막으로 확인한 시각
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

    def _pulse_reset(self):
        """하드 리셋 — 라디오 좀비 상태 복구(데이터 모드로 재부팅)."""
        self._power_cycle(key_high=False)

    # ── AT 모드 / 대상 전환 ──

    def _at(self, cmd, timeout=None):
        """AT 명령 1개 송신 후 응답 줄 수집. 'OK' 를 받았는지와 응답을 함께 돌려준다."""
        if timeout is None:
            timeout = config.BT_AT_TIMEOUT
        self.flush_input()
        self.uart.write(cmd.encode() + b"\r\n")
        lines, deadline = [], time.time() + timeout
        while time.time() < deadline:
            if self.uart.any():
                ln = self.readline()
                if ln:
                    lines.append(ln)
                    if ln == "OK" or ln.startswith("ERROR"):
                        break
            else:
                time.sleep_ms(20)
        return ("OK" in lines), lines

    def _rebind_key(self, addr):
        """★고속 경로 — 전원을 끊지 않고 KEY 만으로 대상을 바꾼다(회당 1~2초).

        데이터시트(ZG1643) AT 모드 진입 Way 1: 전원이 켜진 상태에서 PIN34(KEY)를 HIGH 로
        올리면 AT 모드로 들어가고, 보레이트는 **통신값 그대로**(9600)라 전환이 필요 없다.
        주 (3) "When PIN34 keeps high level, all commands can be used" — 전환 내내 KEY 를
        올려둔 채 진행하므로 AT+DISC/AT+BIND/AT+LINK 을 모두 쓸 수 있다.

        AT+BIND 와 AT+LINK 을 둘 다 보내는 이유: BIND 는 '다음 자동 연결 대상'을 기억시키고
        (전원이 나갔다 들어와도 같은 상대로 붙는다), LINK 는 '지금 즉시' 붙인다. 하나만
        쓰면 재부팅 후 엉뚱한 상대로 가거나(BIND 누락) 지금 안 붙는다(LINK 누락)."""
        if self.key is None:
            return False, "KEY 핀(BT_KEY_PIN) 미배선 — 고속 전환 불가"
        self.key.value(1)
        time.sleep(config.BT_KEY_SETTLE_SECS)
        try:
            ok, lines = self._at("AT")
            if not ok:
                return False, "KEY AT 모드 무응답(Way 1 미지원 펌웨어?): %s" % (lines or "(없음)")
            # 현재 연결 해제 — 애초에 안 붙어 있었으면 NO_SLC 가 오는데 이건 정상이다.
            self._at("AT+DISC")
            for cmd in ("AT+ROLE=1", "AT+CMODE=0", "AT+BIND=%s" % addr):
                ok, lines = self._at(cmd)
                if not ok:
                    return False, "%s 실패: %s" % (cmd, lines or "(응답 없음)")
            ok, lines = self._at("AT+LINK=%s" % addr, timeout=config.BT_LINK_TIMEOUT)
            if not ok:
                return False, "AT+LINK 실패(상대 전원/거리/페어링 확인): %s" % (
                    lines or "(응답 없음)")
        finally:
            self.key.value(0)          # ★어느 경로로 빠져나가든 데이터 모드로 되돌린다
            time.sleep(config.BT_KEY_SETTLE_SECS)
        return True, ""

    def _rebind_power(self, addr):
        """폴백 경로 — AT 모드로 부팅해 바인드 주소를 바꾼 뒤 데이터 모드로 복귀시킨다.

        Way 1(고속 경로)이 안 먹는 펌웨어 리비전과, 라디오가 좀비라 AT 조차 응답하지 않는
        상태를 위해 남긴다. ROLE/CMODE/UART 를 매번 다시 넣는 이유는 전원 이상으로 설정이
        날아간 모듈을 조용히 잘못된 역할로 쓰는 것보다 매번 확정하는 편이 안전해서다."""
        if not self._power_cycle(key_high=True):
            return False, "전원 제어 핀(BT_POWER_PIN) 미배선 — 전원 경로 전환 불가"
        self._set_baud(config.BT_AT_BAUD)
        ok, lines = self._at("AT")
        if not ok:
            self._set_baud(config.BAUD)
            return False, "AT 모드 무응답(KEY/전원 배선 확인): %s" % (lines or "(없음)")
        for cmd in ("AT+ROLE=1", "AT+CMODE=0", "AT+BIND=%s" % addr,
                    "AT+UART=%d,0,0" % config.BAUD):
            ok, lines = self._at(cmd)
            if not ok:
                self._set_baud(config.BAUD)
                return False, "%s 실패: %s" % (cmd, lines or "(응답 없음)")
        # 데이터 모드 복귀 — KEY 를 내리고 전원 재투입
        self._set_baud(config.BAUD)
        self._power_cycle(key_high=False)
        return True, ""

    def _rebind(self, addr):
        """설정된 방식으로 재바인드. auto 면 고속 경로 시도 후 실패 시 전원 경로로 폴백."""
        mode = getattr(config, "BT_SWITCH_MODE", "auto")
        if mode == "power":
            return self._rebind_power(addr)
        ok, err = self._rebind_key(addr)
        if ok or mode == "key":
            return ok, err
        self.log("  [BT] 고속 전환 실패(%s) → 전원 경로로 폴백" % err)
        self._event("rebind_key_fail", err)
        return self._rebind_power(addr)

    def _ask(self, probe, eol, mine, theirs, timeout):
        """조회를 보내고 서명을 기다린다. 반환: ("ok"|"wrong"|"silent", 줄들).

        ★응답이 없으면 주기적으로 다시 묻는다. 모드 전환(KEY↓) 직후나 재연결 직후에는
        첫 바이트가 유실되기 쉬운데, 한 번만 던지고 기다리면 그 유실이 곧바로 '무응답'
        판정이 되어 불필요한 재전환을 부른다. 조회는 양쪽 다 부작용이 없으므로(status/ls)
        다시 물어도 안전하다."""
        self.flush_input()
        deadline = time.time() + timeout
        next_ask = 0.0
        lines = []
        while time.time() < deadline:
            if time.time() >= next_ask:
                self.uart.write(probe.encode() + eol)
                next_ask = time.time() + config.LINK_PING_TIMEOUT
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

        ★교차 프로브: 대상의 조회에 침묵이 오면 *다른 장비의 조회 형식*으로 한 번 더 묻는다.
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
        for other, ospec in TARGETS.items():
            if other == target:
                continue
            v, olines = self._ask(ospec["probe"], ospec["eol"], ospec["sig"], (),
                                  config.LINK_PING_TIMEOUT)
            if v == "ok":
                self.log("    [BT] 교차 프로브 응답 — 실제로 붙은 상대는 '%s'" % ospec["name"])
                return "wrong", olines
        return "silent", lines

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
        if not addr:
            return False, ("%s 의 BIND 주소가 config 에 비어 있음 — 오접속 방지를 위해 중단"
                           % spec["name"])
        if self.key is None or self.power is None:
            return False, "KEY/전원 제어 핀 미배선 — 대상 전환 불가"

        self.target = None      # 검증 전까지는 '어느 장비인지 모름'
        for attempt in range(1, config.BT_SWITCH_TRIES + 1):
            self.log("  [BT] %s 로 전환 시도 %d/%d (bind %s)"
                     % (spec["name"], attempt, config.BT_SWITCH_TRIES, addr))
            ok, err = self._rebind(addr)
            if not ok:
                self.log("  [BT] 재바인드 실패: %s" % err)
                self._event("rebind_fail", err)
                continue
            verdict, lines = self._probe_identity(target)
            if verdict == "ok":
                self.target = target
                self.last_ok_at = rwtime.stamp()
                self.log("  [BT] %s 연결 확인 — 신원 서명 일치" % spec["name"])
                self._event("switch_ok", "attempt=%d" % attempt)
                self.flush_input()
                return True, ""
            if verdict == "wrong":
                # ★엉뚱한 장비에 붙었다 — 재시도하지 않고 즉시 동결한다. 바인드 주소가
                #   뒤바뀌었을 가능성이 높고, 재시도는 같은 오접속을 반복할 뿐이다.
                self.frozen = ("%s 를 요청했는데 다른 장비가 응답 — BIND 주소가 뒤바뀐 것으로 "
                               "보임(config.BIND_ADDR_* 확인)" % spec["name"])
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
                    self.uart.write(self.eol)   # 펌웨어가 빈 줄 무시 — 링크만 깨움
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
        deadline = time.time() + config.LINK_PING_TIMEOUT
        while time.time() < deadline:
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
_link = None

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


def status():
    """현재 링크 상태 — 정비페이지 표시용.

    ★UART 를 만지지 않는다: 이 함수는 웹 스레드(상태 폴링)에서 불리는데, 여기서 핑을
    보내면 측정 중인 메인 스레드의 응답과 뒤섞인다. 그래서 '지금 살아 있나'를 새로 묻지
    않고, 링크가 남긴 흔적(마지막 응답 확인 시각·최신 RF 이벤트·STATE 핀 레벨)만 읽는다.
    실제 생존 확인이 필요하면 정비페이지의 'BT 연결 점검'(큐 작업)을 쓴다."""
    binds = {k: {"name": v["name"], "addr_set": bool(v["bind"]()),
                 "addr": v["bind"](), "source": bind_source(k)}
             for k, v in TARGETS.items()}
    if _link is None:
        return {"target": None, "target_name": "미연결(부팅 후 아직 안 잡음)", "frozen": None,
                "verified": False, "motor_running": None, "state_pin": None,
                "last_ok_at": None, "last_event": None,
                "switch_locked": bool(state.measuring), "targets": binds}
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
            "switch_locked": bool(state.measuring),
            "targets": binds}
