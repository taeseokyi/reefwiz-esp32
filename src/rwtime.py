# KST 시각 헬퍼 — CPython datetime 대체
import time
import config


# ── 단조 시각(구간 타이머) ──
# ★time.time() 으로 데드라인을 만들면 안 된다(2026-08-26 실측 진단): ESP32 MicroPython 은
#   32비트 **단정밀도 float** 라, 시계가 실제 시각(2000년 기준 ~8.4억 초)으로 맞춰지면
#   `time.time() + 2.0` 이 현재 시각과 **같은 float 로 반올림**돼 `while time.time() < deadline`
#   루프가 한 번도 안 돌고 즉시 끝난다(작은 float 타임아웃이 float32 양자 ~64초 아래로 사라짐).
#   → 링크의 AT 응답 수집 루프가 즉시 종료돼 "무응답"이 되고 BT 전환·측정 통신이 전부 죽는다.
#   정수 초를 더하면 정확하지만(int+int), 구간 타이밍은 원리적으로 ticks_ms 로 하는 것이 맞다.
try:
    from time import ticks_ms as _ticks_ms, ticks_add as _ticks_add, ticks_diff as _ticks_diff

    def mono_ms():
        return _ticks_ms()

    def deadline_ms(secs):
        return _ticks_add(_ticks_ms(), int(secs * 1000))

    def before(dl):
        """데드라인 이전인가(루프 계속 조건). 만료면 False."""
        return _ticks_diff(dl, _ticks_ms()) > 0

    def remaining_s(dl):
        return _ticks_diff(dl, _ticks_ms()) / 1000.0

    def elapsed_s(t0):
        return _ticks_diff(_ticks_ms(), t0) / 1000.0
except ImportError:                     # CPython(테스트·PC 도구) — monotonic 초
    from time import monotonic as _mono

    def mono_ms():
        return _mono()

    def deadline_ms(secs):
        return _mono() + secs

    def before(dl):
        return _mono() < dl

    def remaining_s(dl):
        return dl - _mono()

    def elapsed_s(t0):
        return _mono() - t0


def now_tuple():
    """KST localtime 튜플 (y, m, d, hh, mm, ss, wd, yd)."""
    return time.localtime(time.time() + config.TZ_OFFSET_S)


def hour():
    return now_tuple()[3]


def stamp():
    """'YYYY-MM-DD HH:MM:SS' — 로그/이력 타임스탬프."""
    t = now_tuple()
    return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


def stamp_after(secs):
    """지금부터 secs 초 뒤의 'YYYY-MM-DD HH:MM:SS' — 만료 시각 계산용.
    ★반드시 **정수 초**로 더한다: float 를 더하면 위 float32 함정에 그대로 빠진다
      (기기의 time.time() 은 int 를 돌려주고, float 가 섞이는 순간 ~64초 아래가 사라진다).
    ★형식이 zero-padded 라 문자열 비교가 곧 시각 비교다(stamp() >= until)."""
    t = time.localtime(time.time() + config.TZ_OFFSET_S + int(secs))
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
    # ★단일 settime 이 불량 망에서 오래 막히지 않도록 소켓 타임아웃을 짧게 둔다(기본 1s).
    #   netmaint 스레드에서 도므로 측정과는 격리돼 있지만, 재시도 간격을 짧게 유지한다.
    try:
        ntptime.timeout = 2
    except Exception:
        pass
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
