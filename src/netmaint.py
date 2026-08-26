# 네트워크 유지 스레드 — WiFi 접속/재접속 + AP 폴백 + NTP 동기를 **측정 스레드와 분리**해서 돈다.
#
# ★왜 별도 스레드인가(2026-08-26 사용자 요구: "WiFi 에 어떤 오류가 있어도 측정은 유지"):
#   종전에는 메인(측정·스케줄) 루프가 매 틱 wifinet.ensure()/ntp_sync() 를 직접 불렀다. 그러면
#   WiFi 드라이버가 C 레벨에서 오래 막히는 드문 경우 메인이 함께 막혀 → 워치독이 '행'으로 보고
#   리셋 → 측정이 중단됐다. 이 스레드로 옮기면 메인은 WiFi 를 아예 만지지 않으므로, WiFi 가
#   완전히 멈춰도 메인은 계속 WDT 를 먹이며 측정을 이어 간다.
#
# ★이 스레드는 절대 watchdog.feed() 를 부르지 않는다 — WDT 는 메인(측정) 스레드의 진행만
#   감시해야 한다. 여기서 먹이면 메인이 멈춰도 리셋이 안 걸려 감시가 무의미해진다.
#
# 공유는 state 플래그로만 한다: ntp_done(측정 게이트) / wifi_connected / ap_active 를 쓰고,
# state.wifi_reconnect(웹이 새 자격 저장 시 True)를 소비한다.
import time
import _thread

import config
import rwtime
import state
import wifinet

_last_ntp_day = None


def _tick():
    global _last_ntp_day
    if state.wifi_reconnect:
        state.wifi_reconnect = False
        wifinet.connect()
    ok = wifinet.ensure(timeout=config.WIFI_ENSURE_TIMEOUT)
    state.wifi_connected = ok
    state.ap_active = wifinet.ap_is_active()
    if not ok:
        return
    if not state.ntp_done:
        # ★최초 동기만 게이트를 연다(성공해야 True). 실패해도 다음 틱에 재시도한다.
        state.ntp_done = rwtime.ntp_sync()
    else:
        # 이미 시각을 아는 뒤의 하루 1회 재동기 — 드리프트 보정. 실패해도 게이트는 닫지 않는다.
        today = rwtime.date_str()
        if _last_ntp_day != today:
            _last_ntp_day = today
            rwtime.ntp_sync()


def _loop():
    while True:
        try:
            _tick()
        except Exception as e:
            print("[netmaint] %r" % e)
        time.sleep(config.NET_MAINT_INTERVAL_S)


def start():
    _thread.start_new_thread(_loop, ())
