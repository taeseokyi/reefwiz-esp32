# ★vendored: mpy-webota v1.2.0 (device/webota.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota — MicroPython 앱을 위한 배포 패키지 설치 서버(LAN :8266).
#
# ★1.0.0 — 원격으로 할 수 있는 것은 **서명된 패키지(wpk) 설치**와 **웹 수동 정리** 둘뿐이다
#   (사용자 결정 2026-09-25). 파일 API(ls·get·put·rm·mv)·원격 배포·원격 리셋·토큰 바꾸기·출처
#   더하기는 없앴다 — 기기에 코드를 넣는 길은 USB 와 '서명된 패키지' 둘뿐이다(좀비 설치 방지).
#   패키지는 USB 로 심은 공개키로 서명을 확인하고(webota_sig), GitHub 은 TLS 인증서를 CA 로
#   검증하며, GitHub 토큰은 USB 로만 심는다(어떤 응답에도 싣지 않는다).
#
# 모든 API 는 `X-Token`(기기 토큰, /webota.json)이 필요하다. 토큰이 없으면 모두 거부 — 단 아래의
# 무인증·AP 전용 경로는 예외.
#   GET    /                            설치 화면(무인증 — API 는 화면에서 넣은 토큰으로)
#   GET    /hello                       무인증 — {webota, claimed, from_ap}
#   POST   /claim {"token"}             토큰이 없을 때만 · 설정용 AP 에서만 — 기기 등록
#   POST   /login (폼)                  토큰 확인 → 303 / — 크롬 비밀번호 관리자가 저장·동기화하도록
#   GET    /wifi · /wifi/scan · POST /wifi   WiFi 상태·스캔·저장 — 설정용 AP 에서는 토큰 없이
#   GET    /status · /history           상태(비밀은 있다/없다만) · 배포 결과 이력
#   GET    /pkg/sources · /pkg/list     출처(USB 로 정한 것) · 그 출처의 패키지 목록
#   POST   /pkg/plan {"url",...}        설치 계획 — 서명까지 확인(매니페스트만 읽는다)
#   POST   /pkg/install {"url","src","force","switch_app","reset_settings","reset_data"}
#   GET    /pkg/orphans · POST /pkg/clean   남은 파일 목록 · 지우기(설정·데이터는 거부)
#   POST   /auth/start {"action","body"} · /auth/poll {"auth_id"}   GitHub 확인(1.2.0 — webota_auth)
#   (install 은 앱이 set_guard() 로 등록한 가드를 거친다 — force 로 무시)
# ★1.2.0 — GitHub 확인이 설정된 기기(/webota.json 의 github_auth — USB 로만)는 **기기를 바꾸는 모든
#   작업**(install · clean · 공유기 쪽 POST /wifi)에 기기 토큰과 함께 `auth_id` 가 필요하다: 허용된
#   GitHub 계정이 그 작업 하나를 방금 승인했다는 증표. 한 번 쓰면 없어진다(작업마다 새로 승인).
#   작업 내용(url·초기화 선택·지울 파일·SSID)까지 묶이므로 승인 뒤 다른 작업으로 바꿔치기할 수 없다.
#   보기(status · history · list · plan · orphans)는 기기 토큰만. 설정용 AP 의 WiFi 설정 · claim 은 예외.
import json
import os
import sys
import time

import webota_boot as wb

VERSION = "1.2.0"
CONFIG = "/webota.json"
DEFAULTS = {"port": 8266, "app": "app", "entry": "main", "wifi_file": None,
            "wifi_keys": ["ssid", "pass"], "wifi_timeout_s": 20, "confirm_s": 90,
            "token": None, "app_id": None, "packages": None, "sources": None, "ui": "/webota_ui.html",
            "wifi": None, "ap": None, "hostname": None}
CHUNK = 2048
MAX_JSON = 64 * 1024

