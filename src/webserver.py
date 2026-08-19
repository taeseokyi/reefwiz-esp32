# 로컬 웹서버 — dkh_server.py 대체 + 대시보드 정적 서빙 + 설정 API.
# 원본 dkh_server 는 /api/dkh 하나였고 설정 저장은 대시보드→GitHub API 커밋이었다.
# ESP32 는 데이터 주인과 서버가 같은 기기이므로 설정도 로컬 POST 로 받는다(토큰 불요):
#   GET  /api/dkh              → {"dkh": <마지막 tank_kh>}   (dkh_server 동일)
#   GET/POST /api/override     → doser_override.json  (POST 시 id 자동 부여, 즉시 적용 이벤트)
#   GET  /api/override/state   → doser_override_state.json (적용 여부 표시용)
#   GET/POST /api/config       → doser_config.json {target_dkh}
#   GET/POST /api/ph_cal       → ph_cal.json (표시 전용 한나 보정)
#   GET/POST /api/bt           → BT 접속 정보(BIND 주소). config.py 값보다 우선
#   조치(ops.py): GET  /api/ops/status | /api/ops/log | /api/ops/result
#                 POST /api/ops/abort | /clear_latch | /liquid | /job
#   WiFi:         GET  /api/wifi (상태) | /api/wifi/scan (주변 AP)   POST /api/wifi (저장·재접속)
#   백업(SD 대체): GET  /api/backup (설정 번들 다운로드) | /api/files (파일 목록)
#                 POST /api/restore (설정만 복원)
#                 GET  /data/<경로> (아카이브·로그 파일 원본 다운로드 — LAN 전용 기기)
#                 ※AP 폴백 모드에서도 이 서버가 응답한다 → 현장에서 공유기 변경 가능
#   정적: /www (index.html, ops.html, vendor/*)  +  /data 의 대시보드 JSON(상대경로 fetch 대응)
# vendor/chart.umd.min.js 는 .gz 로 올려두면 Content-Encoding: gzip 으로 서빙(70KB).
import json
import os
import socket
import _thread

import archive
import config
import datalog
import link
import ops
import rwtime
import state
import wifinet

DATA_FILES = {"dkh.dat", "dkh_series.json", "dkh_latest.json",
              "doser_history.json", "doser_override.json", "doser_override_state.json",
              "doser_config.json", "ph_cal.json"}
# 대시보드가 배열로 기대하지만 저장은 JSONL 인 파일 — 줄 단위 스트리밍으로 조립해 보낸다.
JSONL_AS_ARRAY = {"dkh_plateau_history.json": datalog.PLATEAU_JSONL}

MIME = {"html": "text/html; charset=utf-8", "js": "application/javascript",
        "json": "application/json", "png": "image/png", "css": "text/css",
        "webmanifest": "application/manifest+json", "dat": "text/plain",
        "ico": "image/x-icon", "svg": "image/svg+xml"}


def _mime(path):
    return MIME.get(path.rsplit(".", 1)[-1], "application/octet-stream")


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _send_head(conn, status, ctype, length, extra=""):
    conn.send(("HTTP/1.0 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
               "Connection: close\r\n%s\r\n" % (status, ctype, length, extra)).encode())


def _send_json(conn, obj, status="200 OK"):
    body = json.dumps(obj).encode()
    _send_head(conn, status, "application/json", len(body), "Cache-Control: no-store\r\n")
    conn.send(body)


def _send_file(conn, path, gz=False, cache=False):
    size = os.stat(path)[6]
    extra = ""
    if gz:
        extra += "Content-Encoding: gzip\r\n"
    extra += "Cache-Control: max-age=86400\r\n" if cache else "Cache-Control: no-store\r\n"
    ctype = _mime(path[:-3] if gz else path)
    _send_head(conn, "200 OK", ctype, size, extra)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            conn.send(chunk)


def _send_jsonl_array(conn, path):
    """JSONL 파일을 JSON 배열로 스트리밍 — 파싱·연결 없이 그대로 흘려보낸다(힙 무관).
    Content-Length 를 미리 못 구하므로 length 헤더 없이 보내고 연결 종료로 끝을 알린다."""
    conn.send(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
              b"Cache-Control: no-store\r\nConnection: close\r\n\r\n[")
    first = True
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                conn.send(ln.encode() if first else b"," + ln.encode())
                first = False
    except OSError:
        pass
    conn.send(b"]")


