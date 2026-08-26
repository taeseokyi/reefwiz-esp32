# reefwiz-esp32 메인 — WiFi/NTP → 웹서버(스레드) → 스케줄 루프(메인 스레드).
# Windows 작업 스케줄러 + dkh_server 폴러의 역할을 이 루프 하나가 맡는다:
#   - 측정 회차(schedule.measure_hours — 기본 5·13·21시)마다 측정 1회 → 오버라이드 확인
#   - 도저 조정 회차(기본 13시)는 도저 자동 조정(--slot-adjust 상당)까지
#   - 도징기 시계 동기는 장치별 시각(devices sync_hours — 기본 0시)에 순회 실행
#   - 웹 POST 오버라이드는 이벤트로 즉시 적용(종전 5분 폴링보다 빠름)
#   - 조치 작업(ops.run_pending_job)은 측정과 겹치지 않게 이 루프에서만 실행
# ★회차·동기 시각은 정비페이지에서 바꾼다(파일 우선, config 폴백) — 루프가 매 틱 읽으므로
#   저장하면 다음 틱부터 적용된다.
# ★스레드 배치(3개):
#   - 메인(이 루프): 측정·스케줄·조치 잡. UART/HC-05 를 만지는 것은 여기뿐. WDT 는 이 루프의
#     진행만 감시한다(다른 스레드는 feed 하지 않는다).
#   - webserver: LAN 웹/API(자가 치유 리스너).
#   - netmaint: WiFi 접속/재접속·AP 폴백·NTP 동기. ★WiFi 를 측정 스레드에서 **완전히 분리**해
#     WiFi 에 어떤 오류가 나도 측정이 멈추지 않게 한다(2026-08-26 사용자 요구). 메인은 WiFi 를
#     직접 만지지 않고 state.ntp_done/wifi_connected 플래그만 읽는다.
# ★온보드 RGB LED(GPIO48)로 상태를 표시한다 — 평상시 소등, 경고 시 앰버/치명 시 빨강 깜빡.
import gc
import os
import time

import archive
import config
import datalog
import devices
import doser
import link
import measure
import netmaint
import ops
import rwtime
import schedule
import state
import statusled
import watchdog
import webserver


def _ensure_dirs():
    for d in (config.DATA_DIR,):
        try:
            os.mkdir(d)
        except OSError:
            pass


def _update_led(blink_on):
    """상태 → RGB LED. 우선순위: 치명(RED 깜빡) > 경고(AMBER) > 정상(OFF).
    치명 = 측정이 멈췄거나 위협받음(에러 래치·링크 동결·시각 미동기로 자동측정 게이트 닫힘).
    경고 = 측정은 되지만 저하(WiFi 끊김/AP·BT 대상 미검증)."""
    critical = False
    warning = False
    try:
        if datalog.last_dat_is_error():          # 에러 래치(프로브 보호로 측정 정지)
            critical = True
        if not state.ntp_done:                   # 시각 미확보 → 정시 측정 게이트 닫힘
            critical = True
        lk = link.get_if_created()
        if lk is not None and lk.frozen:         # BT 신원 불일치 — 명령 차단
            critical = True
        if not state.wifi_connected or state.ap_active:
            warning = True                       # 대시보드 접근 불가(측정은 무관)
        if lk is not None and lk.target is None:
            warning = True                       # BT 대상 미검증(원격 꺼짐/전환 실패)
    except Exception:
        pass
    statusled.render(critical, warning, blink_on)