cfg = dict(DEFAULTS)
app_state = "boot"          # boot → starting → running → exited | rescue | stopped
app_error = ""
reset_hook = None           # 테스트가 machine.reset 대신 부를 함수를 넣는다
_guard = None
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
    """설정을 읽는다. ★파일이 없으면 기본값으로 만든다(첫 부팅) — 토큰이 없으니 API 는 닫혀 있고,
    WiFi 도 없으니 설정용 AP 가 뜬다. 휴대폰으로 AP 에 붙어 설치 화면에서 '기기 등록'(토큰)과
    WiFi 를 정한다. 그래서 USB 로는 webota 파일만 올려도 된다."""
    global cfg
    disk = wb.read_json(path)
    if disk is None:
        disk = {"port": DEFAULTS["port"], "app": DEFAULTS["app"], "entry": DEFAULTS["entry"],
                "confirm_s": DEFAULTS["confirm_s"]}
        try:
            wb.write_json(path, disk)
            print("[webota] %s 없음 — 기본값으로 만들었다(설정용 AP 에서 기기 등록)" % path)
        except OSError as e:
            print("[webota] 설정 파일을 못 만들었다: %r" % e)
    c = dict(DEFAULTS)
    c.update(disk)
    cfg = c
    return c


def _set_token(tok):
    tok = (tok or "").strip()
    if len(tok) < 16:
        return False, "토큰은 16자 이상"
    disk = wb.read_json(CONFIG, {}) or {}
    disk["token"] = tok
    wb.write_json(CONFIG, disk)
    cfg["token"] = tok
    return True, "등록됨"


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


def _wifi_status():
    try:
        import webota_net
        return webota_net.status(cfg)
    except Exception:
        return None


def _inst_summary():
    i = wb.read_json(wb.DIR + "/installed.json")
    return {"app_id": i.get("app_id"), "label": i.get("label"), "files": len(i.get("files") or [])} if i else None


def status():
    st = {"webota": VERSION, "uptime_s": uptime_s(), "app": {"name": cfg.get("app"),
          "state": app_state, "error": app_error[-1500:]},
          "trial": wb.read_json(wb.DIR + "/trial.json"),
          "pending": wb.exists(wb.DIR + "/pending.json"),
          "last": wb.read_json(wb.DIR + "/last.json"),
          "confirm_s": cfg.get("confirm_s"),
          # 비밀은 있다/없다만 — 토큰·키 자체는 어떤 응답에도 싣지 않는다
          "security": {"pkg_keys": [k.get("id") for k in _keys()], "github_token": bool(cfg.get("github_token")),
                       "ca": wb.exists("/webota_ca.pem"),
                       "github_auth": _auth_owners()},
          "app_id": cfg.get("app_id"), "current": _current(),
          "modified": _modified_now(),
          "installed": _inst_summary(), "prev": wb.exists(wb.DIR + "/prev"),
          "wifi": _wifi_status(),
          "keep": dict(zip(("settings", "data"), keep_lists()))}
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


# ── 파일 구분: 코드 · 설정 · 데이터 ──
#   코드   패키지가 기기를 **그대로 맞춘다**(없는 건 지우고, 다른 것만 쓴다). 정리 대상.
#   설정   덮어쓰지 않는다. 패키지에 기본값이 있으면 **기기에 없을 때만** 넣는다. 정리 제외.
#   데이터 건드리지 않는다. 정리 제외.
# ★어디가 설정·데이터인지는 **앱 패키지만** 선언한다(사용자 결정 2026-09-24) — 매니페스트의
#   "settings"·"data"(프로젝트 파일 webota.project.json 에서 온다). 기기 설정에는 두지 않는다.
#   항목은 파일이나 디렉토리(그 아래 전부). webota 자신의 /webota·/webota.json 만 늘 보존한다.
#   ★**선언이 없으면 모든 것이 정리 대상**이다(사용자 결정 2026-09-24) — 설치하는 패키지의
#   선언만이 기준이고, 이전 판·이전 앱의 선언은 이어지지 않는다. 그래서 설치 전에 계획
#   (/pkg/plan — 바뀔 것·지울 것·보존할 것)을 보여 주고 확인받는다.
# webota 자신(CORE)은 패키지가 갱신은 하지만 지우지는 않는다.
CORE = ("/boot.py", "/main.py", "/webota.py", "/webota_boot.py", "/webota_pkg.py", "/webota_auth.py",
        "/webota_net.py", "/webota_sig.py", "/webota_ca.pem", "/webota_ui.html")


