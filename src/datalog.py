# 데이터 기록 — dkh.dat + 대시보드 JSON(series/latest/plateau) 직접 생성.
# 원본 구조에서는 WSL sync_dkh_dat.py 가 dkh.dat/로그를 파싱해 docs/*.json 을 만들었지만,
# ESP32 는 측정 직후 같은 형식의 JSON 을 바로 쓴다(날짜 보유 — 접미 정렬 핵 불필요).
# 스키마는 2026-08-13 시점 저장소 docs/ 실물에서 확인한 것과 동일:
#   dkh_series.json: [{hh, ref_kh, tank_kh, temp, is_flat, date, co2_suspect}]
#   dkh_latest.json: 위 + count(dkh.dat 누적 행수)
#   dkh_plateau_history.json: [{run_started, mode, completed, tank:[{n,ph,elapsed}], ref:[...],
#                               tank_flat_n, ref_flat_n, co2_suspect, ref_net_mph}]
import json
import os

import config
import dkh_dat

DAT_FILE = config.DATA_DIR + "/dkh.dat"
SERIES_FILE = config.DATA_DIR + "/dkh_series.json"
LATEST_FILE = config.DATA_DIR + "/dkh_latest.json"
# plateau 이력은 JSONL(런 1개 = 1줄) — 전체를 파이썬 객체로 올리지 않기 위해서다.
# 대시보드가 요청하는 dkh_plateau_history.json 배열은 webserver 가 줄 단위로 스트리밍 조립한다.
PLATEAU_JSONL = config.DATA_DIR + "/plateau.jsonl"
LOG_FILE = config.DATA_DIR + "/measure_kh.log"

_log_f = None

# 보관 창 밖으로 밀려난 기록을 받아가는 싱크 — storage.py 가 SD 마운트에 성공하면 연결한다.
# SD 가 없으면 None 이고, 그때는 종전처럼 그냥 버려진다(측정을 막지 않는다).
archive_sink = None
# 로그 전문 미러 싱크 — 플래시 로그는 LOG_MAX_BYTES 에서 돌려쓰지만 SD 에는 다 남긴다.
sd_log_sink = None


def _archive(src_path, line):
    """트림으로 버려지는 한 줄을 아카이브로 흘려보낸다. 실패는 삼킨다 — 보관 실패가
    측정·트림을 멈추면 안 된다."""
    if archive_sink is None:
        return
    try:
        archive_sink(src_path, line)
    except Exception as e:      # SD 탈착 등 어떤 예외도 트림을 죽이면 안 된다
        log("[경고] 아카이브 기록 실패(무시하고 진행): %r" % e)


def log(msg):
    """print + measure_kh.log 기록(상한 초과 시 새로 시작) — 원본 setup_logging 대체.
    SD 가 있으면 전문을 그쪽에도 미러링한다(플래시는 돌려쓰기라 과거가 사라진다)."""
    global _log_f
    print(msg)
    if sd_log_sink is not None:
        try:
            sd_log_sink(msg)
        except Exception:
            pass        # SD 미러 실패가 측정 로그를 죽이면 안 됨
    try:
        if _log_f is None:
            try:
                mode = "w" if os.stat(LOG_FILE)[6] > config.LOG_MAX_BYTES else "a"
            except OSError:
                mode = "a"
            _log_f = open(LOG_FILE, mode)
        _log_f.write(msg + "\n")
        _log_f.flush()
    except OSError:
        _log_f = None   # 로그 실패가 측정을 죽이면 안 됨


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)   # 쓰기 도중 전원 단절 대비 원자적 교체


