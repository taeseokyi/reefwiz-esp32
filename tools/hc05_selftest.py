# HC-05 자체진단 — 장치(REPL)에서 실행. AT 모드 진입/종료가 실제로 되는지 단계별로 확인한다.
#
# 왜 있나: HC-05(ZS-040, 펌웨어 3.0-20170601)는 KEY↑ 만으로 들어가는 Way1 에서도 AT 콘솔이
#   38400 이다(데이터시트의 '통신 보레이트 그대로'가 아님). 이 가정을 틀리면 고속 전환이 매번
#   무응답→전원 폴백으로 떨어진다(2026-08-26 진단). 이 스크립트는 그 회귀를 언제든 재검증한다.
#
# 사용(REPL):
#   >>> import hc05_selftest as t
#   >>> t.lowlevel()            # 원격 장비 없이: KEY/전원/TX·RX/AT 콘솔 보레이트 확인
#   >>> t.full('meas')          # 원격 장비(HC-06) 켜져 있을 때: 실제 전환+신원검증
#   >>> t.full('doser')
#
# ★반드시 main 루프가 멈춘 상태(REPL)에서 실행한다 — UART 를 공유하므로 동시 실행 금지.
import time
from machine import UART, Pin
import config


def _fresh_uart(baud):
    return UART(config.BT_UART_ID, baudrate=baud, tx=config.BT_TX, rx=config.BT_RX,
                timeout=300, timeout_char=50, rxbuf=512)


def _at(u, cmd, wait=0.6):
    while u.any():
        u.read()
    u.write(cmd + b"\r\n")
    time.sleep(wait)
    return u.read()


def _release_link_uart():
    """main 이 돌다 멈췄으면 link 싱글턴이 UART 를 쥐고 있다 — 놓아준다."""
    try:
        import link
        lk = link.get_if_created()
        if lk is not None:
            lk.uart.deinit()
    except Exception:
        pass


def lowlevel():
    """원격 장비 없이 하는 저수준 점검. PASS 면 배선(KEY/전원/TX·RX)과 AT 콘솔 보레이트가 정상."""
    if config.BT_KEY_PIN is None or config.BT_POWER_PIN is None:
        print("[FAIL] KEY/POWER 핀 미배선 — config.BT_KEY_PIN/BT_POWER_PIN 확인")
        return False
    _release_link_uart()
    pw = Pin(config.BT_POWER_PIN, Pin.OUT,
             value=(1 if config.BT_POWER_ACTIVE_HIGH else 0))   # 전원 ON
    ky = Pin(config.BT_KEY_PIN, Pin.OUT, value=0)               # 데이터 모드
    time.sleep(0.5)
    print("[1] 전원 ON, 데이터 모드(KEY=0) 정착")
    ky.value(1)
    time.sleep(0.3)
    print("[2] KEY↑ (Way1 AT 모드 진입)")
    found = None
    lines = None
    for baud in (config.BT_AT_BAUD, config.BAUD):
        u = _fresh_uart(baud)
        time.sleep(0.15)
        r = _at(u, b"AT")
        print("    AT @ %d -> %r" % (baud, r))
        if r and b"OK" in r:
            found = baud
            break
    if found is None:
        print("[FAIL] 어느 보레이트에서도 AT 무응답 — KEY↑ 미동작(PIN34 납땜?) 또는 TX/RX 배선 확인")
        ky.value(0)
        return False
    print("[3] AT 콘솔 보레이트 = %d %s" % (
        found, "(예상대로 38400)" if found == config.BT_AT_BAUD else "(★9600 미니모드 — 명령 일부만 먹을 수 있음)"))
    for c in (b"AT+VERSION?", b"AT+UART?", b"AT+ROLE?", b"AT+ADDR?", b"AT+STATE?", b"AT+DISC"):
        print("    %s -> %r" % (c.decode(), _at(u, c)))
    # 종료: 데이터 보레이트로 되돌리고 KEY↓
    u.init(baudrate=config.BAUD, tx=config.BT_TX, rx=config.BT_RX,
           timeout=300, timeout_char=50, rxbuf=512)
    ky.value(0)
    time.sleep(0.3)
    print("[4] 종료: 데이터 모드(9600, KEY=0) 복귀")
    u.deinit()
    print("[PASS] 저수준 점검 통과 — AT 진입/종료 정상")
    return True


def full(target):
    """실제 전환 경로(link.select_target)로 대상 장비에 붙고 신원까지 검증한다.
    원격 장비(HC-06)가 켜져 있어야 한다. 실패하면 사유를 그대로 출력한다."""
    _release_link_uart()
    import link
    link.refresh_targets()
    lk = link.get()
    lk.log = print
    print("[full] 대상=%s 로 전환 시도 (force=True: 사람이 눈으로 확인했다는 전제)" % target)
    ok, err = lk.select_target(target, force=True)
    if ok:
        print("[PASS] %s 전환+신원검증 성공" % target)
    else:
        print("[FAIL] %s: %s" % (target, err))
    return ok
