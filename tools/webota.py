#!/usr/bin/env python3
# ★vendored: mpy-webota v0.8.1 (client/webota.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
"""webota 클라이언트 — MicroPython 기기의 webota 서버(:8266)를 원격으로 다룬다.

표준 라이브러리만 쓴다. CLI 로도, import 해서 라이브러리(`Client`)로도 쓴다.

    webota.py status
    webota.py ls /data -r
    webota.py get /data/measure_kh.log            # 디렉토리면 재귀로 내려받는다
    webota.py put local.py /app.py
    webota.py rm '/data/*.bak'                    # ★따옴표: 글롭은 기기 쪽에서 푼다
    webota.py mkdir /data/x ;  webota.py mv /a /b
    webota.py reset
    webota.py deploy [--delete] [--dry-run] [--label L]  # map 대로, 바뀐 파일만(라벨 기본: git describe)
    webota.py history [-n 20]                     # 배포 결과 이력
    webota.py pack --app-id ID --version V [--out dist/]   # map 대로 배포 패키지(.wpk) 만들기
    webota.py pkg-list ;  webota.py pkg-install <URL>      # 기기가 직접 내려받아 설치
    webota.py clean [-y]                          # 지금 판에 없는 남은 코드 파일 보여 주고 지우기
    webota.py token                               # 새 토큰 생성(파일 저장)
    webota.py device-config [--out webota.json]   # 프로젝트의 app_id·device 절 + 토큰 → 기기 설정 파일
    webota.py claim                               # 토큰 없는 기기 등록(설정용 AP 에 붙은 PC 에서)
    webota.py set-token --new-token-file F        # 등록된 기기의 토큰 바꾸기

설정 찾는 순서:
  host  : --host  > $WEBOTA_HOST > 프로젝트 파일 "host"
  token : --token > $WEBOTA_TOKEN > --token-file > 프로젝트 "token_file" > ~/.config/webota/<host>.token
  프로젝트 파일: 현재 디렉토리부터 위로 올라가며 찾는 `webota.project.json`
    {"host": "192.168.0.47", "map": [{"src": "src/*.py", "dst": "/"},
                                     {"src": "www/", "dst": "/www/"}], "exclude": ["*.pyc"]}
"""
import argparse
import fnmatch
import glob
import hashlib
import http.client
import json
import os
import secrets
import sys
import time
import urllib.parse

VERSION = "0.8.1"
DEFAULT_PORT = 8266
PROJECT_FILE = "webota.project.json"
# 잘못 바꾸면 원격으로 못 되돌리는 파일(USB 로만 복구) — 바꿀 때 한 번 더 묻는다.
CRITICAL = ("/boot.py", "/main.py", "/webota.py", "/webota_boot.py", "/webota.json")


class WebotaError(Exception):
    pass


def sha_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def token_path(host):
    return os.path.expanduser("~/.config/webota/%s.token" % host.split(":")[0])


# ── 배포 패키지(.wpk) — 형식은 device/webota_pkg.py 머리 참조 ──

PKG_MAGIC = b"WPK1\n"


