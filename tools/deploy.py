#!/usr/bin/env python3
"""배포 — 저장소의 코드·자산을 기기에 **한 번에** 올린다.

두 경로가 있고 **올리는 파일 묶음은 같다**(아래 표 · gzip · 배포 스탬프):
  - `--http HOST` (2026-09-24~, ★기본으로 쓴다): WiFi 로 원격 배포 — webota(:8266) 가 바뀐
    파일만 받아 부팅 때 적용하고, 새 판이 90초 버티지 못하면 스스로 롤백한다. USB·Windows 불필요.
        python3 tools/deploy.py --http 192.168.0.47 [--dry-run]
  - USB(mpremote 1회 호출): 첫 설치 · webota 자체 설치 · 원격이 막혔을 때의 복구 경로.

★왜 이 스크립트가 있나: 저장소와 기기 파일시스템이 1:1 이 아닌 지점이 딱 하나다 —
  `src/*.py` 는 **기기 루트**로 가야 한다(MicroPython 은 부팅 시 루트의 `main.py` 를
  실행한다). 그래서 `mpremote fs cp -r src/ :` 는 쓸 수 없다: 그러면 `/src/main.py` 가
  되어 부팅해도 아무 일이 없다. `www/` · `data/` 는 이름 그대로 1:1 이라 재귀 복사가 그대로
  먹는다. 결국 손으로 하면 명령이 두세 줄로 갈라지는데, 갈라지면 하나를 빼먹는다.

    저장소        기기
    ---------     ---------------
    src/*.py  →   /*.py   (루트)
    www/      →   /www/
    data/     →   /data/   (첫 설치에만 — 아래 참조)

★쉘 글롭에 의존하지 않는다: PowerShell 은 `src/*.py` 를 확장하지 않고 네이티브 exe 에
  문자열 그대로 넘긴다. Git Bash 에서만 되는 명령을 문서에 적어 두면 이 PC 에서 반쯤
  실패한다(개발 환경이 PowerShell + Git Bash 혼용이다). 여기서는 파이썬이 목록을 만든다.

★`chart.umd.min.js` 는 **gzip 으로** 올린다(201KB → 68KB). 웹서버가 `.gz` 가 있으면
  `Content-Encoding: gzip` 으로 서빙한다(webserver._static). 원본 .js 는 올리지 않는다.

★`/data` 는 기본으로 **올리지 않는다**: 저장소 `data/` 는 원본 실데이터의 최근 14일치
  픽스처인데, 이미 돌고 있는 기기에 덮어쓰면 dkh.dat·이력이 과거로 되돌아가 도저 계산의
  수준·추세가 튄다(archive.restore 가 측정 데이터를 복원하지 않는 것과 같은 이유).
  첫 설치에서 이력을 이어 가려면 `--with-data` 를 명시한다.

★재배포는 싸다: mpremote 는 SHA256 이 같은 파일을 건너뛴다(강제하려면 `--force`).

★배포 스탬프(2026-08-30): 코드와 함께 `buildinfo.py`(커밋 해시·미커밋 여부·배포 시각·배포자)
  를 만들어 올린다. 기기의 `GET /api/version` 과 정비페이지가 이 값을 그대로 보여 주므로,
  화면에서 읽은 버전으로 저장소의 그 커밋을 정확히 되짚을 수 있다. 저장소에는 커밋하지
  않는다(생성물). `--no-stamp` 로 끄면 기기 표시가 `+dev` 가 된다 — '어느 커밋인지 보증
  없음'이라는 뜻이다. 판(version.VERSION) 자체를 올리는 절차는 `src/version.py` 헤더 참조.

사용:
    python3 tools/deploy.py                  # 포트 자동 탐지, 코드 + 자산
    python3 tools/deploy.py --port COM3
    python3 tools/deploy.py --with-data      # 첫 설치 — data/ 픽스처까지
    python3 tools/deploy.py --dry-run        # 실행할 mpremote 명령만 보여 준다

★이 PC(WSL2)에서는 **Windows 쪽 파이썬으로** 실행한다(2026-08-29): usbipd 가 없어 WSL 에는
  COM 포트가 안 올라오므로 WSL 에서 돌리면 mpremote 가 장치를 못 찾는다. WSL 에서 래퍼를 쓴다
  (2026-09-24 — `C:/Temp` 로 복사해 Windows 파이썬으로 이 스크립트를 부르고 해시를 넘긴다):

      ./tools/deploy_wsl.sh --port COM4 --reset

  ★PATH 를 맞출 필요가 없다(2026-08-30): `mpremote.exe` 가 PATH 에 없으면 스크립트가
    `python -m mpremote` 로 알아서 돌아간다(mpremote_cmd).

  (mpremote 가 없으면 `python -m pip install mpremote`.)
  ※시리얼 브릿지(tools/mpy_bridge.sh)는 REPL 실행·소량 확인용이다. 파일 배포는 이 경로가
    정석이다 — mpremote 는 SHA256 이 같은 파일을 건너뛰고, 전송 실패를 조용히 넘기지 않는다.
"""
import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
WWW = os.path.join(ROOT, "www")
sys.path.insert(0, SRC)                  # version.py 를 그대로 읽는다 — 버전 문자열을 베끼지 않는다
import version                           # noqa: E402  (src/version.py — config 만 의존해 PC 에서도 돈다)
# gzip 으로 올릴 자산 — 저장소에는 원본만 두고 여기서 압축한다(생성물을 커밋하지 않는다).
GZIP_ASSETS = ("vendor/chart.umd.min.js",)


