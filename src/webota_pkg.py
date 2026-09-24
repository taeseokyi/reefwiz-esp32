# ★vendored: mpy-webota v0.5.3 (device/webota_pkg.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota_pkg — 배포 패키지(.wpk) 목록 조회 · 내려받아 바로 설치.
#
# 패키지 형식(webota-pkg/1) — 기기가 **스트리밍으로** 풀 수 있게 압축·아카이브 없이 이어 붙인다:
#   b"WPK1\n" + b"<매니페스트 바이트 수>\n" + <매니페스트 JSON> + <파일1 바이트> + <파일2 바이트> ...
#   매니페스트: {"format":1, "app_id", "app", "entry", "name", "version", "label", "built_at",
#               "webota", "settings":[경로], "data":[경로],
#               "files":[{"path","size","sha","kind"?}], "delete":[...]}
#   kind "setting" = 설정 기본값 — 기기에 **없을 때만** 쓴다(있으면 건너뛴다, 덮어쓰지 않는다).
#   파일 바이트는 files 순서 그대로, 각 size 만큼.
#
# 패키지 출처(/webota.json 의 "sources" — 목록, 첫 항목이 기본. 옛 "packages" 한 개도 읽는다):
#   {"github": "owner/repo"}    GitHub Releases 의 *.wpk 체부파일(공개 저장소 — 토큰 불필요)
#   {"index": "http://.../index.json"}   [{tag, name, url, size, published}] 목록(자체 호스팅·시험)
#   설치 화면에서 저장소 URL(https://github.com/owner/repo) 이나 owner/repo 로 더하고 뺀다.
# 패키지 파일 이름 규약: <app_id>-v<판>....wpk — 목록에서 앱을 알아보는 데 쓴다(설치 때는
#   매니페스트의 app_id 로 다시 확인한다).
#
# ★TLS 인증서는 검증하지 않는다 — 기기에 CA 묶음이 없다. 파일 무결성은 매니페스트 해시로
#   확인하지만, 매니페스트도 같은 출처에서 오므로 **경로 위조에는 무력**하다(LAN·공개 저장소 전제).
import json

import webota_boot as wb

FORMAT = 1
MAGIC = b"WPK1\n"
MAX_INDEX = 512 * 1024          # 목록 응답 상한(GitHub releases JSON 은 본문까지 들어 있어 크다)
MAX_MANIFEST = 64 * 1024
_cache = {}                     # 출처 키 → {"at", "list", "err"}


# ── 최소 HTTP(S) 클라이언트(리다이렉트 · chunked) ──

def _split(url):
    scheme, _, rest = url.partition("://")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    tls = scheme == "https"
    return tls, host, int(port or (443 if tls else 80)), "/" + path


def _connect(host, port, tls):
    import socket
    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    s.settimeout(30)
    s.connect(addr)
    if not tls:
        return s
    import ssl
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        try:
            ctx.check_hostname = False
        except AttributeError:
            pass
        ctx.verify_mode = ssl.CERT_NONE
        return ctx.wrap_socket(s, server_hostname=host)
    except AttributeError:
        return ssl.wrap_socket(s, server_hostname=host)       # 옛 MicroPython