def _read_json_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json_file(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.rename(tmp, path)


def _wifi_api(conn, method, path, body):
    """WiFi 설정 — AP 모드에서도 같은 서버가 응답하므로 여기서 공유기를 바꿀 수 있다."""
    if path == "/api/wifi":
        if method == "GET":
            return _send_json(conn, wifinet.status())
        ok, msg = wifinet.save(body.get("ssid"), body.get("pass"))
        if ok:
            state.wifi_reconnect = True        # 메인 루프가 즉시 재접속(응답을 먼저 보낸 뒤)
        return _send_json(conn, {"ok": ok, "msg": msg})

    if path == "/api/wifi/scan":
        nets, err = wifinet.scan()
        return _send_json(conn, {"nets": nets, "err": err})

    _send_json(conn, {"err": "not found"}, "404 Not Found")


def _ops_api(conn, method, path, body, query):
    """조치 API — 장비 접촉 작업은 큐잉만 하고 메인 루프가 실행(UART 경합 방지)."""
    if path == "/api/ops/status":
        return _send_json(conn, ops.snapshot())

    if path == "/api/ops/log":
        n = 40
        for kv in query.split("&"):
            if kv.startswith("n="):
                try:
                    n = max(1, min(300, int(kv[2:])))
                except ValueError:
                    pass
        return _send_json(conn, {"lines": ops.log_tail(n)})

    if path == "/api/ops/result":
        return _send_json(conn, {"result": state.job_result, "busy": state.job_busy,
                                 "pending": state.job["kind"] if state.job else None})

    if method != "POST":
        return _send_json(conn, {"err": "POST 필요"}, "405 Method Not Allowed")

    if path == "/api/ops/abort":
        ok, msg = ops.request_abort()
        return _send_json(conn, {"ok": ok, "msg": msg})

    if path == "/api/ops/clear_latch":
        ok, msg = ops.clear_error_latch()
        return _send_json(conn, {"ok": ok, "msg": msg})

    if path == "/api/ops/liquid":
        ok, msg = ops.set_liquid(body.get("chamber"), body.get("holding"))
        return _send_json(conn, {"ok": ok, "msg": msg})

    if path == "/api/ops/job":
        kind = body.get("kind")
        if kind not in ops.JOB_KINDS:
            return _send_json(conn, {"ok": False, "msg": "kind 는 %s 중 하나"
                                     % (ops.JOB_KINDS,)}, "400 Bad Request")
        if state.measuring:
            # ★측정 중에는 장비 조작 전부 금지 — cmd 도 예외가 아니다(2026-08-19 사용자 지시).
            #   종전에는 cmd 만 큐잉을 허용했는데, "요청됨"이라고 응답해 놓고 몇 시간 뒤에
            #   실행되는 셈이라 운영자가 지금 실행된 줄로 오해할 수 있었다.
            return _send_json(conn, {"ok": False,
                                     "msg": "측정 중 — 먼저 '측정 중단' 하거나 종료를 기다리세요"})
        if kind == "cmd":
            # ★명령 콘솔은 Idle 에서만 그냥 열린다. 래치·위치 불명은 원래 콘솔로 정리하는
            #   상황이라(결정 #10) 운영자 확인(ack)을 받고 연다. 동결·작업 중은 우회 불가.
            #   판정은 ops.device_state() 한 곳 — 화면의 잠금과 서버의 거부가 항상 같은 이유다.
            dev = ops.device_state()
            if not dev["console_allowed"]:
                if not (dev["console_override"] and body.get("ack")):
                    return _send_json(conn, {"ok": False, "state": dev["state"],
                                             "msg": "%s — %s" % (dev["label"],
                                                                 dev["console_reason"])})
                datalog.log("[조치] 명령 콘솔 잠금 해제 실행(운영자 확인) — 상태=%s" % dev["state"])
        args = {k: v for k, v in body.items() if k != "kind"}
        if not state.put_job(kind, **args):
            return _send_json(conn, {"ok": False, "msg": "다른 작업이 대기/실행 중입니다"})
        return _send_json(conn, {"ok": True, "msg": "%s 요청됨 — 결과는 폴링" % kind})

    _send_json(conn, {"err": "not found"}, "404 Not Found")


def _send_download(conn, obj, filename):
    """JSON 본문을 파일로 저장되게 보낸다 — 브라우저 '백업 내려받기' 버튼용."""
    body = json.dumps(obj).encode()
    _send_head(conn, "200 OK", "application/json", len(body),
               'Content-Disposition: attachment; filename="%s"\r\nCache-Control: no-store\r\n'
               % filename)
    conn.send(body)


def _list_dir(rel):
    """/data 하위 파일 목록 — 다운로드 링크 생성용. 디렉토리는 재귀 1단만 본다."""
    out = []
    base = config.DATA_DIR + (("/" + rel) if rel else "")
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        fp = base + "/" + name
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if st[0] & 0x4000:                       # 디렉토리
            if not rel:                          # 한 단만 내려간다(/data/archive)
                out.extend(_list_dir(name))
            continue
        out.append({"path": (rel + "/" + name) if rel else name, "bytes": st[6]})
    return out


def _backup_api(conn, method, path, body):
    """설정 백업·복원 — SD 를 뺀 뒤의 장기 보관 경로(상세 근거는 archive.py 헤더)."""
    if path == "/api/backup" and method == "GET":
        return _send_download(conn, archive.bundle(), "reefwiz-backup.json")
    if path == "/api/files" and method == "GET":
        return _send_json(conn, {"dir": config.DATA_DIR, "files": _list_dir(""),
                                 "archive": archive.status()})
    if path == "/api/restore" and method == "POST":
        try:
            obj = json.loads(body)
        except ValueError:
            return _send_json(conn, {"ok": False, "msg": "JSON 파싱 실패"}, "400 Bad Request")
        ok, msg = archive.restore(obj)
        if ok:
            state.override_pending = True        # 도징량이 바뀌었을 수 있다 → 즉시 반영
        return _send_json(conn, {"ok": ok, "msg": msg}, "200 OK" if ok else "400 Bad Request")
    return _send_json(conn, {"err": "not found"}, "404 Not Found")


def _api(conn, method, path, body, query=""):
    if path.startswith("/api/ops/"):
        return _ops_api(conn, method, path, body, query)
    if path in ("/api/backup", "/api/files", "/api/restore"):
        return _backup_api(conn, method, path, body)
    if path.startswith("/api/wifi"):
        return _wifi_api(conn, method, path, body)
    d = config.DATA_DIR
    if path == "/api/dkh":                       # dkh_server.py 동일 — 실패 시 0.0
        # ★반드시 파서 경유(2026-08-19 수정): 종전에는 lines[-1][4] 로 위치 인덱싱을 했는데,
        #   날짜 컬럼(2026-08-16)이 붙은 줄에서 그 자리는 tank_kh 가 아니라 **ref_kh** 다
        #   → 8.83(기준) 을 수조 dKH 로 응답하고 있었다. 원본 dkh_server.read_last_dkh 는
        #   row["tank_kh"] 를 돌려준다. 0.0=에러·음수=미평탄 표식은 원본대로 그대로 통과시킨다.
        row = datalog.last_dat_row()
        return _send_json(conn, {"dkh": row["tank_kh"] if row else 0.0})

    if path == "/api/override":
        if method == "GET":
            return _send_json(conn, _read_json_file(d + "/doser_override.json") or {})
        ml = body.get("ml_day")
        # 대시보드 규칙: 0(정지) 또는 1.5~18mL/일 — 방어적으로 서버에서도 검증
        if not isinstance(ml, (int, float)) or not (ml == 0 or 1.5 <= ml <= 18):
            return _send_json(conn, {"ok": False, "err": "0 또는 1.5~18mL/일"}, "400 Bad Request")
        ov = {"ml_day": ml, "id": rwtime.iso_id()}
        _write_json_file(d + "/doser_override.json", ov)
        state.override_pending = True            # 메인 루프가 즉시 적용(측정 중이면 종료 후)
        return _send_json(conn, {"ok": True, "id": ov["id"]})

    if path == "/api/override/state":
        return _send_json(conn, _read_json_file(d + "/doser_override_state.json") or {})

    if path == "/api/config":
        if method == "GET":
            return _send_json(conn, _read_json_file(d + "/doser_config.json")
                              or {"target_dkh": config.TARGET_DKH})
        t = body.get("target_dkh")
        if not isinstance(t, (int, float)) or not (config.TARGET_LO <= t <= config.TARGET_HI):
            return _send_json(conn, {"ok": False, "err": "목표 %.1f~%.1f dKH"
                                     % (config.TARGET_LO, config.TARGET_HI)}, "400 Bad Request")
        _write_json_file(d + "/doser_config.json", {"target_dkh": t})
        return _send_json(conn, {"ok": True})

    if path == "/api/bt":
        # ★BT 접속 정보(BIND 주소) — 웹 설정이 config.py 값보다 우선한다(link.bind_addr).
        #   실장 전 필수 작업인데 종전에는 소스를 고쳐 다시 올려야 했다.
        if method == "GET":
            return _send_json(conn, link.status()["targets"])
        ok, msg = link.set_binds(body, log=datalog.log)
        return _send_json(conn, {"ok": ok, "msg": msg, "targets": link.status()["targets"]},
                          "200 OK" if ok else "400 Bad Request")

    if path == "/api/ph_cal":
        if method == "GET":
            return _send_json(conn, _read_json_file(d + "/ph_cal.json") or {})
        if not isinstance(body, dict) or "offset" not in body:
            return _send_json(conn, {"ok": False, "err": "offset 필요"}, "400 Bad Request")
        _write_json_file(d + "/ph_cal.json", body)
        return _send_json(conn, {"ok": True})

    _send_json(conn, {"err": "not found"}, "404 Not Found")


def _static(conn, path):
    name = path.lstrip("/")
    if not name:
        name = "index.html"
    if ".." in name:
        return _send_json(conn, {"err": "bad path"}, "400 Bad Request")
    if name.startswith("data/"):                 # 아카이브·로그 원본 다운로드(LAN 전용)
        fp = config.DATA_DIR + "/" + name[5:]
        if _exists(fp):
            return _send_file(conn, fp)
        body = b"Not Found"
        _send_head(conn, "404 Not Found", "text/plain", len(body))
        return conn.send(body)
    base = name.rsplit("/", 1)[-1]
    if base in JSONL_AS_ARRAY:                   # plateau 이력 — JSONL → 배열 스트리밍
        return _send_jsonl_array(conn, JSONL_AS_ARRAY[base])
    if base in DATA_FILES:                       # 대시보드의 상대경로 fetch → /data
        fp = config.DATA_DIR + "/" + base
        if _exists(fp):
            return _send_file(conn, fp)
        # 데이터가 아직 없으면 빈 구조 반환(대시보드 fetch 가 죽지 않게)
        empty = "[]" if base.endswith(("history.json", "series.json")) else "{}"
        _send_head(conn, "200 OK", "application/json", len(empty), "Cache-Control: no-store\r\n")
        return conn.send(empty.encode())
    fp = config.WWW_DIR + "/" + name
    if _exists(fp + ".gz"):
        return _send_file(conn, fp + ".gz", gz=True, cache=(name != "index.html"))
    if _exists(fp):
        return _send_file(conn, fp, cache=name.startswith("vendor/") or name.startswith("icons/"))
    body = b"Not Found"
    _send_head(conn, "404 Not Found", "text/plain", len(body))
    conn.send(body)


def _handle(conn):
    conn.settimeout(10)
    rf = conn.makefile("rb")
    req = rf.readline().decode()
    parts = req.split()
    if len(parts) < 2:
        return
    method, raw_path = parts[0], parts[1]
    path, _, query = raw_path.partition("?")
    clen = 0
    while True:                                  # 헤더 소비(Content-Length 만 필요)
        ln = rf.readline()
        if not ln or ln in (b"\r\n", b"\n"):
            break
        if ln.lower().startswith(b"content-length:"):
            try:
                clen = int(ln.split(b":", 1)[1].strip())
            except ValueError:
                clen = 0
    body = {}
    if method == "POST" and clen:
        raw = rf.read(min(clen, config.HTTP_MAX_BODY))
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
    if path.startswith("/api/"):
        _api(conn, method, path, body, query)
    elif method == "GET":
        _static(conn, path)
    else:
        _send_json(conn, {"err": "method"}, "405 Method Not Allowed")


def _serve():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", config.HTTP_PORT))
    s.listen(4)
    print("[web] listening on :%d" % config.HTTP_PORT)
    while True:
        try:
            conn, _addr = s.accept()
            try:
                _handle(conn)
            finally:
                conn.close()
        except Exception as e:
            print("[web] request error: %r" % e)   # 서버는 어떤 요청 오류에도 계속


def start():
    _thread.start_new_thread(_serve, ())