def src_files():
    """기기 루트로 갈 파일 목록(저장소 상대경로) — `.py` 와 webota 설치 화면(`webota_ui.html`).
    확장자로 고르므로 `__pycache__` 는 자연히 빠진다 — .pyc 를 올리면 용량만 먹고 쓰이지 않는다."""
    out = []
    for name in sorted(os.listdir(SRC)):
        if name.endswith((".py", ".html")):
            out.append(os.path.join("src", name))
    if not out:
        raise SystemExit("src/*.py 를 찾지 못했다 — 저장소가 온전한지 확인")
    return out


def _git(*args):
    """git 한 줄 실행 — 저장소가 아니거나 git 이 없으면 None(배포는 계속된다)."""
    try:
        out = subprocess.check_output(("git",) + args, cwd=ROOT,
                                      stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.decode("utf-8", "replace").strip()


def _head_from_files():
    """`.git` 을 **파일로 직접 읽어** 커밋 해시를 구한다 — git 실행파일 없이.

    ★왜 필요한가(2026-08-30 실측): 이 PC 의 정규 배포 경로는 **Windows 파이썬이 UNC 경로
      (`//wsl.localhost/...`)의 저장소를 읽는 것**인데, 그쪽에는 git 이 없어 `git rev-parse`
      가 조용히 실패한다. 그러면 스탬프가 늘 `+dev` 가 되어 **버전 스탬프의 존재 이유가
      통째로 사라진다**(어느 커밋이 올라갔는지 모른다). 해시는 평범한 파일에 적혀 있으므로
      git 없이도 읽을 수 있다.
    ★미커밋 변경(dirty) 여부는 이 방법으로 알 수 없다 — 그건 작업트리 전체를 봐야 한다.
      모르는 것을 '깨끗하다'고 적으면 거짓이 되므로 None(불명)으로 남긴다."""
    try:
        with open(os.path.join(ROOT, ".git", "HEAD")) as f:
            head = f.read().strip()
    except OSError:
        return None
    if not head.startswith("ref:"):
        return head[:7] or None                  # detached HEAD — 해시가 그대로 들어 있다
    ref = head[4:].strip()
    try:
        with open(os.path.join(ROOT, ".git", *ref.split("/"))) as f:
            return f.read().strip()[:7] or None
    except OSError:
        pass
    try:                                          # 느슨한 ref 가 없으면 packed-refs 를 본다
        with open(os.path.join(ROOT, ".git", "packed-refs")) as f:
            for ln in f:
                parts = ln.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0][:7]
    except OSError:
        pass
    return None


