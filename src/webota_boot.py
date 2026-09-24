# ★vendored: mpy-webota v0.5.2 (device/webota_boot.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota_boot — 부팅 때 배포를 적용하고, 새 판이 자리를 못 잡으면 되돌린다.
#
# boot.py 가 `webota_boot.apply()` 한 줄로 부른다. 앱 모듈을 하나도 import 하지 않는다 —
# 앱이 깨져 있어도 이 파일은 돌아야 하기 때문이다(MicroPython · CPython 양쪽에서 돈다).
#
# 상태 파일(모두 /webota 아래):
#   stage/files/<경로>   배포 트랜잭션이 올려 둔 새 파일
#   pending.json         커밋됨 — 다음 부팅에 적용할 것 {id, files:[경로], delete:[경로]}
#   prev/files/<경로>    적용 직전의 원래 파일(롤백용)  + prev/manifest.json
#   trial.json           새 판 시험 중 {id, boots} — 앱이 confirm_s 동안 살아 있으면 지운다
#   last.json            마지막 배포 결과 {id, label, result: ok|rolled_back, reason, at}
#   history.jsonl        배포 결과 이력(한 줄 = 한 배포, 최근 HISTORY_MAX 건)
#   modified.json        파일 API 로 손댄 코드 경로(데이터 제외) — 배포가 다시 덮으면 빠진다
#
# ★적용은 멱등이다: 적용 도중 전원이 나가면 다음 부팅에 pending 이 그대로 남아 있어 다시
#   돈다. 이미 옮겨진 파일(stage 에 없음)은 건너뛰고, 백업은 처음 한 번만 뜬다.
import json
import os
import time

ROOT = ""                 # 테스트가 임시 디렉토리로 바꾼다. 기기에서는 "" (= 파일시스템 루트)
DIR = "/webota"
MAX_BOOTS = 3             # 확인(confirm) 없이 이만큼 부팅하면 롤백 — WDT/행으로 반복 리셋되는 경우
HISTORY_MAX = 50


def p(path):
    """기기 경로 → 실제 경로(테스트 루트 반영)."""
    return ROOT + path


def exists(path):
    try:
        os.stat(p(path))
        return True
    except OSError:
        return False


def is_dir(path):
    try:
        return bool(os.stat(p(path))[0] & 0x4000)
    except OSError:
        return False


def makedirs(path):
    cur = ""
    for part in path.strip("/").split("/"):
        if not part:
            continue
        cur += "/" + part
        try:
            os.mkdir(p(cur))
        except OSError:
            pass


def parent(path):
    i = path.rstrip("/").rfind("/")
    return path[:i] if i > 0 else "/"


def rmtree(path):
    if is_dir(path):
        for name in os.listdir(p(path)):
            rmtree(path.rstrip("/") + "/" + name)
        try:
            os.rmdir(p(path))
        except OSError:
            pass
    elif exists(path):
        os.remove(p(path))


def move(src, dst):
    """src → dst 로 옮긴다(dst 가 있으면 대체). 상위 디렉토리는 만든다."""
    makedirs(parent(dst))
    try:
        os.rename(p(src), p(dst))
    except OSError:
        # 대상이 있으면 rename 이 실패하는 파일시스템이 있다 — 지우고 다시.
        try:
            os.remove(p(dst))
        except OSError:
            pass
        os.rename(p(src), p(dst))


def copy(src, dst):
    makedirs(parent(dst))
    with open(p(src), "rb") as fi, open(p(dst), "wb") as fo:
        while True:
            b = fi.read(2048)
            if not b:
                break
            fo.write(b)


