#!/usr/bin/env python3
"""배포 — 저장소의 코드·자산을 기기에 **한 번에** 올린다(mpremote 1회 호출).

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
  COM 포트가 안 올라오므로 WSL 에서 돌리면 mpremote 가 장치를 못 찾는다. PowerShell 에서:

      cd //wsl.localhost/Ubuntu/home/tsyi/work/reefwiz-esp32
      python tools/deploy.py --port COM4

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
    """기기 루트로 갈 .py 목록(저장소 상대경로). `.py` 만 고르므로 `__pycache__` 는 자연히
    빠진다 — .pyc 를 올리면 기기 용량만 먹고 쓰이지 않는다."""
    out = []
    for name in sorted(os.listdir(SRC)):
        if name.endswith(".py"):
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


def stage_buildinfo(tmp):
    """`buildinfo.py` 를 만들어 경로를 돌려준다 — 기기의 version.py 가 이걸 읽는다.

    ★왜 배포가 만드나: 커밋 해시는 커밋 시점에 정해지므로 저장소 안의 파일에 미리 적어 둘
      수 없다(적으면 항상 한 판 뒤처진다). 배포는 '올리는 순간'을 알고 있는 유일한 지점이다.
    ★dirty(미커밋 변경 있음)를 반드시 남긴다 — 손으로 고친 채 올린 판은 해시가 가리키는
      커밋과 **내용이 다르다**. 그걸 숨기면 버전 표시가 거짓말이 된다."""
    commit = _git("rev-parse", "--short=7", "HEAD")
    if commit:
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
        with open(plain, "rb") as f_in, gzip.open(plain + ".gz", "wb") as f_out:
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


def build_cmd(port, staged_www, with_data, force, stamp=None):
    """mpremote 명령 1개 — `+` 로 이어 붙여 **연결 한 번**으로 전부 올린다.
    (fs 하위명령은 인자를 여러 개 받으므로 다음 명령 앞에 `+` 로 끊어 줘야 한다.)"""
    cmd = mpremote_cmd()
    if port:
        cmd += ["connect", port]             # 생략하면 mpremote 가 USB 포트를 자동 탐지한다
    cp = ["fs", "cp"] + (["-f"] if force else [])
    cmd += cp + src_files() + ([stamp] if stamp else []) + [":"]
    cmd += ["+"] + cp + ["-r", staged_www, ":"]
    if with_data:
        cmd += ["+"] + cp + ["-r", "data", ":"]
    return cmd


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
    a = ap.parse_args()

    print("%s — 펌웨어 v%s (%s 릴리스)" % (version.MODEL, version.VERSION, version.RELEASED))
    tmp = tempfile.mkdtemp(prefix="reefwiz-deploy-")
    try:
        stamp = None if a.no_stamp else stage_buildinfo(tmp)
        staged = stage_www(tmp)
        cmd = build_cmd(a.port, staged, a.with_data, a.force, stamp)
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