def stage_buildinfo(tmp, given_commit=None, given_dirty=None):
    """`buildinfo.py` 를 만들어 경로를 돌려준다 — 기기의 version.py 가 이걸 읽는다.

    ★왜 배포가 만드나: 커밋 해시는 커밋 시점에 정해지므로 저장소 안의 파일에 미리 적어 둘
      수 없다(적으면 항상 한 판 뒤처진다). 배포는 '올리는 순간'을 알고 있는 유일한 지점이다.
    ★dirty(미커밋 변경 있음)를 반드시 남긴다 — 손으로 고친 채 올린 판은 해시가 가리키는
      커밋과 **내용이 다르다**. 그걸 숨기면 버전 표시가 거짓말이 된다."""
    commit = _git("rev-parse", "--short=7", "HEAD")
    if given_commit:
        # 복사본에서 배포할 때(tools/deploy_wsl.sh) — 원본 저장소에서 구한 값을 그대로 쓴다.
        commit, dirty = given_commit, given_dirty
    elif commit:
        dirty = bool(_git("status", "--porcelain"))
    else:
        # git 이 없다(Windows 쪽 배포) — 해시만 파일에서 읽고 dirty 는 '불명'으로 둔다.
        commit, dirty = _head_from_files(), None
    who = _git("config", "user.email") or os.environ.get("USER") or os.environ.get("USERNAME")
    path = os.path.join(tmp, "buildinfo.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 생성 파일 — tools/deploy.py 가 배포마다 새로 만든다. 저장소에 없다(커밋 금지).\n")
        f.write("COMMIT = %r\n" % commit)
        f.write("DIRTY = %r\n" % dirty)
        f.write("BUILT_AT = %r\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("BUILT_BY = %r\n" % who)
    tag = (commit + ("-dirty" if dirty else "")) if commit else "dev(git 정보 없음)"
    print("  스탬프 %s v%s+%s" % (version.MODEL, version.VERSION, tag))
    if dirty:
        print("  ! 미커밋 변경이 있는 채로 올린다 — 기기 버전에 '-dirty' 로 표시된다")
    elif dirty is None and commit:
        print("  (git 실행파일이 없어 미커밋 변경 여부는 확인하지 못했다 — 해시만 기록)")
    return path


def stage_www(tmp):
    """www/ 를 임시 디렉토리에 그대로 복사하되 gzip 대상은 .gz 로 바꿔 넣는다.

    ★왜 스테이징을 하나: 재귀 복사 한 번으로 /www 전체를 올리고 싶은데, 원본 .js 를 그대로
      올리면 201KB 를 쓰고 서버는 .gz 를 먼저 찾으므로 쓰이지도 않는다. 압축본을 저장소에
      커밋하지 않는 이유는 생성물이기 때문이다(원본만 관리한다)."""
    staged = os.path.join(tmp, "www")
    shutil.copytree(WWW, staged)
    for rel in GZIP_ASSETS:
        plain = os.path.join(staged, *rel.split("/"))
        if not os.path.exists(plain):
            print("  ! %s 없음 — gzip 생략" % rel)
            continue
        before = os.path.getsize(plain) / 1024.0
        # ★mtime=0(2026-09-24): gzip 헤더에 압축 시각이 들어가면 내용이 같아도 매번 해시가
        #   달라져, 원격 배포(바뀐 파일만)가 68KB 를 매번 다시 올린다.
        with open(plain, "rb") as f_in, open(plain + ".gz", "wb") as raw, \
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as f_out:
            shutil.copyfileobj(f_in, f_out)
        after = os.path.getsize(plain + ".gz") / 1024.0
        os.remove(plain)                     # 기기에는 .gz 만 올린다
        print("  gzip %s → %s.gz (%.0f → %.0f KB)" % (rel, rel, before, after))
    return staged


def mpremote_cmd():
    """mpremote 를 어떻게 부를지 정한다 — PATH 의 실행파일, 없으면 `python -m mpremote`.

    ★왜(2026-08-30 실측): Windows 에서 pip 가 스크립트를 PATH 밖에 깔면 `mpremote.exe` 가
      없다. 종전에는 그때마다 PATH 를 손으로 맞추는 절차를 문서에 적어 뒀는데(파일 헤더),
      그 절차 자체가 실패 지점이었다 — 스크립트 경로를 찾아 넣어도 exe 가 거기 없을 수 있다.
      **모듈 실행은 같은 파이썬에 설치돼 있으면 항상 된다**(`python -m mpremote`)."""
    if shutil.which("mpremote"):
        return ["mpremote"]
    return [sys.executable, "-m", "mpremote"]


def build_cmd(port, staged_www, with_data, force, stamp=None, webota_config=None):
    """mpremote 명령 1개 — `+` 로 이어 붙여 **연결 한 번**으로 전부 올린다.
    (fs 하위명령은 인자를 여러 개 받으므로 다음 명령 앞에 `+` 로 끊어 줘야 한다.)"""
    cmd = mpremote_cmd()
    if port:
        cmd += ["connect", port]             # 생략하면 mpremote 가 USB 포트를 자동 탐지한다
    cp = ["fs", "cp"] + (["-f"] if force else [])
    cmd += cp + src_files() + ([stamp] if stamp else []) + [":"]
    if webota_config:                        # 원격 배포 설정(토큰) — 이름을 바꿔 루트에 둔다
        cmd += ["+"] + cp + [webota_config, ":webota.json"]
    cmd += ["+"] + cp + ["-r", staged_www, ":"]
    if with_data:
        cmd += ["+"] + cp + ["-r", "data", ":"]
    return cmd


def http_files(staged_www, stamp):
    """원격 배포용 {기기 경로: 로컬 경로} — USB 경로(build_cmd)와 **같은 묶음**이다."""
    files = {"/" + os.path.basename(f): os.path.join(ROOT, f) for f in src_files()}
    if stamp:
        files["/buildinfo.py"] = stamp
    for dp, _dn, fn in os.walk(staged_www):
        for n in fn:
            lp = os.path.join(dp, n)
            files["/www/" + os.path.relpath(lp, staged_www).replace(os.sep, "/")] = lp
    return files


def webota_drift():
    """vendored webota 가 원본(mpy-webota)과 어긋났는지 — 경고 문구 목록(없으면 빈 목록).
    머리 한 줄(출처 주석)을 뺀 본문을 비교한다. 원본이 없는 PC 면 검사하지 않는다."""
    src = os.path.expanduser(os.environ.get("WEBOTA_SRC", "~/work/mpy-webota"))
    if not os.path.isdir(os.path.join(src, "device")):
        return []
    out = []

    def body(path, drop_head=0, drop_tail=False):
        with open(path, encoding="utf-8") as f:
            t = f.read()
        if drop_head:
            t = t.split("\n", drop_head)[-1]
        if drop_tail:                                # HTML 은 출처 주석이 맨 끝에 있다
            t = t.rsplit("<!-- ★vendored:", 1)[0]
        return t

    pairs = (("src/webota.py", "device/webota.py", 1, 0),
             ("src/webota_boot.py", "device/webota_boot.py", 1, 0),
             ("src/webota_pkg.py", "device/webota_pkg.py", 1, 0),
             ("tools/webota.py", "client/webota.py", 2, 1),
             ("src/webota_ui.html", "device/webota_ui.html", "tail", 0))
    for mine, orig, head, orig_head in pairs:
        try:
            if head == "tail":
                a = body(os.path.join(ROOT, mine), drop_tail=True)
            else:
                a = body(os.path.join(ROOT, mine), head)
            b = body(os.path.join(src, orig), orig_head)
        except OSError as e:
            out.append("%s: %s" % (mine, e))
            continue
        if a != b:
            out.append("%s ≠ mpy-webota/%s" % (mine, orig))
    return out


def stamp_label(stamp):
    """스탬프 → 배포 라벨 `v1.1.0+abc1234[-dirty]` — 기기의 webota 이력에 남는다."""
    info = {}
    if stamp:
        with open(stamp, encoding="utf-8") as f:
            exec(f.read(), info)                  # 방금 만든 생성물(COMMIT·DIRTY)이다
    commit = info.get("COMMIT") or "dev"
    return "v%s+%s%s" % (version.VERSION, commit, "-dirty" if info.get("DIRTY") else "")


def deploy_http(a, staged, stamp):
    """webota 로 원격 배포 — 바뀐 파일만 올리고, 새 판 확인(또는 롤백)까지 기다린다."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import webota as cl                       # tools/webota.py (mpy-webota 클라이언트 vendored)
    project = cl.find_project(ROOT)
    ns = argparse.Namespace(host=a.http, token=None, token_file=None)
    host, token = cl.resolve(ns, project)
    c = cl.Client(host, token, settings=project.get("settings") or [], data=project.get("data") or [])
    drift = webota_drift()
    if drift:
        print("  ! vendored webota 가 원본과 다르다 — tools/sync_webota.sh 로 맞춘다:")
        for d in drift:
            print("      " + d)
    files = http_files(staged, stamp)
    crit = [r for r in files if r in cl.CRITICAL]
    rsha = c.sha(crit) if crit else {}
    touched = [r for r in crit if rsha.get(r) != cl.sha_of(files[r])]
    if not a.dry_run and not cl._confirm_critical(touched, a.yes):
        return 1
    print("  원격 %s:%d — 파일 %d개 중 바뀐 것만 올린다" % (c.host, c.port, len(files)))
    # ★스탬프(buildinfo.py)는 배포 시각이 들어 있어 늘 다르다 — 그것만 다르면 배포할 게 없다
    #   (코드가 같은데 리셋해 회차를 위협할 이유가 없다). 코드가 바뀌면 함께 올라간다.
    rs = c.sha(files.keys())
    changed = [r for r in files if rs.get(r) != cl.sha_of(files[r])]
    if set(changed) <= {"/buildinfo.py"}:
        print("  바뀐 코드 없음 — 기기가 이미 이 판이다(배포 안 함)")
        return 0
    try:
        label = stamp_label(stamp)
        print("  라벨 %s — 기기 이력: python3 tools/webota.py history" % label)
        res = c.deploy(files, force=a.force_guard, dry_run=a.dry_run, label=label)
    except cl.WebotaError as e:
        print("✗ %s" % e)
        return 1
    if res["result"] == "ok":
        print("배포 완료 — 버전: curl http://%s/api/version" % c.host)
    return 0


APP_ID = "reefwiz-controller"             # 패키지·기기(/webota.json)가 같은 값이어야 설치된다


def pack(a, staged, stamp):
    """배포 패키지(.wpk) — 원격·USB 와 **같은 묶음**. 기기 설치 화면(:8266)에서 골라 설치한다.
    릴리스는 tools/release.sh 가 이걸 불러 GitHub Releases 에 올린다."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import webota as cl
    label = stamp_label(stamp)
    out = os.path.join(a.pack, "%s-%s.wpk" % (APP_ID, label))
    project = cl.find_project(ROOT)             # 설정·데이터 경로의 단일 출처(webota.project.json)
    man = cl.build_package(http_files(staged, stamp), out, APP_ID, version.VERSION, label,
                           name="%s v%s" % (version.MODEL, version.VERSION),
                           webota_version=_vendored_webota(),
                           settings=project.get("settings") or [], data=project.get("data") or [])
    print("  패키지 %s — 파일 %d개, %d KB" % (out, len(man["files"]), os.path.getsize(out) // 1024))
    return 0


def _vendored_webota():
    with open(os.path.join(SRC, "webota.py"), encoding="utf-8") as f:
        for ln in f:
            if ln.startswith("VERSION = "):
                return ln.split('"')[1]
    return None


def main():
    # ★Windows 콘솔(cp949)에서 죽지 않게(2026-08-29 실측): 진행 문구의 '—' 를 인코딩하지 못해
    #   **전송이 끝난 뒤** UnicodeEncodeError 로 죽었다. 배포는 성공했는데 실패로 보인다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="reefwiz-esp32 배포 — 코드·자산을 한 번에 올린다")
    ap.add_argument("--port", help="USB 시리얼 포트 (예: COM3, /dev/ttyACM0). 생략 시 자동 탐지")
    ap.add_argument("--with-data", action="store_true",
                    help="data/ 픽스처까지 올린다 — ★첫 설치에만(기기 실데이터를 덮는다)")
    ap.add_argument("--force", action="store_true",
                    help="해시가 같아도 다시 올린다(기본은 같은 파일 건너뜀)")
    ap.add_argument("--no-stamp", action="store_true",
                    help="빌드 스탬프(buildinfo.py)를 올리지 않는다 — 기기 버전 표시가 '+dev' 가 된다")
    ap.add_argument("--dry-run", action="store_true", help="명령만 출력하고 실행하지 않는다")
    ap.add_argument("--commit", help="스탬프에 적을 커밋 해시 — 저장소 복사본에서 배포할 때"
                    "(tools/deploy_wsl.sh 가 넘긴다). 주면 git 조회를 하지 않는다")
    ap.add_argument("--dirty", choices=("0", "1"),
                    help="--commit 과 함께: 미커밋 변경 여부(1=있음). 생략하면 '불명'")
    ap.add_argument("--http", metavar="HOST",
                    help="WiFi 원격 배포(webota :8266) — 예: 192.168.0.47. USB 가 필요 없다")
    ap.add_argument("--force-guard", action="store_true",
                    help="--http: 기기 가드(측정 중·회차 임박) 무시 — 회차가 깨질 수 있다")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="--http: boot.py·main.py·webota*.py 변경 확인을 생략")
    ap.add_argument("--webota-config", metavar="PATH",
                    help="USB: 기기 루트 /webota.json 으로 함께 올릴 파일(tools/deploy_wsl.sh 가 만든다)")
    ap.add_argument("--pack", metavar="DIR",
                    help="배포 패키지(.wpk)를 DIR 에 만든다 — 기기 설치 화면용(tools/release.sh)")
    a = ap.parse_args()
    if a.http and a.with_data:
        ap.error("--http 에는 --with-data 가 없다 — 운영 중인 기기의 실측 데이터를 덮는다")

    print("%s — 펌웨어 v%s (%s 릴리스)" % (version.MODEL, version.VERSION, version.RELEASED))
    tmp = tempfile.mkdtemp(prefix="reefwiz-deploy-")
    try:
        dirty = None if a.dirty is None else a.dirty == "1"
        stamp = None if a.no_stamp else stage_buildinfo(tmp, a.commit, dirty)
        staged = stage_www(tmp)
        if a.pack:
            return pack(a, staged, stamp)
        if a.http:
            return deploy_http(a, staged, stamp)
        cmd = build_cmd(a.port, staged, a.with_data, a.force, stamp, a.webota_config)
        # 임시 경로가 길어 읽기 어려우므로 출력에서는 줄여 보여 준다(실행은 원본 그대로).
        print("$ " + " ".join(c.replace(tmp + os.sep, "<tmp>/") for c in cmd))
        if a.dry_run:
            return 0
        if not a.with_data:
            print("  (data/ 는 올리지 않는다 — 첫 설치라면 --with-data)")
        try:
            # ★cwd=ROOT: 명령의 소스 경로가 저장소 상대경로다(출력이 읽히도록). 스크립트를
            #   어느 디렉토리에서 불러도 같은 결과가 나온다.
            rc = subprocess.call(cmd, cwd=ROOT)
        except OSError as e:
            print("mpremote 실행 실패: %r — `python -m pip install mpremote` 확인" % e)
            return 1
        if rc != 0:
            print("mpremote 종료코드 %d — 포트·REPL 점유(다른 터미널이 잡고 있는지)를 확인" % rc)
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("배포 완료 — 리셋 후 http://reefwiz.local (또는 IP) / ops.html")
    print("  버전 확인: curl http://reefwiz.local/api/version  (정비페이지 맨 아래에도 표시)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
