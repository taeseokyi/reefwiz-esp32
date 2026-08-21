# 측정 스케줄 — 회차(시각) 목록과 도저 자동조정 회차. 정비페이지에서 바꾼다.
#
# ★왜 생겼나(2026-08-21): 종전엔 config.MEASURE_HOURS / DOSER_SLOT_HOUR 하드코딩이라
#   회차를 바꾸려면 소스를 고쳐 다시 올려야 했다(BIND 주소가 2026-08-19 에 겪은 것과 같은
#   종류의 문제). `/data/schedule.json` 이 config 값보다 우선하고, 파일이 없거나 깨지면
#   config 로 떨어진다(기존 배포·테스트 호환).
#
# ★시각 단위는 '시'다(분 없음, 사용자 확정 2026-08-21). main 루프의 판정이 종전과 같은
#   형태(`t[3] in hours`)로 남으므로 새 버그 여지가 가장 적다.
#
# ★최소 간격 2시간(config.MEASURE_MIN_GAP_H, 사용자 확정): "측정이 2시간을 넘긴 적이 없다".
#   간격은 **원형으로** 본다 — [23, 0] 은 1시간 간격이라 거부된다.
import json

import config

SCHEDULE_FILE = config.DATA_DIR + "/schedule.json"

_cache = None            # {"measure_hours": [...], "doser_slot_hour": h}. None=아직 안 읽음


def _read():
    global _cache
    if _cache is None:
        cur = None
        try:
            with open(SCHEDULE_FILE) as f:
                raw = json.load(f)
            hours, slot, err, _warn = _validate(raw.get("measure_hours"),
                                                raw.get("doser_slot_hour"))
            if err:
                # 깨진 파일로 부팅을 막지 않는다 — config 값으로 돌아가고 사실만 알린다.
                print("[schedule] schedule.json 무시(%s) — config 값으로 폴백" % err)
            else:
                cur = {"measure_hours": hours, "doser_slot_hour": slot}
        except (OSError, ValueError, AttributeError):
            pass
        _cache = cur or {"measure_hours": sorted(config.MEASURE_HOURS),
                         "doser_slot_hour": config.DOSER_SLOT_HOUR}
    return _cache


def invalidate():
    """캐시 무효화 — 파일이 밖에서 바뀐 뒤(백업 복원 등) 다음 조회가 다시 읽게 한다."""
    global _cache
    _cache = None


def measure_hours():
    return _read()["measure_hours"]


def doser_slot_hour():
    return _read()["doser_slot_hour"]


def source():
    """값의 출처 — 정비페이지 표시용."""
    try:
        with open(SCHEDULE_FILE):
            return "file"
    except OSError:
        return "config"


# ── 검증 ──

def _gaps(hours):
    """원형 인접 간격 목록(시간). 1개면 24 하나."""
    if len(hours) == 1:
        return [24]
    out = []
    for i in range(len(hours)):
        nxt = hours[(i + 1) % len(hours)]
        out.append((nxt - hours[i]) % 24 or 24)
    return out


def _validate(hours, slot):
    """(정렬된 hours, slot, 오류사유, 경고) — 오류가 있으면 아무것도 쓰지 않는다."""
    if not isinstance(hours, (list, tuple)) or not hours:
        return None, None, "측정 회차를 1개 이상 골라야 합니다", ""
    clean = []
    for h in hours:
        try:
            h = int(h)
        except (TypeError, ValueError):
            return None, None, "회차에 숫자가 아닌 값: %r" % (h,), ""
        if not (0 <= h <= 23):
            return None, None, "회차는 0~23시여야 합니다: %d" % h, ""
        if h in clean:
            return None, None, "회차가 중복됐습니다: %d시" % h, ""
        clean.append(h)
    clean = sorted(clean)
    if len(clean) > config.MEASURE_HOURS_MAX:
        return None, None, ("회차는 최대 %d개입니다(최소 간격 %dh 의 상한)"
                            % (config.MEASURE_HOURS_MAX, config.MEASURE_MIN_GAP_H)), ""
    worst = min(_gaps(clean))
    if worst < config.MEASURE_MIN_GAP_H:
        return None, None, ("회차 간격이 %dh 입니다 — 최소 %dh 이상이어야 합니다"
                            "(자정을 넘는 간격도 같이 봅니다)"
                            % (worst, config.MEASURE_MIN_GAP_H)), ""
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        return None, None, "도저 조정 회차를 골라야 합니다", ""
    if slot not in clean:
        return None, None, ("도저 조정 회차(%d시)가 측정 회차에 없습니다 — 측정하지 않는 "
                            "시각에는 조정도 못 합니다" % slot), ""
    # ★경고(저장은 허용): 도저 계산은 창 7일 안에 유효 측정 MIN_VALID 점이 있어야 돈다.
    #   하루 1회면 7점뿐이라 매 회차 "유효 측정 부족"으로 중단된다 — 막지는 않고 알린다.
    warn = ""
    if len(clean) * config.WINDOW_DAYS < config.MIN_VALID:
        warn = ("하루 %d회면 창 %d일에 %d점뿐입니다(도저 계산은 %d점 필요) — 자동조정이 "
                "매번 '유효 측정 부족'으로 중단됩니다"
                % (len(clean), config.WINDOW_DAYS, len(clean) * config.WINDOW_DAYS,
                   config.MIN_VALID))
    return clean, slot, None, warn


