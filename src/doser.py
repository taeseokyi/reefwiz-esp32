# AFR 도저 자동 조정 이식 — 원본 reefwiz/bin/doser_adjust.py (597줄).
# 계산(compute/theil_sen/plan_lrt)과 적용 시퀀스(lrt 에코 검증 → refresh all → 재확인 →
# 실패 시 롤백)는 원본 그대로. 달라진 점:
#   - 오버라이드/목표/보정 파일이 GitHub API 커밋이 아니라 로컬(/data) — 웹서버가 쓴다.
#   - CO2 의심 플래그도 로컬 dkh_series.json 에서 직접 읽음 → 원본의 접미(suffix) 정렬
#     핵이 필요 없어짐(원격 series 로 날짜를 복원하려고 존재하던 코드). 키 매칭만 남긴다.
#   - 도저 링크는 측정기와 공유하는 HC-05 1개. link.acquire("doser") 로 전환·신원검증 후
#     송신한다. LF 만 송신(CR 붙으면 펌웨어 미실행 — 원본 확인).
# 창·추세 산정은 원본 2026-08-16 변경(dkh.dat 날짜 컬럼)을 반영해 날짜 기준으로 바뀌었다.
import json
import re
import time

import config
import datalog
import dkh_dat
import link
import rwtime
from link import _decode

HISTORY_FILE = config.DATA_DIR + "/doser_history.json"
OVERRIDE_FILE = config.DATA_DIR + "/doser_override.json"
OVERRIDE_STATE_FILE = config.DATA_DIR + "/doser_override_state.json"
CONFIG_FILE = config.DATA_DIR + "/doser_config.json"
SERIES_FILE = config.DATA_DIR + "/dkh_series.json"

LRT_RE = re.compile(r"왼쪽 동작\(RUN\) 시간 설정 값:\s*(\d+)")
LGT_RE = re.compile(r"왼쪽 휴지\(GAP\) 시간 설정 값:\s*(\d+)")

log = datalog.log


def _median(seq):
    s = sorted(seq)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


# ── 변환/계획 (원본 동일) ──

def lrt_to_ml_day(lrt_ms):
    return lrt_ms / config.MS_PER_ML * config.DOSES_PER_DAY * config.DILUTION


def ml_day_to_lrt(ml_day):
    return ml_day / (config.DOSES_PER_DAY * config.DILUTION) * config.MS_PER_ML


def plan_lrt(ml_day):
    """수동 설정(원액 mL/일) → lrt(ms). 0 이하=정지(lrt 0), 양수는 범위 클램프."""
    if ml_day <= 0:
        return 0, "정지"
    raw = int(round(ml_day_to_lrt(ml_day) / 100.0) * 100)
    lrt = max(config.LRT_MIN, min(config.LRT_MAX, raw))
    return lrt, ("" if lrt == raw else "범위 클램프 %d->%dms" % (raw, lrt))


# ── 측정 데이터 ──

def _series_key(hh, tank_kh, temp):
    return (int(hh), round(abs(float(tank_kh)), 3), round(float(temp), 1))


def co2_excluded_keys():
    """로컬 dkh_series.json 에서 co2_suspect 행의 키 집합. 실패 = 빈 집합(제외 없이 계산)."""
    series = _read_json(SERIES_FILE, [])
    keys = set()
    if not isinstance(series, list):
        return keys
    for r in series:
        try:
            if r.get("co2_suspect"):
                keys.add(_series_key(r["hh"], r["tank_kh"], r["temp"]))
        except (KeyError, TypeError, ValueError):
            pass
    return keys


def build_row_days(lines):
    """{줄 인덱스: 측정일 서수} — 창을 날짜로 자르기 위한 시간축(원본 2026-08-16 반영).

    dkh.dat 의 날짜 컬럼(dkh_dat)을 그대로 읽는다. 에러 행(전부 0)은 측정이 아니라
    시간축에 넣지 않는다. 날짜 없는 구형식 행은 맵에 안 들어간다(창 밖 취급).
    날짜를 가진 행이 하나도 없으면 None — ★호출부는 근사하지 말고 중단할 것."""
    days = {}
    for i, parts in enumerate(lines):
        row = dkh_dat.parse_parts(parts)
        if row and row["date"] and not row["is_error"]:
            o = dkh_dat.day_ordinal(row["date"])
            if o is not None:
                days[i] = o
    return days or None


def build_times(lines, row_days):
    """{줄 인덱스: 일 단위 시각} — 날짜(build_row_days) + 측정 시각(HH).
    row_days 의 행은 전부 파싱되는 측정 행이므로 키 집합이 같다(창 안 모든 점의 시각을
    theil_sen_per_day 가 찾을 수 있어야 한다)."""
    times = {}
    for i, o in row_days.items():
        row = dkh_dat.parse_parts(lines[i])
        if row:
            times[i] = o + row["hh"] / 24.0
    return times


