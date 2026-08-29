# HC-05 CMODE=1 접속 실험 도구 — 장치(REPL)에서 실행. **벤치 실험 전용**이다.
#
# 왜 있나(2026-08-29): 현재 확정 경로는 `AT+RMAAD → ROLE=1 → CMODE=0 → BIND=<주소> →
#   **콜드 부팅**` 이다(link._rebind_key). CMODE=0 은 주변 탐색 없이 바인드 주소로 바로
#   붙어 빠르지만, 이 펌웨어(3.0-20170601)는 `AT+LINK` 이 항상 FAIL 이라 **전원 재투입이
#   있어야만** 연결이 성립한다 → MOSFET 전원 게이팅이 필요하다.
#   CMODE=1 은 '아무 주소나' 모드다. 대신 `AT+INIT/AT+PAIR/AT+LINK` 으로 **지금 즉시** 붙일
#   수 있어(성공하면) 전원 재투입 없이 대상 전환이 끝난다. 그 가설을 실측으로 확인하는 도구다.
#
# ★두 가지 실험을 분리해 둔 이유:
#   ① link_to(addr, pswd) — **주소로 고른다**(PAIR+LINK). 어느 장비에 붙을지 결정적이다.
#   ② by_pswd(pswd)       — **암호로 고른다**. CMODE=1 자동연결은 '주변에서 찾은 아무 장비'
#                            에 붙으므로, 장비마다 PIN 이 다르면 페어링이 실패하는 장비는
#                            걸러진다는 가설이다. 선택이 아니라 **필터**라 ①보다 약하다.
#   둘 다 마지막에 신원 검증(무해한 조회의 응답 서명)으로 '실제로 누구에게 붙었는지'를 찍는다 —
#   OK 응답은 붙었다는 증거가 못 된다(현 코드의 안전 레일과 같은 원칙).
#
# 사용(REPL — ★main 루프가 멈춘 상태에서):
#   >>> import hc05_cmode1 as c
#   >>> c.show()                       # 현재 모듈 설정 덤프(ROLE/CMODE/BIND/PSWD/ADDR)
#   >>> c.scan()                       # 주변 장비 검색(+이름 조회) — 주소·이름 확인용
#   >>> c.hc06_help()                  # 상대 HC-06 의 이름/암호 바꾸는 법
#   >>> c.set_pswd("1234")             # 마스터 암호 설정
#   >>> c.link_to("98:DA:60:0F:C5:7A", pswd="1234")   # ① 주소로 지금 즉시 접속
#   >>> c.by_pswd("1234")              # ② 암호만으로 자동연결(전원 재투입 없이)
#   >>> c.by_bind("98:DA:60:0F:C5:7A")               # ③ CMODE=0 + RESET 자동연결
#   >>> c.link_to_name("reef-meas", expect="meas")   # ④ 이름으로 골라 접속
#   >>> c.find_by_name("reef-meas")                  # 이름 -> 주소 해석만
#   >>> c.whoami()                     # 지금 붙어 있는 상대가 누구인지만 확인
#
# 배포: deploy.py 는 src/*.py 만 올린다. 이 파일은 따로 복사한다 —
#   mpremote fs cp tools/hc05_cmode1.py :        (또는 mpy_bridge 의 base64 청크 전송)
import time
from machine import UART, Pin

import config
import devices

INQ_TIMEOUT = 65.0          # AT+INQ 완주 대기 — INQM 의 48*1.28s ≈ 61초를 덮는다
PAIR_TIMEOUT = 25.0         # AT+PAIR / AT+LINK — 상대를 실제로 찾아 붙는 시간이라 길다


def _release_link_uart():
    """main 이 돌다 멈췄으면 link 싱글턴이 UART 를 쥐고 있다 — 놓아준다."""
    try:
        import link
        lk = link.get_if_created()
        if lk is not None:
            lk.uart.deinit()
    except Exception:
        pass


