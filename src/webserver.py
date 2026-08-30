# 로컬 웹서버 — dkh_server.py 대체 + 대시보드 정적 서빙 + 설정 API.
# 원본 dkh_server 는 /api/dkh 하나였고 설정 저장은 대시보드→GitHub API 커밋이었다.
# ESP32 는 데이터 주인과 서버가 같은 기기이므로 설정도 로컬 POST 로 받는다(토큰 불요):
#   GET  /api/dkh              → {"dkh": <마지막 tank_kh>}   (dkh_server 동일)
#   GET  /api/version          → 장비명·펌웨어 판·빌드 스탬프(version.info) — 버전 조회
#   GET/POST /api/override     → doser_override.json  (POST 시 id 자동 부여, 즉시 적용 이벤트)
#   GET  /api/override/state   → doser_override_state.json (적용 여부 표시용)
#   GET/POST /api/config       → doser_config.json {target_dkh}
#   GET/POST /api/ph_cal       → ph_cal.json (표시 전용 한나 보정)
#   GET/POST /api/devices      → 장치 목록(BT 주소·이름·시계 동기 시각). config 보다 우선
#   GET/POST /api/schedule     → 측정 회차·도저 조정 회차. config 보다 우선
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
import select
import time
import _thread

import archive
import config
import datalog
import devices
import link
import ops
import rwtime
import schedule
import state
import version
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


