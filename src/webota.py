# ★vendored: mpy-webota v0.5.2 (device/webota.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota — MicroPython 앱을 위한 웹 API OTA · 원격 파일 관리 서버.
#
# 앱과 **별도 포트·별도 스레드**로 돈다(기본 :8266). 부팅 런처(main.py)가 앱보다 먼저 띄우므로
# 앱이 import 오류로 죽어도 이 서버는 살아 있어 원격으로 고칠 수 있다. 앱 모듈을 하나도
# import 하지 않는다 — 어느 프로젝트에나 그대로 붙인다.
#
# 모든 요청은 `X-Token` 헤더가 설정(/webota.json 의 token)과 같아야 한다. 토큰이 설정돼 있지
# 않으면 **모든 요청을 거부**한다(안전 기본값).
#
#   GET    /status                      가동 시간·메모리·FS·앱 상태·마지막 배포 결과
#   GET    /history[?n=10]              배포 결과 이력(라벨·ok|rolled_back·사유·시각)
#   GET    /fs/<경로>[?r=1&sha=1]        파일 내용 / 디렉토리 목록(JSON)
#   PUT    /fs/<경로>[?sha=<hex>]        파일 쓰기(임시 파일 → 해시 확인 → 교체)
#   DELETE /fs/<경로>[?r=1]              파일 · 디렉토리(r=1 재귀) 삭제
#   POST   /fs/<경로>?op=mkdir           디렉토리 생성(상위 포함)
#   POST   /fs/<경로>?op=mv&to=<경로>    이동 · 이름 변경
#   POST   /sha        {"paths":[...]}   경로별 SHA256(없으면 null) — 바뀐 파일만 올리기용
#   POST   /deploy/begin                 배포 트랜잭션 시작 → {"id"}
#   PUT    /deploy/<id>/<경로>?sha=<hex> 새 파일 스테이징
#   POST   /deploy/<id>/commit           {"files":[{path,sha}], "delete":[...], "reset":true,
#                                         "label":"v1.2.0+abc1234"}   ← 라벨은 이력에 남는다
#   DELETE /deploy                       진행 중 트랜잭션 폐기
#   POST   /reset
#   GET    /                            설치 화면(webota_ui.html — 토큰은 화면에서 입력, 이 페이지만 무인증)
#   GET    /pkg/sources                 패키지 출처(저장소) 목록 — 첫 항목이 기본
#   POST   /pkg/sources {"add":"<URL|owner/repo>"} | {"remove":"<키>"} | {"default":"<키>"}
#   GET    /pkg/list[?src=<키>&fresh=1] 그 출처의 배포 패키지 목록(각 항목에 app_id)
#   POST   /pkg/install {"url","src","force","switch_app"}
#                                        패키지를 기기가 직접 내려받아 검증 → 배포(커밋·리셋).
#                                        다른 앱이면 409 {code:"app_mismatch"} — switch_app 으로
#                                        '앱 교체'(새 /webota.json 도 같은 트랜잭션 → 롤백되면 복원)
#   (commit · reset 은 앱이 set_guard() 로 등록한 가드를 거친다 — ?force=1 로 무시)
#
# 경로 제한은 없다: boot.py · main.py · 데이터 파일까지 전부 다룬다(설계 결정).
import json
import os
import sys
import time

import webota_boot as wb

VERSION = "0.5.2"
CONFIG = "/webota.json"
DEFAULTS = {"port": 8266, "app": "app", "entry": "main", "wifi_file": None,
            "wifi_keys": ["ssid", "pass"], "wifi_timeout_s": 20, "confirm_s": 90,
            "token": None, "app_id": None, "packages": None, "sources": None, "ui": "/webota_ui.html",
            "data_dirs": ["/data"]}
CHUNK = 2048
MAX_JSON = 64 * 1024