def _under(path, roots):
    return any(path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/") for d in roots)


def build_package(files, out_path, app_id, version, label=None, name=None, delete=(),
                  webota_version=None, app="app", entry="main", settings=(), data=()):
    """files: {기기 경로: 로컬 경로} → .wpk. 매니페스트를 돌려준다. app·entry 는 앱 교체 때
    기기 런처가 부를 모듈·함수(기본 app.main). 파일 이름 규약: <app_id>-v<판>….wpk
    settings·data: 앱이 설정·데이터를 두는 경로(파일·디렉토리). 기기는 이 아래를 코드로 보지
    않는다 — 패키지로 맞추지도, 정리하지도 않는다. settings 아래 파일을 패키지에 넣으면
    '기본값'이 되어 기기에 없을 때만 들어간다. data 아래 파일은 패키지에 넣을 수 없다."""
    bad = [r for r in files if _under(r, data)]
    if bad:
        raise WebotaError("데이터 경로의 파일은 패키지에 넣지 않는다: " + ", ".join(bad))
    man = {"format": 1, "app_id": app_id, "app": app, "entry": entry,
           "settings": sorted(settings), "data": sorted(data),
           "name": name or app_id, "version": version,
           "label": label or ("v" + version), "built_at": time.strftime("%Y-%m-%d %H:%M"),
           "webota": webota_version or VERSION, "delete": sorted(delete), "files": []}
    order = sorted(files)
    for r in order:
        e = {"path": r, "size": os.path.getsize(files[r]), "sha": sha_of(files[r])}
        if _under(r, settings):
            e["kind"] = "setting"
        man["files"].append(e)
    mj = json.dumps(man, ensure_ascii=False).encode()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(PKG_MAGIC + str(len(mj)).encode() + b"\n" + mj)
        for r in order:
            with open(files[r], "rb") as fi:
                for b in iter(lambda: fi.read(65536), b""):
                    f.write(b)
    return man


def read_manifest(path):
    with open(path, "rb") as f:
        if f.read(len(PKG_MAGIC)) != PKG_MAGIC:
            raise WebotaError("패키지가 아니다: " + path)
        n = int(f.readline())
        return json.loads(f.read(n))


class Client:
    def __init__(self, host, token, port=None, timeout=60, settings=(), data=()):
        h, _, p = host.partition(":")
        self.host = h
        self.port = int(p or port or DEFAULT_PORT)
        self.token = token
        self.timeout = timeout
        self.settings, self.data = settings, data      # 프로젝트의 설정·데이터 경로(배포 기록용)

    # ── 전송 ──
    def _req(self, method, path, query=None, body=None, length=None, stream_to=None,
             timeout=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        url = urllib.parse.quote(path, safe="/")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        hdr = {"X-Token": self.token or ""}
        if body is not None:
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
                hdr["Content-Type"] = "application/json"
            hdr["Content-Length"] = str(len(body) if length is None else length)
        try:
            conn.request(method, url, body=body, headers=hdr)
            r = conn.getresponse()
            if stream_to is not None and r.status == 200:
                with open(stream_to, "wb") as f:
                    for b in iter(lambda: r.read(65536), b""):
                        f.write(b)
                return r.status, None
            data = r.read()
        finally:
            conn.close()
        obj = None
        if r.getheader("Content-Type", "").startswith("application/json"):
            try:
                obj = json.loads(data)
            except ValueError:
                pass
        if r.status >= 300:
            msg = obj.get("err") if isinstance(obj, dict) else data[:200]
            raise WebotaError("%d %s — %s %s" % (r.status, r.reason, method, path) +
                              (": %s" % msg if msg else ""))
        return r.status, (obj if obj is not None else data)

    # ── 조회 ──
    def status(self, timeout=None):
        return self._req("GET", "/status", timeout=timeout)[1]

    def ls(self, path="/", recursive=False, sha=False):
        """디렉토리 목록(entries). path 가 파일이면 그 파일 하나의 항목."""
        q = {}
        if recursive:
            q["r"] = "1"
        if sha:
            q["sha"] = "1"
        parent = path.rstrip("/").rsplit("/", 1)[0] or "/"
        _, obj = self._req("GET", "/fs" + path, q or None)
        if isinstance(obj, dict) and "entries" in obj:
            return obj["entries"]
        # 파일이었다 — 부모 목록에서 그 항목을 찾아 돌려준다(본문은 버린다).
        _, par = self._req("GET", "/fs" + parent, {"sha": "1"} if sha else None)
        return [e for e in par["entries"] if e["path"] == path]

    def history(self, n=10):
        return self._req("GET", "/history", {"n": str(n)})[1]["history"]

    def pkg_sources(self, add=None, remove=None, default=None):
        if add or remove or default:
            body = {"add": add} if add else {"remove": remove} if remove else {"default": default}
            return self._req("POST", "/pkg/sources", body=body)[1]["sources"]
        return self._req("GET", "/pkg/sources")[1]["sources"]

    def pkg_list(self, fresh=False, src=None):
        q = {}
        if fresh:
            q["fresh"] = "1"
        if src:
            q["src"] = src
        return self._req("GET", "/pkg/list", q or None)[1]

    def pkg_plan(self, url, reset_settings=False, reset_data=False):
        """설치 계획 — 바뀔 파일(write)·지울 파일(delete)·그중 지금 설정·데이터였던 것
        (delete_kept_now)·보존할 경로. 기기가 매니페스트만 읽고 계산한다(설치하지 않는다)."""
        return self._req("POST", "/pkg/plan", body={"url": url, "reset_settings": reset_settings,
                                                    "reset_data": reset_data}, timeout=120)[1]

    def pkg_install(self, url, force=False, wait=True, log=print, switch_app=False, src=None,
                    reset_settings=False, reset_data=False):
        """기기가 url 의 패키지를 직접 내려받아 설치한다. 새 판 확인(또는 롤백)까지 기다린다.
        다른 앱의 패키지는 switch_app=True 여야 한다(앱 교체)."""
        st0 = self.status()
        r = self._req("POST", "/pkg/install", body={"url": url, "force": force, "src": src,
                                                    "switch_app": switch_app, "reset_settings": reset_settings,
                                                    "reset_data": reset_data}, timeout=300)[1]
        if r.get("result") == "unchanged":
            log("  이미 이 판이다(%s) — 바뀐 파일 없음" % r.get("label"))
            return "unchanged"
        log("  %s — 파일 %d개 변경, 재부팅" % (r.get("label"), r.get("files", 0)))
        return self._wait(r["id"], st0, log) if wait else "committed"

    def orphans(self):
        return self._req("GET", "/pkg/orphans")[1]

    def clean(self, paths):
        return self._req("POST", "/pkg/clean", body={"paths": list(paths)})[1]

    def sha(self, paths):
        return self._req("POST", "/sha", body={"paths": list(paths)})[1]["sha"]

    def get(self, remote, local):
        """파일 하나 또는 디렉토리(재귀)를 내려받는다. 내려받은 로컬 경로 목록."""
        _, obj = self._req("GET", "/fs" + remote)
        if isinstance(obj, dict) and "entries" in obj:
            out = []
            for e in self.ls(remote, recursive=True):
                if e["type"] != "f":
                    continue
                rel = e["path"][len(remote.rstrip("/")) + 1:]
                dst = os.path.join(local, *rel.split("/"))
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                self._req("GET", "/fs" + e["path"], stream_to=dst)
                out.append(dst)
            return out
        if os.path.isdir(local):
            local = os.path.join(local, remote.rsplit("/", 1)[-1])
        os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
        with open(local, "wb") as f:
            f.write(obj if isinstance(obj, bytes) else json.dumps(obj).encode())
        return [local]

    # ── 변경 ──
    def put(self, local, remote):
        with open(local, "rb") as f:
            return self._req("PUT", "/fs" + remote, {"sha": sha_of(local)}, body=f,
                             length=os.path.getsize(local))[1]

    def rm(self, remote, recursive=False):
        return self._req("DELETE", "/fs" + remote, {"r": "1"} if recursive else None)[1]

    def mkdir(self, remote):
        return self._req("POST", "/fs" + remote, {"op": "mkdir"})[1]

    def mv(self, src, dst):
        return self._req("POST", "/fs" + src, {"op": "mv", "to": dst})[1]

    def reset(self, force=False):
        return self._req("POST", "/reset", {"force": "1"} if force else None)[1]

    def expand(self, pattern):
        """기기 쪽 글롭(* ? [)을 푼다 — 부모 디렉토리 목록에서 이름을 맞춘다."""
        if not any(c in pattern for c in "*?["):
            return [pattern]
        parent, _, name = pattern.rstrip("/").rpartition("/")
        return [e["path"] for e in self.ls(parent or "/")
                if fnmatch.fnmatch(e["path"].rsplit("/", 1)[-1], name)]

    # ── 배포 ──
    def deploy(self, files, delete=(), force=False, reset=True, wait=True, dry_run=False,
               log=print, label=None):
        """files: {원격 경로: 로컬 경로}. 해시가 다른 파일만 올린다. label(예: 커밋)은 기기
        이력(history)에 남는다 — 무엇이 언제 올라갔고 롤백됐는지 기기만 봐도 알 수 있게.
        반환: {"id", "changed": [...], "delete": [...], "result"}. 롤백되면 WebotaError."""
        remote_sha = self.sha(files.keys()) if files else {}
        # 설정 파일은 기기에 이미 있으면 올리지 않는다(운영 중 바뀐 값을 지킨다 — 바꾸려면 put).
        changed = [r for r, l in sorted(files.items()) if remote_sha.get(r) != sha_of(l)
                   and not (_under(r, self.settings) and remote_sha.get(r) is not None)]
        delete = sorted(delete)
        res = {"id": None, "changed": changed, "delete": delete, "result": None}
        if not changed and not delete:
            log("  바뀐 파일 없음 — 배포할 것이 없다")
            res["result"] = "unchanged"
            return res
        for r in changed:
            log("  %s %s (%d B)" % ("+" if remote_sha.get(r) is None else "~", r,
                                   os.path.getsize(files[r])))
        for r in delete:
            log("  - %s" % r)
        if dry_run:
            res["result"] = "dry-run"
            return res
        st0 = self.status()
        did = self._req("POST", "/deploy/begin")[1]["id"]
        res["id"] = did
        items = []
        for r in changed:
            sha = sha_of(files[r])
            with open(files[r], "rb") as f:
                self._req("PUT", "/deploy/%s%s" % (did, r), {"sha": sha}, body=f,
                          length=os.path.getsize(files[r]))
            items.append({"path": r, "sha": sha})
        self._req("POST", "/deploy/%s/commit" % did, {"force": "1"} if force else None,
                  body={"files": items, "delete": delete, "reset": reset, "label": label,
                        "all_files": sorted(files),          # 이 판을 이루는 파일 전체(정리 기준)
                        "settings": list(self.settings), "data": list(self.data)})
        log("  커밋 %s%s — 파일 %d개, 삭제 %d개%s" % (did, " [%s]" % label if label else "",
                                                    len(items), len(delete),
                                                 " · 리셋" if reset else " (다음 부팅에 적용)"))
        if not (reset and wait):
            res["result"] = "committed"
            return res
        res["result"] = self._wait(did, st0, log)
        return res

    def _wait(self, did, st0, log):
        """리셋 → 새 판 시험 → 확인(ok) 또는 롤백까지 기다린다."""
        limit = time.time() + int(st0.get("confirm_s") or 90) + 240
        rebooted = False
        last_note = None
        while time.time() < limit:
            time.sleep(2)
            try:
                st = self.status(timeout=5)
            except (OSError, WebotaError, http.client.HTTPException):
                if not rebooted:
                    log("  재부팅 중…")
                rebooted = True
                continue
            last = st.get("last") or {}
            ours = last.get("id") == did or (st.get("trial") or {}).get("id") == did
            if not rebooted and not ours and st.get("uptime_s", 0) >= st0.get("uptime_s", 0):
                continue                         # 아직 리셋 전
            rebooted = True
            if last.get("id") == did and last.get("result") == "rolled_back":
                raise WebotaError("★롤백됨 — %s (앱 오류: %s)"
                                  % (last.get("reason"), (st.get("app") or {}).get("error", "")[-300:]))
            if last.get("id") == did and last.get("result") == "ok":
                log("  ✓ 새 판 확인 — 가동 %ss, 앱 %s" % (st.get("uptime_s"), st["app"]["state"]))
                return "ok"
            note = "  시험 중 — 가동 %ss / 확인 %ss, 앱 %s" % (
                st.get("uptime_s"), st.get("confirm_s"), st["app"]["state"])
            if st["app"]["state"] == "rescue":
                raise WebotaError("★앱이 구조 모드 — %s" % st["app"].get("error", "")[-300:])
            if note != last_note and st.get("uptime_s", 0) % 20 < 3:
                log(note)
                last_note = note
        raise WebotaError("시간 초과 — 새 판 확인을 받지 못했다(webota.py status 로 확인)")


# ── 프로젝트 설정 · map ──

def find_project(start="."):
    d = os.path.abspath(start)
    while True:
        f = os.path.join(d, PROJECT_FILE)
        if os.path.exists(f):
            with open(f) as fh:
                cfg = json.load(fh)
            cfg["_root"] = d
            return cfg
        up = os.path.dirname(d)
        if up == d:
            return {}
        d = up


def map_files(project):
    """프로젝트 map → {원격: 로컬}, 그리고 --delete 판정용 원격 범위 [(디렉토리, 글롭|None)]."""
    root = project.get("_root", ".")
    excl = project.get("exclude") or []
    files, scopes = {}, []
    for m in project.get("map") or []:
        src, dst = m["src"], m["dst"]
        if not dst.endswith("/"):
            dst += "/"
        if src.endswith("/"):
            base = os.path.join(root, src)
            for dp, _dn, fn in os.walk(base):
                for n in fn:
                    lp = os.path.join(dp, n)
                    rel = os.path.relpath(lp, base).replace(os.sep, "/")
                    if not any(fnmatch.fnmatch(rel, e) or fnmatch.fnmatch(n, e) for e in excl):
                        files[dst + rel] = lp
            scopes.append((dst, None))
        else:
            for lp in sorted(glob.glob(os.path.join(root, src))):
                n = os.path.basename(lp)
                if os.path.isfile(lp) and not any(fnmatch.fnmatch(n, e) for e in excl):
                    files[dst + n] = lp
            scopes.append((dst, os.path.basename(src)))
    return files, scopes


def extra_remote(client, files, scopes):
    """map 범위 안에서 원격에만 있는 파일 — --delete 대상."""
    extra = set()
    for dst, pat in scopes:
        d = dst.rstrip("/") or "/"
        try:
            ents = client.ls(d, recursive=pat is None)
        except WebotaError:
            continue
        for e in ents:                           # 글롭 범위는 비재귀 목록이라 직계 파일뿐
            if e["type"] == "f" and e["path"] not in files and (
                    pat is None or fnmatch.fnmatch(e["path"].rsplit("/", 1)[-1], pat)):
                extra.add(e["path"])
    return sorted(extra)


def git_label(root):
    """프로젝트의 git describe(태그 없으면 해시) — 없으면 None."""
    import subprocess
    try:
        out = subprocess.check_output(["git", "-C", root, "describe", "--tags", "--always",
                                       "--dirty"], stderr=subprocess.DEVNULL)
        return out.decode().strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


DEVICE_DEFAULTS = {"port": DEFAULT_PORT, "app": "app", "entry": "main", "confirm_s": 90}


def device_config(project, token):
    """기기 설정(/webota.json) — webota 기본값 + 프로젝트의 app_id + "device" 절 + 토큰.
    ★webota.json 은 이 함수로만 만든다(앱 저장소에 생성 코드를 두지 않는다). 프로젝트 파일 예:
      {"app_id": "myapp", "device": {"ap": {"ssid": "myapp-setup", "pass": "..."},
       "hostname": "myapp", "sources": [{"github": "owner/repo"}]}}"""
    c = dict(DEVICE_DEFAULTS)
    if project.get("app_id"):
        c["app_id"] = project["app_id"]
    c.update(project.get("device") or {})
    c["token"] = token
    return c


def ensure_token(tf):
    """토큰 파일이 없으면 만든다(랜덤 32자, 0600). 토큰을 돌려준다."""
    tf = os.path.expanduser(tf)
    if not os.path.exists(tf):
        os.makedirs(os.path.dirname(tf), exist_ok=True)
        with open(tf, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(tf, 0o600)
        print("새 토큰: %s" % tf, file=sys.stderr)
    with open(tf) as f:
        return f.read().strip()


def _confirm_critical(paths, yes):
    hit = [p for p in paths if p in CRITICAL]
    if not hit or yes:
        return True
    print("★다음 파일은 잘못되면 원격으로 되돌릴 수 없다(USB 로만 복구): " + ", ".join(hit))
    try:
        return input("계속할까요? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def resolve(args, project):
    host = args.host or os.environ.get("WEBOTA_HOST") or project.get("host")
    if not host:
        raise SystemExit("host 를 모른다 — --host 또는 %s 의 \"host\"" % PROJECT_FILE)
    token = args.token or os.environ.get("WEBOTA_TOKEN")
    if not token:
        tf = args.token_file or project.get("token_file") or token_path(host)
        tf = os.path.expanduser(tf)
        try:
            with open(tf) as f:
                token = f.read().strip()
        except OSError:
            raise SystemExit("토큰 파일이 없다: %s (webota.py token 으로 만든다)" % tf)
    return host, token


def _fmt_size(n):
    return "%7.1fK" % (n / 1024.0) if n is not None else "       -"


def main(argv=None):
    ap = argparse.ArgumentParser(description="webota 클라이언트 v" + VERSION)
    ap.add_argument("--host")
    ap.add_argument("--token")
    ap.add_argument("--token-file")
    ap.add_argument("-y", "--yes", action="store_true", help="중요 파일 변경 확인을 생략")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("ls"); s.add_argument("path", nargs="?", default="/")
    s.add_argument("-r", action="store_true"); s.add_argument("--sha", action="store_true")
    s = sub.add_parser("get"); s.add_argument("remote"); s.add_argument("local", nargs="?", default=".")
    s = sub.add_parser("put"); s.add_argument("local"); s.add_argument("remote")
    s = sub.add_parser("rm"); s.add_argument("remote", nargs="+"); s.add_argument("-r", action="store_true")
    s = sub.add_parser("mkdir"); s.add_argument("remote")
    s = sub.add_parser("mv"); s.add_argument("src"); s.add_argument("dst")
    s = sub.add_parser("reset"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("deploy")
    s.add_argument("--delete", action="store_true", help="map 범위에서 원격에만 있는 파일 삭제")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="앱 가드(측정 중 등) 무시")
    s.add_argument("--no-reset", action="store_true", help="커밋만 — 다음 부팅에 적용")
    s.add_argument("--label", help="이력에 남길 라벨(기본: 프로젝트 git describe --always --dirty)")
    s = sub.add_parser("history"); s.add_argument("-n", type=int, default=10)
    s = sub.add_parser("pack", help="map 대로 배포 패키지(.wpk)를 만든다")
    s.add_argument("--app-id"); s.add_argument("--version"); s.add_argument("--label")
    s.add_argument("--name"); s.add_argument("--out", default="dist")
    s = sub.add_parser("pkg-list"); s.add_argument("--fresh", action="store_true"); s.add_argument("--src")
    s = sub.add_parser("pkg-install"); s.add_argument("url"); s.add_argument("--force", action="store_true")
    s.add_argument("--switch-app", action="store_true", help="다른 앱의 패키지로 기기를 교체"); s.add_argument("--src")
    s.add_argument("--reset-settings", action="store_true", help="선언된 설정을 패키지 기본값으로(토큰·WiFi 는 유지)")
    s.add_argument("--reset-data", action="store_true", help="★선언된 데이터를 모두 지운다")
    sub.add_parser("clean", help="지금 판에 없는 남은 코드 파일을 보여 주고 지운다(데이터 제외)")
    s = sub.add_parser("sources", help="기기의 패키지 출처(저장소) 목록 · 추가 · 삭제 · 기본")
    s.add_argument("--add"); s.add_argument("--remove"); s.add_argument("--default")
    s = sub.add_parser("device-config", help="기기 설정 파일(webota.json)을 만든다 — 프로젝트 app_id·device + 토큰")
    s.add_argument("--out", default="webota.json")
    sub.add_parser("claim", help="토큰 없는 기기에 토큰을 등록(설정용 AP 로 붙어서)")
    s = sub.add_parser("set-token", help="등록된 기기의 토큰을 바꾼다(지금 토큰으로 인증)")
    s.add_argument("--new-token-file", required=True)
    s = sub.add_parser("token", help="새 토큰을 만들어 토큰 파일에 저장")
    s.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    project = find_project()

    if a.cmd == "token":
        host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host")
        tf = os.path.expanduser(a.token_file or project.get("token_file") or token_path(host or "default"))
        if os.path.exists(tf) and not a.overwrite:
            print("이미 있다: %s (--overwrite 로 새로)" % tf)
            return 0
        os.makedirs(os.path.dirname(tf), exist_ok=True)
        with open(tf, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(tf, 0o600)
        print("토큰 저장: %s" % tf)
        return 0

    if a.cmd == "pack":
        files, _ = map_files(project)
        app_id = a.app_id or project.get("app_id")
        version = a.version or project.get("version")
        if not files or not app_id or not version:
            raise SystemExit("pack 에는 map · app_id · version 이 필요하다(인자 또는 %s)" % PROJECT_FILE)
        label = a.label or git_label(project.get("_root", ".")) or ("v" + version)
        out = os.path.join(a.out, "%s-%s.wpk" % (app_id, label))
        man = build_package(files, out, app_id, version, label, a.name or project.get("name"),
                            settings=project.get("settings") or [], data=project.get("data") or [])
        print("%s — 파일 %d개, %d B" % (out, len(man["files"]), os.path.getsize(out)))
        return 0

    if a.cmd == "device-config":
        host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host") or "default"
        tok = a.token or ensure_token(a.token_file or project.get("token_file") or token_path(host))
        with open(a.out, "w") as f:
            json.dump(device_config(project, tok), f, ensure_ascii=False, indent=2)
        os.chmod(a.out, 0o600)
        print("기기 설정: %s" % a.out)
        return 0
    if a.cmd == "claim":
        host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host") or "192.168.4.1"
        tok = a.token or ensure_token(a.token_file or project.get("token_file") or token_path(host))
        try:
            r = Client(host, "")._req("POST", "/claim", body={"token": tok})[1]
            print(r.get("msg"))
        except WebotaError as e:
            print("✗ %s" % e, file=sys.stderr)
            return 1
        return 0

    host, token = resolve(a, project)
    c = Client(host, token, settings=project.get("settings") or [], data=project.get("data") or [])
    try:
        if a.cmd == "status":
            print(json.dumps(c.status(), ensure_ascii=False, indent=2))
        elif a.cmd == "ls":
            for e in c.ls(a.path, recursive=a.r, sha=a.sha):
                print("%s %s  %s%s" % (e["type"], _fmt_size(e.get("size")), e["path"],
                                       ("  " + (e.get("sha") or "")) if a.sha else ""))
        elif a.cmd == "get":
            for p in c.get(a.remote, a.local):
                print(p)
        elif a.cmd == "put":
            if not _confirm_critical([a.remote], a.yes):
                return 1
            r = c.put(a.local, a.remote)
            print("%s  %s" % (r["path"], r["sha"]))
        elif a.cmd == "rm":
            paths = [p for pat in a.remote for p in c.expand(pat)]
            if not paths:
                print("맞는 파일 없음")
                return 1
            if not _confirm_critical(paths, a.yes):
                return 1
            for p in paths:
                c.rm(p, recursive=a.r)
                print("삭제 %s" % p)
        elif a.cmd == "mkdir":
            c.mkdir(a.remote)
        elif a.cmd == "mv":
            if not _confirm_critical([a.src, a.dst], a.yes):
                return 1
            c.mv(a.src, a.dst)
        elif a.cmd == "reset":
            c.reset(force=a.force)
            print("리셋 요청됨")
        elif a.cmd == "deploy":
            files, scopes = map_files(project)
            if not files:
                raise SystemExit("map 에 맞는 로컬 파일이 없다(%s)" % PROJECT_FILE)
            dels = extra_remote(c, files, scopes) if a.delete else []
            crit = [r for r in files if r in CRITICAL]
            rsha = c.sha(crit) if crit else {}
            touched = [r for r in crit if rsha.get(r) != sha_of(files[r])] + dels
            if not a.dry_run and not _confirm_critical(touched, a.yes):
                return 1
            c.deploy(files, dels, force=a.force, reset=not a.no_reset, dry_run=a.dry_run,
                     label=a.label or git_label(project.get("_root", ".")))
        elif a.cmd == "set-token":
            new = ensure_token(a.new_token_file)
            print(c._req("POST", "/token", body={"token": new})[1].get("msg"))
        elif a.cmd == "clean":
            r = c.orphans()
            if not r.get("ok"):
                print(r.get("err"))
                return 1
            o = r["orphans"]
            if not o:
                print("남은 파일 없음 — 기기가 지금 판 그대로다")
                return 0
            for e in o:
                print("  %7.1fK  %s" % (e["size"] / 1024.0, e["path"]))
            print("합계 %d개 · %.1f KB" % (len(o), r["bytes"] / 1024.0))
            if not a.yes:
                try:
                    if input("지울까요? [y/N] ").strip().lower() != "y":
                        return 1
                except EOFError:
                    return 1
            res = c.clean([e["path"] for e in o])
            print("삭제 %d개%s" % (len(res["deleted"]), (" · 거부 " + ", ".join(res["refused"])) if res["refused"] else ""))
        elif a.cmd == "sources":
            for i, k in enumerate(c.pkg_sources(a.add, a.remove, a.default)):
                print("%s %s" % ("*" if i == 0 else " ", k))
        elif a.cmd == "pkg-list":
            r = c.pkg_list(a.fresh, a.src)
            if r.get("err"):
                print("! " + r["err"])
            cur = r.get("current") or ""
            for p in r.get("packages") or []:
                mark = "*" if cur == p.get("tag") or cur.startswith((p.get("tag") or "") + "+") else " "
                other = p.get("app_id") and r.get("app_id") and p["app_id"] != r["app_id"]
                print("%s %-22s %-18s %-17s %7s  %s" % (mark, p.get("name") or p.get("tag"),
                                                        ("[다른 앱] " if other else "") + (p.get("app_id") or "?"),
                                                        p.get("published", ""),
                                                        "%dK" % ((p.get("size") or 0) // 1024), p.get("url")))
        elif a.cmd == "pkg-install":
            pl = c.pkg_plan(a.url, a.reset_settings, a.reset_data)
            print("설치 계획 — %s (%s)" % (pl.get("label"), pl.get("pkg_app_id")))
            print("  쓸 파일 %d개 · 지울 파일 %d개%s" % (len(pl["write"]), len(pl["delete"]),
                  "" if pl["declared"] else "  ★이 패키지는 설정·데이터를 선언하지 않았다 — webota 말고는 전부 정리 대상"))
            if pl["reset_settings"] or pl["reset_data"]:
                print("  ★초기화: %s — 파일 %d개" % (" · ".join(n for n, on in (("설정", pl["reset_settings"]),
                      ("데이터", pl["reset_data"])) if on), len(pl["delete_reset"])))
            for x in pl["delete"][:30]:
                print("    - %s%s" % (x, "   ★초기화" if x in pl["delete_reset"] else
                                       "   ★지금 설정·데이터" if x in pl["delete_kept_now"] else ""))
            if len(pl["delete"]) > 30:
                print("    … 외 %d개" % (len(pl["delete"]) - 30))
            print("  보존: 설정 %s · 데이터 %s" % (", ".join(pl["keep_settings"]), ", ".join(pl["keep_data"])))
            if (pl["delete"] or pl["delete_kept_now"]) and not a.yes:
                try:
                    if input("계속할까요? [y/N] ").strip().lower() != "y":
                        return 1
                except EOFError:
                    return 1
            c.pkg_install(a.url, force=a.force, switch_app=a.switch_app, src=a.src,
                          reset_settings=a.reset_settings, reset_data=a.reset_data)
        elif a.cmd == "history":
            for e in c.history(a.n):
                print("%s  %-11s %-24s %s%s" % (e.get("at", ""), e.get("result", ""),
                                               e.get("label") or "-", e.get("id", ""),
                                               ("  — " + e["reason"]) if e.get("reason") else ""))
    except WebotaError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
