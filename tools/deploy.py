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

사용:
    python3 tools/deploy.py                  # 포트 자동 탐지, 코드 + 자산
    python3 tools/deploy.py --port COM3
    python3 tools/deploy.py --with-data      # 첫 설치 — data/ 픽스처까지
    python3 tools/deploy.py --dry-run        # 실행할 mpremote 명령만 보여 준다
"""
import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
WWW = os.path.join(ROOT, "www")
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


def build_cmd(port, staged_www, with_data, force):
    """mpremote 명령 1개 — `+` 로 이어 붙여 **연결 한 번**으로 전부 올린다.
    (fs 하위명령은 인자를 여러 개 받으므로 다음 명령 앞에 `+` 로 끊어 줘야 한다.)"""
    cmd = ["mpremote"]
    if port:
        cmd += ["connect", port]             # 생략하면 mpremote 가 USB 포트를 자동 탐지한다
    cp = ["fs", "cp"] + (["-f"] if force else [])
    cmd += cp + src_files() + [":"]
    cmd += ["+"] + cp + ["-r", staged_www, ":"]
    if with_data:
        cmd += ["+"] + cp + ["-r", "data", ":"]
    return cmd


def main():
    ap = argparse.ArgumentParser(description="reefwiz-esp32 배포 — 코드·자산을 한 번에 올린다")
    ap.add_argument("--port", help="USB 시리얼 포트 (예: COM3, /dev/ttyACM0). 생략 시 자동 탐지")
    ap.add_argument("--with-data", action="store_true",
                    help="data/ 픽스처까지 올린다 — ★첫 설치에만(기기 실데이터를 덮는다)")
    ap.add_argument("--force", action="store_true",
                    help="해시가 같아도 다시 올린다(기본은 같은 파일 건너뜀)")
    ap.add_argument("--dry-run", action="store_true", help="명령만 출력하고 실행하지 않는다")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="reefwiz-deploy-")
    try:
        staged = stage_www(tmp)
        cmd = build_cmd(a.port, staged, a.with_data, a.force)
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
            print("mpremote 실행 실패: %r — `pip install mpremote` 확인" % e)
            return 1
        if rc != 0:
            print("mpremote 종료코드 %d — 포트·REPL 점유(다른 터미널이 잡고 있는지)를 확인" % rc)
            return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("배포 완료 — 리셋 후 http://reefwiz.local (또는 IP) / ops.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