def read_recent_kh(row_days, days=None):
    """최근 창의 (줄 인덱스, tank_kh) 유효값만. 0.0=에러·음수=미평탄 제외.

    ★창 산정(원본 2026-08-16): row_days 로 **날짜**를 보고 자른다 — 마지막 측정일 포함
    `days`일. 측정이 빠진 날이 있어도 창이 과거로 늘어나지 않고, 추가 측정을 돌린 날이
    있어도 창 안쪽이 밀려나지 않는다. 종전의 "최근 21행 ≈ 7일" 회차 근사는 폐기했다.
    날짜를 모르는 행(구형식 백업본)은 창 밖으로 본다.

    CO2 의심 행은 키 매칭으로 추가 제외한다 — 제외돼도 건너뛰기만 하므로 시간축은 그대로
    유지된다(기존 유효성 탈락과 같은 방식). 반환: (pts, 창 안에서 CO2 로 제외된 행 수)."""
    if days is None:
        days = config.WINDOW_DAYS
    lines = datalog.read_dat_lines()
    excl = co2_excluded_keys()
    cut = max(row_days.values()) - (days - 1)
    pts, n_co2 = [], 0
    for i in sorted(row_days):
        if row_days[i] < cut:
            continue
        row = dkh_dat.parse_parts(lines[i])
        if row is None:
            continue
        kh = row["tank_kh"]
        if not (config.VALID_LO < kh < config.VALID_HI):
            continue
        if excl and _series_key(row["hh"], row["tank_kh"], row["temp"]) in excl:
            n_co2 += 1
            continue
        pts.append((i, kh))
    return pts, n_co2


def theil_sen_per_day(pts, times):
    """쌍별 기울기 중앙값(dKH/일).

    times({줄 인덱스: 일 단위 시각}, build_times) = **실제 측정 시각 간격**(원본 2026-08-16).
    종전의 "행 간격 8h 균일 가정"(ROW_DAYS) 근사는 폐기했다 — 05/13/21시가 실제로 8h
    간격이라 결측 없는 창에서는 같은 답이지만, 결측·추가 측정이 있으면 시간축이 어긋난다."""
    slopes = []
    for a in range(len(pts)):
        i, ki = pts[a]
        for b in range(a + 1, len(pts)):
            j, kj = pts[b]
            d = times[j] - times[i]
            if d:
                slopes.append((kj - ki) / d)
    return _median(slopes)


def compute(level, slope, cur_lrt, target=None):
    """새 lrt 와 근거 — 순수 계산(원본 동일: 스텝 캡, 절대범위, 데드밴드, 정지 유지 가드)."""
    if target is None:
        target = config.TARGET_DKH
    error = target - level
    desired_rate = max(-config.DAILY_RATE_CAP,
                       min(config.DAILY_RATE_CAP, error / config.APPROACH_DAYS))
    delta_rate = desired_rate - slope
    delta_ml = delta_rate / config.SENS
    cur_ml = lrt_to_ml_day(cur_lrt)
    raw_lrt = ml_day_to_lrt(cur_ml + delta_ml)

    if cur_lrt == 0:
        # 정지(0) 유지 — 하한이 0 을 2000ms 로 끌어올려 '멈춘 도저 재가동' 권고가 나오던
        # 사고(원본 7/25~27) 방지. 재개는 대시보드 수동 설정으로만.
        notes = ["정지(0) 유지 — 재개는 수동 설정으로만"]
        if error > 0:
            notes.append("목표 미만 — 재개 검토 필요")
        return {"error": round(error, 3), "desired_rate": round(desired_rate, 4),
                "delta_rate": round(delta_rate, 4), "delta_ml": round(delta_ml, 1),
                "new_lrt": 0, "notes": notes}

    notes = []
    step_cap = cur_lrt * config.STEP_MAX_FRAC
    if abs(raw_lrt - cur_lrt) > step_cap:
        raw_lrt = cur_lrt + (step_cap if raw_lrt > cur_lrt else -step_cap)
        notes.append("스텝 +-30% 제한")
    if raw_lrt < config.LRT_MIN:
        raw_lrt = config.LRT_MIN
        notes.append("하한 %dms" % config.LRT_MIN)
    elif raw_lrt > config.LRT_MAX:
        raw_lrt = config.LRT_MAX
        notes.append("상한 %dms" % config.LRT_MAX)
    new_lrt = int(round(raw_lrt / 100.0) * 100)
    if abs(new_lrt - cur_lrt) < config.DEADBAND_MS:
        new_lrt = cur_lrt
        notes.append("데드밴드(<200ms) — 변경 없음")
    return {"error": round(error, 3), "desired_rate": round(desired_rate, 4),
            "delta_rate": round(delta_rate, 4), "delta_ml": round(delta_ml, 1),
            "new_lrt": new_lrt, "notes": notes}