def _under(path, roots):
    for d in roots:
        d = d.rstrip("/") or "/"
        if path == d or path.startswith(d + "/"):
            return True
    return False


def keep_lists(extra=None, current=True):
    """(설정 경로들, 데이터 경로들) — webota 자신 + 지금 판의 선언(installed; current=False 면
    빼고) + extra(설치할 판의 매니페스트). 설치 계획은 current=False — 새 판의 선언만 본다."""
    inst = (wb.read_json(wb.DIR + "/installed.json") or {}) if current else {}
    settings = [CONFIG] + list(inst.get("settings") or [])
    data = [wb.DIR] + list(inst.get("data") or [])
    if extra:
        settings += list(extra.get("settings") or [])
        data += list(extra.get("data") or [])
    uniq = lambda xs: [x for i, x in enumerate(xs) if x not in xs[:i]]   # 기기·패키지 선언이 겹친다
    return uniq(settings), uniq(data)


def kind_of(path, extra=None):
    """설정이 데이터보다 먼저다 — 데이터 디렉토리 안의 설정 파일(예: /data/devices.json)도
    선언했으면 설정이다(설정 초기화는 되고, 데이터 초기화는 안 된다)."""
    settings, data = keep_lists(extra)
    if _under(path, settings):
        return "setting"
    if _under(path, data):
        return "data"
    return "core" if path in CORE else "code"


def _protected(path, extra=None):
    return kind_of(path, extra) != "code"


def _code_files(extra=None, current=True):
    """기기의 코드 파일 전부(설정 · 데이터 · webota 자신 제외). extra: 설치할 판의 매니페스트,
    current=False: 지금 판의 선언은 빼고 extra 의 선언만으로 가른다(설치 계획)."""
    settings, data = keep_lists(extra, current)
    skip = settings + data
    out = []

    def walk(d):
        try:
            names = os.listdir(wb.p(d) or "/")
        except OSError:
            return
        for n in names:
            fp = d.rstrip("/") + "/" + n
            if _under(fp, skip):
                continue
            try:
                st = os.stat(wb.p(fp))
            except OSError:
                continue
            if st[0] & 0x4000:
                walk(fp)
            elif fp not in CORE:
                out.append(fp)
    walk("/")
    return out


def orphans():
    """지금 판을 이루는 파일(installed.json)에 없는 코드 파일 [{path, size}] — 앱 교체·판 변경
    뒤에 남은 것들. 목록이 없으면 None(아직 이 기능으로 설치한 적이 없다)."""
    inst = wb.read_json(wb.DIR + "/installed.json")
    if not inst:
        return None
    keep = set(inst.get("files") or [])
    out = []
    for fp in _code_files():
        if fp not in keep:
            try:
                out.append({"path": fp, "size": os.stat(wb.p(fp))[6]})
            except OSError:
                pass
    return sorted(out, key=lambda e: e["path"])


def _files_under(roots):
    """roots(파일·디렉토리) 아래의 파일 전부 — 초기화 대상 목록."""
    out = []

    def walk(path):
        try:
            st = os.stat(wb.p(path))
        except OSError:
            return
        if st[0] & 0x4000:
            for n in os.listdir(wb.p(path)):
                walk(path.rstrip("/") + "/" + n)
        else:
            out.append(path)
    for r in roots:
        walk(r)
    return out


