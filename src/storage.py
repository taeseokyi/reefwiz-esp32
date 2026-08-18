# SD 카드 저장소 — 플래시의 보관 한계를 걷어내는 '있으면 좋은' 계층.
#
# 왜 필요한가: 내장 플래시는 4MB 뿐이라 measure_kh.log 를 512KB 에서 돌려쓰고(LOG_MAX_BYTES),
# dkh.dat·plateau 이력은 14일 창 밖으로 밀린 것을 그냥 버린다. 그런데 사고 분석에 필요한
# 것은 대개 "밀려난 쪽"이다 — 순단이 반복되면 원인 구간이 로그에서 먼저 사라진다.
# SD 가 있으면 그 세 가지를 무기한 남긴다:
#   1) 측정 로그 전문           /sd/reefwiz/log/measure-YYYY-MM.log
#   2) 보관 창 밖 기록 아카이브  /sd/reefwiz/archive/{dkh.dat, plateau.jsonl}
#   3) ★RF/BT 이벤트 원장       /sd/reefwiz/rf.jsonl
#
# 3번이 이 모듈을 만든 실질적 이유다. HC-05 1개로 두 장비를 번갈아 쓰게 되면서 전환·재연결이
# 잦아졌는데, 지금은 "왜 끊겼는지"가 기록에 남지 않는다. 전환 시도별 소요·재연결 횟수·신원
# 불일치를 원장에 쌓으면 RECONNECT_BACKOFF 를 추정치가 아니라 실측 분포로 튜닝할 수 있고,
# 사용자가 중시하는 "측정이 끊겼을 때 조치 용이성"에도 직접 도움이 된다.
#
# ★설계 원칙: SD 는 절대 측정을 막지 않는다. 미장착·마운트 실패·쓰기 실패·중간 탈착 — 어떤
#   경우에도 예외를 밖으로 내보내지 않고 비활성으로 내려앉는다(플래시 동작 그대로 유지).
#
# ★드라이버: SPI 모드 SD 는 micropython-lib 의 `sdcard.py` 가 표준이다. 저장소에 포함하지
#   않았으므로 배포 시 함께 올려야 한다(README 배포 절차 참조). 없으면 이 모듈은 조용히
#   비활성으로 남는다 — 그래서 임포트를 지연시킨다.
import os

import config

_sd = None            # 마운트 성공 여부
_fail = None          # 마지막 실패 사유(정비페이지 표시용)
_log_month = None     # 현재 열려 있는 월별 로그 파일 키
_log_f = None


def _p(*parts):
    return "/".join((config.SD_DIR,) + parts)


def _mkdirs(path):
    """중간 디렉터리까지 생성 — MicroPython os.mkdir 은 -p 가 없다."""
    cur = ""
    for seg in path.strip("/").split("/"):
        cur += "/" + seg
        try:
            os.mkdir(cur)
        except OSError:
            pass          # 이미 있음(또는 상위가 없음 — 다음 단계에서 드러난다)


def mount():
    """SD 마운트 시도. 성공하면 True, 실패하면 False(사유는 status() 에).

    실패는 정상적인 운용 상태다 — SD 를 안 꽂았거나 드라이버를 안 올렸을 수 있다."""
    global _sd, _fail
    if _sd:
        return True
    if not config.SD_ENABLED:
        _fail = "config.SD_ENABLED=False"
        return False
    try:
        import machine
        import sdcard          # ★micropython-lib — 배포 시 함께 업로드
        import vfs
    except ImportError as e:
        # vfs 는 구버전 MicroPython 에서 os 에 통합돼 있다 — 그 경우 os 를 쓴다.
        try:
            import machine
            import sdcard
            vfs = os
        except ImportError:
            _fail = "드라이버 없음(%s) — sdcard.py 를 함께 업로드하세요" % e
            return False
    try:
        spi = machine.SPI(config.SPI_ID, baudrate=config.SD_BAUD, polarity=0, phase=0,
                          sck=machine.Pin(config.SPI_SCK),
                          mosi=machine.Pin(config.SPI_MOSI),
                          miso=machine.Pin(config.SPI_MISO))
        card = sdcard.SDCard(spi, machine.Pin(config.SD_CS, machine.Pin.OUT))
        vfs.mount(vfs.VfsFat(card), config.SD_MOUNT)
    except Exception as e:      # 카드 미삽입·포맷 이상·배선 오류 전부 여기로
        _fail = "마운트 실패: %r" % e
        _sd = False
        return False
    try:
        _mkdirs(config.SD_DIR)
        _mkdirs(_p("log"))
        _mkdirs(_p("archive"))
    except Exception as e:
        _fail = "디렉터리 생성 실패: %r" % e
        _sd = False
        return False
    _sd, _fail = True, None
    return True