def _fmt(addr):
    """사람이 준 MAC → AT 명령용 'nnnn,nn,nnnnnn'. 잘못된 값이면 예외."""
    a, err = devices.normalize_addr(addr)
    if err or not a:
        raise ValueError("주소 형식 오류(%s): %r" % (err or "빈 값", addr))
    return a


class Session:
    """AT 모드 세션 — KEY↑ 로 들어가고, **어느 경로로 빠져나가든** KEY↓ + 9600 으로 되돌린다.

    ★KEY 잔류는 실측으로 확인된 사고 유형이다(link._rebind_power 주석): KEY 를 올린 채
      두면 HC-05 가 AT 모드에 갇혀 이후 모든 데이터 통신이 죽는다. with 문으로만 쓴다."""

    def __init__(self):
        _release_link_uart()
        self.key = Pin(config.BT_KEY_PIN, Pin.OUT, value=0)
        self.state = (Pin(config.BT_STATE_PIN, Pin.IN)
                      if getattr(config, "BT_STATE_PIN", None) is not None else None)
        self.u = None
        self.baud = None

    # ── UART / AT ──

    def _set_baud(self, b):
        if self.u is None:
            self.u = UART(config.BT_UART_ID, baudrate=b, tx=config.BT_TX, rx=config.BT_RX,
                          timeout=300, timeout_char=50, rxbuf=2048)
        else:
            self.u.init(baudrate=b, bits=8, parity=None, stop=1,
                        tx=config.BT_TX, rx=config.BT_RX,
                        timeout=300, timeout_char=50, rxbuf=2048)
        self.baud = b

    def _readline(self):
        ln = self.u.readline()
        if not ln:
            return ""
        try:
            return ln.decode("utf-8").strip()
        except (UnicodeError, ValueError):
            return "".join(chr(c) if c < 0x80 else "?" for c in ln).strip()

    def at(self, cmd, timeout=None, quiet=False):
        """AT 명령 1개. 반환 (성공?, 줄들). ERROR/FAIL 도 '끝'으로 보고 즉시 돌려준다."""
        if timeout is None:
            timeout = config.BT_AT_TIMEOUT
        while self.u.any():
            self.u.read()
        self.u.write(cmd.encode() + b"\r\n")
        lines, end = [], time.ticks_add(time.ticks_ms(), int(timeout * 1000))
        while time.ticks_diff(end, time.ticks_ms()) > 0:
            _feed()
            if self.u.any():
                ln = self._readline()
                if ln:
                    lines.append(ln)
                    if ln == "OK" or ln.startswith("ERROR") or ln.startswith("FAIL"):
                        break
            else:
                time.sleep_ms(20)
        ok = "OK" in lines
        if not quiet:
            print("    %-28s -> %s" % (cmd, lines or "(무응답)"))
        return ok, lines

    def enter(self):
        """KEY↑ 로 AT 모드 진입 + 콘솔 보레이트 탐색. 반환: 찾은 보레이트 또는 None.

        ★보레이트가 상태에 따라 다르다(실측): 연결 중이면 9600, 미연결이면 38400."""
        self.key.value(1)
        time.sleep(config.BT_KEY_SETTLE_SECS)
        for b in (config.BAUD, config.BT_AT_BAUD):
            self._set_baud(b)
            ok, _ = self.at("AT", quiet=True)
            if ok:
                print("[AT] 진입 OK — 콘솔 보레이트 %d" % b)
                return b
        print("[AT] ★진입 실패 — KEY↑ 안 먹거나 TX/RX 배선 확인")
        return None

    def leave(self):
        """데이터 모드 복귀(KEY↓ + 9600). 예외가 나도 반드시 지나가야 하는 경로."""
        self._set_baud(config.BAUD)
        self.key.value(0)
        time.sleep(config.BT_KEY_SETTLE_SECS)

    def reset_into_data(self):
        """★AT+RESET 으로 재시작시키되, 부팅되는 동안 KEY 를 내려 **데이터 모드로 올라오게** 한다.

        전원 재투입을 대신하려는 시도다. KEY 를 올린 채 리셋하면 다시 AT 모드로 부팅해
        자동연결(CMODE 규칙)이 일어나지 않는다 — 리셋 직후 KEY 를 내리는 순서가 핵심이다."""
        self.at("AT+RESET", timeout=2.0)
        self.key.value(0)                      # ★부팅 중에 내린다(데이터 모드로 부팅)
        self._set_baud(config.BAUD)
        time.sleep(config.BT_RESET_WAIT_SECS)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.leave()
        return False