# ── 도저 링크 (공유 HC-05, LF only) ──
# ★HC-05 1개 체제(2026-08-18): 종전에는 도저 전용 UART2 를 따로 잡았지만, 이제 측정기와
#   같은 모듈을 번갈아 쓴다. 명령 전에 link.acquire("doser") 로 전환·신원검증을 거치므로
#   도저 명령이 측정기로 가는 사고가 구조적으로 막힌다. LF only 규약은 TARGETS 의 eol 이
#   들고 있다(CR 이 붙으면 도저 펌웨어가 명령을 실행하지 않는다 — 원본 확인).


def send_cmd(cmd, wait=3.0):
    """명령 한 줄(LF only) 전송 후 wait초 응답 수집. 링크 확보 실패 시 빈 목록.
    ★빈 목록은 호출부에서 '파싱 실패'로 이어져 도징이 바뀌지 않는다 — 안전한 방향이다."""
    lk, err = link.acquire("doser", log=log)
    if lk is None:
        log("[도저] 링크 확보 실패 — %s" % err)
        return []
    try:
        lk.flush_input()
        lk.write_line(cmd)
    except link.LinkFrozen as e:
        log("[도저] 링크 동결 — 명령 미송신: %s" % e)
        return []
    lines, deadline = [], time.time() + wait
    while time.time() < deadline:
        if lk.uart.any():
            ln = _decode(lk.uart.readline()).strip()
            if ln:
                lines.append(ln)
        else:
            time.sleep_ms(50)
    return lines


def query_left():
    """`ls`로 (lrt_ms, lgt_min) 조회. 파싱 실패 (None, None)."""
    text = "\n".join(send_cmd("ls"))
    m_rt, m_gt = LRT_RE.search(text), LGT_RE.search(text)
    return (int(m_rt.group(1)) if m_rt else None,
            int(m_gt.group(1)) if m_gt else None)


def apply_lrt(new_lrt, old_lrt, retries=3):
    """lrt 에코 검증 → refresh all(타이머 반영 필수) → 재확인. 실패 시 이전값 롤백."""
    for attempt in range(1, retries + 1):
        echo = "\n".join(send_cmd("lrt %d" % new_lrt))
        m = LRT_RE.search(echo)
        if m and int(m.group(1)) == new_lrt:
            ack = "\n".join(send_cmd("refresh all"))
            if "Refreshed all timers!" in ack:
                confirmed, _ = query_left()
                if confirmed == new_lrt:
                    return True
            log("[재시도 %d/%d] refresh all 확인 실패" % (attempt, retries))
        else:
            log("[재시도 %d/%d] lrt %d 에코 검증 실패" % (attempt, retries, new_lrt))
        time.sleep(1)
    rb = "\n".join(send_cmd("lrt %d" % old_lrt))
    m = LRT_RE.search(rb)
    log("[롤백] lrt %d 복원 %s" % (old_lrt,
        "성공" if m and int(m.group(1)) == old_lrt else "실패(링크 사망?)"))
    return False


# ── 이력/상태 ──

def load_history():
    h = _read_json(HISTORY_FILE, [])
    return h if isinstance(h, list) else []


def append_history(entry):
    h = load_history()
    h.append(entry)
    _write_json(HISTORY_FILE, h[-config.HISTORY_MAX:])


def computed_run_count(history):
    return sum(1 for e in history if e.get("mode") in ("advisory", "auto"))


def record_abort(note):
    log("[중단] %s — 도저 변경 없음" % note)
    append_history({"ts": rwtime.stamp(), "mode": "abort", "applied": False, "note": note})


def fetch_target():
    data = _read_json(CONFIG_FILE)
    try:
        t = float(data["target_dkh"])
    except (KeyError, TypeError, ValueError):
        return config.TARGET_DKH
    return t if config.TARGET_LO <= t <= config.TARGET_HI else config.TARGET_DKH


# ── 진입점 (원본 CLI 분기 대체) ──

