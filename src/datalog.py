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
import rwtime

DAT_FILE = config.DATA_DIR + "/dkh.dat"
SERIES_FILE = config.DATA_DIR + "/dkh_series.json"
LATEST_FILE = config.DATA_DIR + "/dkh_latest.json"
# plateau 이력은 JSONL(런 1개 = 1줄) — 전체를 파이썬 객체로 올리지 않기 위해서다.
# 대시보드가 요청하는 dkh_plateau_history.json 배열은 webserver 가 줄 단위로 스트리밍 조립한다.
PLATEAU_JSONL = config.DATA_DIR + "/plateau.jsonl"
LOG_FILE = config.DATA_DIR + "/measure_kh.log"

_log_f = None


def log(msg):
    """print + measure_kh.log 기록(상한 초과 시 새로 시작) — 원본 setup_logging 대체."""
    global _log_f
    print(msg)
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


def _trim_dat():
    """dkh.dat 를 최근 DAT_MAX_ROWS(14일×3회=42행)로 유지 — ★모든 정보 14일 정책.
    도저 계산은 최근 21행(ROWS)만 보고, 에러 래치는 마지막 줄만 보므로 의미 불변."""
    try:
        with open(DAT_FILE) as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
        if len(lines) <= config.DAT_MAX_ROWS:
            return
        tmp = DAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines[-config.DAT_MAX_ROWS:]) + "\n")
        os.rename(tmp, DAT_FILE)
    except OSError as e:
        log("[경고] dkh.dat 트림 실패: %r" % e)


def log_kh(hour, ref_ph, tank_ph, ref_kh, tank_kh, temp):
    """dkh.dat 한 줄 추가 — 형식: HH ref_pH tank_pH ref_kh tank_kh temp (원본 동일).
    기록 후 14일 초과분을 앞에서 잘라낸다."""
    line = "%02d %.3f %.3f %.3f %.3f %.1f" % (hour, ref_ph, tank_ph, ref_kh, tank_kh, temp)
    with open(DAT_FILE, "a") as f:
        f.write(line + "\n")
    _trim_dat()
    log("[LOG] dkh.dat <- " + line)


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
    """마지막 줄이 에러 표식(값 전부 0)이면 True — 에러 래치(원본 동일: 프로브 보호)."""
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
    parts = last.split()
    if len(parts) < 6:
        return False
    try:
        return all(float(x) == 0.0 for x in parts[1:6])
    except ValueError:
        return False


def read_dat_lines():
    """도저 계산용 — 공백 분리 필드 리스트의 리스트."""
    try:
        with open(DAT_FILE) as f:
            return [ln.split() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


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


def append_series(hour, ref_kh, tank_kh, temp, is_flat, co2_suspect=False):
    """정상 측정 행을 series/latest 에 반영(에러 행은 호출하지 않음).
    tank_kh 는 음수(미평탄) 그대로 받아 |값|+is_flat 으로 분리(원본 series 규칙)."""
    row = {"hh": hour, "ref_kh": round(ref_kh, 3), "tank_kh": round(abs(tank_kh), 3),
           "temp": round(temp, 1), "is_flat": bool(is_flat), "date": rwtime.date_str(),
           "co2_suspect": bool(co2_suspect)}
    series = _read_json(SERIES_FILE, [])
    series.append(row)
    _write_json(SERIES_FILE, series[-config.SERIES_MAX:])
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


def _trim_plateau():
    """PLATEAU_MAX 초과분을 앞에서 잘라낸다 — 한 줄씩 옮겨 힙 사용을 줄당 크기로 묶는다."""
    n = plateau_count()
    if n <= config.PLATEAU_MAX:
        return
    skip = n - config.PLATEAU_MAX
    tmp = PLATEAU_JSONL + ".tmp"
    try:
        with open(PLATEAU_JSONL) as src, open(tmp, "w") as dst:
            i = 0
            for ln in src:
                if not ln.strip():
                    continue
                i += 1
                if i > skip:
                    dst.write(ln if ln.endswith("\n") else ln + "\n")
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
