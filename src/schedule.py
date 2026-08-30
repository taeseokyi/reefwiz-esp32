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
import os

import config
import rwtime

SCHEDULE_FILE = config.DATA_DIR + "/schedule.json"
HOLD_FILE = config.DATA_DIR + "/measure_hold.json"

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


def measure_gate(t, last_slot, ntp_done, held):
    """정시 측정 게이트 — ("run" | "skip_hold" | "wait", 슬롯).

    ★main 루프 안에 두지 않는다(due_measure 를 뺀 것과 같은 이유 — 루프 안의 판정은
      테스트할 수 없다). 규칙이 셋이고 서로 미묘하게 다르다:
        run       측정한다.
        skip_hold 보류 중이라 건너뛴다 — ★호출부가 **슬롯을 소비해야 한다**. 안 그러면
                  보류를 푸는 순간 지나간 회차가 곧바로 튀어나온다(06:30 에 풀었는데
                  05시 회차가 시작되는 식). dkh.dat 에는 아무것도 쓰지 않는다.
        wait      회차가 아니거나 시각 미동기 — **슬롯을 소비하지 않는다**. 시각이 늦게
                  맞으면 그 회차는 아직 유효하다(종전 동작 유지)."""
    due, slot = due_measure(t, last_slot)
    if not due:
        return "wait", slot
    if held:
        return "skip_hold", slot
    return ("run" if ntp_done else "wait"), slot


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


# ─────────────────────────────────────────────
# 측정 보류(정비 래치) — 정시 측정을 의도적으로 멈춘다
# ─────────────────────────────────────────────
#
# ★왜 필요한가(2026-08-30 사용자 요청): 장비를 정비하거나 수질을 안정화시키는 동안에는
#   정시 측정이 오히려 방해다 — 시약·시료를 낭비하고, 프로브를 뽑아 둔 상태면 엉뚱한 값이
#   기록된다. 종전에는 멈출 수단이 **에러 래치를 일부러 만드는 것**뿐이었는데 그건 고장과
#   구분되지 않는다(빨강 깜빡 + dkh.dat 에 0.000 줄이 회차마다 쌓인다).
#
# ★에러 래치와 다른 점:
#   ①의도된 상태다 — LED 는 파랑(치명 아님), 화면에도 사유·해제 예정 시각이 보인다.
#   ②**dkh.dat 에 아무것도 쓰지 않는다** — 건너뛴 회차는 기록 자체가 없다. 에러 래치는
#     회차마다 에러 표식을 다시 써서 '0.000 줄이 7개' 같은 것이 쌓였다(2026-08-29 실장).
#   ③수동 측정('지금 측정')은 막지 않는다 — 운영자 의도가 명확한 경로이고, 정비 뒤 확인
#     측정이 바로 그 용도다(시각 게이트가 수동을 막지 않는 것과 같은 규칙).
#
# ★만료 시각을 함께 둔다(사용자 확정 2026-08-30): 걸어 두고 잊으면 수조가 무기한 방치된다.
#   만료되면 메인 루프가 자동 해제하고 로그를 남긴다. 무기한(until=None)도 고를 수 있다.
#
# ★재부팅을 견뎌야 한다 — 정비 중 전원을 내렸다 올리는 것은 흔하다. 그래서 파일에 남긴다.

HOLD_MAX_H = 720         # 만료 상한 30일 — 그 이상은 '무기한'을 쓰라는 뜻

_hold = None             # None=아직 안 읽음 / False=보류 없음 / dict=보류 중


def _hold_read():
    global _hold
    if _hold is None:
        try:
            with open(HOLD_FILE) as f:
                h = json.load(f)
            _hold = h if isinstance(h, dict) and h.get("since") else False
        except (OSError, ValueError, AttributeError):
            _hold = False
    return _hold


def _hold_expired(h):
    """만료됐는가 — until 은 zero-padded 라 문자열 비교가 곧 시각 비교다."""
    return bool(h.get("until")) and rwtime.stamp() >= h["until"]


def hold_status():
    """보류 상태 — 화면·API 용. active=False 면 나머지 필드는 없다."""
    h = _hold_read()
    if not h:
        return {"active": False}
    return {"active": True, "since": h.get("since"), "until": h.get("until"),
            "reason": h.get("reason", ""), "expired": _hold_expired(h)}


def set_hold(hours=None, reason=""):
    """측정 보류 시작 — hours=None 이면 무기한. 반환 (ok, msg).
    ★이미 보류 중이어도 그냥 덮어쓴다(만료 연장·사유 수정이 잦은 조작이다)."""
    global _hold
    if hours is not None:
        try:
            hours = int(hours)
        except (TypeError, ValueError):
            return False, "보류 시간이 숫자가 아닙니다"
        if hours < 1 or hours > HOLD_MAX_H:
            return False, "보류 시간은 1~%d시간입니다(그 이상은 '무기한')" % HOLD_MAX_H
    h = {"since": rwtime.stamp(),
         "until": rwtime.stamp_after(hours * 3600) if hours else None,
         "hours": hours,
         "reason": (reason or "").strip()[:120]}
    try:
        tmp = HOLD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(h, f)
        os.rename(tmp, HOLD_FILE)
    except OSError as e:
        return False, "보류 파일 저장 실패: %r" % e
    _hold = h
    return True, ("측정 보류 시작 — %s"
                  % ("%d시간 뒤(%s) 자동 해제" % (hours, h["until"]) if hours else "무기한(수동 해제 전까지)"))


def clear_hold():
    """보류 해제. 반환: 해제된 보류 dict 또는 None(보류가 아니었음)."""
    global _hold
    was = _hold_read() or None
    _hold = False
    try:
        os.remove(HOLD_FILE)
    except OSError:
        pass
    return was


def hold_check():
    """메인 루프용 — 만료됐으면 **자동 해제**한다. 반환 (보류중인가, 방금 자동해제된 보류)."""
    h = _hold_read()
    if not h:
        return False, None
    if _hold_expired(h):
        return False, clear_hold()
    return True, None