def validate(obj):
    """저장 형태가 쓸 수 있는 값인지 — (ok, 사유). 백업 복원(archive._validate)이 쓴다:
    깨진 회차를 되돌리면 측정 시각이 조용히 기본값으로 돌아간다."""
    if not isinstance(obj, dict):
        return False, "형식 오류"
    _h, _s, err, _w = _validate(obj.get("measure_hours"), obj.get("doser_slot_hour"))
    return (False, err) if err else (True, "")


def set_schedule(body):
    """웹에서 온 스케줄 저장 — (ok, 메시지, 경고). 검증을 통과해야만 쓴다."""
    if not isinstance(body, dict):
        return False, "형식 오류", ""
    hours, slot, err, warn = _validate(body.get("measure_hours"),
                                       body.get("doser_slot_hour"))
    if err:
        return False, err, ""
    obj = {"measure_hours": hours, "doser_slot_hour": slot}
    try:
        tmp = SCHEDULE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        import os
        os.rename(tmp, SCHEDULE_FILE)
    except OSError as e:
        return False, "저장 실패: %r" % e, ""
    global _cache
    _cache = obj                       # 메인 루프가 매 틱 읽으므로 다음 틱부터 새 회차
    return True, ("측정 회차 %s시 · 도저 조정 %d시 저장"
                  % ("·".join("%d" % h for h in hours), slot)), warn


# ── 회차 판정 (main 루프) ──

def slot_of(t):
    """시각 튜플 → 회차 키 (y, m, d, hh). 회차당 1회 보장에 쓴다."""
    return (t[0], t[1], t[2], t[3])


def due_measure(t, last_slot):
    """지금 측정할 회차인가 — (실행할지, 이번 슬롯 키).

    ★순수 함수로 뺀 이유: main 루프 안에 있던 판정이라 테스트가 불가능했다. 회차 간격을
    2시간까지 좁힐 수 있게 되면서 '긴 회차가 다음 슬롯을 잡아먹는' 경계가 실제로 생겼고
    (05시 회차가 07:30 에 끝나면 07시 회차가 곧바로 시작된다), 그 규칙을 시험할 수 있어야
    한다. 소비 처리는 호출부가 측정 종료 후 `slot_of(now)` 로 갱신하는 것으로 한다."""
    slot = slot_of(t)
    if t[3] not in measure_hours():
        return False, slot
    return slot != last_slot, slot


def next_hour(t):
    """다음 회차 시각(시) — 정비페이지 표시용. 회차가 없으면 None."""
    hours = measure_hours()
    if not hours:
        return None
    for h in hours:
        if h > t[3]:
            return h
    return hours[0]


# ── 보관 백스톱 ──

def rows_cap(base=None):
    """날짜 컷 뒤에 적용할 행수 백스톱 — 회차 수에 맞춰 늘어난다.

    ★종전 config.SERIES_MAX / PLATEAU_MAX 는 `RETENTION_DAYS * 6` 고정값(= 하루 3회 ×2
    여유)이었다. 회차를 웹에서 바꿀 수 있게 되면서 하루 6회를 넘기면 **날짜 컷을 통과한
    14일 창 안의 데이터가 행수 컷에서 잘리는** 문제가 생긴다(하루 12회면 168행 → 84행).
    회차 수의 2배를 하한으로 두어 수동 측정 여유까지 남긴다."""
    if base is None:
        base = config.SERIES_BASE_PER_DAY
    return config.RETENTION_DAYS * max(base, len(measure_hours()) * 2)