def _cut_days(items, day_of, days=None):
    """최근 `days`일치만 남긴다 — ★기준일은 오늘이 아니라 목록 안의 **마지막 날짜**.

    측정이 며칠 끊겨도 마지막 창이 통째로 사라지지 않게 하려는 것이다(원본 trim_to_days
    규칙). day_of(item) 이 'YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS' 를 돌려주면 되고,
    날짜를 못 읽는 항목은 창 밖으로 본다. 날짜 있는 항목이 하나도 없으면 원본을 그대로
    돌려준다(자를 기준이 없으므로 호출부의 행수 상한만 적용된다)."""
    if days is None:
        days = config.RETENTION_DAYS
    ords = []
    for it in items:
        d = day_of(it)
        ords.append(dkh_dat.day_ordinal(d[:10]) if d else None)
    dated = [o for o in ords if o is not None]
    if not dated:
        return items
    cut = max(dated) - (days - 1)
    return [it for it, o in zip(items, ords) if o is not None and o >= cut]


def _trim_dat():
    """dkh.dat 를 최근 RETENTION_DAYS(14일)치로 유지 — ★모든 정보 14일 정책.

    ★보관 기준은 회차가 아니라 날짜다(원본 2026-08-16 반영): 종전 42행(=14일×3회) 컷은
    하루 3회를 가정한 근사라, 측정이 빠진 날이 있으면 창이 14일보다 길어지고 추가 측정을
    돌린 날이 있으면 14일 안쪽 데이터가 밀려나 잘렸다. "최근 14일"은 무조건 14일이어야 한다.
    기준일은 오늘이 아니라 **마지막 기록일** — 측정이 며칠 끊겨도 마지막 창은 보존된다.

    날짜 없는 구형식 행은 창 밖으로 본다(원본과 동일 규칙). 다만 날짜 가진 행이 하나도
    없는 순수 구형식 파일이면 자를 기준 자체가 없으므로 종전 행수 컷으로 폴백한다.
    잘려나간 줄은 반환한다 — SD 아카이브가 있으면 storage 가 무기한 보관한다.
    """
    try:
        with open(DAT_FILE) as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
    except OSError as e:
        log("[경고] dkh.dat 읽기 실패: %r" % e)
        return []

    ords = []
    for ln in lines:
        row = dkh_dat.parse(ln)
        ords.append(dkh_dat.day_ordinal(row["date"]) if row and row["date"] else None)

    dated = [o for o in ords if o is not None]
    if dated:
        cut = max(dated) - (config.RETENTION_DAYS - 1)
        flags = [o is not None and o >= cut for o in ords]
    else:                                        # 순수 구형식 폴백 — 종전 행수 컷
        first = max(0, len(lines) - config.DAT_MAX_ROWS)
        flags = [i >= first for i in range(len(lines))]

    keep = [ln for ln, k in zip(lines, flags) if k]
    dropped = [ln for ln, k in zip(lines, flags) if not k]
    if not dropped:
        return []
    try:
        tmp = DAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(keep) + ("\n" if keep else ""))
        os.rename(tmp, DAT_FILE)
    except OSError as e:
        log("[경고] dkh.dat 트림 실패: %r" % e)
        return []
    for ln in dropped:
        _archive(DAT_FILE, ln + "\n")
    return dropped


def log_kh(day, hour, ref_ph, tank_ph, ref_kh, tank_kh, temp):
    """dkh.dat 한 줄 추가 — 형식: YYYY-MM-DD HH ref_pH tank_pH ref_kh tank_kh temp.

    ★날짜는 측정 *시작일*이다(원본 measure_kh_once 규칙): 사전폭기 25분 + 평탄 추종으로
    측정이 자정을 넘겨 끝나도 시작일에 귀속시킨다 — 그래야 05/13/21시 회차가 날짜별로
    깔끔히 묶인다. 호출부(run_once)가 시작 시점에 잡아둔 날짜를 그대로 넘긴다.
    기록 후 14일 초과분을 앞에서 잘라낸다."""
    line = dkh_dat.format_line(day, hour, ref_ph, tank_ph, ref_kh, tank_kh, temp)
    with open(DAT_FILE, "a") as f:
        f.write(line + "\n")
    dropped = _trim_dat()
    log("[LOG] dkh.dat <- " + line)
    return dropped