def _plan(man, reset_settings=False, reset_data=False):
    """설치 계획 — 매니페스트만으로 안다(파일마다 해시가 있다). 새 판의 선언만 기준.
    reset_settings: 선언된 설정을 패키지 기본값으로(기본값 없는 설정 파일은 지운다).
    reset_data: 선언된 데이터를 모두 지운다. 둘 다 webota 자신(/webota.json · /webota/)은 제외."""
    new_files = [f["path"] for f in man.get("files") or []]
    newset = set(new_files)
    write, skip_setting = [], []
    for f in man.get("files") or []:
        if f.get("kind") == "setting" and wb.exists(f["path"]) and (
                not reset_settings or f["path"] == cfg.get("wifi_file")):
            skip_setting.append(f["path"])
        elif sha_file(f["path"]) != f["sha"].lower():
            write.append(f["path"])
    deletes = [e for e in _code_files(man, current=False) if e not in newset]
    ks0, kd0 = keep_lists(man, current=False)
    # 초기화에서도 지키는 것: webota 자신, 그리고 webota 가 WiFi 에 붙을 때 읽는 파일
    #   (wifi_file — 이걸 지우면 원격 접속이 끊긴다).
    never = [CONFIG, wb.DIR] + ([cfg["wifi_file"]] if cfg.get("wifi_file") else [])
    reset = []
    if reset_settings:
        reset += [e for e in _files_under(ks0[1:]) if e not in newset and not _under(e, never)]
    if reset_data:                                     # 선언된 설정은 데이터 초기화에서 빠진다
        reset += [e for e in _files_under(kd0[1:]) if not _under(e, never + ks0[1:])]
    deletes += [e for e in reset if e not in deletes]
    cur_s, cur_d = keep_lists()                        # 지금 판에서 설정·데이터였던 것
    risky = [e for e in deletes if _under(e, cur_s[1:] + cur_d[1:])]
    ks, kd = keep_lists(man, current=False)
    return {"write": write, "delete": deletes, "delete_kept_now": risky, "skip_setting": skip_setting,
            "delete_reset": reset, "reset_settings": bool(reset_settings), "reset_data": bool(reset_data),
            "keep_settings": ks, "keep_data": kd, "declared": "settings" in man or "data" in man}


def _commit(did, paths, deletes, label, reset, force, installed=None):
    """스테이징된 배포를 확정한다 — pending 기록 후(reset 이면) 리셋 예약. (ok, 상태, 메시지).
    installed: 이 판을 이루는 파일 전체 {app_id, label, files} — 적용 때 installed.json 이 된다."""
    global _reset_pending
    if not paths and not deletes:
        return False, "400 Bad Request", "바꿀 것이 없다"
    ok, msg = _check_guard(force)
    if not ok:
        return False, "423 Locked", msg or "앱 가드가 거부"
    wb.write_json(wb.DIR + "/pending.json", {"id": did, "label": label, "files": paths,
                                             "delete": deletes, "installed": installed,
                                             "at": wb.stamp()})
    if reset:
        _reset_pending = True
    return True, "200 OK", ""


def _installed_files():
    return set((wb.read_json(wb.DIR + "/installed.json") or {}).get("files") or [])


def _modified_now():
    """판 이탈의 **지금** 모습 — 기록된 경로 중 ①아직 있는 것(더했거나 고친 것) ②판에 있는데
    사라진 것(지운 것)만 남긴다. 판에 없던 파일을 더했다가 지웠으면 이탈이 아니다(0.9.2: 정리로
    지운 파일이 '수동 변경'에 계속 남던 문제)."""
    m = wb.read_json(wb.DIR + "/modified.json")
    if not m:
        return None
    inst = _installed_files()
    paths = [x for x in m.get("paths") or [] if wb.exists(x) or x in inst]
    if paths != (m.get("paths") or []):
        _write_modified(paths)
    return {"paths": paths, "at": m.get("at")} if paths else None


def _write_modified(paths):
    if paths:
        wb.write_json(wb.DIR + "/modified.json", {"paths": paths[-200:], "at": wb.stamp()})
    else:
        wb.remove(wb.DIR + "/modified.json")


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


def _keys():
    return cfg.get("pkg_keys") or []


def _ensure_time():
    """TLS 인증서의 유효 기간을 보려면 시각이 맞아야 한다 — 2025 년 이전이면 NTP 를 한 번 시도."""
    try:
        if time.localtime()[0] >= 2025:
            return True
        import webota_net
        return webota_net.ntp()
    except Exception:
        return False


def _auth_owners():
    import webota_auth as au
    ga = au.conf(cfg)
    return list(ga["owners"]) if ga else None