def main():
    _ensure_dirs()
    # ★재부팅 원인 로깅(2026-08-26) — 정전/수동인지 **WDT 리셋(=행 발생)**인지 사후 구분.
    #   종전엔 아무 원인 기록이 없어 재부팅 사유를 알 수 없었다.
    try:
        import machine
        rc = machine.reset_cause()
        wdt_rc = getattr(machine, "WDT_RESET", None)
        datalog.log("[boot] reset_cause=%s%s" % (rc, " ★WDT 리셋(행 발생)" if rc == wdt_rc else ""))
    except Exception:
        pass
    # 장기 저장소 — 디렉토리 확보 후 용량 백스톱을 한 번 돌린다(부팅 시 정리).
    # 실패해도 그냥 진행한다: 아카이브는 '있으면 좋은' 것이고 측정을 막지 않는다.
    if archive.ensure():
        archive.guard()
        archive.snapshot("boot")        # 부팅 시점 설정을 이력에 남긴다(값이 튀면 되돌릴 근거)
        st = archive.status()
        print("[archive] %s — %dKB 보관, 플래시 여유 %s KB"
              % (st["dir"], st["bytes"] // 1024, st["free_kb"]))
    # 상태 LED 준비(평상시 소등) + 부팅 확인 점멸.
    statusled.init()
    statusled.boot_blip()
    # ★웹서버를 먼저 올린다: WiFi 가 안 붙어도 AP 모드(reefwiz-setup)에서 설정 페이지가 떠야
    #   현장에서 공유기를 바꿀 수 있다(LAN 전용 기기의 유일한 백도어).
    webserver.start()
    # ★WiFi 접속·NTP 동기는 netmaint 스레드가 비동기로 맡는다 — 메인(측정) 루프는 WiFi 를
    #   직접 만지지 않아, WiFi 에 어떤 오류가 나도 측정이 멈추지 않는다(사용자 요구 2026-08-26).
    #   시각 게이트(state.ntp_done)는 netmaint 가 최초 NTP 동기에 성공하면 열린다.
    netmaint.start()

    # ★부팅 시 자동 연결 — 운영자가 아무것도 누르지 않아도 붙는다.
    #   측정기를 기본 대상으로 잡아 두면 정시 회차가 전환 없이 바로 시작하고, 정비
    #   페이지도 부팅 직후부터 "BT: 측정 장비"로 보인다. 실패해도 그냥 진행한다 —
    #   측정·도저가 각자 필요할 때 다시 붙으므로(link.acquire) 여기서 막을 이유가 없다.
    try:
        lk, err = link.acquire("meas", log=datalog.log)
        print("[bt] 부팅 자동 연결: %s" % ("측정 장비 확인됨" if lk else err))
    except Exception as e:
        print("[bt] 부팅 자동 연결 예외(무시하고 진행): %r" % e)

    last_meas_slot = None      # (y, m, d, hour) — 회차당 1회 보장
    last_sync_slot = {}        # 도징기 id → 마지막 시계 동기 슬롯(장치별 회차당 1회)
    last_guard_day = rwtime.date_str()   # 아카이브 용량 백스톱 하루 1회 실행 추적
    blink = False              # LED 깜빡임 위상(치명 경고용) — 매 틱 토글
    print("[main] scheduler start — measure hours %s, doser slot %02dh"
          % (schedule.measure_hours(), schedule.doser_slot_hour()))

    # ★워치독은 부팅이 끝난 여기서 켠다(부팅 중 WiFi/NTP 지연 오작동 방지). 이후 이 루프와
    #   측정·링크의 장기 블로킹 루프가 feed 한다(webserver 스레드는 feed 하지 않는다).
    watchdog.enable()

    while True:
        watchdog.feed()                        # 루프가 도는 한 살아 있다는 신호
        try:
            # 조치 작업(측정·정리·명령·링크 점검 등) — UART 는 이 스레드에서만 만진다.
            # ★WiFi/NTP 는 netmaint 스레드가 맡는다 — 이 루프는 WiFi 를 만지지 않는다(측정 격리).
            ops.run_pending_job()

            # 웹 오버라이드 즉시 적용 — 도저도 같은 HC-05 를 쓰므로
            # (전환은 doser.send_cmd 안에서) 측정 중에는 건드리지 않는다.
            if state.override_pending and not state.measuring:
                state.override_pending = False
                try:
                    doser.check_override()
                except Exception as e:
                    datalog.log("[오버라이드] 적용 예외: %r — 다음 회차 재시도" % e)

            t = rwtime.now_tuple()
            # ★ntp_done 조건: NTP 미동기화면 정시 측정을 하지 않는다 — ESP32 는 2000-01-01 로
            #   부팅하므로 시각이 무의미하고, 엉뚱한 시간에 측정이 돌면 시료·시약을 낭비한다.
            #   (수동 측정은 조치 도구로 언제든 가능 — 그건 운영자 의도가 명확하다.)
            due, slot = schedule.due_measure(t, last_meas_slot)
            if state.ntp_done and due:
                last_meas_slot = slot
                state.measuring = True
                try:
                    measure.run_once()         # 에러 래치·기록·JSON 갱신 포함
                except Exception as e:
                    datalog.log("[main] 측정 예외: %r" % e)
                finally:
                    state.measuring = False
                    # ★긴 회차가 다음 회차를 잡아먹지 않게 종료 시각의 슬롯도 소비한다.
                    #   회차 간격을 2시간까지 좁힐 수 있으므로(2026-08-21) 05시 회차가
                    #   07:30 에 끝나면 07시 회차가 곧바로 이어 시작되는 경우가 생긴다.
                    #   지나간 회차는 건너뛰는 것이 맞다 — 연속 측정은 시약 낭비다.
                    last_meas_slot = schedule.slot_of(rwtime.now_tuple())
                try:
                    # 매 측정 후 오버라이드 확인 + 13시 회차만 자동 조정. ★수동 우선 규칙
                    #   (새 오버라이드를 적용한 회차는 자동 조정 생략)은 doser 쪽에 있다 —
                    #   원본 doser_adjust.main() 의 순서를 그대로 옮긴 것.
                    doser.post_measure(t[3])
                except Exception as e:
                    datalog.log("[main] 도저 예외: %r" % e)

            # 도징기 시계 동기 — 장치별 시각(devices sync_hours)에 회차당 1회.
            # ★도저는 자체 타이머로 도징한다: 시계가 밀리면 도징 시각이 어긋난다(원본
            #   스케줄러 작업 set_time.py doser 가 하던 일). 도징기가 여러 대일 수 있어
            #   장치별로 돌린다(2026-08-21) — 명령·값이 전 도징기 동일하므로 자동 전환 허용.
            # ★측정 중에는 미룬다: 전환이 측정 명령을 다른 장비로 보낼 수 있고, 애초에
            #   select_target 이 거부한다. 측정이 끝난 뒤 같은 슬롯 안이면 그때 실행된다.
            # ★ntp_done 게이트: 시각이 안 맞는 상태로 보내면 도저 시계를 2000-01-01 로
            #   **망친다** — 동기를 건너뛰는 것보다 나쁘다.
            # ★시각을 다시 읽는다 — 위 t 는 측정 전에 잡은 값이라 회차가 몇 시간 걸린 뒤엔
            #   낡았다(05시 회차가 07:30 에 끝나면 t[3]=5 다).
            if state.ntp_done and not state.measuring:
                tn = rwtime.now_tuple()
                slot_now = schedule.slot_of(tn)
                for dev in devices.dosers():
                    if tn[3] not in dev["sync_hours"]:
                        continue
                    if last_sync_slot.get(dev["id"]) == slot_now:
                        continue
                    last_sync_slot[dev["id"]] = slot_now
                    try:
                        doser.sync_clock(dev["id"])
                    except Exception as e:
                        datalog.log("[main] %s 시계 동기화 예외: %r" % (dev["name"], e))

            # 하루 1회 용량 백스톱(자정 이후 첫 틱). ★NTP 재동기는 netmaint 스레드가 맡는다.
            # ★측정 중에는 미룬다: 21시 회차는 자정을 넘겨 끝날 수 있다.
            if rwtime.date_str() != last_guard_day and not state.measuring:
                last_guard_day = rwtime.date_str()
                try:
                    archive.guard()    # 로그·아카이브 용량 백스톱
                except Exception as e:
                    datalog.log("[main] 아카이브 백스톱 예외: %r" % e)

            # ★웹서버 생존 관측(2026-08-26) — 자가 치유 리스너가 유휴에도 하트비트를 갱신한다.
            #   오래 멎으면 리스너가 막힌 것이라 경고를 남긴다(자가 치유가 곧 되살리지만,
            #   반복되면 운영자가 원인을 봐야 한다). 재시작은 하지 않는다 — 살아 있는(막힌)
            #   스레드를 죽일 수단이 _thread 에 없어 이중 바인드 위험만 키운다.
            age = webserver.alive_age()
            if age is not None and age > config.WEB_HEARTBEAT_STALE_S:
                datalog.log("[main] ★웹서버 하트비트 %ds 정지 — 리스너 이상 의심" % int(age))
        except Exception as e:
            print("[main] loop error: %r" % e)
        # 상태 LED 갱신(평상시 소등, 경고/치명 시 점등) — 예외와 무관하게 항상 반영.
        blink = not blink
        _update_led(blink)
        # ★단편화 예방(2026-08-26) — 상시 가동 + 웹/측정 할당의 누적 단편화를 매 틱 회수한다.
        #   SPIRAM 8MB 라 비용은 무시할 수준이고, 측정 종료 직후 틱에서 큰 리스트도 회수된다.
        gc.collect()
        time.sleep(2)                          # 조치 요청 반응성(웹 버튼 → 최대 2s)


main()