cfg = dict(DEFAULTS)
app_state = "boot"          # boot → starting → running → exited | rescue | stopped
app_error = ""
reset_hook = None           # 테스트가 machine.reset 대신 부를 함수를 넣는다
_guard = None
_deploy_id = None
_reset_pending = False
_trial = False
_stop = False               # 테스트용 — 서버 루프 종료
_idle_forever = True        # 테스트용 — idle() 이 바로 돌아오게
_t0 = None


# ── 공용 ──

def _ticks():
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def uptime_s():
    if _t0 is None:
        return 0
    try:
        return time.ticks_diff(_ticks(), _t0) // 1000
    except AttributeError:
        return (_ticks() - _t0) // 1000


def _hex(b):
    import binascii
    return binascii.hexlify(b).decode()


def _sha():
    import hashlib
    return hashlib.sha256()


def sha_file(path):
    """기기 경로 파일의 SHA256(hex). 없거나 디렉토리면 None."""
    if not wb.exists(path) or wb.is_dir(path):
        return None
    h = _sha()
    with open(wb.p(path), "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return _hex(h.digest())


def load_config(path=CONFIG):
    global cfg
    c = dict(DEFAULTS)
    c.update(wb.read_json(path, {}) or {})
    cfg = c
    return c


def set_guard(fn):
    """앱이 부른다: fn() → (ok, 메시지). commit · reset 전에 확인한다(측정 중 배포 금지 등)."""
    global _guard
    _guard = fn


def _check_guard(force):
    if force or _guard is None:
        return True, ""
    try:
        r = _guard()
        return bool(r[0]), (r[1] if len(r) > 1 else "")
    except Exception as e:
        return False, "가드 오류: %r" % e


def _unquote(s):
    if "%" not in s:
        return s
    out = bytearray()
    b = s.encode()
    i = 0
    while i < len(b):
        if b[i] == 37 and i + 2 < len(b):             # '%XX'
            out.append(int(b[i + 1:i + 3].decode(), 16))
            i += 3
        else:
            out.append(b[i])
            i += 1
    return out.decode()


def _qs(s):
    q = {}
    for kv in s.split("&"):
        if kv:
            k, _, v = kv.partition("=")
            q[_unquote(k)] = _unquote(v)
    return q


def _norm(path):
    """URL 경로 조각 → 기기 절대 경로. '..' 조각은 뜻이 없으므로 거부(None)."""
    path = "/" + _unquote(path).strip("/")
    for part in path.split("/"):
        if part == "..":
            return None
    return path


def _fmt_exc(e):
    try:
        import io
        buf = io.StringIO()
        sys.print_exception(e, buf)          # MicroPython
        return buf.getvalue()
    except AttributeError:
        import traceback
        return "".join(traceback.format_exception(type(e), e, e.__traceback__))


# ── HTTP ──

def _send(conn, status, body=b"", ctype="application/json", length=None):
    if isinstance(body, str):
        body = body.encode()
    n = len(body) if length is None else length
    head = ("HTTP/1.0 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
            "Connection: close\r\n\r\n" % (status, ctype, n))
    _sendall(conn, head.encode())
    if body:
        _sendall(conn, body)


def _sendall(conn, b):
    try:
        conn.sendall(b)
    except AttributeError:
        conn.write(b)


def _json(conn, obj, status="200 OK"):
    _send(conn, status, json.dumps(obj))


def _err(conn, status, msg):
    _json(conn, {"ok": False, "err": msg}, status)


def _read_body(rf, n, limit=MAX_JSON):
    if n > limit:
        return None
    buf = b""
    while len(buf) < n:
        b = rf.read(n - len(buf))
        if not b:
            break
        buf += b
    return buf


def _read_json_body(rf, n):
    raw = _read_body(rf, n)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _recv_file(rf, n, dst, want_sha):
    """본문 n 바이트를 dst 로 받는다: dst.part 에 쓰며 해시 → 대조 → 교체. (ok, 메시지, sha)."""
    wb.makedirs(wb.parent(dst))
    tmp = dst + ".part"
    h = _sha()
    left = n
    with open(wb.p(tmp), "wb") as f:
        while left > 0:
            b = rf.read(min(CHUNK, left))
            if not b:
                break
            f.write(b)
            h.update(b)
            left -= len(b)
    got = _hex(h.digest())
    if left:
        wb.remove(tmp)
        return False, "본문이 %d바이트 모자란다(연결 끊김)" % left, got
    if want_sha and want_sha.lower() != got:
        wb.remove(tmp)
        return False, "SHA256 불일치 — 받은 %s" % got, got
    wb.move(tmp, dst)
    return True, "", got


def _listing(path, recursive, with_sha):
    out = []
    try:
        names = sorted(os.listdir(wb.p(path)))
    except OSError:
        return out
    for name in names:
        fp = path.rstrip("/") + "/" + name
        try:
            st = os.stat(wb.p(fp))
        except OSError:
            continue
        if st[0] & 0x4000:
            out.append({"path": fp, "type": "d"})
            if recursive:
                out.extend(_listing(fp, True, with_sha))
        else:
            e = {"path": fp, "type": "f", "size": st[6]}
            if with_sha:
                e["sha"] = sha_file(fp)
            out.append(e)
    return out


def status():
    st = {"webota": VERSION, "uptime_s": uptime_s(), "app": {"name": cfg.get("app"),
          "state": app_state, "error": app_error[-1500:]},
          "trial": wb.read_json(wb.DIR + "/trial.json"),
          "pending": wb.exists(wb.DIR + "/pending.json"),
          "last": wb.read_json(wb.DIR + "/last.json"),
          "deploy_id": _deploy_id, "confirm_s": cfg.get("confirm_s"),
          "app_id": cfg.get("app_id"), "current": _current(),
          "modified": wb.read_json(wb.DIR + "/modified.json")}
    try:
        import gc
        st["mem_free"] = gc.mem_free()
    except AttributeError:
        pass
    try:
        v = os.statvfs(wb.p("/") or "/")
        st["fs"] = {"total": v[0] * v[2], "free": v[0] * v[3]}
    except (AttributeError, OSError):
        pass
    return st


def _fs(conn, method, path, q, rf, clen):
    if method == "GET":
        if wb.is_dir(path):
            return _json(conn, {"path": path, "entries": _listing(
                path, q.get("r") == "1", q.get("sha") == "1")})
        if not wb.exists(path):
            return _err(conn, "404 Not Found", "없음: " + path)
        size = os.stat(wb.p(path))[6]
        _send(conn, "200 OK", b"", "application/octet-stream", size)
        with open(wb.p(path), "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                _sendall(conn, b)
        return
    if method == "PUT":
        if wb.is_dir(path):
            return _err(conn, "409 Conflict", "디렉토리다: " + path)
        ok, msg, got = _recv_file(rf, clen, path, q.get("sha"))
        if ok:
            _mark_modified(path)
        return _json(conn, {"ok": ok, "err": msg, "path": path, "sha": got},
                     "200 OK" if ok else "400 Bad Request")
    if method == "DELETE":
        if not wb.exists(path):
            return _err(conn, "404 Not Found", "없음: " + path)
        if wb.is_dir(path):
            if q.get("r") == "1":
                wb.rmtree(path)
            else:
                try:
                    os.rmdir(wb.p(path))
                except OSError:
                    return _err(conn, "409 Conflict", "비어 있지 않다(r=1 로 재귀 삭제): " + path)
        else:
            os.remove(wb.p(path))
        _mark_modified(path)
        return _json(conn, {"ok": True, "path": path})
    if method == "POST":
        op = q.get("op")
        if op == "mkdir":
            wb.makedirs(path)
            return _json(conn, {"ok": True, "path": path})
        if op == "mv":
            to = _norm(q.get("to", ""))
            if not to or to == "/":
                return _err(conn, "400 Bad Request", "to 가 필요하다")
            if not wb.exists(path):
                return _err(conn, "404 Not Found", "없음: " + path)
            wb.move(path, to)
            _mark_modified(path)
            _mark_modified(to)
            return _json(conn, {"ok": True, "path": to})
        return _err(conn, "400 Bad Request", "op 는 mkdir | mv")
    return _err(conn, "405 Method Not Allowed", method)


def _commit(did, paths, deletes, label, reset, force):
    """스테이징된 배포를 확정한다 — pending 기록 후(reset 이면) 리셋 예약. (ok, 상태, 메시지)."""
    global _reset_pending
    if not paths and not deletes:
        return False, "400 Bad Request", "바꿀 것이 없다"
    ok, msg = _check_guard(force)
    if not ok:
        return False, "423 Locked", msg or "앱 가드가 거부"
    wb.write_json(wb.DIR + "/pending.json", {"id": did, "label": label, "files": paths,
                                             "delete": deletes, "at": wb.stamp()})
    if reset:
        _reset_pending = True
    return True, "200 OK", ""


def _mark_modified(path):
    """파일 API 로 **코드**를 손댔다 — 기기가 더는 '현재 판' 그대로가 아니다. 데이터 디렉토리
    (data_dirs)와 /webota 는 운영 중 늘 바뀌므로 세지 않는다. 배포·패키지 설치가 그 파일을
    다시 덮으면 부팅 적용 때 목록에서 빠진다(webota_boot)."""
    for d in (cfg.get("data_dirs") or []) + [wb.DIR]:
        d = d.rstrip("/")
        if path == d or path.startswith(d + "/"):
            return
    m = wb.read_json(wb.DIR + "/modified.json", {}) or {}
    paths = m.get("paths") or []
    if path not in paths:
        paths.append(path)
    wb.write_json(wb.DIR + "/modified.json", {"paths": paths[-200:], "at": wb.stamp()})


def _save_config(c):
    """설정 파일을 고쳐 쓴다 — 파일에 있던 키(토큰 등)는 그대로 두고 바뀐 키만."""
    on_disk = wb.read_json(CONFIG, {}) or {}
    for k in ("sources", "app_id", "app", "entry"):
        if c.get(k) is not None:
            on_disk[k] = c[k]
    if c.get("sources") is not None:
        on_disk.pop("packages", None)              # 옛 한 개짜리 출처는 sources 로 옮겨 갔다
    wb.write_json(CONFIG, on_disk)


def _find_source(pkg, key):
    for src in pkg.sources(cfg):
        if pkg.source_key(src) == key:
            return src
    return None


def _current():
    """지금 기기에 올라가 있는 판의 라벨 — 시험 중이면 그 판, 아니면 마지막 확인된 판."""
    t = wb.read_json(wb.DIR + "/trial.json")
    if t:
        return t.get("label")
    for e in reversed(wb.history(50)):
        if e.get("result") == "ok":
            return e.get("label")
    return None


def _pkg(conn, method, rest, q, rf, clen):
    global _deploy_id
    import webota_pkg as pkg
    if rest == "sources":
        if method == "POST":
            body = _read_json_body(rf, clen) or {}
            lst = pkg.sources(cfg)
            if body.get("add"):
                src = pkg.parse_source(body["add"])
                if not src:
                    return _err(conn, "400 Bad Request",
                                "저장소를 알아볼 수 없다 — https://github.com/owner/repo 또는 owner/repo")
                if not _find_source(pkg, pkg.source_key(src)):
                    lst.append(src)
            elif body.get("remove"):
                lst = [x for x in lst if pkg.source_key(x) != body["remove"]]
            elif body.get("default"):
                hit = [x for x in lst if pkg.source_key(x) == body["default"]]
                lst = hit + [x for x in lst if pkg.source_key(x) != body["default"]]
            cfg["sources"] = lst
            _save_config(cfg)
        return _json(conn, {"ok": True, "sources": [pkg.source_key(x) for x in pkg.sources(cfg)]})
    if rest == "list" and method == "GET":
        lst_src = pkg.sources(cfg)
        src = _find_source(pkg, q["src"]) if q.get("src") else (lst_src[0] if lst_src else None)
        if src is None:
            return _json(conn, {"ok": False, "err": "패키지 출처가 없다 — 저장소를 더한다", "packages": [],
                                "current": _current(), "app_id": cfg.get("app_id"), "src": None})
        lst, err = pkg.list_packages(src, now=uptime_s() if q.get("fresh") != "1" else None)
        return _json(conn, {"ok": err is None, "err": err, "packages": lst or [], "src": pkg.source_key(src),
                            "current": _current(), "app_id": cfg.get("app_id")})
    if rest == "install" and method == "POST":
        body = _read_json_body(rf, clen)
        if not body or not body.get("url"):
            return _err(conn, "400 Bad Request", "url 이 필요하다")
        force = bool(body.get("force")) or q.get("force") == "1"
        switch = bool(body.get("switch_app"))
        ok, msg = _check_guard(force)                  # 내려받기 전에 먼저 거른다(헛수고 방지)
        if not ok:
            return _err(conn, "423 Locked", msg or "앱 가드가 거부")
        stage = wb.DIR + "/stage/files"
        wb.rmtree(wb.DIR + "/stage")
        wb.makedirs(stage)
        did = "%d-%d" % (time.time(), _ticks() % 100000)
        _deploy_id = None                              # 진행 중이던 수동 배포는 무효
        try:
            ok, msg, man, changed = pkg.install(body["url"], None if switch else cfg.get("app_id"),
                                                stage, sha_file)
        except Exception as e:
            ok, msg, man, changed = False, "내려받기 실패: %r" % e, None, []
        if not ok:
            wb.rmtree(wb.DIR + "/stage")
            if man and cfg.get("app_id") and man.get("app_id") != cfg.get("app_id"):
                return _json(conn, {"ok": False, "code": "app_mismatch", "err": msg,
                                    "app_id": cfg.get("app_id"), "pkg_app_id": man.get("app_id"),
                                    "label": man.get("label")}, "409 Conflict")
            return _err(conn, "400 Bad Request", msg)
        if switch and man.get("app_id") != cfg.get("app_id"):
            # ★앱 교체 — 새 앱도 webota 를 싣고 있어야 교체 뒤에도 원격이 산다.
            paths = [f["path"] for f in man.get("files") or []]
            missing = [x for x in ("/webota.py", "/webota_boot.py", "/main.py", "/boot.py")
                       if x not in paths]
            if missing and not force:
                wb.rmtree(wb.DIR + "/stage")
                return _err(conn, "400 Bad Request",
                            "이 패키지에는 webota 가 없다(%s) — 교체하면 원격 배포·설치 화면이 사라진다"
                            " (그래도 하려면 force)" % ", ".join(missing))
            # 새 설정을 **같은 트랜잭션**으로 — 새 앱이 자리를 못 잡아 롤백되면 설정도 돌아온다.
            new = wb.read_json(CONFIG, {}) or {}
            new["app_id"] = man.get("app_id")
            new["app"] = man.get("app") or "app"
            new["entry"] = man.get("entry") or "main"
            key = body.get("src")
            lst = pkg.sources(cfg)
            if key:
                lst = [x for x in lst if pkg.source_key(x) == key] + \
                      [x for x in lst if pkg.source_key(x) != key]
            new["sources"] = lst
            new.pop("packages", None)
            wb.write_json(stage + CONFIG, new)
            changed.append(CONFIG)
        deletes = [d for d in (man.get("delete") or []) if _norm(d) and wb.exists(d)]
        label = man.get("label") or man.get("version")
        if not changed and not deletes:
            wb.rmtree(wb.DIR + "/stage")
            return _json(conn, {"ok": True, "result": "unchanged", "label": label})
        ok, st, msg = _commit(did, changed, deletes, label, True, force)
        if not ok:
            wb.rmtree(wb.DIR + "/stage")
            return _err(conn, st, msg)
        return _json(conn, {"ok": True, "result": "committed", "id": did, "label": label,
                            "files": len(changed), "delete": len(deletes)})
    return _err(conn, "404 Not Found", "pkg/" + rest)


def _ui(conn):
    path = cfg.get("ui") or "/webota_ui.html"
    if not wb.exists(path):
        return _send(conn, "200 OK", "<p>webota %s — 설치 화면 파일이 없다(%s)</p>"
                     % (VERSION, path), "text/html; charset=utf-8")
    size = os.stat(wb.p(path))[6]
    _send(conn, "200 OK", b"", "text/html; charset=utf-8", size)
    with open(wb.p(path), "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            _sendall(conn, b)


def _deploy(conn, method, rest, q, rf, clen):
    global _deploy_id, _reset_pending
    stage = wb.DIR + "/stage/files"
    if rest == "begin" and method == "POST":
        wb.rmtree(wb.DIR + "/stage")
        wb.makedirs(stage)
        _deploy_id = "%d-%d" % (time.time(), _ticks() % 100000)
        return _json(conn, {"ok": True, "id": _deploy_id})
    if rest == "" and method == "DELETE":
        wb.rmtree(wb.DIR + "/stage")
        _deploy_id = None
        return _json(conn, {"ok": True})
    did, _, sub = rest.partition("/")
    if did != _deploy_id or _deploy_id is None:
        return _err(conn, "409 Conflict", "진행 중인 배포가 아니다(begin 부터): " + did)
    if sub == "commit" and method == "POST":
        body = _read_json_body(rf, clen)
        if body is None:
            return _err(conn, "400 Bad Request", "JSON 본문 파싱 실패")
        files = body.get("files") or []
        deletes = [d for d in (body.get("delete") or []) if _norm(d)]
        paths = []
        for f in files:
            path = _norm(f.get("path", ""))
            if not path or path == "/":
                return _err(conn, "400 Bad Request", "잘못된 경로: %r" % f.get("path"))
            if sha_file(stage + path) != (f.get("sha") or "").lower():
                return _err(conn, "409 Conflict", "스테이징 파일이 없거나 해시가 다르다: " + path)
            paths.append(path)
        label = body.get("label")
        reset = body.get("reset", True)
        ok, st, msg = _commit(_deploy_id, paths, deletes, label, reset, q.get("force") == "1")
        if not ok:
            return _err(conn, st, msg)
        _json(conn, {"ok": True, "id": _deploy_id, "label": label, "files": len(paths),
                     "delete": len(deletes), "reset": bool(reset)})
        _deploy_id = None
        return
    if method == "PUT" and sub:
        path = _norm(sub)
        if not path or path == "/":
            return _err(conn, "400 Bad Request", "잘못된 경로")
        if not q.get("sha"):
            return _err(conn, "400 Bad Request", "스테이징에는 sha 가 필요하다")
        ok, msg, got = _recv_file(rf, clen, stage + path, q.get("sha"))
        return _json(conn, {"ok": ok, "err": msg, "path": path, "sha": got},
                     "200 OK" if ok else "400 Bad Request")
    return _err(conn, "404 Not Found", "deploy/" + rest)


def _handle(conn):
    global _reset_pending
    conn.settimeout(30)
    rf = conn.makefile("rb")
    parts = rf.readline().decode().split()
    if len(parts) < 2:
        return
    method, target = parts[0], parts[1]
    raw_path, _, qs = target.partition("?")
    q = _qs(qs)
    clen = 0
    token = None
    while True:
        ln = rf.readline()
        if not ln or ln in (b"\r\n", b"\n"):
            break
        k, _, v = ln.decode().partition(":")
        k = k.strip().lower()
        if k == "content-length":
            try:
                clen = int(v.strip())
            except ValueError:
                clen = 0
        elif k == "x-token":
            token = v.strip()
    if raw_path in ("/", "/ui") and method == "GET":
        return _ui(conn)                           # 화면 자체는 비밀이 없다 — API 는 토큰
    want = cfg.get("token")
    if not want:
        return _err(conn, "403 Forbidden", "토큰이 설정되지 않았다(/webota.json) — 모든 요청 거부")
    if token != want:
        return _err(conn, "401 Unauthorized", "토큰 불일치")
    if raw_path == "/status" and method == "GET":
        return _json(conn, status())
    if raw_path == "/history" and method == "GET":
        try:
            n = int(q.get("n") or 10)
        except ValueError:
            n = 10
        return _json(conn, {"ok": True, "history": wb.history(n)})
    if raw_path == "/fs" or raw_path.startswith("/fs/"):
        path = _norm(raw_path[3:])
        if path is None:
            return _err(conn, "400 Bad Request", "'..' 는 쓸 수 없다")
        return _fs(conn, method, path, q, rf, clen)
    if raw_path == "/sha" and method == "POST":
        body = _read_json_body(rf, clen)
        if body is None:
            return _err(conn, "400 Bad Request", "JSON 본문 파싱 실패")
        out = {}
        for path in body.get("paths") or []:
            n = _norm(path)
            out[path] = sha_file(n) if n else None
        return _json(conn, {"ok": True, "sha": out})
    if raw_path == "/deploy" or raw_path.startswith("/deploy/"):
        return _deploy(conn, method, raw_path[8:], q, rf, clen)
    if raw_path.startswith("/pkg/"):
        return _pkg(conn, method, raw_path[5:], q, rf, clen)
    if raw_path == "/reset" and method == "POST":
        ok, msg = _check_guard(q.get("force") == "1")
        if not ok:
            return _err(conn, "423 Locked", msg or "앱 가드가 거부")
        _json(conn, {"ok": True})
        _reset_pending = True
        return
    return _err(conn, "404 Not Found", raw_path)


def _do_reset():
    time.sleep(0.5)
    if reset_hook is not None:
        return reset_hook()
    import machine
    machine.reset()


def _tick():
    global _trial
    cs = cfg.get("confirm_s")
    if _trial and app_state == "running" and uptime_s() >= int(90 if cs is None else cs):
        if wb.confirm():
            print("[webota] 새 판 확인 — 가동 %ds" % uptime_s())
        _trial = False


def _serve(port):
    global _reset_pending
    import select
    import socket
    while not _stop:
        s = None
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.listen(2)
            poller = select.poll()
            poller.register(s, select.POLLIN)
            print("[webota] listening on :%d" % port)
            while not _stop:
                _tick()
                if not poller.poll(1000):
                    continue
                conn, _addr = s.accept()
                try:
                    _handle(conn)
                except Exception as e:
                    print("[webota] 요청 오류: %r" % e)
                    try:
                        _err(conn, "500 Internal Server Error", "서버 오류: %r" % e)
                    except Exception:
                        pass
                finally:
                    conn.close()
                if _reset_pending:
                    _reset_pending = False
                    _do_reset()
        except Exception as e:
            print("[webota] 리스너 재생성(%r)" % e)
            time.sleep(2)
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


def start(c=None):
    """OTA 서버 스레드 시작. 런처가 앱보다 먼저 부른다."""
    global _t0, _trial
    import _thread
    if c is None:
        c = load_config()
    if _t0 is None:
        _t0 = _ticks()
    _trial = wb.in_trial()
    if not c.get("token"):
        print("[webota] ★토큰 없음 — 모든 요청을 거부한다(/webota.json 에 token 설정)")
    prev = None
    try:
        prev = _thread.stack_size(16 * 1024)      # JSON·해시 처리 여유(기본 스택은 빠듯하다)
    except (AttributeError, ValueError):
        pass
    _thread.start_new_thread(_serve, (int(c.get("port") or 8266),))
    if prev is not None:
        try:
            _thread.stack_size(prev)              # 앱이 만드는 스레드에는 영향을 주지 않는다
        except (AttributeError, ValueError):
            pass


def wifi_up(c=None):
    """WiFi 최소 접속 — 이미 붙어 있으면 그대로. 앱이 WiFi 를 따로 관리해도 충돌하지 않는다
    (같은 SSID 로 붙어 있으면 앱은 그냥 넘어간다). 실패해도 예외 없이 False."""
    c = c or cfg
    try:
        import network
    except ImportError:
        return False
    try:
        w = network.WLAN(network.STA_IF)
        w.active(True)
        if w.isconnected():
            return True
        ssid = pw = None
        if c.get("wifi_file"):
            d = wb.read_json(c["wifi_file"], {}) or {}
            keys = c.get("wifi_keys") or ["ssid", "pass"]
            ssid, pw = d.get(keys[0]), d.get(keys[1])
        if not ssid and isinstance(c.get("wifi"), dict):
            ssid, pw = c["wifi"].get("ssid"), c["wifi"].get("pass")
        if not ssid:
            return False
        print("[webota] WiFi '%s' 접속 중…" % ssid)
        w.connect(ssid, pw or "")
        t = _ticks()
        while not w.isconnected():
            if time.ticks_diff(_ticks(), t) > int(c.get("wifi_timeout_s") or 20) * 1000:
                print("[webota] WiFi 접속 실패(시간 초과)")
                return False
            time.sleep(0.25)
        print("[webota] WiFi 접속 — %s" % w.ifconfig()[0])
        return True
    except Exception as e:
        print("[webota] WiFi 오류: %r" % e)
        return False


def run_app(c=None):
    """앱을 import · 실행한다. 예외가 나면 기록하고, 시험 중이면 롤백 후 리셋,
    아니면 구조 모드(리셋하지 않고 OTA 만 살려 둔다). Ctrl-C 는 그대로 REPL 로 보낸다."""
    global app_state, app_error
    c = c or cfg
    try:
        app_state = "starting"
        mod = __import__(c.get("app") or "app")
        app_state = "running"
        fn = getattr(mod, c.get("entry") or "main", None)
        if fn is not None:
            fn()
        app_state = "exited"
    except KeyboardInterrupt:
        app_state = "stopped"
        raise
    except BaseException as e:
        app_error = _fmt_exc(e)
        print("[webota] 앱 예외:\n" + app_error)
        try:
            wb.makedirs(wb.DIR)
            with open(wb.p(wb.DIR + "/crash.txt"), "w") as f:
                f.write(wb.stamp() + "\n" + app_error)
        except Exception:
            pass
        if wb.in_trial():
            wb.rollback("앱 예외: %s" % (app_error.strip().splitlines() or ["?"])[-1])
            _do_reset()
            return
        app_state = "rescue"
        print("[webota] 구조 모드 — 앱은 멈췄고 OTA(:%s)만 살아 있다" % c.get("port"))
    idle()


def idle():
    """앱이 끝났거나 구조 모드일 때 — 리셋 없이 OTA 를 계속 살려 둔다. 앱이 WDT 를 켜 둔 채
    죽었으면 리셋 루프가 되므로 WDT 를 계속 먹인다(같은 메인 태스크에서)."""
    wdt = None
    try:
        from machine import WDT
        wdt = WDT(timeout=120000)
    except Exception:
        pass
    while _idle_forever and not _stop:
        if wdt is not None:
            try:
                wdt.feed()
            except Exception:
                pass
        time.sleep(5)