def auth_detail(action, body):
    """승인이 묶이는 작업 내용 — 클라이언트·화면은 본문만 보내고, 묶는 규칙은 기기가 만든다."""
    body = body or {}
    if action == "install":
        return "install|%s|%d%d%d%d" % (body.get("url"), bool(body.get("switch_app")),
                                        bool(body.get("reset_settings")), bool(body.get("reset_data")),
                                        bool(body.get("force")))
    if action == "clean":
        return "clean|" + ",".join(sorted(_norm(x) or "" for x in body.get("paths") or []))
    if action == "wifi":
        return "wifi|%s" % (body.get("ssid") or "").strip()
    return None


def _need_auth(conn, action, body):
    """GitHub 확인이 설정된 기기면 body.auth_id 의 승인을 쓴다 — 거부면 응답을 보내고 False."""
    import webota_auth as au
    ok, msg, login = au.consume(cfg, (body or {}).get("auth_id"), auth_detail(action, body))
    if not ok:
        _json(conn, {"ok": False, "code": "auth_required", "err": msg, "action": action}, "403 Forbidden")
        return False
    if login:
        print("[webota] GitHub 승인(%s) — %s" % (login, action))
    return True


def _auth(conn, method, rest, rf, clen):
    import webota_auth as au
    body = _read_json_body(rf, clen) or {}
    if not au.conf(cfg):
        return _err(conn, "404 Not Found", "GitHub 확인이 설정되지 않은 기기다(github_auth — USB 로 심는다)")
    try:
        _ensure_time()
        if rest == "start" and method == "POST":
            d = auth_detail(body.get("action"), body.get("body"))
            if d is None:
                return _err(conn, "400 Bad Request", "알 수 없는 작업: %s" % body.get("action"))
            r = au.start(cfg, d)
            r.update({"ok": True, "action": body.get("action")})
            return _json(conn, r)
        if rest == "poll" and method == "POST":
            r = au.poll(cfg, body.get("auth_id"))
            r["ok"] = r["state"] != "denied" and r["state"] != "expired"
            return _json(conn, r)
    except Exception as e:
        return _err(conn, "502 Bad Gateway", "GitHub 확인 실패: %s" % (e,))
    return _err(conn, "404 Not Found", "auth/" + rest)