def read_json(path, default=None):
    try:
        with open(p(path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    makedirs(parent(path))
    tmp = path + ".tmp"
    with open(p(tmp), "w") as f:
        json.dump(obj, f)
    move(tmp, path)


def remove(path):
    try:
        os.remove(p(path))
    except OSError:
        pass


def stamp():
    try:
        t = time.localtime()
        return "%04d-%02d-%02d %02d:%02d:%02d" % t[:6]
    except Exception:
        return ""


def _log(msg):
    print("[webota] " + msg)


def record(entry):
    """배포 결과를 last.json 에 쓰고 history.jsonl 에 한 줄 덧붙인다(최근 HISTORY_MAX 건)."""
    write_json(DIR + "/last.json", entry)
    lines = []
    try:
        with open(p(DIR + "/history.jsonl")) as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
    except OSError:
        pass
    lines = lines[-(HISTORY_MAX - 1):] + [json.dumps(entry)]
    tmp = DIR + "/history.jsonl.tmp"
    with open(p(tmp), "w") as f:
        f.write("\n".join(lines) + "\n")
    move(tmp, DIR + "/history.jsonl")


def history(n=10):
    try:
        with open(p(DIR + "/history.jsonl")) as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
    except OSError:
        return []
    out = []
    for ln in lines[-n:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


# ── 적용 · 롤백 · 확인 ──

def apply():
    """부팅 때 한 번. pending 이 있으면 적용, trial 중이면 부팅 횟수를 세고 넘치면 롤백.
    어떤 예외도 밖으로 내보내지 않는다 — 여기서 죽으면 부팅이 멈춘다."""
    try:
        if exists(DIR + "/pending.json"):
            _apply_pending()
        elif exists(DIR + "/trial.json"):
            t = read_json(DIR + "/trial.json", {}) or {}
            t["boots"] = int(t.get("boots", 0)) + 1
            if t["boots"] >= MAX_BOOTS:
                rollback("확인 없이 %d회 부팅(행·리셋 반복)" % t["boots"])
            else:
                write_json(DIR + "/trial.json", t)
                _log("시험 중 — 부팅 %d/%d" % (t["boots"], MAX_BOOTS))
    except Exception as e:
        _log("apply 오류: %r" % e)


def _apply_pending():
    pend = read_json(DIR + "/pending.json", {}) or {}
    files = pend.get("files") or []
    deletes = pend.get("delete") or []
    stage = DIR + "/stage/files"
    prev = DIR + "/prev/files"
    man = read_json(DIR + "/prev/manifest.json")
    if not man or man.get("id") != pend.get("id"):
        # 새 트랜잭션 — 지난 백업을 비운다(같은 id 면 적용 도중 재부팅이므로 이어 간다).
        rmtree(DIR + "/prev")
        man = {"id": pend.get("id"), "restore": [], "remove": []}
    for path in files + deletes:
        if path in man["restore"] or path in man["remove"]:
            continue                                  # 이미 백업했다(재개)
        if exists(path) and not is_dir(path):
            copy(path, prev + path)
            man["restore"].append(path)
        else:
            man["remove"].append(path)                # 새로 생기는 파일 — 롤백 때 지운다
        write_json(DIR + "/prev/manifest.json", man)
    for path in files:
        if exists(stage + path):
            move(stage + path, path)
    for path in deletes:
        if is_dir(path):
            rmtree(path)
        else:
            remove(path)
    mod = read_json(DIR + "/modified.json")
    if mod:                                   # 배포가 다시 덮은 파일은 더는 '수동 변경'이 아니다
        left = [x for x in mod.get("paths") or [] if x not in files and x not in deletes]
        if left:
            write_json(DIR + "/modified.json", {"paths": left, "at": mod.get("at")})
        else:
            remove(DIR + "/modified.json")
    write_json(DIR + "/trial.json", {"id": pend.get("id"), "label": pend.get("label"),
                                     "boots": 0, "at": stamp()})
    remove(DIR + "/pending.json")
    rmtree(DIR + "/stage")
    _log("배포 %s 적용 — 파일 %d개, 삭제 %d개 (시험 시작)"
         % (pend.get("id"), len(files), len(deletes)))


def rollback(reason):
    """prev 로 되돌린다. trial 을 지우고 결과를 last.json 에 남긴다."""
    man = read_json(DIR + "/prev/manifest.json", {}) or {}
    prev = DIR + "/prev/files"
    for path in man.get("restore", []):
        if exists(prev + path):
            copy(prev + path, path)
    for path in man.get("remove", []):
        if exists(path) and not is_dir(path):
            remove(path)
    t = read_json(DIR + "/trial.json", {}) or {}
    record({"id": t.get("id") or man.get("id"), "label": t.get("label"), "result": "rolled_back",
            "reason": reason, "at": stamp()})
    remove(DIR + "/trial.json")
    _log("롤백 — %s" % reason)


def in_trial():
    return exists(DIR + "/trial.json")


def confirm():
    """새 판이 자리를 잡았다 — trial 해제. 시험 중이 아니면 아무것도 안 한다."""
    t = read_json(DIR + "/trial.json")
    if t is None:
        return False
    record({"id": t.get("id"), "label": t.get("label"), "result": "ok", "at": stamp()})
    remove(DIR + "/trial.json")
    _log("배포 %s 확인 — 정상" % t.get("id"))
    return True
