#!/usr/bin/env python3
"""개발용 스텁 서버 — ESP32 없이 대시보드·정비 페이지를 검증한다.

src/webserver.py 와 **같은 경로·같은 응답 형태**를 CPython 으로 흉내낸다:
정적(www/) + 데이터(data/) + /api/* + /api/ops/* + /api/wifi.
장비 조작은 실제로 하지 않고 그럴듯한 결과만 돌려준다(계약 검증용).

사용:
    python3 tools/devserver.py            # http://localhost:8080
    python3 tools/devserver.py --seed     # 저장소 docs/ 실물 JSON 을 data/ 로 내려받고 시작

주의: 이 파일은 ESP32 에 올라가지 않는다(개발 도구).
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))     # dkh_dat — 파싱 규약을 기기와 공유
import dkh_dat                                    # noqa: E402
WWW = os.path.join(ROOT, "www")
DATA = os.path.join(ROOT, "data")

# ★스케줄·장치 레지스트리는 **기기 코드를 그대로 import 해서** 쓴다(2026-08-21):
#   검증 규칙(최소 간격 2h, 도저 상한, 주소 정규화)을 스텁에 베껴 두면 둘이 갈라진다.
#   두 모듈은 config 만 의존하므로(machine 불요) CPython 에서 그대로 돈다.
#   파일 경로가 import 시점에 config.DATA_DIR 로 굳으므로 그 전에 갈아 끼운다.
import config as _cfg                              # noqa: E402
_cfg.DATA_DIR = DATA
# 스텁 기본 주소 — data/devices.json 이 없을 때의 폴백이 '미설정'이면 화면이 온통 경고가 된다
_cfg.BIND_ADDR_MEAS, _cfg.BIND_ADDR_DOSER = "98da,60,0fc57a", "98da,60,056895"
import devices as _devices                         # noqa: E402
import schedule as _schedule                       # noqa: E402
# ★백업·복원도 **기기 코드를 그대로 import 한다**(2026-08-24): 종전에는 복원 검증과 번들
#   조립을 이 파일에 베껴 뒀는데, 그래서 기기 쪽 `/api/restore` 가 본문을 두 번 파싱해
#   죽는 결함을 스텁이 잡아내지 못했다(스텁만 옳게 동작했다). 사본을 없애면 갈라지지 않는다.
#   archive 도 config 만 의존한다(machine 불요) — 단, 경로가 import 시점에 굳으므로
#   ARCHIVE_DIR 을 저장소 data/archive 로 먼저 돌려놓는다.
_cfg.ARCHIVE_DIR = os.path.join(DATA, "archive")
import archive as _archive                         # noqa: E402
# 버전도 기기 코드를 그대로 쓴다 — 스텁이 문자열을 베껴 두면 화면 검증이 거짓이 된다.
# (machine 이 없으면 serial 이 'SIM' 이 되어 PC 에서 돌린 스텁임이 화면에 드러난다.)
import version as _version                         # noqa: E402

# ★작업 종류 목록은 **기기 ops.py 에서 읽어 온다**(2026-08-29): ops 는 machine 을 쓰므로
#   CPython 에서 import 할 수 없어 그 상수만 소스에서 꺼낸다. 여기에 베껴 두면 갈라지고,
#   실제로 스텁이 아무 kind 나 받아 주는 바람에 tools/ui_check.mjs 의 [D] 가 "서버가 모르는
#   작업을 거부한다" 를 실기에서만 통과하고 스텁에서는 늘 FAIL 로 찍혔다.
def _job_kinds():
    import ast
    src = open(os.path.join(ROOT, "src", "ops.py"), encoding="utf-8").read()
    for node in ast.parse(src).body:
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", None) == "JOB_KINDS"):
            return tuple(ast.literal_eval(node.value))
    raise RuntimeError("src/ops.py 에서 JOB_KINDS 를 찾지 못했다")


JOB_KINDS = _job_kinds()

RAW = "https://raw.githubusercontent.com/taeseokyi/reefwiz/master/docs/"
SEED_FILES = ("dkh_latest.json", "dkh_series.json", "dkh_plateau_history.json",
              "doser_history.json", "doser_override.json", "doser_config.json", "ph_cal.json")

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
        ".json": "application/json", ".png": "image/png", ".css": "text/css",
        ".webmanifest": "application/manifest+json", ".dat": "text/plain",
        ".ico": "image/x-icon", ".svg": "image/svg+xml"}

# ESP32 는 plateau 를 JSONL 로 저장하고 서버가 배열로 조립한다 — 여기서도 동일하게 흉내낸다.
PLATEAU_JSONL = os.path.join(DATA, "plateau.jsonl")

_state = {"measuring": False, "job_result": None, "abort": False,
          "liquid": {"chamber": "KCL", "holding": "EMPTY"},
          # HC-05 1개 — 스텁도 '지금 붙어 있는 대상'을 들고 있어야 콘솔·도징 잠금을 재현한다
          # (주소·이름·동기 시각은 devices 모듈이 data/devices.json 에 들고 있다)
          "bt_target": "meas"}


def _targets():
    """기기 link.status()['targets'] 와 같은 형태 — 장치 레지스트리에서 만든다."""
    out = {}
    for d in _devices.all_devices():
        out[d["id"]] = {"name": d["name"], "kind": d["kind"],
                        "addr_set": bool(d["addr"]), "addr": d["addr"],
                        "source": _devices.source() if d["addr"] else "none",
                        "sync_hours": list(d.get("sync_hours") or ()),
                        "primary": _devices.is_primary_doser(d["id"])}
    return out


def _dev_ver():
    """기기 link.dev_ver 와 같은 형태 — 종류별로 규약 한 줄을 흉내낸다.
    ★'#' 는 **빌드 커밋**이라 세 장비가 같은 값을 낸다(한 저장소에서 같이 빌드된다) —
      실기 실측과 같은 모습이다. 개체가 겹친 것이 아니다."""
    at = time.strftime("%Y-%m-%d %H:%M:%S")
    out = {}
    for i, d in enumerate(_devices.all_devices()):
        if d["kind"] == "meas":
            line = "ReefWiz Meter M-1 v1.0.0 #55DAFC"
        elif i == 1:                       # 첫 도저 = 기본 도저
            line = "ReefWiz Doser D-1 v1.0.0 #55DAFC"
        else:                              # 그다음 = 에어 분배기(도저 펌웨어 파생)
            line = "ReefWiz Air A-1 v1.0.0 #55DAFC"
        info = _version.parse_ver([line]) if line else None
        out[d["id"]] = dict(info or {"ver": None, "model": None, "version": None,
                                     "serial": None}, at=at)
    return out


def _target_ids():
    return [d["id"] for d in _devices.all_devices()]


def seed():
    import urllib.request
    os.makedirs(DATA, exist_ok=True)
    for name in SEED_FILES:
        try:
            with urllib.request.urlopen(RAW + name, timeout=20) as r:
                body = r.read()
            with open(os.path.join(DATA, name), "wb") as f:
                f.write(body)
            print("seeded", name, len(body), "B")
        except Exception as e:
            print("seed 실패", name, e)
    # plateau 배열 → JSONL 변환(ESP32 저장 형식)
    src = os.path.join(DATA, "dkh_plateau_history.json")
    if os.path.exists(src):
        with open(src, encoding="utf-8") as f:
            runs = json.load(f)
        with open(PLATEAU_JSONL, "w", encoding="utf-8") as f:
            for r in runs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.remove(src)
        print("plateau.jsonl:", len(runs), "runs (배열 파일은 제거 — 서버가 조립)")
    # dkh.dat 이 없으면 series 로부터 대충 만든다(도저 계산·에러 래치 확인용)
    dat = os.path.join(DATA, "dkh.dat")
    ser = os.path.join(DATA, "dkh_series.json")
    if not os.path.exists(dat) and os.path.exists(ser):
        with open(ser, encoding="utf-8") as f:
            rows = json.load(f)
        with open(dat, "w") as f:
            for r in rows:
                kh = r["tank_kh"] * (1 if r.get("is_flat", True) else -1)
                f.write("%02d %.3f %.3f %.3f %.3f %.1f\n"
                        % (r["hh"], 0.0, 0.0, r["ref_kh"], kh, r["temp"]))
        print("dkh.dat:", len(rows), "rows")


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _dat_lines():
    try:
        with open(os.path.join(DATA, "dkh.dat"), encoding="utf-8") as f:
            return [ln.split() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return []


def _last_plateau():
    try:
        last = None
        with open(PLATEAU_JSONL, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    last = ln
        return json.loads(last) if last else {}
    except (OSError, ValueError):
        return {}


_fake_log_cache = None


def _fake_log(n):
    """plateau 실데이터로 measure_kh.log 형식을 합성 — 로그 카드 UI 검증용(장치 로그 모사)."""
    global _fake_log_cache
    if _fake_log_cache is None:
        lines = []
        try:
            with open(PLATEAU_JSONL, encoding="utf-8") as f:
                runs = [json.loads(ln) for ln in f if ln.strip()]
        except (OSError, ValueError):
            runs = []
        for run in runs[-4:]:
            lines.append("")
            lines.append("===== ReefWiz KH 측정 V4 (ESP32) %s [%s] ====="
                         % (run.get("run_started"), run.get("mode")))
            for phase in ("tank", "ref"):
                for r in run.get(phase) or []:
                    lines.append("    [%s] %d회 pH:%.3f (%ds)"
                                 % (phase, r["n"], r["ph"], r["elapsed"]))
                fn = run.get(phase + "_flat_n")
                if fn:
                    lines.append("    [평탄] %s %d회 → 평형" % (phase, fn))
            lines.append("[LOG] dkh.dat <- (기록됨)" if run.get("completed")
                         else "**[경고] 미완료 런 — 로그 확인")
        _fake_log_cache = lines or ["(plateau 데이터 없음 — 스텁 로그)"]
    return (_fake_log_cache + _fake_live())[-n:] if n else _fake_log_cache + _fake_live()


_STARTED = time.time()

def _fake_live():
    """★스텁 로그도 시간이 지나면 자란다(2026-08-30) — 실기 없이 '자동 갱신=새 줄만 이어붙이기'
    를 확인하려면 로그가 실제로 늘어나야 한다. 5초마다 한 줄씩 늘어난 것처럼 계산해 만든다
    (핸들러에 부작용을 두지 않으려고 저장하지 않고 매번 시각으로 유도한다)."""
    ticks = int((time.time() - _STARTED) // 5)
    return ["    [stub] %s 하트비트 %d — 자동 갱신(tail -f) 확인용"
            % (time.strftime("%H:%M:%S", time.localtime(_STARTED + i * 5)), i)
            for i in range(1, ticks + 1)]


def _fake_log_slice(n, since):
    """기기(webserver.py + ops.log_since)와 **같은 계약**을 흉내낸다 — (lines, off, reset).
    스텁에는 파일이 없으므로 합성 로그 텍스트의 바이트 길이를 오프셋으로 쓴다."""
    all_lines = _fake_log(None)
    ends, tot = [], 0
    for ln in all_lines:                       # 각 줄이 끝나는 바이트 위치
        tot += len(ln.encode("utf-8")) + 1
        ends.append(tot)
    if since is None:
        return all_lines[-n:], tot, False
    if since > tot:                            # 회전 상당 — 이어 붙일 수 없다
        return all_lines[-n:], tot, True
    fresh = [ln for ln, e in zip(all_lines, ends) if e > since]
    return fresh, tot, False


def _device_state(latch):
    """기기 ops.device_state() 의 스텁 판정 — 우선순위·문구를 같은 규칙으로 흉내낸다."""
    liq = _state["liquid"]
    if _state["measuring"]:
        return {"state": "measuring", "label": "측정 중",
                "detail": "측정 회차가 진행 중입니다(수 시간). 중단하려면 '측정 중단'.",
                "console_allowed": False, "console_override": False,
                "console_reason": "임의 명령이 측정을 망칩니다 — '측정 중단' 후 사용하세요"}
    if latch:
        return {"state": "latched", "label": "에러 래치 — 측정 정지됨",
                "detail": "마지막 회차가 에러로 끝났습니다. 정시 측정은 래치를 풀 때까지 건너뜁니다.",
                "console_allowed": False, "console_override": True,
                "console_reason": "수동 정리 목적이면 아래 잠금을 해제하세요"}
    if "UNKNOWN" in (liq.get("chamber"), liq.get("holding")):
        return {"state": "liquid_unknown", "label": "액체 위치 불명 — 정리 동결",
                "detail": "이송 도중 중단돼 위치를 모릅니다. 실물 확인 후 '액체 위치 수동 지정'.",
                "console_allowed": False, "console_override": True,
                "console_reason": "수동 정리 목적이면 아래 잠금을 해제하세요"}
    hold = _schedule.hold_status()      # 실제 schedule 모듈을 그대로 쓴다(스텁 아님)
    if hold.get("active"):
        return {"state": "hold", "label": "측정 보류 중 — 정시 측정 멈춤",
                "detail": "정시 측정을 의도적으로 멈춰 두었습니다(%s). 해제: %s. "
                          "수동 '지금 측정'은 그대로 됩니다."
                          % (hold.get("reason") or "사유 없음", hold.get("until") or "수동 해제 전까지"),
                "console_allowed": True, "console_override": False,
                # ★사유를 비워 두면 화면이 "대기(Idle) 상태 — 콘솔 사용 가능"으로 폴백해
                #   배너의 '측정 보류 중'과 서로 다른 말을 한다(래치 때 겪은 것과 같은 함정).
                "console_reason": "측정 보류 중 — 콘솔은 그대로 쓸 수 있습니다(정비가 곧 콘솔 작업)"}
    return {"state": "idle", "label": "대기 (Idle)",
            "detail": "정상 대기 상태입니다. 다음 정시 회차를 기다립니다.",
            "console_allowed": True, "console_override": False, "console_reason": ""}


def _snapshot():
    latest = _read(os.path.join(DATA, "dkh_latest.json"), {}) or {}
    run = _last_plateau()
    hist = _read(os.path.join(DATA, "doser_history.json"), []) or []
    last_dose = hist[-1] if hist else {}
    lines = _dat_lines()
    # ★파서 경유 — 종전 lines[-1][1:6] 위치 읽기는 날짜 컬럼이 붙으면 한 칸씩 밀려
    #   temp 대신 tank_kh 까지만 검사했다(기기 쪽 datalog 와 같은 수정).
    _last = dkh_dat.parse_parts(lines[-1]) if lines else None
    latch = bool(_last and _last["is_error"])
    return {
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": _version.brief(),          # 기기 ops.snapshot 과 같은 자리·같은 형태
        # 기기 ops.device_state() 와 같은 형태 — 정비페이지 상태 배너·콘솔 잠금이 이걸 본다.
        "device": _device_state(latch),
        "measuring": _state["measuring"], "abort_requested": _state["abort"],
        "job_busy": False, "job_pending": None, "job_result": _state["job_result"],
        "error_latch": latch, "liquid": _state["liquid"],
        "dat_rows": len(lines), "last_dat": " ".join(lines[-1]) if lines else None,
        "latest": latest,
        "last_run": {"run_started": run.get("run_started"), "mode": run.get("mode"),
                     "completed": run.get("completed"), "tank_flat_n": run.get("tank_flat_n"),
                     "ref_flat_n": run.get("ref_flat_n"),
                     "tank_reads": len(run.get("tank") or []),
                     "ref_reads": len(run.get("ref") or []),
                     "co2_suspect": run.get("co2_suspect"),
                     "ref_net_mph": run.get("ref_net_mph")},
        "doser": {"ts": last_dose.get("ts"), "mode": last_dose.get("mode"),
                  "lrt_new": last_dose.get("lrt_new"), "applied": last_dose.get("applied"),
                  "ml_day_new": last_dose.get("ml_day_new"), "note": last_dose.get("note"),
                  "auto_apply": False},
        # 스케줄은 기기와 같은 모듈이 계산한다(파일 우선, config 폴백)
        "schedule": {"hold": _schedule.hold_status(),
                     "hours": _schedule.measure_hours(),
                     "doser_slot": _schedule.doser_slot_hour(),
                     "next_hour": _schedule.next_hour(time.localtime()),
                     "min_gap_h": _cfg.MEASURE_MIN_GAP_H,
                     "hours_max": _cfg.MEASURE_HOURS_MAX,
                     "doser_max": _cfg.DOSER_MAX,
                     "sync_max": _cfg.DOSER_SYNC_MAX,
                     "source": _schedule.source()},
        "wifi": {"connected": True, "ip": "127.0.0.1", "saved_ssid": "dev-stub", "rssi": -55,
                 "ap_active": False, "ap_ssid": "reefwiz-setup", "ap_pass": "reefwiz1234",
                 "ap_ip": "192.168.4.1"},
        # HC-05 1개 구성 — 스텁은 '측정 장비에 붙어 있고 신원 확인됨' 상태로 둔다.
        # 기기 link.status() 와 같은 형태(정비페이지 BT 카드가 이 키들을 그린다).
        "link": {"target": _state["bt_target"],
                 "target_name": _targets().get(_state["bt_target"], {}).get("name", "미확정"),
                 "frozen": None, "verified": bool(_state["bt_target"]),
                 "motor_running": None, "state_pin": None,
                 "last_ok_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "last_event": {"kind": "switch_ok", "detail": "attempt=1",
                                "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                 "switch_locked": bool(_state.get("measuring")),
                 # 상대 펌웨어의 판 — 기기 link.status()['dev_ver'] 와 같은 형태.
                 # 장비 3종이 각자 자기 이름을 낸다 — 화면이 종류를 갈라 보여 주는지가
                 # 검증 대상이다(개체 #는 장비마다 달라야 한다 — README '실장 확인' 참조).
                 "dev_ver": _dev_ver(),
                 "targets": _targets(), "ids": _target_ids()},
        # 장기 저장소(SD 대체) — 스텁은 data/archive 실물을 그대로 센다.
        "archive": _archive.status(),
        "heap_free": 71234,
    }


ARCHIVE = _cfg.ARCHIVE_DIR
CONFIG_FILES = _archive.CONFIG_FILES            # 사본 금지 — 기기와 같은 목록을 그대로 쓴다


def _archive_files():
    """/api/files — /data 와 /data/archive 의 파일 목록(기기와 같은 상대경로 규약)."""
    out = []
    for root, rel in ((DATA, ""), (ARCHIVE, "archive")):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            fp = os.path.join(root, name)
            if os.path.isfile(fp):
                out.append({"path": (rel + "/" + name) if rel else name,
                            "bytes": os.path.getsize(fp)})
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s\n" % (fmt % a))

    # ── 응답 헬퍼 ──

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path):
        ext = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _jsonl_array(self, path):
        """ESP32 webserver._send_jsonl_array 와 같은 동작 — 줄을 배열로 조립."""
        chunks = [b"["]
        first = True
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    chunks.append(ln.encode() if first else b"," + ln.encode())
                    first = False
        except OSError:
            pass
        chunks.append(b"]")
        body = b"".join(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}
        except (ValueError, TypeError):
            return {}

    # ── 라우팅 ──

    def do_GET(self):
        path = self.path.split("?")[0]
        query = self.path.split("?")[1] if "?" in self.path else ""
        if path.startswith("/api/"):
            return self._api_get(path, query)
        name = path.lstrip("/") or "index.html"
        if name.startswith("data/"):               # 아카이브·로그 원본(기기와 동일 경로)
            fp = os.path.join(DATA, *name[5:].split("/"))
            if os.path.isfile(fp):
                return self._file(fp)
            return self.send_error(404)
        base = os.path.basename(name)
        if base == "dkh_plateau_history.json":
            return self._jsonl_array(PLATEAU_JSONL)
        for d in (DATA, WWW):
            fp = os.path.join(d, base if d == DATA else name)
            if os.path.isfile(fp):
                return self._file(fp)
        if base.endswith(".json"):                 # 데이터 미존재 → 빈 구조(ESP32 동일)
            return self._json([] if base.endswith(("history.json", "series.json")) else {})
        self.send_error(404)

    def _api_get(self, path, query):
        if path == "/api/backup":
            body = json.dumps(_archive.bundle()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition",
                             'attachment; filename="reefwiz-backup.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/ver":
            # 기기 webserver.py 와 같은 응답 — 한 줄 text/plain(파싱 없이 그대로 쓴다)
            body = (_version.ver() + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/version":
            return self._json(_version.info())
        if path == "/api/files":
            return self._json({"dir": "/data", "files": _archive_files(),
                               "archive": _archive.status()})
        if path == "/api/dkh":
            # ★기기(webserver.py)와 동일 — 위치 인덱싱 금지, 파서가 집은 tank_kh 를 돌려준다
            lines = _dat_lines()
            row = dkh_dat.parse_parts(lines[-1]) if lines else None
            return self._json({"dkh": row["tank_kh"] if row else 0.0})
        if path == "/api/override":
            return self._json(_read(os.path.join(DATA, "doser_override.json"), {}) or {})
        if path == "/api/override/state":
            return self._json(_read(os.path.join(DATA, "doser_override_state.json"), {}) or {})
        if path == "/api/config":
            return self._json(_read(os.path.join(DATA, "doser_config.json"),
                                    {"target_dkh": 7.2}) or {"target_dkh": 7.2})
        if path == "/api/ph_cal":
            return self._json(_read(os.path.join(DATA, "ph_cal.json"), {}) or {})
        if path == "/api/devices":
            return self._json({"devices": _devices.all_devices(), "targets": _targets(),
                               "ids": _target_ids(), "doser_max": _cfg.DOSER_MAX,
                               "sync_max": _cfg.DOSER_SYNC_MAX})
        if path == "/api/schedule":
            return self._json({"measure_hours": _schedule.measure_hours(),
                               "doser_slot_hour": _schedule.doser_slot_hour(),
                               "min_gap_h": _cfg.MEASURE_MIN_GAP_H,
                               "hours_max": _cfg.MEASURE_HOURS_MAX,
                               "source": _schedule.source()})
        if path == "/api/ops/status":
            return self._json(_snapshot())
        if path == "/api/ops/log":
            n, since = 40, None
            for kv in query.split("&"):
                try:
                    if kv.startswith("n="):
                        # 상한은 기기(webserver.py)와 같은 값이어야 한다 — 2026-08-30: 300→5000
                        n = max(1, min(5000, int(kv[2:] or 40)))
                    elif kv.startswith("since="):
                        since = max(0, int(kv[6:]))
                except ValueError:
                    pass
            lines, off, reset = _fake_log_slice(n, since)
            return self._json({"lines": lines, "off": off, "reset": reset})
        if path == "/api/ops/result":
            return self._json({"result": _state["job_result"], "busy": False, "pending": None})
        if path == "/api/wifi":
            return self._json(_snapshot()["wifi"])
        if path == "/api/wifi/scan":
            return self._json({"nets": [{"ssid": "reef-2g", "rssi": -52, "secure": True},
                                        {"ssid": "guest", "rssi": -71, "secure": False}],
                               "err": None})
        self._json({"err": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/override":
            ml = body.get("ml_day")
            if not isinstance(ml, (int, float)) or not (ml == 0 or 1.5 <= ml <= 18):
                return self._json({"ok": False, "err": "0 또는 1.5~18mL/일"}, 400)
            ov = {"ml_day": ml, "id": time.strftime("%Y-%m-%dT%H:%M:%S+09:00")}
            _write(os.path.join(DATA, "doser_override.json"), ov)
            return self._json({"ok": True, "id": ov["id"]})
        if path == "/api/config":
            t = body.get("target_dkh")
            if not isinstance(t, (int, float)) or not (6.0 <= t <= 9.0):
                return self._json({"ok": False, "err": "목표 6.0~9.0 dKH"}, 400)
            _write(os.path.join(DATA, "doser_config.json"), {"target_dkh": t})
            return self._json({"ok": True})
        if path == "/api/ph_cal":
            if "offset" not in body:
                return self._json({"ok": False, "err": "offset 필요"}, 400)
            _write(os.path.join(DATA, "ph_cal.json"), body)
            return self._json({"ok": True})
        if path == "/api/devices":
            ok, msg = _devices.set_devices(body)
            if ok and _state["bt_target"] not in _target_ids():
                _state["bt_target"] = None        # 사라진 장치에 붙어 있다고 두지 않는다
            return self._json({"ok": ok, "msg": msg, "devices": _devices.all_devices(),
                               "targets": _targets(), "ids": _target_ids()},
                              200 if ok else 400)
        if path == "/api/schedule":
            ok, msg, warn = _schedule.set_schedule(body)
            return self._json({"ok": ok, "msg": msg, "warn": warn,
                               "measure_hours": _schedule.measure_hours(),
                               "doser_slot_hour": _schedule.doser_slot_hour()},
                              200 if ok else 400)
        if path == "/api/restore":
            # 본문은 do_POST 가 이미 dict 으로 읽어 뒀다(재읽기 금지 — 블록된다).
            # ★검증·기록은 기기와 **같은 함수**가 한다(_archive.restore) — 사본을 두면
            #   기기 쪽 결함을 스텁이 못 잡는다(2026-08-24 실제 사례).
            if not isinstance(body, dict) or not body:
                return self._json({"ok": False, "msg": "본문 파싱 실패 — JSON 형식 확인"}, 400)
            ok, msg = _archive.restore(body)
            if ok:
                _schedule.invalidate()            # 기기와 같은 후처리 — 복원값을 바로 반영
                _devices.reload()
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)
        if path == "/api/wifi":
            if not (body.get("ssid") or "").strip():
                return self._json({"ok": False, "msg": "SSID 가 비었습니다"})
            return self._json({"ok": True, "msg": "저장됨 — '%s' 로 접속을 시도합니다(stub)"
                                                 % body["ssid"]})
        if path == "/api/ops/abort":
            return self._json({"ok": False, "msg": "측정 중이 아닙니다"})
        if path == "/api/ops/clear_latch":
            lines = _dat_lines()
            latch = bool(lines) and len(lines[-1]) >= 6 and all(float(x) == 0.0 for x in lines[-1][1:6])
            if not latch:
                return self._json({"ok": False, "msg": "에러 래치 상태가 아닙니다"})
            with open(os.path.join(DATA, "dkh.dat"), "w") as f:
                f.write("\n".join(" ".join(p) for p in lines[:-1]) + "\n")
            return self._json({"ok": True, "msg": "래치 해제(stub)"})
        if path == "/api/ops/hold":
            # 기기(webserver.py)와 같은 계약 — 실제 schedule 모듈이 처리하므로 동작도 같다.
            if body.get("clear"):
                was = _schedule.clear_hold()
                if was is None:
                    return self._json({"ok": False, "msg": "보류 상태가 아닙니다"})
                return self._json({"ok": True, "msg": "측정 보류 해제 — 다음 정시 회차부터 측정합니다",
                                   "hold": _schedule.hold_status()})
            ok, msg = _schedule.set_hold(body.get("hours"), body.get("reason") or "")
            return self._json({"ok": ok, "msg": msg, "hold": _schedule.hold_status()})

        if path == "/api/ops/liquid":
            _state["liquid"] = {"chamber": body.get("chamber"), "holding": body.get("holding")}
            return self._json({"ok": True, "msg": "지정됨(stub) — %s / %s"
                                                 % (body.get("chamber"), body.get("holding"))})
        if path == "/api/ops/job":
            kind = body.get("kind")
            if kind not in JOB_KINDS:
                return self._json({"ok": False, "msg": "알 수 없는 작업: %s" % kind})
            lines = _dat_lines()
            _last = dkh_dat.parse_parts(lines[-1]) if lines else None
            dev = _device_state(bool(_last and _last["is_error"]))
            if _state["measuring"]:
                return self._json({"ok": False,
                                   "msg": "측정 중 — 먼저 '측정 중단' 하거나 종료를 기다리세요"})
            if kind == "cmd" and not dev["console_allowed"] and not (
                    dev["console_override"] and body.get("ack")):
                return self._json({"ok": False, "state": dev["state"],
                                   "msg": "%s — %s" % (dev["label"], dev["console_reason"])})
            if kind == "bt_target":
                _state["bt_target"] = body.get("target", "meas")   # 전환 결과를 실제로 반영
            # ★도징량 조작은 기본 도저에서만(2026-08-21) — 시계 동기는 값이 같아 무해하므로
            #   대상을 가리지 않는다(기기 ops._job_doser_clock 과 같은 규칙).
            if (kind in ("doser_query", "doser_apply")
                    and _state["bt_target"] != _devices.PRIMARY_DOSER_ID):
                pname = _targets().get(_devices.PRIMARY_DOSER_ID, {}).get("name", "기본 도저")
                return self._json({"ok": False,
                                   "msg": "BT 대상이 '%s' 가 아닙니다 — '%s로 전환' 후 "
                                          "실행하세요" % (pname, pname)})
            _state["job_result"] = {"kind": kind, "ok": True,
                                    "msg": "stub 실행 — 실제 장비 동작 없음",
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            return self._json({"ok": True, "msg": "%s 요청됨 — 결과는 폴링(stub)" % kind})
        self._json({"err": "not found"}, 404)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--seed", action="store_true", help="저장소 docs/ 실물 JSON 을 data/ 로 내려받기")
    a = ap.parse_args()
    if a.seed:
        seed()
    os.makedirs(DATA, exist_ok=True)
    print("serving %s + %s on http://localhost:%d  (dashboard / ops.html)"
          % (WWW, DATA, a.port))
    HTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
