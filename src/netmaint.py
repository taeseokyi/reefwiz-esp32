# 네트워크 유지 스레드 — NTP 동기(+ WiFi 상태 반영)를 **측정 스레드와 분리**해서 돈다.
# ★2026-09-24: WiFi 접속·재접속·AP 폴백은 webota(webota_net)가 전담한다 — 여기서는 상태를
#   읽어 state 에 반영하고, 붙어 있으면 NTP 만 맞춘다.
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
import time
import _thread

import config
import datalog
import rwtime
import state
import wifinet

_last_ntp_day = None
_fail_n = 0                 # 최초 동기 연속 실패 횟수 — 첫 실패와 이후 50회(약 10분)마다 로그
_FAIL_LOG_EVERY = 50


def _log(msg):
    try:
        datalog.log(msg)
    except Exception as e:                       # 로그 실패가 이 스레드를 죽이면 안 된다
        print("[netmaint] log fail %r: %s" % (e, msg))


def _tick():
    global _last_ntp_day, _fail_n
    ok = wifinet.is_connected()                  # 접속·AP 는 webota 가 한다 — 읽기만
    state.wifi_connected = ok
    state.ap_active = wifinet.ap_is_active()
    if not state.ntp_done and rwtime.clock_valid():
        # ★시계가 이미 맞다(2026-10-26 v1.12.1): webota 가 부팅 때 NTP 를 맞췄거나 소프트 리셋이
        #   RTC 를 유지한 경우. 종전엔 그래도 여기서 같은 NTP 를 **한 번 더** 성공해야 게이트가
        #   열렸다 — 부팅 직후 WiFi 가 흔들리면 시계는 맞는데 정시 측정만 막히는 사각지대였다.
        #   2024년 이상의 시각은 NTP 로만 생기므로(배터리 RTC 없음) 그대로 인정한다.
        state.ntp_done = True
        _last_ntp_day = rwtime.date_str()        # 오늘 재동기는 건너뛴다 — 방금 맞춘 시계다
        _log("[시각] 시계 유효(%s) — 부팅 동기 인정, 정시 측정 게이트 열림" % rwtime.stamp())
        return
    if not ok:
        return
    if not state.ntp_done:
        # ★최초 동기만 게이트를 연다(성공해야 True). 실패해도 다음 틱에 재시도한다.
        state.ntp_done = rwtime.ntp_sync()
        if state.ntp_done:
            _fail_n = 0
            _log("[시각] NTP 동기 성공(%s) — 정시 측정 게이트 열림" % rwtime.stamp())
        else:
            _fail_n += 1
            if _fail_n == 1 or _fail_n % _FAIL_LOG_EVERY == 0:
                _log("[시각] NTP 동기 실패 x%d(%s) — 게이트 닫힘, %ds 뒤 재시도"
                     % (_fail_n, config.NTP_HOST, config.NET_MAINT_INTERVAL_S))
    else:
        # 이미 시각을 아는 뒤의 하루 1회 재동기 — 드리프트 보정. 실패해도 게이트는 닫지 않는다.
        today = rwtime.date_str()
        if _last_ntp_day != today:
            _last_ntp_day = today
            if not rwtime.ntp_sync():
                _log("[시각] 하루 1회 NTP 재동기 실패(%s) — 게이트는 유지, 내일 재시도" % config.NTP_HOST)


def _loop():
    while True:
        try:
            _tick()
        except Exception as e:
            print("[netmaint] %r" % e)
        time.sleep(config.NET_MAINT_INTERVAL_S)


def start():
    _thread.start_new_thread(_loop, ())
