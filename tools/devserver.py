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
          "liquid": {"chamber": "KCL", "holding": "EMPTY"}}


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
            lines.append("===== AquaWiz KH 측정 V4 (ESP32) %s [%s] ====="
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
    return _fake_log_cache[-n:]


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
        "schedule": {"hours": [5, 13, 21], "doser_slot": 13},
        "wifi": {"connected": True, "ip": "127.0.0.1", "saved_ssid": "dev-stub", "rssi": -55,
                 "ap_active": False, "ap_ssid": "reefwiz-setup", "ap_pass": "reefwiz1234",
                 "ap_ip": "192.168.4.1"},
        # HC-05 1개 구성 — 스텁은 '측정 장비에 붙어 있고 신원 확인됨' 상태로 둔다.
        "link": {"target": "meas", "target_name": "측정 장비", "frozen": None},
        # 장기 저장소(SD 대체) — 스텁은 data/archive 실물을 그대로 센다.
        "archive": _archive_status(),
        "heap_free": 71234,
    }


ARCHIVE = os.path.join(DATA, "archive")
CONFIG_FILES = ("doser_config.json", "doser_override.json", "ph_cal.json")


def _archive_status():
    """기기 archive.status() 와 같은 형태 — ops.html 표시 경로를 그대로 확인할 수 있다."""
    files, total = [], 0
    if os.path.isdir(ARCHIVE):
        for name in sorted(os.listdir(ARCHIVE)):
            fp = os.path.join(ARCHIVE, name)
            if os.path.isfile(fp):
                n = os.path.getsize(fp)
                total += n
                files.append({"name": name, "bytes": n})
    try:
        st = os.statvfs(DATA)                       # 유닉스 계열만 — 없으면 None
        free_kb = st.f_bsize * st.f_bavail // 1024
    except (AttributeError, OSError):
        free_kb = None
    return {"enabled": True, "dir": "/data/archive", "files": files, "bytes": total,
            "free_kb": free_kb, "min_free_kb": 1024}


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


def _backup_bundle():
    out = {"kind": "reefwiz-backup", "v": 1, "config": {}}
    for name in CONFIG_FILES:
        out["config"][name] = _read(os.path.join(DATA, name), None)
    try:
        with open(os.path.join(DATA, "dkh.dat"), encoding="utf-8") as f:
            out["dkh_dat"] = f.read()
    except OSError:
        out["dkh_dat"] = ""
    out["latest"] = _read(os.path.join(DATA, "dkh_latest.json"), {}) or {}
    out["archive"] = _archive_status()
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
            body = json.dumps(_backup_bundle()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Disposition",
                             'attachment; filename="reefwiz-backup.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/files":
            return self._json({"dir": "/data", "files": _archive_files(),
                               "archive": _archive_status()})
        if path == "/api/dkh":
            lines = _dat_lines()
            try:
                return self._json({"dkh": float(lines[-1][4]) if lines else 0.0})
            except (ValueError, IndexError):
                return self._json({"dkh": 0.0})
        if path == "/api/override":
            return self._json(_read(os.path.join(DATA, "doser_override.json"), {}) or {})
        if path == "/api/override/state":
            return self._json(_read(os.path.join(DATA, "doser_override_state.json"), {}) or {})
        if path == "/api/config":
            return self._json(_read(os.path.join(DATA, "doser_config.json"),
                                    {"target_dkh": 7.2}) or {"target_dkh": 7.2})
        if path == "/api/ph_cal":
            return self._json(_read(os.path.join(DATA, "ph_cal.json"), {}) or {})
        if path == "/api/ops/status":
            return self._json(_snapshot())
        if path == "/api/ops/log":
            n = 40
            for kv in query.split("&"):
                if kv.startswith("n="):
                    n = max(1, min(300, int(kv[2:] or 40)))
            return self._json({"lines": _fake_log(n)})
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
        if path == "/api/restore":
            obj = body                            # do_POST 가 이미 읽어 둔 본문(재읽기 금지 — 블록된다)
            if not isinstance(obj, dict) or obj.get("kind") != "reefwiz-backup":
                return self._json({"ok": False,
                                   "msg": "백업 형식이 아니다(kind=reefwiz-backup 아님)"}, 400)
            cfg = obj.get("config") or {}
            written, skipped = [], []
            for name in CONFIG_FILES:
                val = cfg.get(name)
                if not isinstance(val, dict):
                    skipped.append(name + "(없음)")
                    continue
                if name == "doser_config.json" and val.get("target_dkh") is not None:
                    t = float(val["target_dkh"])
                    if not (6.0 <= t <= 9.0):
                        skipped.append("%s(target_dkh %.2f 범위 밖)" % (name, t))
                        continue
                if name == "doser_override.json" and val.get("ml_day") is not None:
                    ml = float(val["ml_day"])
                    if ml != 0 and not (1.5 <= ml <= 18.0):
                        skipped.append("%s(ml_day %.2f 범위 밖)" % (name, ml))
                        continue
                _write(os.path.join(DATA, name), val)
                written.append(name)
            if not written:
                return self._json({"ok": False,
                                   "msg": "복원한 항목 없음 — " + (", ".join(skipped) or "빈 번들")}, 400)
            msg = "복원: " + ", ".join(written)
            if skipped:
                msg += " / 건너뜀: " + ", ".join(skipped)
            return self._json({"ok": True, "msg": msg})
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
        if path == "/api/ops/liquid":
            _state["liquid"] = {"chamber": body.get("chamber"), "holding": body.get("holding")}
            return self._json({"ok": True, "msg": "지정됨(stub) — %s / %s"
                                                 % (body.get("chamber"), body.get("holding"))})
        if path == "/api/ops/job":
            kind = body.get("kind")
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