class _Body:
    """응답 본문 읽개 — Content-Length · chunked · 연결 종료를 모두 read(n) 하나로."""

    def __init__(self, sock, rf, length, chunked):
        self.sock, self.rf, self.left, self.chunked = sock, rf, length, chunked
        self.chunk_left = 0
        self.done = False

    def _raw(self, n):
        return self.rf.read(n)

    def read(self, n):
        if self.done:
            return b""
        if self.chunked:
            if self.chunk_left == 0:
                ln = self.rf.readline().strip()
                size = int(ln.split(b";")[0] or b"0", 16)
                if size == 0:
                    self.done = True
                    return b""
                self.chunk_left = size
            b = self._raw(min(n, self.chunk_left))
            self.chunk_left -= len(b)
            if self.chunk_left == 0:
                self.rf.readline()                         # 조각 끝의 CRLF
            return b
        if self.left is not None:
            if self.left <= 0:
                self.done = True
                return b""
            b = self._raw(min(n, self.left))
            self.left -= len(b)
            if not b:
                self.done = True
            return b
        b = self._raw(n)
        if not b:
            self.done = True
        return b

    def read_exact(self, n):
        buf = b""
        while len(buf) < n:
            b = self.read(n - len(buf))
            if not b:
                raise OSError("본문이 끊겼다(%d/%d)" % (len(buf), n))
            buf += b
        return buf

    def read_all(self, limit):
        buf = b""
        while True:
            b = self.read(4096)
            if not b:
                return buf
            buf += b
            if len(buf) > limit:
                raise OSError("응답이 너무 크다(>%d)" % limit)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def get(url, accept="*/*", redirects=5):
    """GET → _Body(200 일 때). 리다이렉트를 따라간다. 실패는 OSError."""
    for _ in range(redirects + 1):
        tls, host, port, path = _split(url)
        s = _connect(host, port, tls)
        req = ("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: mpy-webota\r\nAccept: %s\r\n"
               "Connection: close\r\n\r\n" % (path, host, accept))
        try:
            s.sendall(req.encode())
        except AttributeError:
            s.write(req.encode())
        rf = s.makefile("rb") if hasattr(s, "makefile") else s
        status = rf.readline().decode().split()
        code = int(status[1]) if len(status) > 1 else 0
        length, chunked, loc = None, False, None
        while True:
            ln = rf.readline()
            if not ln or ln in (b"\r\n", b"\n"):
                break
            k, _, v = ln.decode().partition(":")
            k, v = k.strip().lower(), v.strip()
            if k == "content-length":
                length = int(v)
            elif k == "transfer-encoding" and "chunked" in v.lower():
                chunked = True
            elif k == "location":
                loc = v
        if code in (301, 302, 303, 307, 308) and loc:
            s.close()
            if loc.startswith("/"):
                loc = ("https" if tls else "http") + "://" + host + (
                    "" if port in (80, 443) else ":%d" % port) + loc
            url = loc
            continue
        body = _Body(s, rf, length, chunked)
        if code != 200:
            try:
                msg = body.read_all(2048)[:200]
            except OSError:
                msg = b""
            body.close()
            raise OSError("HTTP %d %s — %s" % (code, url, msg))
        return body
    raise OSError("리다이렉트가 너무 많다: " + url)


def get_json(url, limit=MAX_INDEX, accept="application/json"):
    b = get(url, accept)
    try:
        return json.loads(b.read_all(limit))
    finally:
        b.close()


# ── 목록 ──

def _match(name, pattern):
    if pattern.startswith("*"):
        return name.endswith(pattern[1:])
    return name == pattern


def parse_source(text):
    """사용자가 넣은 문자열 → 출처 dict. 모르면 None.
    'owner/repo' · 'github.com/owner/repo' · 'https://github.com/owner/repo(.git|/releases…)'
    · 'http(s)://…/index.json'(index 출처)."""
    t = (text or "").strip()
    if not t:
        return None
    if t.endswith(".json") and "://" in t:
        return {"index": t}
    for pre in ("https://", "http://"):
        if t.startswith(pre):
            t = t[len(pre):]
    if t.startswith("www."):
        t = t[4:]
    if t.startswith("github.com/"):
        t = t[len("github.com/"):]
    parts = [x for x in t.split("/") if x]
    if len(parts) < 2 or "." in parts[0]:          # github.com 말고 다른 호스트는 모른다
        return None
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    if not parts[0] or not repo:
        return None
    return {"github": parts[0] + "/" + repo}


def source_key(src):
    return (src or {}).get("github") or (src or {}).get("index") or ""


def sources(cfg):
    """설정의 출처 목록 — 새 'sources' 가 없으면 옛 'packages' 한 개."""
    lst = cfg.get("sources")
    if isinstance(lst, list):
        return [x for x in lst if isinstance(x, dict) and source_key(x)]
    one = cfg.get("packages")
    return [one] if isinstance(one, dict) and source_key(one) else []


def app_from_asset(name):
    """'<app_id>-v<숫자>…​.wpk' → app_id. 규약을 안 따르면 None."""
    i = 0
    while True:
        i = name.find("-v", i)
        if i < 0:
            return None
        if i + 2 < len(name) and name[i + 2] in "0123456789":
            return name[:i] or None
        i += 2