def _feed():
    """워치독이 돌고 있으면 먹인다(실험이 길어 리셋되는 걸 막는다)."""
    try:
        import watchdog
        watchdog.feed()
    except Exception:
        pass


def _connected(sess):
    """STATE 핀으로 본 연결 여부. 핀이 없으면 None(모름)."""
    return None if sess.state is None else bool(sess.state.value())


def _wait_state(sess, secs):
    """자동연결이 성립할 때까지 STATE 를 폴링한다. 반환: 붙는 데 걸린 초 또는 None.

    ★고정 대기로 재면 안 된다(2026-08-29 실측): 같은 절차인데 한 번은 5.5초에 붙고 한 번은
      9.5초에도 안 붙어서 '실패'로 오판했다. 자동연결 소요는 회차마다 흔들린다."""
    if sess.state is None:
        time.sleep(secs)
        return None
    end = time.ticks_add(time.ticks_ms(), int(secs * 1000))
    t0 = time.ticks_ms()
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        _feed()
        if sess.state.value():
            el = time.ticks_diff(time.ticks_ms(), t0) / 1000.0
            print("[STATE] %.1f 초 만에 연결" % el)
            return el
        time.sleep_ms(200)
    print("[STATE] %g 초 동안 연결 안 됨" % secs)
    return None


# ── 데이터 모드 신원 검증 ──

def _probe_on(uart, secs=None):
    """무해한 조회를 양쪽 규약으로 던지고 응답 서명으로 상대 종류를 판별한다.

    반환: 'meas' | 'doser' | None(무응답). ★OK 응답이 아니라 **상대의 말투**로 판별한다 —
    AT 가 OK 를 줘도 실제로 누구에게 붙었는지는 알 수 없다는 것이 이 프로젝트의 전제다.
    조회는 양쪽 다 부작용이 없다(status / ls)."""
    if secs is None:
        secs = config.BT_CONNECT_SECS
    while uart.any():
        uart.read()
    end = time.ticks_add(time.ticks_ms(), int(secs * 1000))
    nxt = time.ticks_ms()
    seen = []
    while time.ticks_diff(end, time.ticks_ms()) > 0:
        _feed()
        if time.ticks_diff(time.ticks_ms(), nxt) >= 0:
            for spec in devices.KINDS.values():
                uart.write(spec["probe"].encode() + spec["eol"])
                time.sleep_ms(120)
            nxt = time.ticks_add(time.ticks_ms(), int(config.LINK_PING_TIMEOUT * 1000))
        if uart.any():
            ln = uart.readline()
            if not ln:
                continue
            try:
                ln = ln.decode("utf-8").strip()
            except (UnicodeError, ValueError):
                ln = str(ln)
            if not ln:
                continue
            seen.append(ln)
            for kind, spec in devices.KINDS.items():
                if any(s in ln for s in spec["sig"]):
                    print("    응답: %r → 서명 일치" % ln)
                    return kind
        else:
            time.sleep_ms(20)
    if seen:
        print("    받은 줄(서명 불일치): %s" % seen[:6])
    return None


def whoami(secs=None):
    """지금 붙어 있는 상대가 누구인지만 확인한다(모드 전환 없음, 데이터 모드 그대로)."""
    _release_link_uart()
    u = UART(config.BT_UART_ID, baudrate=config.BAUD, tx=config.BT_TX, rx=config.BT_RX,
             timeout=300, timeout_char=50, rxbuf=2048)
    kind = _probe_on(u, secs)
    if kind:
        print("[whoami] %s (%s)" % (devices.KINDS[kind]["label"], kind))
    else:
        print("[whoami] ★무응답 — 안 붙었거나 상대 전원 꺼짐")
    return kind