def dat_line_count():
    try:
        n = 0
        with open(DAT_FILE) as f:
            for ln in f:
                if ln.strip():
                    n += 1
        return n
    except OSError:
        return 0


def last_dat_is_error():
    """마지막 줄이 에러 표식(값 전부 0)이면 True — 에러 래치(원본 동일: 프로브 보호).

    ★파서 경유 필수: 종전 구현은 parts[1:6] 으로 값 5개를 위치로 읽었는데, 날짜 컬럼이
    붙으면 한 칸씩 밀려 temp 대신 tank_kh 까지만 검사하게 된다. dkh_dat 이 날짜 유무를
    흡수하므로 신·구 형식 모두 정확히 판정된다(구형식 백업본의 래치도 계속 읽힌다)."""
    try:
        last = None
        with open(DAT_FILE) as f:
            for ln in f:
                if ln.strip():
                    last = ln.strip()
    except OSError:
        return False
    if not last:
        return False
    row = dkh_dat.parse(last)
    return bool(row and row["is_error"])


def read_dat_lines():
    """공백 분리 필드 리스트의 리스트 — 소비자는 반드시 dkh_dat.parse_parts 로 해석할 것.
    ★위치 인덱싱(parts[4] 등) 금지: 날짜 컬럼 유무에 따라 필드가 밀린다."""
    try:
        with open(DAT_FILE) as f:
            return [ln.split() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def read_dat_rows():
    """dkh.dat 전체를 파싱된 dict 목록으로 — 날짜 유무를 신경 쓸 필요 없는 표준 경로."""
    return dkh_dat.load(DAT_FILE)


def classify_co2_suspect(ref_readings, ref_flat_n):
    """CO2 편향 의심 판정 — parse_plateau_log.classify_co2_suspect 이식(원본 2026-07-13).
    ref 전구간 net(첫 판독→마지막 판독, mpH)과 평탄 도달 횟수의 AND 결합.
    반환: (co2_suspect, ref_net_mph). ref 2점 미만이면 (False, None)."""
    if len(ref_readings) < 2:
        return False, None
    try:
        ref_net_mph = round((ref_readings[-1]["ph"] - ref_readings[0]["ph"]) * 1000)
    except (KeyError, TypeError):
        return False, None
    suspect = (ref_flat_n is not None and ref_flat_n >= config.CO2_FLAT_N_MIN
               and ref_net_mph <= config.CO2_REF_NET_MPH_MAX)
    return suspect, ref_net_mph


def append_series(day, hour, ref_kh, tank_kh, temp, is_flat, co2_suspect=False):
    """정상 측정 행을 series/latest 에 반영(에러 행은 호출하지 않음).
    tank_kh 는 음수(미평탄) 그대로 받아 |값|+is_flat 으로 분리(원본 series 규칙).
    ★날짜는 dkh.dat 과 같은 측정 시작일을 받는다 — 자정을 넘긴 회차에서 두 파일의 날짜가
    어긋나면 대시보드 날짜 컷과 도저 CO2 키 매칭이 서로 다른 창을 보게 된다."""
    row = {"hh": hour, "ref_kh": round(ref_kh, 3), "tank_kh": round(abs(tank_kh), 3),
           "temp": round(temp, 1), "is_flat": bool(is_flat), "date": day,
           "co2_suspect": bool(co2_suspect)}
    series = _read_json(SERIES_FILE, [])
    series.append(row)
    _write_json(SERIES_FILE, _cut_days(series, lambda r: r.get("date"))[-config.SERIES_MAX:])
    latest = dict(row)
    latest["count"] = dat_line_count()
    _write_json(LATEST_FILE, latest)


def plateau_count():
    try:
        n = 0
        with open(PLATEAU_JSONL) as f:
            for ln in f:
                if ln.strip():
                    n += 1
        return n
    except OSError:
        return 0


def last_plateau():
    """마지막 런 1건만 파싱 — 상태 표시용(전체를 올리지 않는다)."""
    try:
        last = None
        with open(PLATEAU_JSONL) as f:
            for ln in f:
                if ln.strip():
                    last = ln
        return json.loads(last) if last else {}
    except (OSError, ValueError):
        return {}


def _run_started_day(line):
    """plateau JSONL 한 줄에서 run_started 의 날짜(YYYY-MM-DD)만 뽑는다.

    ★json.loads 를 쓰지 않는 이유: 런 1줄이 수 KB(판독 100점×2phase)라 트림하려고 전체를
    객체로 올리면 힙이 위험하다. 필요한 건 앞부분 10글자뿐이므로 문자열로 찾는다."""
    k = line.find('"run_started"')
    if k < 0:
        return None
    q = line.find('"', line.find(":", k) + 1)
    return line[q + 1:q + 11] if q > 0 else None


def _trim_plateau():
    """최근 RETENTION_DAYS(14일)치 런만 남긴다 — 1차 날짜 컷, 2차 PLATEAU_MAX 행수 백스톱.

    ★회차 컷에서 날짜 컷으로 바꾼 이유는 dkh.dat 과 같다(원본 2026-08-16): 42런 컷은 하루
    3회 가정이라 추가 측정을 돌린 날이 있으면 14일 안쪽 궤적이 밀려나 잘렸다.
    한 줄씩 옮겨 힙 사용을 줄당 크기로 묶는다. 잘려나간 궤적은 사후 분석 표본이므로
    archive_sink(SD 있으면 storage 가 연결)로 흘려보낸다 — 원본 plateau_archive 상당."""
    try:
        days, total = [], 0
        with open(PLATEAU_JSONL) as f:
            for ln in f:
                if not ln.strip():
                    continue
                total += 1
                o = dkh_dat.day_ordinal(_run_started_day(ln) or "")
                if o is not None:
                    days.append(o)
        if not total:
            return
        cut = (max(days) - (config.RETENTION_DAYS - 1)) if days else None
        # 날짜 컷 통과분이 상한을 넘으면 오래된 것부터 추가로 버린다(힙 백스톱).
        kept_est = sum(1 for o in days if cut is None or o >= cut) if days else total
        skip_extra = max(0, kept_est - config.PLATEAU_MAX)

        tmp = PLATEAU_JSONL + ".tmp"
        dropped = 0
        with open(PLATEAU_JSONL) as src, open(tmp, "w") as dst:
            for ln in src:
                if not ln.strip():
                    continue
                o = dkh_dat.day_ordinal(_run_started_day(ln) or "")
                keep = (cut is None) or (o is not None and o >= cut)
                if keep and skip_extra > 0:
                    keep, skip_extra = False, skip_extra - 1
                if keep:
                    dst.write(ln if ln.endswith("\n") else ln + "\n")
                else:
                    dropped += 1
                    _archive(PLATEAU_JSONL, ln)
        if not dropped:
            os.remove(tmp)
            return
        os.rename(tmp, PLATEAU_JSONL)
    except OSError as e:
        log("[경고] plateau 이력 트림 실패: %r" % e)


def append_plateau(run_started, mode, completed, tank_readings, ref_readings,
                   tank_flat, ref_flat):
    """plateau 이력 1줄 추가 + CO2 판정. 반환: co2_suspect (series 행에 전달용)."""
    ref_flat_n = ref_readings[-1]["n"] if ref_flat and ref_readings else None
    co2_suspect, ref_net_mph = classify_co2_suspect(ref_readings, ref_flat_n)
    entry = {
        "run_started": run_started, "mode": mode, "completed": bool(completed),
        "tank": tank_readings, "ref": ref_readings,
        "tank_flat_n": (tank_readings[-1]["n"] if tank_flat and tank_readings else None),
        "ref_flat_n": ref_flat_n,
        "co2_suspect": co2_suspect,
        "ref_net_mph": ref_net_mph,
    }
    try:
        with open(PLATEAU_JSONL, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _trim_plateau()
    except OSError as e:
        log("[경고] plateau 이력 기록 실패: %r" % e)
    return co2_suspect
