# 장기 저장소 — 내장 플래시(16MB) 아카이브 + 설정 스냅샷 + 백업/복원 번들.
#
# ★왜 USB 저장소가 아닌가(2026-08-19 조사 결론, 재조사 불요):
#   S3 는 네이티브 USB 를 갖고 있지만 **스톡 MicroPython 으로는 저장소로 쓸 수 없다**.
#   - USB MSC 디바이스(기기를 PC 에 USB 드라이브로 보이게): esp32 포트 미구현
#     (micropython#8426, discussions#11282). STM32 포트에만 있다.
#   - USB 호스트(USB 메모리를 기기에 꽂아 마운트): MicroPython 전체에 아직 없다
#     (discussions#15477 — 공통 호스트 API 설계가 TODO).
#   둘 다 ESP-IDF/TinyUSB C 코드로 커스텀 펌웨어를 빌드해야 하는데, 그건 "공식 배포본을
#   수정 없이 쓴다"는 전제를 깨고 펌웨어 유지보수를 떠안는 일이라 하지 않는다.
#
# 그래서 SD 가 하던 일(무기한 보관·설정 백업)을 아래 3층으로 대체한다:
#   ① 이 파일 — 플래시 `/data/archive` 에 14일 창 밖 기록·설정 스냅샷을 쌓는다.
#      16MB 중 펌웨어·앱·자산을 빼고 10MB 이상 남으므로 SD 없이도 몇 년치가 들어간다.
#   ② LAN — `GET /api/backup`(설정 번들) / `GET /api/files`+`/data/...`(파일 다운로드).
#      PC·NAS 에서 주기 실행하면 무인 장기 보관이 된다(tools/backup.py).
#   ③ USB(Type-C) — USB CDC 경유 `mpremote fs cp -r :/data ./backup`.
#      "USB 로 뽑는다"는 목적은 이 경로로 달성한다(저장소로 쓰는 것과 결과는 같다).
#
# 원칙: **아카이브 실패는 측정을 막지 않는다.** 모든 진입점이 예외를 삼키고, 공간이
# 부족하면 오래된 아카이브부터 스스로 줄인다(측정 데이터 본체는 절대 건드리지 않는다).
import json
import os

import config

log = print          # datalog 가 파일 로거로 교체(import 시점에 연결)

# 아카이브 파일명 — 창 밖으로 밀려난 원본 파일명에 대응시킨다.
_MAP = {"dkh.dat": "dkh.dat", "plateau.jsonl": "plateau.jsonl"}
CONFIG_SNAP = "config-snapshots.jsonl"
# 백업/복원이 다루는 설정 파일 — 대시보드·정비페이지가 쓰는 것들.
# ★devices.json(장치 목록·BIND 주소) 도 설정이다 — 기기를 새로 굽고 복원할 때 이게 없으면
#   장비에 붙지 못해 아무것도 못 한다(가장 먼저 필요한 값).
# ★bt.json 은 구형식(2026-08-19)이지만 목록에 **남긴다** — 옛 백업을 복원하면 devices 가
#   그걸 읽어 새 형식으로 넘어간다(devices._legacy).
# ★schedule.json(측정 회차, 2026-08-21) 도 복원 대상이다 — 회차가 기본값으로 돌아가면
#   측정 시각이 조용히 바뀐다.
CONFIG_FILES = ("doser_config.json", "doser_override.json", "ph_cal.json",
                "devices.json", "schedule.json", "bt.json")


def _dir():
    return config.ARCHIVE_DIR


def path(name):
    return _dir() + "/" + name


def _data(name):
    return config.DATA_DIR + "/" + name


def _size(p):
    try:
        return os.stat(p)[6]
    except OSError:
        return 0


def ensure():
    """아카이브 디렉토리 확보 — 실패하면 False(호출부는 그냥 진행한다)."""
    if not config.ARCHIVE_ENABLED:
        return False
    for d in (config.DATA_DIR, _dir()):
        try:
            os.mkdir(d)
        except OSError:
            pass                    # 이미 있음 또는 만들 수 없음 — 아래에서 판정
    try:
        os.listdir(_dir())
        return True
    except OSError as e:
        log("[archive] 디렉토리 사용 불가: %r — 아카이브 없이 진행" % e)
        return False


def free_bytes():
    """파일시스템 여유 바이트 — 구할 수 없으면 None(구형 포트·CPython 호환)."""
    try:
        st = os.statvfs(config.DATA_DIR)
        return st[0] * st[3]        # f_bsize * f_bavail
    except (OSError, AttributeError):
        return None


