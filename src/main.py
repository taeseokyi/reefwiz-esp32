# reefwiz-esp32 메인 — WiFi/NTP → 웹서버·디스플레이(각 스레드) → 스케줄 루프(메인 스레드).
# Windows 작업 스케줄러 + dkh_server 폴러의 역할을 이 루프 하나가 맡는다:
#   - MEASURE_HOURS(5·13·21시) 회차마다 측정 1회 → 오버라이드 확인
#   - 13시 회차는 도저 자동 조정(--slot-adjust 상당)까지
#   - 웹/화면 POST 오버라이드는 이벤트로 즉시 적용(종전 5분 폴링보다 빠름)
#   - 조치 작업(ops.run_pending_job)은 측정과 겹치지 않게 이 루프에서만 실행
# ★스레드 배치: 측정은 수 시간 블로킹하므로 웹·화면을 별도 스레드에 둔다 — 측정 중에도
#   상태 조회와 중단(state 플래그)이 되고, UART 를 만지는 작업은 큐에서 순차 실행된다.
import os
import time

import ntptime

import config
import datalog
import display
import doser
import measure
import ops
import rwtime
import state
import storage
import webserver
import wifinet


def ntp_sync():
    ntptime.host = config.NTP_HOST
    for _ in range(5):
        try:
            ntptime.settime()                  # UTC 로 설정 — 표시 변환은 rwtime 이 담당
            print("[ntp] synced: %s KST" % rwtime.stamp())
            return True
        except OSError:
            time.sleep(3)
    print("[ntp] 동기화 실패 — 시각 부정확 상태로 진행(스케줄 부정확 주의)")
    return False


def _ensure_dirs():
    for d in (config.DATA_DIR,):
        try:
            os.mkdir(d)
        except OSError:
            pass


def _display_loop(ui):
    """화면 스레드 — 측정이 메인 스레드를 점유하는 동안에도 상태 표시·터치가 살아 있다."""
    while True:
        try:
            ui.tick()
        except Exception as e:
            print("[disp] tick error: %r" % e)
        time.sleep_ms(120)


def main():
    _ensure_dirs()
    # ★SD 는 가장 먼저 붙인다 — 부팅 이후의 모든 로그·RF 이벤트를 놓치지 않기 위해서다.
    #   실패해도 그냥 진행한다(플래시 동작으로 degrade). 측정을 막는 일은 없다.
    if storage.mount():
        storage.attach()
        print("[sd] 마운트 완료 — 로그 전문·아카이브·RF 원장을 %s 에 기록" % config.SD_DIR)
    else:
        print("[sd] 사용 안 함 — %s (플래시만 사용, 기능은 그대로)" % storage.status()["error"])
    # ★웹서버를 먼저 올린다: WiFi 가 안 붙어도 AP 모드(reefwiz-setup)에서 설정 페이지가
    #   떠야 현장에서 공유기를 바꿀 수 있다(LAN 전용 기기의 유일한 백도어).
    webserver.start()
    online = wifinet.ensure()
    if online:
        ntp_sync()
    else:
        print("[main] WiFi 미접속 — AP '%s' 에서 http://%s/ops.html 로 설정하세요"
              % (wifinet.AP_SSID, wifinet.AP_IP))

    ui = display.create()
    if ui.active:
        import _thread
        _thread.start_new_thread(_display_loop, (ui,))

    last_meas_slot = None      # (y, m, d, hour) — 회차당 1회 보장
    last_ntp_day = rwtime.date_str()
    ntp_done = online
    print("[main] scheduler start — measure hours %s, doser slot %02dh"
          % (config.MEASURE_HOURS, config.DOSER_SLOT_HOUR))

    while True:
        try:
            # WiFi — 새 설정이 저장되면 즉시 재접속, 끊겼으면 재접속(실패 시 AP 유지).
            # ★측정은 WiFi 와 무관하게 진행된다(HC-05 는 별개 경로) — 네트워크가 죽어도 측정은 계속.
            if state.wifi_reconnect:
                state.wifi_reconnect = False
                wifinet.connect()
            if wifinet.ensure(timeout=10) and not ntp_done:
                ntp_done = ntp_sync()          # 뒤늦게 붙었으면 그때 시각 동기화

            # 조치 작업(측정·정리·명령·링크 점검 등) — UART 는 이 스레드에서만 만진다
            ops.run_pending_job()

            # 웹/화면 오버라이드 즉시 적용 — 도저도 같은 HC-05 를 쓰므로
            # (전환은 doser.send_cmd 안에서) 측정 중에는 건드리지 않는다.
            if state.override_pending and not state.measuring:
                state.override_pending = False
                try:
                    doser.check_override()
                except Exception as e:
                    datalog.log("[오버라이드] 적용 예외: %r — 다음 회차 재시도" % e)

            t = rwtime.now_tuple()
            slot = (t[0], t[1], t[2], t[3])
            # ★ntp_done 조건: NTP 미동기화면 정시 측정을 하지 않는다 — ESP32 는 2000-01-01 로
            #   부팅하므로 시각이 무의미하고, 엉뚱한 시간에 측정이 돌면 시료·시약을 낭비한다.
            #   (수동 측정은 조치 도구로 언제든 가능 — 그건 운영자 의도가 명확하다.)
            if ntp_done and t[3] in config.MEASURE_HOURS and slot != last_meas_slot:
                last_meas_slot = slot
                state.measuring = True
                try:
                    measure.run_once()         # 에러 래치·기록·JSON 갱신 포함
                except Exception as e:
                    datalog.log("[main] 측정 예외: %r" % e)
                finally:
                    state.measuring = False
                try:
                    doser.check_override()     # 래퍼의 '매 측정 후 확인' 경로 유지
                    if t[3] == config.DOSER_SLOT_HOUR:
                        doser.slot_adjust()
                except Exception as e:
                    datalog.log("[main] 도저 예외: %r" % e)

            # 하루 1회 NTP 재동기화(자정 이후 첫 틱)
            if rwtime.date_str() != last_ntp_day:
                last_ntp_day = rwtime.date_str()
                ntp_sync()
        except Exception as e:
            print("[main] loop error: %r" % e)
        time.sleep(2)                          # 조치 요청 반응성(웹 버튼 → 최대 2s)


main()