def check_override():
    """대시보드 수동 설정 확인·적용 — 매 측정 후 + 웹서버 POST 직후 호출.
    적용 성공 id 만 상태 저장(실패 시 다음 회차 자동 재시도). 새 오버라이드 적용 시 True."""
    ov = _read_json(OVERRIDE_FILE)
    if not ov:
        return False
    try:
        ml = float(ov["ml_day"])
        oid = str(ov["id"])
    except (KeyError, TypeError, ValueError):
        log("[오버라이드] 형식 오류 무시: %r" % ov)
        return False
    state = _read_json(OVERRIDE_STATE_FILE, {})
    if oid == state.get("applied_id"):
        return False

    new_lrt, plan_note = plan_lrt(ml)
    note = "대시보드 " + ("정지(lrt 0)" if new_lrt == 0 else "수동 설정")
    if plan_note and new_lrt != 0:
        note += " | " + plan_note
    cur_lrt, cur_lgt = query_left()
    if cur_lrt is None:
        log("[수동] ls 파싱 실패 — 다음 회차 재시도")
        return False
    applied = True if new_lrt == cur_lrt else apply_lrt(new_lrt, cur_lrt)
    append_history({
        "ts": rwtime.stamp(), "mode": "manual", "override_id": oid,
        "requested_ml": ml, "lrt_old": cur_lrt, "lrt_new": new_lrt, "lgt_min": cur_lgt,
        "ml_day_old": round(lrt_to_ml_day(cur_lrt), 2),
        "ml_day_new": round(lrt_to_ml_day(new_lrt), 2),
        "applied": applied,
        "note": note if applied else note + " | 적용 실패 — 다음 회차 재시도",
    })
    if applied:
        _write_json(OVERRIDE_STATE_FILE, {"applied_id": oid, "applied_at": rwtime.stamp()})
    log("[수동] %smL/일 요청(id=%s) -> lrt %s->%d 적용=%s" % (ml, oid, cur_lrt, new_lrt, applied))
    return applied


def slot_adjust():
    """정기 자동 조정 — 매일 DOSER_SLOT_HOUR 측정 종료 후 1회(--slot-adjust 상당).
    AUTO_APPLY=False 인 동안은 권고만 기록."""
    lines = datalog.read_dat_lines()
    row_days = build_row_days(lines)
    if row_days is None:
        # ★날짜를 모르면 근사하지 않고 멈춘다(원본 1dd5020 규칙). 종전에는 "최근 21행 ≈ 7일"
        #   로 회차 근사를 했는데, 결측·추가 측정이 있으면 창이 조용히 어긋난 채 도징량이
        #   바뀌었다. 날짜 없는 dkh.dat 은 구형식 백업본뿐이므로 정상 운용에서는 안 걸린다.
        record_abort("dkh.dat 에 날짜 있는 측정 행이 없음 — 창 산정 불가(구형식 파일?)")
        return
    times = build_times(lines, row_days)
    pts, n_co2 = read_recent_kh(row_days)
    co2_note = "CO2 의심 %d점 제외" % n_co2 if n_co2 else ""
    if n_co2 > config.CO2_EXCLUDE_MAX:
        record_abort("CO2 제외 과다(%d>%d) — 판정기 점검 필요" % (n_co2, config.CO2_EXCLUDE_MAX))
        return
    if len(pts) < config.MIN_VALID:
        record_abort("유효 측정 부족(%d/%d)%s" % (len(pts), config.MIN_VALID,
                     " | " + co2_note if co2_note else ""))
        return

    level = _median([kh for _, kh in pts[-3:]])
    slope = theil_sen_per_day(pts, times)
    target = fetch_target()

    cur_lrt, cur_lgt = query_left()
    if cur_lrt is None:
        record_abort("ls 응답 파싱 실패(링크 순단?)")
        return

    r = compute(level, slope, cur_lrt, target)
    mode = ("advisory" if not config.AUTO_APPLY
            or computed_run_count(load_history()) < config.ADVISORY_RUNS else "auto")
    applied = False
    note = ", ".join(r["notes"])
    if co2_note:
        note = (note + " | " if note else "") + co2_note
    if mode == "auto" and r["new_lrt"] != cur_lrt:
        applied = apply_lrt(r["new_lrt"], cur_lrt)
        if not applied:
            note = (note + " | " if note else "") + "적용 실패(에코 검증 불통) — 기존값 유지"
    elif mode == "advisory":
        note = (note + " | " if note else "") + "권고 모드(적용 안 함)"

    entry = {
        "ts": rwtime.stamp(), "mode": mode, "level": round(level, 3),
        "slope_per_day": round(slope, 3), "target": target, "error": r["error"],
        "lrt_old": cur_lrt, "lrt_new": r["new_lrt"], "lgt_min": cur_lgt,
        "ml_day_old": round(lrt_to_ml_day(cur_lrt), 2),
        "ml_day_new": round(lrt_to_ml_day(r["new_lrt"]), 2),
        "applied": applied, "excluded_co2": n_co2, "note": note,
    }
    append_history(entry)
    log("[%s] 수준 %.3f 목표 %s 추세 %+.3f/일 | lrt %d->%d 적용=%s%s"
        % (mode, level, target, slope, cur_lrt, r["new_lrt"], applied,
           " | " + note if note else ""))