def _verify(sess, expect=None):
    """KEY↓ 후 연결 성립 여부 + 신원을 확인하고 결과를 출력한다."""
    sess.leave()
    st = _connected(sess)
    print("[확인] STATE 핀 = %s" % ("연결" if st else ("미연결" if st is False else "핀 없음")))
    kind = _probe_on(sess.u)
    if kind is None:
        print("[결과] ★신원 확인 실패 — 응답 없음")
    else:
        label = devices.KINDS[kind]["label"]
        if expect and kind != expect:
            print("[결과] ★★오접속 — 기대 %s 인데 실제는 %s" % (expect, label))
        else:
            print("[결과] 접속 확인 — %s" % label)
    return kind


# ── ① 설정 조회 / 암호 설정 ──

def show():
    """현재 모듈 설정 덤프. 실험 전후로 무엇이 바뀌었는지 눈으로 보려고 쓴다."""
    s = Session()
    try:
        if not s.enter():
            return None
        out = {}
        for q in ("AT+VERSION?", "AT+ADDR?", "AT+ROLE?", "AT+CMODE?", "AT+BIND?",
                  "AT+PSWD?", "AT+NAME?", "AT+UART?", "AT+STATE?"):
            _ok, lines = s.at(q)
            out[q] = lines
        return out
    finally:
        s.leave()


def _set_pswd_on(sess, pswd):
    """마스터 PIN 설정 — 펌웨어에 따라 따옴표 유무가 갈린다(3.0 계열은 따옴표).
    두 형식을 모두 시도하고 AT+PSWD? 로 실제 반영을 확인한다."""
    for form in ('AT+PSWD="%s"' % pswd, "AT+PSWD=%s" % pswd):
        ok, _ = sess.at(form)
        if ok:
            _ok, lines = sess.at("AT+PSWD?")
            got = "".join(lines)
            if pswd in got:
                print("[PSWD] 설정 확인 — %s" % got)
                return True
            print("[PSWD] ★OK 는 왔는데 조회값이 다르다: %s" % got)
    print("[PSWD] ★설정 실패")
    return False


def set_pswd(pswd):
    """마스터(HC-05) 접속 암호를 바꾼다. 상대 HC-06 의 PIN 과 같아야 페어링이 된다."""
    s = Session()
    try:
        if not s.enter():
            return False
        return _set_pswd_on(s, pswd)
    finally:
        s.leave()


# ── ② 주변 검색 ──

def _parse_inq(lines):
    """'+INQ:98DA:60:0FC57A,1F00,7FFF' → '98da,60,0fc57a' 목록(중복 제거, 순서 유지)."""
    out = []
    for ln in lines:
        if not ln.startswith("+INQ:"):
            continue
        body = ln[5:].split(",")[0]           # 주소 구간(콜론 3구간)
        a, err = devices.normalize_addr(body)
        if not err and a and a not in out:
            out.append(a)
    return out


def _init_on(sess, tag):
    """AT+INIT — 이미 초기화돼 있으면 ERROR:(17) 이 정상이므로 통과시킨다."""
    ok, lines = sess.at("AT+INIT")
    if not ok and not any("17" in ln for ln in lines):
        print("[%s] ★AT+INIT 실패 — INQ/PAIR/LINK 가 안 될 수 있다" % tag)
        return False
    return True


def rname(sess, a):
    """AT+RNAME?<주소> — 상대의 **광고 이름**을 묻는다. 못 받으면 빈 문자열."""
    _ok, nl = sess.at("AT+RNAME?%s" % a, timeout=15.0)
    for ln in nl:
        if ln.startswith("+RNAME:"):
            return ln[7:].strip()
    return ""


