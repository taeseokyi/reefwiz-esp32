# KST 시각 헬퍼 — CPython datetime 대체
import time
import config


def now_tuple():
    """KST localtime 튜플 (y, m, d, hh, mm, ss, wd, yd)."""
    return time.localtime(time.time() + config.TZ_OFFSET_S)


def hour():
    return now_tuple()[3]


def stamp():
    """'YYYY-MM-DD HH:MM:SS' — 로그/이력 타임스탬프."""
    t = now_tuple()
    return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


def date_str():
    t = now_tuple()
    return "%04d-%02d-%02d" % (t[0], t[1], t[2])


def iso_id():
    """오버라이드 id 용 — 원본은 브라우저 ISO 문자열이었음. 유일성만 있으면 됨."""
    t = now_tuple()
    return "%04d-%02d-%02dT%02d:%02d:%02d+09:00" % (t[0], t[1], t[2], t[3], t[4], t[5])


# ── NTP 시각 확보 ──
# ★main 에 있던 것을 여기로 옮겼다(2026-08-24): 시각의 주인은 이 모듈이고, main 안에 있으면
#   테스트가 부를 수 없었다(main.py 는 import 하는 순간 main() 이 돌아 무한 루프에 들어간다).
#   실제로 그 사각지대에서 게이트 결함이 살아 있었다 — 아래 time_ready 주석 참조.
# ★ntptime 은 기기에만 있는 모듈이라 **함수 안에서** import 한다: rwtime 은 PC 테스트·백업
#   도구도 import 하므로, 모듈 최상단에 두면 그쪽 전부에 스텁을 강요한다.
NTP_TRIES = 5           # 재시도 횟수
NTP_GAP_S = 3           # 재시도 간격(초) — 부팅 직후 DHCP·DNS 가 늦게 서는 경우가 있다


def ntp_sync(tries=None, gap=None):
    """NTP 로 시각을 맞춘다(UTC 저장 — KST 변환은 이 모듈의 나머지 함수가 한다).

    ★반환값을 반드시 확인할 것: 실패를 성공으로 취급하면 시계가 2000-01-01(ESP32 부팅
      기본값) 인 채로 정시 측정이 돌고, 도저 시계 동기가 그 값을 장비에 밀어 넣는다."""
    import ntptime                       # 기기 전용 모듈 — 위 주석 참조
    ntptime.host = config.NTP_HOST
    for _ in range(NTP_TRIES if tries is None else tries):
        try:
            ntptime.settime()
            print("[ntp] synced: %s KST" % stamp())
            return True
        except OSError:
            time.sleep(NTP_GAP_S if gap is None else gap)
    print("[ntp] 동기화 실패 — 시각 부정확 상태로 진행(스케줄 부정확 주의)")
    return False


def time_ready(online):
    """정시 측정·도저 시계 동기를 열어도 되는가 — WiFi 가 붙었고 **NTP 까지 성공**했을 때만.

    ★online 만 보고 열면 안 된다(2026-08-24 수정): 인터넷이 없는 공유기나 UDP 123 이 막힌
      망에서는 WiFi 는 붙지만 NTP 가 실패한다. 종전 `ntp_done = online` 은 그 경우에도
      게이트를 열어, ①엉뚱한 시각에 회차가 돌고 ②`set time 00:00:xx` 로 **도저 시계를
      2000-01-01 로 망쳤다** — main 의 ntp_done 주석이 막겠다고 적어 둔 바로 그 사고다.
      시각이 없으면 건너뛰는 것이 맞다(수동 측정은 정비페이지에서 언제든 가능 — 결정 #15)."""
    return bool(online) and ntp_sync()