def list_packages(src, now=None, max_age=60):
    """src(출처 dict) 의 [{tag, name, published, prerelease, asset, app_id, size, url}] — 최신이
    먼저. 출처마다 60초 캐시(now=None 이면 새로 가져온다)."""
    key = source_key(src)
    c = _cache.get(key)
    if now is not None and c and c["at"] is not None and now - c["at"] < max_age:
        return c["list"], c["err"]
    out, err = [], None
    try:
        if src.get("github"):
            rel = get_json("https://api.github.com/repos/%s/releases?per_page=%d"
                           % (src["github"], int(src.get("max", 15))),
                           accept="application/vnd.github+json")
            pat = src.get("asset") or "*.wpk"
            for r in rel:
                if r.get("draft"):
                    continue
                for a in r.get("assets") or []:
                    if _match(a.get("name", ""), pat):
                        out.append({"tag": r.get("tag_name"), "name": r.get("name") or r.get("tag_name"),
                                    "published": (r.get("published_at") or "")[:16].replace("T", " "),
                                    "prerelease": bool(r.get("prerelease")),
                                    "asset": a.get("name"), "app_id": app_from_asset(a.get("name", "")),
                                    "size": a.get("size"), "url": a.get("browser_download_url")})
        elif src.get("index"):
            out = get_json(src["index"])
            for p in out:
                if "app_id" not in p:
                    p["app_id"] = app_from_asset((p.get("url") or "").rsplit("/", 1)[-1])
        else:
            err = "패키지 출처가 없다 — 설치 화면에서 저장소를 더한다"
    except Exception as e:
        err = "목록 조회 실패(%s): %r" % (key, e)
    _cache[key] = {"at": now, "list": out, "err": err}
    return out, err


# ── 설치 ──

def install(url, want_app, stage_dir, sha_file, log=print):
    """패키지를 내려받아 stage_dir 에 풀고 검증한다. 커밋은 부른 쪽(webota)이 한다.
    want_app 이 있으면 매니페스트 app_id 가 같아야 한다(앱 교체는 None 으로 부른다).
    반환 (ok, 메시지, 매니페스트, 바뀐 경로 목록). 바뀌지 않은 파일은 스테이징하지 않는다."""
    import hashlib
    import binascii
    b = get(url)
    try:
        if b.read_exact(len(MAGIC)) != MAGIC:
            return False, "패키지 형식이 아니다(WPK1 머리 없음)", None, []
        n = int(_read_line(b))
        if n > MAX_MANIFEST:
            return False, "매니페스트가 너무 크다", None, []
        man = json.loads(b.read_exact(n))
        if man.get("format") != FORMAT:
            return False, "모르는 패키지 형식: %r" % man.get("format"), man, []
        if want_app and man.get("app_id") != want_app:
            return False, "다른 앱의 패키지다(기기 %s ≠ 패키지 %s)" % (want_app, man.get("app_id")), man, []
        changed = []
        for f in man.get("files") or []:
            path, size, sha = f["path"], int(f["size"]), f["sha"].lower()
            # 설정 기본값은 기기에 이미 있으면 건드리지 않는다(운영자가 바꾼 값을 지키려고).
            same = sha_file(path) == sha or (f.get("kind") == "setting" and wb.exists(path))
            h = hashlib.sha256()
            out = None
            if not same:
                wb.makedirs(wb.parent(stage_dir + path))
                out = open(wb.p(stage_dir + path), "wb")
            left = size
            try:
                while left > 0:
                    chunk = b.read(min(2048, left))
                    if not chunk:
                        raise OSError("패키지가 끊겼다: " + path)
                    h.update(chunk)
                    if out is not None:
                        out.write(chunk)
                    left -= len(chunk)
            finally:
                if out is not None:
                    out.close()
            got = binascii.hexlify(h.digest()).decode()
            if got != sha:
                return False, "해시 불일치: %s" % path, man, []
            if not same:
                changed.append(path)
        log("[webota] 패키지 %s — 파일 %d개 중 %d개 변경"
            % (man.get("label"), len(man.get("files") or []), len(changed)))
        return True, "", man, changed
    finally:
        b.close()


def _read_line(b):
    buf = b""
    while True:
        c = b.read(1)
        if not c or c == b"\n":
            return buf
        buf += c
        if len(buf) > 20:
            raise OSError("매니페스트 길이 줄이 이상하다")
