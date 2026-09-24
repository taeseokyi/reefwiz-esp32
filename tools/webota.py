#!/usr/bin/env python3
# ★vendored: mpy-webota v1.0.2 (client/webota.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
"""webota 클라이언트 — 1.0.0: 서명된 패키지 설치 · 수동 정리 · 서명/설정 도구.

★원격으로 할 수 있는 것은 **서명된 패키지 설치와 정리**뿐이다(파일 API·원격 배포·리셋은 없앴다
— 기기에 코드를 넣는 길은 USB 와 서명된 패키지 둘뿐이다). 표준 라이브러리 + openssl(서명).

  기기 쪽(설치 화면과 같은 일):
    webota.py status | history [-n 20] | sources | pkg-list [--fresh] [--src S]
    webota.py pkg-install <URL> [--switch-app] [--reset-settings] [--reset-data]   # 계획을 먼저 보여 준다
    webota.py clean [-y]
  PC 쪽 도구:
    webota.py signing-key init | show      # 패키지 서명 키(~/.config/webota/signing-key.pem)
    webota.py pack --app-id ID --version V [--out dist/]      # map 대로 **서명된** 패키지(.wpk)
    webota.py device-config [--out webota.json]   # 기기 설정 — app_id·device 절 + 토큰 + ★공개키 + GitHub 토큰
    webota.py token | claim                   # 기기 토큰 만들기 · 토큰 없는 기기 등록(설정용 AP 에서)

설정 찾는 순서:
  host  : --host  > $WEBOTA_HOST > 프로젝트 파일 "host"
  token : --token > $WEBOTA_TOKEN > --token-file > 프로젝트 "token_file" > ~/.config/webota/<host>.token
  프로젝트 파일: 현재 디렉토리부터 위로 찾는 `webota.project.json`
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

VERSION = "1.0.2"
DEFAULT_PORT = 8266
PROJECT_FILE = "webota.project.json"
SIGNING_KEY = "~/.config/webota/signing-key.pem"       # 개인키 — 기기로 가지 않는다
GITHUB_TOKEN_FILE = "~/.config/webota/github-device.token"   # 기기용 GitHub 토큰 — USB 로만 심는다
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

PKG_MAGIC = b"WPK2\n"            # 서명 필수 형식(1.0.0~) — 머리 · 매니페스트 · 서명 · 파일
PKG_MAGIC_V1 = b"WPK1\n"


def _openssl(*args, data=None):
    import subprocess
    try:
        return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout
    except FileNotFoundError:
        raise WebotaError("openssl 이 없다 — 서명에 필요하다")
    except subprocess.CalledProcessError as e:
        raise WebotaError("openssl 실패: %s" % e.stderr.decode(errors="replace").strip())


PASS_ENV = "WEBOTA_SIGN_PASS"          # 무인 빌드용(권하지 않는다) — 없으면 터미널에서 묻는다
_pass_cache = {}


def _askpass(path, confirm=False):
    """서명 키 암호 — 환경변수가 있으면 그것, 아니면 터미널에서(getpass). confirm 이면 두 번 받아
    같은지 확인한다. ★openssl 의 자체 입력에 맡기지 않는다: 1.1 은 확인이 틀려도 성공을 돌려줘
    빈 키 파일을 남겼다(2026-09-25 실측)."""
    if os.environ.get(PASS_ENV):
        return os.environ[PASS_ENV]
    if path in _pass_cache:
        return _pass_cache[path]
    import getpass
    while True:
        pw = getpass.getpass("서명 키 암호(%s): " % os.path.basename(path))
        if confirm:
            if len(pw) < 8:
                print("  8자 이상으로 하세요.", file=sys.stderr)
                continue
            if getpass.getpass("한 번 더: ") != pw:
                print("  암호가 서로 다릅니다 — 다시 입력하세요.", file=sys.stderr)
                continue
        _pass_cache[path] = pw
        return pw


def _encrypted(path):
    with open(path, "rb") as f:
        return b"ENCRYPTED" in f.read(200)


def _run_openssl(args, pw=None, data=None):
    """openssl 실행 — 암호는 **환경변수로만** 넘긴다(명령줄·파일에 남지 않는다)."""
    import subprocess
    env = dict(os.environ)
    if pw is not None:
        env["WEBOTA_OPENSSL_PW"] = pw
    try:
        p = subprocess.run(["openssl"] + args, input=data, capture_output=True, env=env)
    except FileNotFoundError:
        raise WebotaError("openssl 이 없다 — 서명에 필요하다")
    if p.returncode != 0:
        raise WebotaError("openssl 실패: %s" % p.stderr.decode(errors="replace").strip().splitlines()[-1:])
    return p.stdout


def signing_key_init(path=SIGNING_KEY, overwrite=False, passphrase=True):
    """서명 키를 만든다. ★기본은 암호를 거는 키(AES-256) — 서명할 때마다 암호를 묻는다. PC 가
    오염돼도 키를 바로 쓸 수 없게(좀비 패키지가 서명을 통과하려면 이 키가 있어야 한다).
    무인 빌드용으로만 passphrase=False(--no-passphrase). 만든 뒤 **읽혀지는지 확인**하고, 안 되면 지운다."""
    path = os.path.expanduser(path)
    if os.path.exists(path) and not overwrite:
        raise WebotaError("이미 있다: %s (새로 만들면 기기의 공개키도 USB 로 다시 심어야 한다)" % path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pw = _askpass(path, confirm=True) if passphrase else None
    args = ["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", path]
    if pw is not None:
        args += ["-aes-256-cbc", "-pass", "env:WEBOTA_OPENSSL_PW"]
    try:
        _run_openssl(args, pw)
        os.chmod(path, 0o600)
        _run_openssl(["pkey", "-in", path, "-noout"] + (["-passin", "env:WEBOTA_OPENSSL_PW"] if pw else []), pw)
        rec = _pubkey_from_key(path)
    except Exception:
        if os.path.exists(path):
            os.remove(path)                        # 망가진 키를 남기지 않는다
        raise
    with open(path + ".pub.json", "w") as f:       # 공개키는 따로 — device-config 가 암호 없이 읽는다
        json.dump(rec, f)
    return rec


def pubkey_record(path=SIGNING_KEY):
    """기기에 심을 공개키 {id, n, e} — 키 옆의 .pub.json 이 있으면 그것(암호 없이), 없으면 개인키에서."""
    path = os.path.expanduser(path)
    if os.path.exists(path + ".pub.json"):
        with open(path + ".pub.json") as f:
            return json.load(f)
    if not os.path.exists(path):
        raise WebotaError("서명 키가 없다: %s — webota.py signing-key init" % path)
    return _pubkey_from_key(path)


def _pubkey_from_key(path):
    """개인키 → 공개키 {id, n, e}. id = 모듈러스 SHA256 앞 16자. 암호 걸린 키면 암호를 묻는다."""
    pw = _askpass(path) if _encrypted(path) else None
    out = _run_openssl(["rsa", "-in", path, "-noout", "-modulus"] +
                       (["-passin", "env:WEBOTA_OPENSSL_PW"] if pw else []), pw)
    n = out.decode().strip().split("=", 1)[1].lower()
    return {"id": hashlib.sha256(bytes.fromhex(n)).hexdigest()[:16], "n": n, "e": 65537}


def sign(data, path=SIGNING_KEY):
    """매니페스트 서명 — 암호 걸린 키면 한 번 묻고(이 실행 동안 기억), openssl 에 환경변수로 넘긴다."""
    import tempfile
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        raise WebotaError("서명 키가 없다: %s — webota.py signing-key init" % path)
    pw = _askpass(path) if _encrypted(path) else None
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(data)
    try:
        return _run_openssl(["dgst", "-sha256", "-sign", path] +
                            (["-passin", "env:WEBOTA_OPENSSL_PW"] if pw else []) + [t.name], pw)   # 파일은 맨 끝
    finally:
        os.remove(t.name)


def _under(path, roots):
    return any(path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/") for d in roots)


def build_package(files, out_path, app_id, version, label=None, name=None, delete=(),
                  webota_version=None, app="app", entry="main", settings=(), data=(),
                  sign_key=SIGNING_KEY):
    """files: {기기 경로: 로컬 경로} → .wpk. 매니페스트를 돌려준다. app·entry 는 앱 교체 때
    기기 런처가 부를 모듈·함수(기본 app.main). 파일 이름 규약: <app_id>-v<판>….wpk
    settings·data: 앱이 설정·데이터를 두는 경로(파일·디렉토리). 기기는 이 아래를 코드로 보지
    않는다 — 패키지로 맞추지도, 정리하지도 않는다. settings 아래 파일을 패키지에 넣으면
    '기본값'이 되어 기기에 없을 때만 들어간다. data 아래 파일은 패키지에 넣을 수 없다."""
    bad = [r for r in files if _under(r, data)]
    if bad:
        raise WebotaError("데이터 경로의 파일은 패키지에 넣지 않는다: " + ", ".join(bad))
    man = {"format": 2, "app_id": app_id, "app": app, "entry": entry,
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
    sig = sign(mj, sign_key)                # ★서명 없는 패키지는 기기가 받지 않는다
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(PKG_MAGIC + str(len(mj)).encode() + b"\n" + mj + str(len(sig)).encode() + b"\n" + sig)
        for r in order:
            with open(files[r], "rb") as fi:
                for b in iter(lambda: fi.read(65536), b""):
                    f.write(b)
    return man


def read_manifest(path):
    with open(path, "rb") as f:
        magic = f.read(len(PKG_MAGIC))
        if magic not in (PKG_MAGIC, PKG_MAGIC_V1):
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

    def history(self, n=10):
        return self._req("GET", "/history", {"n": str(n)})[1]["history"]

    def pkg_sources(self):
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


def device_config(project, token, pkg_keys=(), github_token=None):
    """기기 설정(/webota.json) — webota 기본값 + 프로젝트의 app_id + "device" 절 + 토큰.
    ★webota.json 은 이 함수로만 만든다(앱 저장소에 생성 코드를 두지 않는다). 프로젝트 파일 예:
      {"app_id": "myapp", "device": {"ap": {"ssid": "myapp-setup", "pass": "..."},
       "hostname": "myapp", "sources": [{"github": "owner/repo"}]}}"""
    c = dict(DEVICE_DEFAULTS)
    if project.get("app_id"):
        c["app_id"] = project["app_id"]
    c.update(project.get("device") or {})
    c["token"] = token
    c["pkg_keys"] = list(pkg_keys)          # ★USB 로만 — 이 공개키로 서명된 패키지만 설치된다
    if github_token:
        c["github_token"] = github_token    # ★USB 로만 — 어떤 웹 응답에도 나가지 않는다
    return c


def ensure_token(tf):
    """토큰 파일이 없으면 만든다(랜덤 32자, 0600). 토큰을 돌려준다."""
    tf = os.path.expanduser(tf)
    if not os.path.exists(tf):
        os.makedirs(os.path.dirname(tf) or ".", exist_ok=True)
        with open(tf, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(tf, 0o600)
        print("새 토큰: %s" % tf, file=sys.stderr)
    with open(tf) as f:
        return f.read().strip()


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
    ap.add_argument("-y", "--yes", action="store_true", help="확인 질문을 생략")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    s = sub.add_parser("history"); s.add_argument("-n", type=int, default=10)
    sub.add_parser("sources", help="기기의 패키지 출처(USB 로 정한 것) — 보기만")
    s = sub.add_parser("pkg-list"); s.add_argument("--fresh", action="store_true"); s.add_argument("--src")
    s = sub.add_parser("pkg-install"); s.add_argument("url"); s.add_argument("--force", action="store_true")
    s.add_argument("--switch-app", action="store_true", help="다른 앱의 패키지로 기기를 교체"); s.add_argument("--src")
    s.add_argument("--reset-settings", action="store_true", help="선언된 설정을 패키지 기본값으로(토큰·WiFi 는 유지)")
    s.add_argument("--reset-data", action="store_true", help="★선언된 데이터를 모두 지운다")
    sub.add_parser("clean", help="지금 판에 없는 남은 코드 파일을 보여 주고 지운다(데이터 제외)")
    s = sub.add_parser("signing-key", help="패키지 서명 키 — init(만들기) · show(공개키 id)")
    s.add_argument("action", choices=("init", "show")); s.add_argument("--key", default=SIGNING_KEY)
    s.add_argument("--no-passphrase", action="store_true", help="암호 없는 키(무인 빌드용 — 권하지 않는다)")
    s = sub.add_parser("pack", help="map 대로 서명된 배포 패키지(.wpk)를 만든다")
    s.add_argument("--app-id"); s.add_argument("--version"); s.add_argument("--label")
    s.add_argument("--name"); s.add_argument("--out", default="dist"); s.add_argument("--key", default=None)
    s = sub.add_parser("device-config", help="기기 설정(webota.json) — app_id·device + 토큰 + 공개키 + GitHub 토큰")
    s.add_argument("--out", default="webota.json"); s.add_argument("--key", default=None)
    s.add_argument("--github-token-file", default=None)
    sub.add_parser("claim", help="토큰 없는 기기에 토큰을 등록(설정용 AP 로 붙어서)")
    s = sub.add_parser("token", help="새 기기 토큰을 만들어 토큰 파일에 저장")
    s.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    project = find_project()

    try:
        if a.cmd == "signing-key":
            rec = signing_key_init(a.key, passphrase=not a.no_passphrase) if a.action == "init" \
                else pubkey_record(a.key)
            print("서명 키 %s — 공개키 id %s (%d비트)" % (os.path.expanduser(a.key), rec["id"], len(rec["n"]) * 4))
            return 0
        if a.cmd == "token":
            host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host")
            tf = os.path.expanduser(a.token_file or project.get("token_file") or token_path(host or "default"))
            if os.path.exists(tf) and not a.overwrite:
                print("이미 있다: %s (--overwrite 로 새로)" % tf)
                return 0
            os.makedirs(os.path.dirname(tf) or ".", exist_ok=True)
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
                                settings=project.get("settings") or [], data=project.get("data") or [],
                                sign_key=a.key or project.get("signing_key") or SIGNING_KEY)
            print("%s — 파일 %d개, %d B, 서명됨" % (out, len(man["files"]), os.path.getsize(out)))
            return 0
        if a.cmd == "device-config":
            host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host") or "default"
            tok = a.token or ensure_token(a.token_file or project.get("token_file") or token_path(host))
            key = pubkey_record(a.key or project.get("signing_key") or SIGNING_KEY)
            gtf = os.path.expanduser(a.github_token_file or project.get("github_token_file") or GITHUB_TOKEN_FILE)
            gtok = open(gtf).read().strip() if os.path.exists(gtf) else None
            with open(a.out, "w") as f:
                json.dump(device_config(project, tok, [key], gtok), f, ensure_ascii=False, indent=2)
            os.chmod(a.out, 0o600)
            print("기기 설정: %s — 공개키 %s · GitHub 토큰 %s" % (a.out, key["id"], "있음" if gtok else "없음"))
            if gtok:
                print("  ★GitHub 토큰은 기기의 모든 코드가 읽을 수 있다(MicroPython 에는 격리가 없다) — 공개 저장소라면"
                      " 심지 말 것. 꼭 필요하면 읽기 전용·저장소 하나·짧은 만료로.", file=sys.stderr)
            return 0
        if a.cmd == "claim":
            host = a.host or os.environ.get("WEBOTA_HOST") or project.get("host") or "192.168.4.1"
            tok = a.token or ensure_token(a.token_file or project.get("token_file") or token_path(host))
            r = Client(host, "")._req("POST", "/claim", body={"token": tok})[1]
            print(r.get("msg"))
            return 0
    except WebotaError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1

    host, token = resolve(a, project)
    c = Client(host, token)
    try:
        if a.cmd == "status":
            print(json.dumps(c.status(), ensure_ascii=False, indent=2))
        elif a.cmd == "history":
            for e in c.history(a.n):
                print("%s  %-11s %-24s %s%s" % (e.get("at", ""), e.get("result", ""),
                                               e.get("label") or "-", e.get("id", ""),
                                               ("  — " + e["reason"]) if e.get("reason") else ""))
        elif a.cmd == "sources":
            for i, k in enumerate(c.pkg_sources()):
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
            print("설치 계획 — %s (%s) · 서명 %s" % (pl.get("label"), pl.get("pkg_app_id"), pl.get("key")))
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
    except WebotaError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