def _scan_on(sess, num=9, secs=48, tag="scan"):
    """세션 안에서 검색 + 이름 조회. 반환 [(주소, 이름), …].

    ★AT 모드 진입은 느리고(리셋·보레이트 탐색) 검색은 더 느리다 — 검색해서 곧바로 붙는
      link_to_name() 이 같은 세션을 쓰도록 본체를 따로 뺐다."""
    sess.at("AT+ROLE=1")
    sess.at("AT+CMODE=1")
    sess.at("AT+INQM=1,%d,%d" % (num, secs))
    _init_on(sess, tag)
    print("[%s] 검색 중… 최대 %d 초" % (tag, secs))
    _ok, lines = sess.at("AT+INQ", timeout=INQ_TIMEOUT)
    addrs = _parse_inq(lines)
    if not addrs:
        print("[%s] ★찾은 장비 없음 — 상대 전원/거리 확인" % tag)
        return []
    found = []
    for a in addrs:
        nm = rname(sess, a)
        found.append((a, nm))
        print("  %s  %s" % (a, nm or "(이름 조회 실패)"))
    return found


def scan(num=9, secs=48):
    """주변 SPP 장비 검색 + 이름 조회. 주소·이름을 눈으로 확인하는 용도다."""
    s = Session()
    try:
        if not s.enter():
            return []
        return _scan_on(s, num, secs)
    finally:
        s.leave()


# ── ③ 실험 A — 주소로 지금 즉시 접속(전원 재투입 없이) ──

def link_to(addr, pswd=None, expect=None, clear_bond=True):
    """CMODE=1 + AT+PAIR/AT+LINK 으로 **지정한 주소에** 지금 붙인다.

    가설: CMODE=0 은 자동연결(=전원 재투입)에 의존하지만, CMODE=1 에서는 INIT→PAIR→LINK 로
    지금 붙일 수 있다. 성공하면 **전원 게이팅 없이 대상 전환이 완결**된다.
    ★AT+LINK 이 FAIL 이면 그 자체가 성과다 — 이 펌웨어에서는 전원 경로가 유일하다는 확증이다.

    clear_bond=False 로 두면 저장된 본딩을 지우지 않는다(재접속이 빠른지 비교용)."""
    a = _fmt(addr)
    s = Session()
    try:
        if not s.enter():
            return None
        _link_on(s, a, pswd, clear_bond)
    except Exception as e:
        s.leave()
        raise e
    return _verify(s, expect)


def _link_on(sess, a, pswd=None, clear_bond=True, tag="link"):
    """세션 안에서 '지금 즉시 붙이기' — INIT → PAIR → LINK."""
    if clear_bond:
        sess.at("AT+RMAAD")               # 남은 본딩이 있으면 엉뚱한 상대로 간다(실측)
    sess.at("AT+ROLE=1")
    sess.at("AT+CMODE=1")
    if pswd:
        _set_pswd_on(sess, pswd)
    _init_on(sess, tag)
    ok, lines = sess.at("AT+PAIR=%s,20" % a, timeout=PAIR_TIMEOUT)
    if not ok:
        print("[%s] ★PAIR 실패 — 암호 불일치이거나 상대가 꺼져 있다: %s" % (tag, lines))
    ok, lines = sess.at("AT+LINK=%s" % a, timeout=PAIR_TIMEOUT)
    if not ok:
        print("[%s] ★LINK 실패: %s — 전원 재투입 경로가 여전히 필요하다는 뜻" % (tag, lines))
    return ok


def find_by_name(name, num=9, secs=48):
    """이름으로 주소를 찾는다(대소문자 무시, 부분 일치). 반환 주소 또는 None.

    ★HC-05 에는 '이름으로 붙어라' 명령이 없다 — AT+LINK/AT+PAIR 은 주소만 받는다. 그래서
      이름 선택은 언제나 **이름 → 주소 해석 → 주소로 접속** 2단계다. 해석 비용이 검색
      시간(최대 secs 초 + 장비 수만큼의 RNAME 왕복)이라 상시 경로로 쓰기에는 느리다."""
    s = Session()
    try:
        if not s.enter():
            return None
        return _match_name(_scan_on(s, num, secs, tag="find"), name)
    finally:
        s.leave()