def available():
    return bool(_sd)


def _disable(why):
    """쓰기 도중 실패 — 카드를 뽑았거나 파일시스템이 깨졌다. 조용히 내려앉는다."""
    global _sd, _fail
    _sd, _fail = False, why


def _append(path, text):
    """추가 기록. 실패하면 SD 를 비활성으로 내리고 False — 예외는 절대 밖으로 안 나간다."""
    if not _sd:
        return False
    try:
        with open(path, "a") as f:
            f.write(text)
        return True
    except Exception as e:
        _disable("쓰기 실패(카드 탈착?): %r" % e)
        return False


# ── 1) 측정 로그 전문 ──

def log_line(msg, stamp=""):
    """datalog.log 가 남기는 줄을 SD 에도 그대로 미러링한다(플래시는 512KB 에서 돌려쓴다).
    월별 파일로 나눠 한 파일이 무한정 커지지 않게 한다."""
    global _log_month, _log_f
    if not _sd:
        return False
    month = stamp[:7] if len(stamp) >= 7 else "unknown"
    path = _p("log", "measure-%s.log" % month)
    if month != _log_month:
        _log_month, _log_f = month, None
    return _append(path, (stamp + " " if stamp else "") + msg + "\n")


# ── 2) 보관 창 밖 기록 아카이브 ──

def archive(src_path, line):
    """datalog 가 14일 창 밖으로 잘라낸 줄을 무기한 보관한다(datalog.archive_sink 로 연결).

    원본 저장소의 dkh_plateau_archive.jsonl(2026-08-04) 과 같은 역할 — 보관 컷은 대시보드
    조회 범위일 뿐이고, 잘려나가는 궤적은 사후 분석 표본이라 따로 쌓아 둔다."""
    if not _sd:
        return False
    name = src_path.rsplit("/", 1)[-1]           # dkh.dat / plateau.jsonl
    if not line.endswith("\n"):
        line += "\n"
    return _append(_p("archive", name), line)


# ── 3) RF/BT 이벤트 원장 ──

def rf_event(kind, target, detail="", stamp=""):
    """링크 계층의 전환·재연결·신원검증 이벤트를 한 줄씩 쌓는다(link.rf_event 로 연결).

    kind 예: switch_ok / switch_silent / switch_fail / rebind_fail / identity_mismatch /
             reconnect_start / reconnect_retry / reconnect_ok / reconnect_fail
    JSON 을 쓰지 않고 탭 구분으로 남긴다 — 파싱은 나중에 PC 에서 하면 되고, 기기에서는
    한 줄 쓰기가 가장 싸고 중간에 전원이 끊겨도 앞 줄들이 온전하다."""
    if not _sd:
        return False
    return _append(_p("rf.jsonl"),
                   "%s\t%s\t%s\t%s\n" % (stamp, kind, target or "-", detail))


def status():
    """정비페이지·화면 표시용 상태."""
    if _sd:
        used = None
        try:
            st = os.statvfs(config.SD_MOUNT)
            total = st[0] * st[2]
            free = st[0] * st[3]
            used = {"total_mb": round(total / 1048576.0, 1),
                    "free_mb": round(free / 1048576.0, 1)}
        except Exception:
            pass
        return {"ok": True, "dir": config.SD_DIR, "space": used, "error": None}
    return {"ok": False, "dir": config.SD_DIR, "space": None, "error": _fail}


def attach():
    """datalog·link 의 싱크에 연결한다. main 이 부팅 직후 1회 호출.
    마운트 실패 시에도 안전하게 호출할 수 있다(싱크가 연결돼도 내부에서 즉시 False)."""
    import datalog
    import link
    import rwtime

    datalog.archive_sink = archive
    datalog.sd_log_sink = lambda msg: log_line(msg, rwtime.stamp())
    lk = link.get_if_created()
    if lk is not None:
        lk.rf_event = lambda kind, target, detail: rf_event(kind, target, detail, rwtime.stamp())
    link.rf_event_default = (
        lambda kind, target, detail: rf_event(kind, target, detail, rwtime.stamp()))