def _pkg(conn, method, rest, q, rf, clen):
    import webota_pkg as pkg
    if rest == "sources" and method == "GET":
        # ★출처는 USB 로 정한 것만(1.0.0) — 웹에서 더하거나 뺄 수 없다(아무 저장소의 패키지 = 좀비 경로).
        return _json(conn, {"ok": True, "sources": [pkg.source_key(x) for x in pkg.sources(cfg)]})
    if rest == "orphans" and method == "GET":
        o = orphans()
        if o is None:
            return _json(conn, {"ok": False, "orphans": [],
                                "err": "설치 파일 목록이 없다 — 패키지를 한 번 설치(또는 배포)하면 생긴다"})
        return _json(conn, {"ok": True, "orphans": o, "bytes": sum(e["size"] for e in o)})
    if rest == "clean" and method == "POST":
        body = _read_json_body(rf, clen) or {}
        if not _need_auth(conn, "clean", body):
            return
        o = orphans() or []
        allowed = set(e["path"] for e in o)
        done, refused = [], []
        for path in body.get("paths") or []:
            n = _norm(path)
            if n in allowed and not _protected(n):      # 설정·데이터는 목록에 있어도 거부
                try:
                    os.remove(wb.p(n))
                    wb.prune_empty(n)
                    done.append(n)
                    _modified_now()                     # 판에 없던 파일을 지웠다 — 이탈 기록에서 빠진다
                except OSError:
                    refused.append(n)
            else:
                refused.append(path)
        return _json(conn, {"ok": not refused, "deleted": done, "refused": refused,
                            "err": ("남은 파일이 아니라 지우지 않았다: " + ", ".join(refused)) if refused else None})
    if rest == "list" and method == "GET":
        lst_src = pkg.sources(cfg)
        src = _find_source(pkg, q["src"]) if q.get("src") else (lst_src[0] if lst_src else None)
        if src is None:
            return _json(conn, {"ok": False, "err": "패키지 출처가 없다 — 저장소를 더한다", "packages": [],
                                "current": _current(), "app_id": cfg.get("app_id"), "src": None})
        _ensure_time()
        lst, err = pkg.list_packages(src, now=uptime_s() if q.get("fresh") != "1" else None,
                                     token=cfg.get("github_token"))
        return _json(conn, {"ok": err is None, "err": err, "packages": lst or [], "src": pkg.source_key(src),
                            "current": _current(), "app_id": cfg.get("app_id")})
    if rest == "plan" and method == "POST":
        body = _read_json_body(rf, clen) or {}
        if not body.get("url"):
            return _err(conn, "400 Bad Request", "url 이 필요하다")
        try:
            _ensure_time()
            man = pkg.read_manifest(body["url"], _keys(), cfg.get("github_token"))
        except Exception as e:
            return _err(conn, "400 Bad Request", "%s" % e)
        pl = _plan(man, bool(body.get("reset_settings")), bool(body.get("reset_data")))
        pl.update({"ok": True, "label": man.get("label"), "pkg_app_id": man.get("app_id"), "key": man.get("_key"),
                   "app_id": cfg.get("app_id"), "files": len(man.get("files") or [])})
        return _json(conn, pl)
    if rest == "install" and method == "POST":
        body = _read_json_body(rf, clen)
        if not body or not body.get("url"):
            return _err(conn, "400 Bad Request", "url 이 필요하다")
        force = bool(body.get("force")) or q.get("force") == "1"
        switch = bool(body.get("switch_app"))
        ok, msg = _check_guard(force)                  # 내려받기 전에 먼저 거른다(헛수고 방지)
        if not ok:
            return _err(conn, "423 Locked", msg or "앱 가드가 거부")
        if not _need_auth(conn, "install", body):      # 가드 뒤에 — 가드가 막으면 승인을 아낀다
            return
        stage = wb.DIR + "/stage/files"
        wb.rmtree(wb.DIR + "/stage")
        wb.makedirs(stage)
        did = "%d-%d" % (time.time(), _ticks() % 100000)
        try:
            _ensure_time()
            ok, msg, man, changed = pkg.install(body["url"], None if switch else cfg.get("app_id"),
                                                stage, sha_file,
                                                reset_settings=bool(body.get("reset_settings")),
                                                keep_always=[cfg["wifi_file"]] if cfg.get("wifi_file") else [],
                                                keys=_keys(), token=cfg.get("github_token"))
        except Exception as e:
            ok, msg, man, changed = False, "내려받기 실패: %r" % e, None, []
        if not ok:
            wb.rmtree(wb.DIR + "/stage")
            if man and cfg.get("app_id") and man.get("app_id") != cfg.get("app_id"):
                return _json(conn, {"ok": False, "code": "app_mismatch", "err": msg,
                                    "app_id": cfg.get("app_id"), "pkg_app_id": man.get("app_id"),
                                    "label": man.get("label")}, "409 Conflict")
            return _err(conn, "400 Bad Request", msg)
        # ★앱이 아직 없는 기기(첫 부팅 기본 설정 — app_id 없음)의 첫 설치는 그 앱을 **받아들인다** —
        #   app_id·app·entry 를 앱 교체와 똑같이 새 /webota.json 으로 같은 트랜잭션에 넣는다
        #   (안 그러면 기기가 계속 '앱 없음'이라 다음 판도 다른 앱도 가려내지 못한다 — 0.8.1 결함).
        adopt = not cfg.get("app_id") and man.get("app_id")
        if (switch or adopt) and man.get("app_id") != cfg.get("app_id"):
            # ★앱 교체 — 새 앱도 webota 를 싣고 있어야 교체 뒤에도 원격이 산다.
            paths = [f["path"] for f in man.get("files") or []]
            missing = [x for x in ("/webota.py", "/webota_boot.py", "/main.py", "/boot.py")
                       if x not in paths]
            if missing and not force and not adopt:
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
        new_files = [f["path"] for f in man.get("files") or []]
        # ★패키지 설치 = "모두 지우고 패키지를 푼 것"과 같은 결과(사용자 결정 2026-09-24) — 차이만
        #   한다. 새 판이 선언한 설정·데이터와 webota 자신만 남고 나머지는 지운다(선언이 없으면
        #   전부). 지우는 파일도 백업되므로 롤백되면 되살아난다. 계획은 /pkg/plan 으로 먼저 본다.
        plan = _plan(man, bool(body.get("reset_settings")), bool(body.get("reset_data")))
        for e in plan["delete"]:
            if e not in deletes:
                deletes.append(e)
        installed = {"app_id": man.get("app_id"), "label": label, "files": new_files,
                     "settings": list(man.get("settings") or []), "data": list(man.get("data") or [])}
        if not changed and not deletes:
            wb.rmtree(wb.DIR + "/stage")
            if not wb.exists(wb.DIR + "/installed.json"):   # 파일은 같다 — 목록만 남긴다
                wb.write_json(wb.DIR + "/installed.json", installed)
            return _json(conn, {"ok": True, "result": "unchanged", "label": label})
        ok, st, msg = _commit(did, changed, deletes, label, True, force, installed)
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