def _match_name(found, name):
    """검색 결과에서 이름으로 주소를 고른다. 모호하면 **아무 데나 붙지 않고 중단**한다.

    ★완전일치 우선(2026-08-29): 현장 이름이 'TSYI01'/'TSYI02' 라 부분일치만 쓰면 'TSYI0' 같은
      입력이 둘 다 걸려 모호해진다. 완전일치가 정확히 하나면 그걸 쓰고, 없을 때만 부분일치로
      내려간다. 측정기 이름 'HC-06' 은 공장 기본값이라 주변에 다른 모듈이 있으면 겹칠 수 있는데,
      그때는 여러 대가 잡혀 중단된다(의도한 동작 — 오접속보다 미접속이 낫다)."""
    want = (name or "").strip().lower()
    if not want:
        print("[이름] ★빈 이름")
        return None
    names = [(a, (nm or "")) for (a, nm) in found]
    exact = [(a, nm) for (a, nm) in names if nm.lower() == want]
    hits = exact if len(exact) == 1 else [(a, nm) for (a, nm) in names if want in nm.lower()]
    if not hits:
        print("[이름] ★'%s' 에 맞는 장비 없음 — 검색된 이름: %s"
              % (name, [nm for (_a, nm) in names] or "(없음)"))
        return None
    if len(hits) > 1:
        print("[이름] ★'%s' 에 %d 대가 맞는다 — 모호해서 중단: %s" % (name, len(hits), hits))
        return None
    a, nm = hits[0]
    print("[이름] '%s' → %s (%s)%s" % (name, a, nm, "" if exact else " (부분일치)"))
    return a


def link_to_name(name, pswd=None, expect=None, clear_bond=True, num=9, secs=48):
    """★이름으로 대상을 고른다 — 검색 → 이름 매칭 → 그 주소로 PAIR/LINK. 한 세션에서 끝낸다.

    PIN 을 못 바꿀 때의 대안이다. 다만 **HC-06 의 이름을 바꾸는 것도 PIN 과 똑같이 USB-TTL
    직결이 필요하다**(무선으로는 못 바꾼다) — 공장 이름이 이미 서로 다를 때만 공짜다.
    scan() 으로 먼저 확인할 것.

    ★이름이 유일하지 않으면(둘 다 'HC-06') 중단한다. 아무 데나 붙는 것보다 안 붙는 편이 낫다."""
    s = Session()
    try:
        if not s.enter():
            return None
        if clear_bond:
            s.at("AT+RMAAD")
        a = _match_name(_scan_on(s, num, secs, tag="link_to_name"), name)
        if a is None:
            s.leave()
            return None
        _link_on(s, a, pswd, clear_bond=False, tag="link_to_name")
    except Exception as e:
        s.leave()
        raise e
    return _verify(s, expect)


# ── ④ 실험 B — 암호만으로 고르기(CMODE=1 자동연결) ──

def by_pswd(pswd, expect=None, wait=None):
    """암호를 바꿔 가며 붙는 대상이 바뀌는지 본다 — 사용자 가설의 직접 검증.

    절차: RMAAD(본딩 비움) → ROLE=1 → CMODE=1 → PSWD=<암호> → AT+RESET(부팅 중 KEY↓)
          → 데이터 모드에서 자동연결 → 신원 검증.

    ★한계를 미리 적어 둔다: CMODE=1 자동연결은 '주변에서 찾은 아무 장비'에 붙는다. 암호는
      **고르는 수단이 아니라 거르는 수단**이라, PIN 이 같은 장비가 둘이면 어느 쪽에 붙을지
      정해지지 않는다. 그래서 반드시 신원 검증까지 보고 판단한다."""
    if wait is None:
        wait = max(config.BT_CONNECT_SECS, 20.0)
    s = Session()
    try:
        if not s.enter():
            return None
        s.at("AT+RMAAD")            # ★본딩이 남아 있으면 암호를 안 물어보고 옛 상대로 간다
        s.at("AT+ROLE=1")
        s.at("AT+CMODE=1")
        if not _set_pswd_on(s, pswd):
            return None
        ok, lines = s.at("AT+INIT")
        if not ok and not any("17" in ln for ln in lines):
            print("[by_pswd] ★AT+INIT 실패")
        print("[by_pswd] AT+RESET 후 데이터 모드로 부팅 — 자동연결 최대 %g 초 대기" % wait)
        s.reset_into_data()
        _wait_state(s, wait)
    except Exception as e:
        s.leave()
        raise e
    return _verify(s, expect)