def _send_json_lines(conn, lines, extra=""):
    """{"lines":[...]} 를 **줄 단위로 흘려보낸다** — 큰 로그를 한 문자열로 만들지 않는다.
    extra 는 배열 뒤에 붙일 필드 문자열(예: ',"off":123,"reset":false').

    ★_send_json 은 body 전체를 만들어 `conn.send(body)` 한 번에 보낸다(2026-08-30 기준).
      수백 KB 를 그렇게 보내면 소켓이 부분 전송을 하고도 그 사실이 무시돼 **응답이 잘린다**
      (_send_file 이 1KB 씩 끊어 보내는 것과 같은 이유). 로그는 5000줄까지 허용하므로 여기만
      스트리밍으로 뺀다. Content-Length 를 미리 못 구하니 연결 종료로 끝을 알린다
      (_send_jsonl_array 와 같은 규약)."""
    conn.send(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
              b"Cache-Control: no-store\r\nConnection: close\r\n\r\n{\"lines\":[")
    first = True
    for ln in lines:
        chunk = json.dumps(ln).encode()
        conn.send(chunk if first else b"," + chunk)
        first = False
    conn.send(b"]" + extra.encode() + b"}")


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
        # since=<바이트위치> 가 오면 **그 뒤에 붙은 줄만** 보낸다(tail -f). 없으면 마지막 n줄.
        n, since = 40, None
        for kv in query.split("&"):
            try:
                if kv.startswith("n="):
                    # ★상한 300→5000(2026-08-30): 300 줄로는 **측정 1회를 통째로 볼 수 없었다**
                    #   (판독 1회당 10줄 안팎 × 수십~수백 판독). 큰 응답은 아래에서 줄 단위로
                    #   흘려보내므로 한 번에 만드는 문자열이 커지지 않는다.
                    n = max(1, min(5000, int(kv[2:])))
                elif kv.startswith("since="):
                    since = max(0, int(kv[6:]))
            except ValueError:
                pass
        if since is not None:
            lines, off, reset = ops.log_since(since)
            if not reset:
                return _send_json_lines(conn, lines, ',"off":%d,"reset":false' % off)
            # 회전했다 — 이어 붙일 수 없으니 전체를 다시 준다(아래 전체 경로와 같은 규칙)
        # ★위치를 **먼저** 읽는다: 뒤에 읽으면 그 사이 쌓인 줄이 이번 응답에도 없고 다음
        #   증분에도 안 잡혀 **조용히 사라진다**. 먼저 읽으면 최악이 한 줄 중복이다.
        off = ops.log_offset()
        return _send_json_lines(conn, ops.log_tail(n),
                                ',"off":%d,"reset":%s' % (off, "true" if since is not None else "false"))

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

    if path == "/api/ops/hold":
        # 측정 보류(정비 래치) — {"clear":true} 로 해제, {"hours":12|null,"reason":"…"} 로 설정.
        # ★UART 를 만지지 않으므로 job 큐를 거치지 않는다(파일 한 개 쓰기). 측정 중에도
        #   걸 수 있다 — "지금 회차가 끝나면 그 다음부터 멈춰라"가 자연스러운 요구다.
        if body.get("clear"):
            was = schedule.clear_hold()
            if was is None:
                return _send_json(conn, {"ok": False, "msg": "보류 상태가 아닙니다"})
            datalog.log("[보류] 운영자 해제 — 시작 %s / 사유: %s"
                        % (was.get("since"), was.get("reason") or "없음"))
            return _send_json(conn, {"ok": True, "msg": "측정 보류 해제 — 다음 정시 회차부터 측정합니다",
                                     "hold": schedule.hold_status()})
        ok, msg = schedule.set_hold(body.get("hours"), body.get("reason") or "")
        if ok:
            datalog.log("[보류] 측정 보류 시작 — %s (사유: %s)"
                        % (msg, body.get("reason") or "없음"))
        return _send_json(conn, {"ok": ok, "msg": msg, "hold": schedule.hold_status()})

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
        # ★본문을 다시 파싱하지 않는다(2026-08-24 수정): `_handle` 이 이미 dict 으로 파싱해
        #   넘기므로 종전 `json.loads(body)` 는 dict 을 넣는 셈이었다 — 그건 ValueError 가
        #   아니라 **TypeError** 라 아래 except 가 못 잡고, 요청이 응답 없이 끊겼다
        #   (정비페이지 '설정 복원' 버튼이 항상 네트워크 오류로 보였다).
        #   빈 dict = 파싱 실패 또는 본문 상한 초과(_handle 이 잘라 담는다) → 사실을 알린다.
        #   번들 형식(kind) 검사는 archive.restore 한 곳에서만 한다.
        if not isinstance(body, dict) or not body:
            return _send_json(conn, {"ok": False,
                                     "msg": "본문 파싱 실패 — JSON 형식과 크기(최대 %dKB) 확인"
                                            % (config.HTTP_MAX_BODY // 1024)},
                              "400 Bad Request")
        ok, msg = archive.restore(body)
        if ok:
            state.override_pending = True        # 도징량이 바뀌었을 수 있다 → 즉시 반영
            # ★파일을 밖에서 갈아치웠으므로 캐시를 버린다 — 안 버리면 복원 전 회차·주소로
            #   계속 돈다(백업에서 되살린 값이 다음 재부팅까지 안 먹는다).
            schedule.invalidate()
            devices.reload()
            link.refresh_targets()
        return _send_json(conn, {"ok": ok, "msg": msg}, "200 OK" if ok else "400 Bad Request")
    return _send_json(conn, {"err": "not found"}, "404 Not Found")


def _api(conn, method, path, body, query=""):
    if path.startswith("/api/ops/"):
        return _ops_api(conn, method, path, body, query)
    if path in ("/api/backup", "/api/files", "/api/restore"):
        return _backup_api(conn, method, path, body)
    if path.startswith("/api/wifi"):
        return _wifi_api(conn, method, path, body)
    if path == "/api/version":
        # ★버전 조회 — 기기에 올라간 판을 사람이 확인하는 유일한 경로(화면 없는 장비).
        #   GET 전용이다: 버전은 배포로만 바뀐다(웹에서 고칠 수 있으면 표시가 거짓이 된다).
        return _send_json(conn, version.info())

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
        # ★저장된 값을 되돌려 준다 — 화면이 "무엇이 저장됐는지"를 사용자에게 그대로 보여 준다.
        return _send_json(conn, {"ok": True, "id": ov["id"], "ml_day": ml})

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

    if path == "/api/devices":
        # ★장치 목록(BT 주소·이름·시계 동기 시각) — 웹 설정이 config.py 값보다 우선한다.
        #   실장 전 필수 작업인데 2026-08-19 전에는 소스를 고쳐 다시 올려야 했다.
        #   2026-08-21: 종전 /api/bt(두 칸 구조)를 대체한다 — 도징기가 여러 대일 수 있다.
        if method == "GET":
            st = link.status()
            return _send_json(conn, {"devices": devices.all_devices(),
                                     "targets": st["targets"], "ids": st["ids"],
                                     "doser_max": config.DOSER_MAX,
                                     "sync_max": config.DOSER_SYNC_MAX})
        ok, msg = devices.set_devices(body, log=datalog.log)
        if ok:
            link.refresh_targets()       # 다음 전환부터 새 목록·주소로 붙는다
        st = link.status()
        return _send_json(conn, {"ok": ok, "msg": msg, "devices": devices.all_devices(),
                                 "targets": st["targets"], "ids": st["ids"]},
                          "200 OK" if ok else "400 Bad Request")

    if path == "/api/schedule":
        # ★측정 회차·도저 조정 회차 — `/data/schedule.json` 이 config 값보다 우선한다.
        #   검증(최소 간격 2h·조정 회차 포함 여부)은 schedule.py 한 곳에서만 한다.
        if method == "GET":
            return _send_json(conn, {"measure_hours": schedule.measure_hours(),
                                     "doser_slot_hour": schedule.doser_slot_hour(),
                                     "min_gap_h": config.MEASURE_MIN_GAP_H,
                                     "hours_max": config.MEASURE_HOURS_MAX,
                                     "source": schedule.source()})
        ok, msg, warn = schedule.set_schedule(body)
        if ok:
            datalog.log("[조치] %s%s" % (msg, (" | 경고: " + warn) if warn else ""))
        return _send_json(conn, {"ok": ok, "msg": msg, "warn": warn,
                                 "measure_hours": schedule.measure_hours(),
                                 "doser_slot_hour": schedule.doser_slot_hour()},
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


_hb = None            # 마지막 accept 루프 하트비트(rwtime.mono_ms) — 메인 루프 생존 관측용
_started = False       # start() 중복 호출 방지(이중 스레드 → 포트 이중 바인드 방지)


def _serve():
    """★자가 치유 리스너(2026-08-26): 종전에는 소켓을 한 번 만들고 accept 오류를 무한
    루프에서 로그만 찍었다 — WiFi 재접속으로 리스닝 소켓이 죽으면 재부팅 전까지 대시보드가
    불통이고, 오류가 즉시 반복되면 스핀·로그 폭주가 났다. 이제 소켓이 죽으면 백오프 후 다시
    만들고, 유휴 중에도 poll 타임아웃으로 깨어 하트비트를 갱신한다(메인 루프가 생존을 관측)."""
    global _hb
    while True:
        s = None
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", config.HTTP_PORT))
            s.listen(4)
            poller = select.poll()
            poller.register(s, select.POLLIN)
            print("[web] listening on :%d" % config.HTTP_PORT)
            while True:
                _hb = rwtime.mono_ms()
                events = poller.poll(config.WEB_POLL_MS)   # 유휴여도 주기적으로 깨어 하트비트 갱신
                if not events:
                    continue                               # 타임아웃 — 대기 중(정상)
                if events[0][1] & (select.POLLERR | select.POLLHUP):
                    raise OSError("listen socket error")   # 소켓 사망 → 바깥에서 재생성
                conn, _addr = s.accept()
                try:
                    _handle(conn)
                except Exception as e:
                    # ★응답을 보내고 닫는다(2026-08-29): 종전에는 연결만 닫아서 브라우저가
                    #   ERR_EMPTY_RESPONSE 를 보고 화면이 조용히 비었다 — 원인을 알 길이 없었다.
                    #   500 과 예외 문구를 돌려주면 무엇이 터졌는지 화면·로그에 남는다.
                    print("[web] request error: %r" % e)   # 요청 오류엔 계속(연결만 닫는다)
                    try:
                        _send_json(conn, {"err": "서버 오류: %r" % e},
                                   "500 Internal Server Error")
                    except Exception:
                        pass
                finally:
                    conn.close()
        except Exception as e:
            print("[web] listener 재생성(%r)" % e)
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass
            time.sleep(config.WEB_RELISTEN_S)              # 백오프 — 스핀·로그 폭주 방지


def alive_age():
    """마지막 하트비트 이후 경과 초(없으면 None) — 메인 루프가 리스너 정지를 관측한다."""
    return rwtime.elapsed_s(_hb) if _hb is not None else None


def start():
    global _started
    if _started:                 # 이미 떠 있으면 재호출은 무시(포트 이중 바인드 방지)
        return
    _started = True
    _thread.start_new_thread(_serve, ())