def _wifi(conn, method, rest, rf, clen, from_ap=False):
    import webota_net as net
    if rest == "" and method == "GET":
        return _json(conn, {"ok": True, "wifi": net.status(cfg)})
    if rest == "scan" and method == "GET":
        nets, err = net.scan()
        return _json(conn, {"ok": err is None, "err": err, "nets": nets})
    if rest == "" and method == "POST":
        body = _read_json_body(rf, clen) or {}
        if not from_ap and not _need_auth(conn, "wifi", body):   # 설정용 AP 는 AP 비밀번호가 인증
            return
        ok, msg = net.save(cfg, body.get("ssid"), body.get("pass"))
        return _json(conn, {"ok": ok, "msg": msg, "wifi": net.status(cfg)}, "200 OK" if ok else "400 Bad Request")
    return _err(conn, "404 Not Found", "wifi/" + rest)


class _Req:
    """요청 본문 읽개 — 읽은 양을 센다. ★응답 뒤 남은 본문을 버려야 한다(0.8.5, 실기에서 발견):
    본문을 다 읽지 않고 오류로 응답한 뒤 닫으면 lwIP 가 FIN 대신 **RST** 를 보내, 클라이언트는
    응답을 받기도 전에 'Connection reset' 을 본다(재등록 409 · 틀린 토큰의 업로드 401 등).
    CPython 은 그렇게 동작하지 않아 시험에서 잡히지 않았다."""

    def __init__(self, rf, clen):
        self.rf, self.clen, self.used = rf, clen, 0

    def read(self, n):
        n = min(n, self.clen - self.used)
        if n <= 0:
            return b""
        b = self.rf.read(n)
        self.used += len(b)
        return b

    def drain(self, limit=2 * 1024 * 1024):
        left = min(self.clen - self.used, limit)
        while left > 0:
            b = self.rf.read(min(CHUNK, left))
            if not b:
                break
            left -= len(b)
            self.used += len(b)


_req = None


HEAD_TIMEOUT_S = 3        # 요청 첫 줄·헤더를 기다리는 시간