# ── ④-2 실험 C — CMODE=0 그대로, 리셋만으로 자동연결시키기 ──

def by_bind(addr, expect=None, wait=None, clear_bond=True):
    """★현행 경로(CMODE=0 + BIND)에서 **전원 재투입 대신 AT+RESET** 으로 자동연결을 유도한다.

    왜 이걸 따로 보나: 현행 `_rebind_key` 는 BIND 를 넣은 뒤 KEY 만 내리고 끝난다 — 모듈이
    **재부팅을 하지 않으므로** CMODE=0 의 자동연결이 아예 발동하지 않는다. 자동연결은 데이터
    모드로 *부팅할 때* 한 번 일어나는 동작이고, '지금 붙이기'는 AT+LINK 뿐인데 그게 FAIL 이었다.
    그래서 전원 재투입 말고는 길이 없었던 것인데, `AT+RESET` 직후 **부팅되는 동안 KEY 를 내리면**
    데이터 모드로 부팅하게 되므로 같은 자동연결이 걸릴 수 있다. 되면 전원 게이팅이 불필요해진다.

    실험 A(link_to)·B(by_pswd)와 달리 **CMODE 를 0 으로 둔 채** 검증한다 — 성공하면 지금 코드에
    한 줄 수준의 변경으로 끝난다."""
    a = _fmt(addr)
    if wait is None:
        wait = max(config.BT_CONNECT_SECS, 20.0)   # 실측 편차가 커서 넉넉히 본다
    s = Session()
    try:
        if not s.enter():
            return None
        if clear_bond:
            s.at("AT+RMAAD")
        s.at("AT+ROLE=1")
        s.at("AT+CMODE=0")
        s.at("AT+BIND=%s" % a)
        print("[by_bind] AT+RESET 후 데이터 모드로 부팅 — 자동연결 최대 %g 초 대기" % wait)
        s.reset_into_data()
        _wait_state(s, wait)
    except Exception as e:
        s.leave()
        raise e
    return _verify(s, expect)


# ── ⑤ 상대(HC-06) 쪽 설정 안내 ──

def hc06_help():
    """상대 HC-06 의 이름/암호를 바꾸는 법 — ★HC-05 를 통해서는 못 바꾼다.

    HC-06 은 **연결돼 있지 않을 때만** AT 를 받는다. 붙어 있는 동안 보낸 글자는 전부 장비
    펌웨어로 가는 데이터라, 무선으로는 설정을 못 바꾼다. USB-TTL 로 HC-06 의 TXD/RXD 에
    직결해야 한다(장비에서 잠시 떼거나 헤더에 물린다)."""
    print("""
HC-06(슬레이브) 이름/암호 변경 — USB-TTL 직결, 9600 8N1
  ★구형 HC-06 펌웨어(linvor 1.x)는 **줄바꿈을 붙이지 않는다**. 한 명령을 1초 안에 다 보낸다.
    AT            -> OK
    AT+NAMEreef-meas    -> OKsetname      (측정 장비)
    AT+NAMEreef-dose1   -> OKsetname      (도저 1)
    AT+PIN1357          -> OKsetPIN       (장비마다 다르게)
    AT+PIN2468
    AT+VERSION          -> 펌웨어 확인
  ★신형(zs-040, AT 응답에 CRLF 요구)이면 AT+NAME=... / AT+PSWD=... 형식이다. 둘 다 해 본다.
  ★PIN 을 바꾸면 마스터(HC-05)에 저장된 본딩이 무효가 된다 — 마스터에서 AT+RMAAD 를 한 번 친다.
  ★바꾼 뒤 이 도구의 scan() 으로 이름이 바뀌었는지 확인하면 배선/설정이 먹었다는 증거가 된다.
""")