def _trim_head(p, keep_bytes):
    """파일 앞쪽을 잘라 keep_bytes 이하로 만든다 — 줄 경계를 지키고 최신 기록을 남긴다.
    한 줄씩 옮기므로 힙 사용이 줄 크기로 묶인다(원본 파일 전체를 메모리에 올리지 않는다)."""
    size = _size(p)
    if size <= keep_bytes:
        return 0
    skip = size - keep_bytes
    tmp = p + ".tmp"
    dropped = 0
    try:
        with open(p) as src, open(tmp, "w") as dst:
            moved = 0
            for ln in src:
                if moved < skip:
                    moved += len(ln)
                    dropped += 1
                    continue
                dst.write(ln)
        os.rename(tmp, p)
    except OSError as e:
        log("[archive] 트림 실패(%s): %r" % (p, e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0
    return dropped


def guard():
    """용량 백스톱 — ①파일별 상한 ②플래시 여유 하한. 측정 데이터는 손대지 않는다.
    ★아카이브는 '있으면 좋은' 것이므로, 공간이 빠듯하면 아카이브가 먼저 줄어든다."""
    if not config.ARCHIVE_ENABLED:
        return
    cap = config.ARCHIVE_MAX_KB * 1024
    for name in list(_MAP.values()) + [CONFIG_SNAP]:
        n = _trim_head(path(name), cap)
        if n:
            log("[archive] %s 상한(%dKB) 초과 — 오래된 %d줄 정리"
                % (name, config.ARCHIVE_MAX_KB, n))
    free = free_bytes()
    if free is None or free >= config.ARCHIVE_MIN_FREE_KB * 1024:
        return
    log("[archive] ★플래시 여유 %dKB — 하한(%dKB) 미달, 아카이브를 절반으로 줄인다"
        % (free // 1024, config.ARCHIVE_MIN_FREE_KB))
    for name in list(_MAP.values()) + [CONFIG_SNAP]:
        half = _size(path(name)) // 2
        if half:
            _trim_head(path(name), half)


def store(src_path, line):
    """14일 창 밖으로 밀려난 줄을 무기한 보관 — datalog 의 트림이 호출한다.
    보관에 실패해도 원본 트림은 이미 끝났으므로 측정에는 영향이 없다."""
    if not config.ARCHIVE_ENABLED:
        return False
    base = src_path.rsplit("/", 1)[-1]
    name = _MAP.get(base)
    if not name:
        return False
    if not line.endswith("\n"):
        line += "\n"
    try:
        with open(path(name), "a") as f:
            f.write(line)
        return True
    except OSError as e:
        log("[archive] 보관 실패(%s): %r" % (name, e))
        return False


def _read_json(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def bundle():
    """백업 번들 — 설정 3종 + dkh.dat 본문 + 메타. `GET /api/backup` 이 그대로 돌려준다.
    ★dkh.dat 은 14일치(≈2KB)라 본문을 통째로 담아도 안전하다. plateau·아카이브처럼
    큰 파일은 넣지 않는다 — 그건 파일 다운로드(`/api/files` → `/data/...`)로 받는다."""
    out = {"kind": "reefwiz-backup", "v": 1, "config": {}}
    for name in CONFIG_FILES:
        out["config"][name] = _read_json(_data(name))
    try:
        with open(_data("dkh.dat")) as f:
            out["dkh_dat"] = f.read()
    except OSError:
        out["dkh_dat"] = ""
    out["latest"] = _read_json(_data("dkh_latest.json"), {})
    out["archive"] = status()
    return out


def restore(obj):
    """백업 번들에서 **설정만** 되돌린다 — (ok, 메시지) 반환.

    ★측정 데이터(dkh.dat·plateau)는 복원하지 않는다. 되돌리면 도저 계산이 과거 수준·추세로
      뛰어 실제 도징량이 튀기 때문이다. 데이터 이관은 사람이 파일로 올린다(README 참조).
    ★값 검증을 통과한 파일만 쓴다 — 잘못된 백업으로 목표 dKH·도징량이 범위를 벗어나면
      안전 레일보다 앞단에서 막는다."""
    if not isinstance(obj, dict) or obj.get("kind") != "reefwiz-backup":
        return False, "백업 형식이 아니다(kind=reefwiz-backup 아님)"
    cfg = obj.get("config") or {}
    written, skipped = [], []
    for name in CONFIG_FILES:
        val = cfg.get(name)
        if not isinstance(val, dict):
            skipped.append(name + "(없음)")
            continue
        ok, why = _validate(name, val)
        if not ok:
            skipped.append("%s(%s)" % (name, why))
            continue
        try:
            snapshot("restore:" + name)      # 되돌리기 전 현재 값을 스냅샷에 남긴다
            tmp = _data(name) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(val, f)
            os.rename(tmp, _data(name))
            written.append(name)
        except OSError as e:
            skipped.append("%s(쓰기 실패 %r)" % (name, e))
    if not written:
        return False, "복원한 항목 없음 — " + (", ".join(skipped) or "빈 번들")
    msg = "복원: " + ", ".join(written)
    if skipped:
        msg += " / 건너뜀: " + ", ".join(skipped)
    log("[archive] " + msg)
    return True, msg


def _validate(name, val):
    """복원 값 검증 — 범위는 config 의 운용 한계를 그대로 쓴다."""
    try:
        if name == "doser_config.json":
            t = val.get("target_dkh")
            if t is None:
                return True, ""
            t = float(t)
            if not (config.TARGET_LO <= t <= config.TARGET_HI):
                return False, "target_dkh %.2f 범위 밖" % t
        elif name == "bt.json":
            # 형식이 깨진 주소를 복원하면 엉뚱한 상대에 붙으려다 실패한다.
            # ★저장된 형태(AT+BIND 'nnnn,nn,nnnnnn')만 검사한다 — link 를 import 하지 않는
            #   이유는 그쪽이 machine(UART/Pin)을 끌고 오기 때문이다. 이 모듈은 PC 백업
            #   도구·테스트에서도 돌아야 한다(입력 정규화는 저장 시점에 link 가 한다).
            for k, v in val.items():
                parts = str(v).split(",")
                hexs = "".join(parts)
                if len(parts) != 3 or len(parts[0]) != 4 or len(parts[1]) != 2 \
                        or len(parts[2]) != 6 or len(hexs) != 12 \
                        or any(c not in "0123456789abcdefABCDEF" for c in hexs):
                    return False, "%s 주소 형식 오류(%r)" % (k, v)
        elif name == "devices.json":
            # ★깨진 장치 목록을 되돌리면 장비에 못 붙는다 — 검증은 devices 가 한다
            #   (그 모듈은 config 만 의존해서 PC 도구·테스트에서도 import 된다).
            import devices
            return devices.validate(val)
        elif name == "schedule.json":
            import schedule
            return schedule.validate(val)
        elif name == "doser_override.json":
            ml = val.get("ml_day")
            if ml is None:
                return True, ""
            ml = float(ml)
            # 0 = 도징 정지(원본 규약). 그 외는 대시보드와 같은 상한/하한.
            if ml != 0 and not (1.5 <= ml <= 18.0):
                return False, "ml_day %.2f 범위 밖" % ml
    except (TypeError, ValueError):
        return False, "숫자 아님"
    return True, ""


def snapshot(reason=""):
    """설정 스냅샷 1줄 추가 — 직전 스냅샷과 내용이 같으면 쓰지 않는다(무한 증식 방지).
    ★목적은 "언제 무엇을 바꿨나"의 이력이다. 값이 튀었을 때 되돌릴 근거가 된다."""
    if not config.ARCHIVE_ENABLED:
        return False
    cur = {}
    for name in CONFIG_FILES:
        cur[name] = _read_json(_data(name))
    body = json.dumps(cur)
    p = path(CONFIG_SNAP)
    try:
        last = None
        with open(p) as f:
            for ln in f:                      # 마지막 줄만 남긴다(힙 절약)
                if ln.strip():
                    last = ln
        if last:
            prev = json.loads(last)
            if json.dumps(prev.get("config")) == body:
                return False
    except (OSError, ValueError):
        pass
    try:
        import rwtime
        stamp = rwtime.stamp()
    except Exception:
        stamp = ""
    try:
        with open(p, "a") as f:
            f.write(json.dumps({"at": stamp, "why": reason, "config": cur}) + "\n")
        return True
    except OSError as e:
        log("[archive] 스냅샷 실패: %r" % e)
        return False


def status():
    """정비페이지 표시용 — 아카이브 파일 크기와 플래시 여유."""
    files = []
    total = 0
    try:
        for name in os.listdir(_dir()):
            n = _size(path(name))
            total += n
            files.append({"name": name, "bytes": n})
    except OSError:
        pass
    free = free_bytes()
    return {
        "enabled": config.ARCHIVE_ENABLED,
        "dir": _dir(),
        "files": files,
        "bytes": total,
        "free_kb": (free // 1024) if free is not None else None,
        "min_free_kb": config.ARCHIVE_MIN_FREE_KB,
    }