def _handle(conn, peer=None):
    global _reset_pending, _req
    # ★요청 첫 줄·헤더는 짧게만 기다린다(0.9.1, 실기에서 발견): 크롬은 페이지를 옮길 때 **아무것도
    #   보내지 않는 예비 연결**을 미리 연다. 한 번에 한 연결만 받는 이 서버가 그 빈 연결에서 30초를
    #   기다리면 뒤의 진짜 요청이 밀리고, 시간 초과(OSError 116)가 500 으로 새 페이지에 섞였다.
    #   아무것도 안 온 연결은 **응답 없이** 닫는다.
    conn.settimeout(HEAD_TIMEOUT_S)
    rf = conn.makefile("rb")
    try:
        parts = rf.readline().decode().split()
    except OSError:
        return                                   # 빈 예비 연결 — 조용히 닫는다
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
    conn.settimeout(30)                           # 본문(업로드)은 넉넉히
    rf = _req = _Req(rf, clen)                    # 이후 본문은 모두 이걸로 읽는다(남은 양을 안다)
    if raw_path in ("/", "/ui") and method == "GET":
        return _ui(conn)                           # 화면 자체는 비밀이 없다 — API 는 토큰
    if raw_path == "/login" and method == "POST":
        # ★크롬 비밀번호 관리자용(0.9.0): 설치 화면의 토큰 칸은 이 주소로 제출되는 진짜 로그인 폼이다.
        #   크롬은 폼 제출 뒤 페이지가 바뀌는 것을 보고 '비밀번호를 저장할까요?' 를 띄우고, 구글 계정으로
        #   다른 기기에 동기화한다(같은 주소에서 칸을 누르면 자동 입력). 여기서는 맞는지만 알려 준다.
        form = _qs((_read_body(rf, clen, 4096) or b"").decode())
        ok = bool(cfg.get("token")) and form.get("password", "") == cfg.get("token")
        where = "/?login=ok" if ok else "/?login=bad"
        _sendall(conn, ("HTTP/1.0 303 See Other\r\nLocation: %s\r\nContent-Length: 0\r\n"
                        "Connection: close\r\n\r\n" % where).encode())
        return
    if raw_path == "/hello" and method == "GET":
        import webota_net as net
        return _json(conn, {"webota": VERSION, "claimed": bool(cfg.get("token")),
                            "from_ap": net.from_ap(peer), "app_id": cfg.get("app_id")})
    if raw_path == "/claim" and method == "POST":
        import webota_net as net
        if cfg.get("token"):
            return _err(conn, "409 Conflict", "이미 등록된 기기다 — 토큰을 바꾸려면 /token(지금 토큰 필요)")
        if not net.from_ap(peer):
            return _err(conn, "403 Forbidden", "기기 등록은 설정용 AP 로 붙어서 한다")
        body = _read_json_body(rf, clen) or {}
        ok, msg = _set_token(body.get("token"))
        return _json(conn, {"ok": ok, "msg": msg, "err": None if ok else msg}, "200 OK" if ok else "400 Bad Request")
    if raw_path == "/wifi" or raw_path.startswith("/wifi/"):
        import webota_net as net
        ap = net.from_ap(peer)
        if ap or (cfg.get("token") and token == cfg.get("token")):
            return _wifi(conn, method, raw_path[6:], rf, clen, ap)
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
    if raw_path.startswith("/pkg/"):
        return _pkg(conn, method, raw_path[5:], q, rf, clen)
    if raw_path.startswith("/auth/"):
        return _auth(conn, method, raw_path[6:], rf, clen)
    return _err(conn, "404 Not Found", raw_path)


def _do_reset():
    time.sleep(0.5)
    if reset_hook is not None:
        return reset_hook()
    import machine
    machine.reset()


_net_at = None


def _tick():
    global _trial, _net_at
    now = uptime_s()
    if _net_at is None or now - _net_at >= 5:      # WiFi 유지(재접속 · AP) — 5초마다
        _net_at = now
        try:
            import webota_net
            webota_net.tick(cfg)
        except Exception as e:
            print("[webota] net: %r" % e)
    cs = cfg.get("confirm_s")
    if _trial and app_state == "running" and uptime_s() >= int(90 if cs is None else cs):
        if wb.confirm():
            print("[webota] 새 판 확인 — 가동 %ds" % uptime_s())
        _trial = False


def _serve(port):
    global _reset_pending, _req
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
                conn, addr = s.accept()
                _req = None
                try:
                    _handle(conn, addr[0] if addr else None)
                    if _req is not None:
                        _req.drain()               # 읽지 않은 본문을 버려야 RST 없이 닫힌다
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
    """부팅 때 WiFi — webota_net.boot: 접속을 시도하고 안 되면 설정용 AP 를 올린다.
    ★앱은 WiFi 를 만지지 않는다(상태는 webota_net.is_connected()/status() 로 읽기만)."""
    import webota_net
    return webota_net.boot(c or cfg)


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
