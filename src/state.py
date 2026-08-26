# 스레드 간 공유 상태 — 웹서버(별도 스레드)가 요청을 올리고 메인 루프가 소비한다.
# UART 는 스레드 안전하지 않으므로 장비를 만지는 작업은 전부 job 큐를 거쳐 메인 루프에서만
# 실행한다(웹 스레드가 측정 중 UART 에 끼어들면 응답 뒤섞임 → 오측정).


class Aborted(Exception):
    """운영자가 측정 중단을 요청 — 장비 이상이 아니므로 에러 래치를 걸지 않는다."""


override_pending = False   # POST /api/override 직후 True — 메인 루프가 즉시 적용 시도
wifi_reconnect = False     # POST /api/wifi 직후 True — netmaint 스레드가 새 자격으로 재접속
measuring = False          # 측정 중
abort_requested = False    # 측정 중단 요청 — 측정 루프가 확인 후 Aborted 발생(비상정리는 실행됨)

# ── WiFi/NTP 상태 (★netmaint 스레드가 쓰고, 메인 루프·LED·웹이 읽는다) ──
# ★2026-08-26: WiFi 를 메인(측정) 스레드에서 완전히 분리했다 — WiFi 에 어떤 오류가 나도
#   측정 루프가 막히거나 WDT 가 트립되지 않게 하려는 것(측정이 이 장비의 주 목적).
#   메인 루프는 이 플래그만 읽고 WiFi/NTP 를 직접 만지지 않는다.
ntp_done = False           # NTP 동기 성공 여부 — 정시 측정 게이트(시각 미확보 시 자동측정 보류)
wifi_connected = False     # STA 접속 상태(LED·표시용)
ap_active = False          # AP 폴백 활성(설정 필요 상태 표시용)

job = None                 # {"kind": str, "args": dict} — 장비 조작 요청(1건 대기)
job_result = None          # {"kind", "ok", "msg", "at"} — 마지막 실행 결과(웹이 폴링)
job_busy = False           # 실행 중 표시


def put_job(kind, **args):
    """장비 조작 요청 큐잉. 이미 대기 중이면 False(중복 실행 방지)."""
    global job
    if job is not None or job_busy:
        return False
    job = {"kind": kind, "args": args}
    return True
