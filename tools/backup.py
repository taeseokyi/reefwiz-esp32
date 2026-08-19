#!/usr/bin/env python3
"""장기 백업 — 기기의 데이터·아카이브·설정을 PC/NAS 로 끌어온다 (SD 카드 대체).

★왜 이 스크립트가 필요한가: S3 의 USB 는 스톡 MicroPython 에서 저장소로 쓸 수 없다
  (MSC 디바이스 미구현 micropython#8426 / USB 호스트 미구현 discussions#15477).
  그래서 "USB 저장소에 쌓기" 대신 **기기 → PC 로 끌어오기**가 실제 경로다. 두 방식:

    LAN(권장, 무인 가능):  python3 tools/backup.py --http reefwiz.local
    USB 케이블(현장):      python3 tools/backup.py --usb COM3

  LAN 방식은 PC·NAS 의 스케줄러(cron / 작업 스케줄러)에 걸어 두면 사람 손이 필요 없다.
  USB 방식은 mpremote 를 그대로 호출하므로 WiFi 가 죽었을 때의 대피 경로가 된다.

받은 것은 `<out>/YYYY-MM-DD_HHMM/` 에 원본 그대로 저장한다(가공하지 않는다 — 나중에
`mpremote fs cp` 로 되돌릴 수 있어야 하므로). 기기의 14일 창 정책과 무관하게 PC 쪽은
무기한 쌓인다: 그게 이 스크립트의 목적이다.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from urllib.request import urlopen


def _stamp():
    t = time.localtime()
    return "%04d-%02d-%02d_%02d%02d" % (t[0], t[1], t[2], t[3], t[4])


def _get(url, timeout=20):
    with urlopen(url, timeout=timeout) as r:
        return r.read()


def backup_http(host, outdir):
    """LAN 백업 — /api/files 목록을 받아 /data/<경로> 를 그대로 내려받는다."""
    base = host if host.startswith("http") else "http://" + host
    listing = json.loads(_get(base + "/api/files").decode())
    files = listing.get("files") or []
    os.makedirs(outdir, exist_ok=True)
    # 설정 번들은 별도로 — 복원(POST /api/restore)에 그대로 쓸 수 있는 형태다.
    with open(os.path.join(outdir, "reefwiz-backup.json"), "wb") as f:
        f.write(_get(base + "/api/backup"))
    got, total = 0, 0
    for item in files:
        rel = item["path"]
        dst = os.path.join(outdir, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            data = _get(base + "/data/" + rel)
        except Exception as e:                       # 파일 하나가 실패해도 나머지는 받는다
            print("  ! %s 실패: %r" % (rel, e))
            continue
        with open(dst, "wb") as f:
            f.write(data)
        got += 1
        total += len(data)
        print("  + %s (%.1f KB)" % (rel, len(data) / 1024))
    arc = (listing.get("archive") or {})
    print("LAN 백업 완료 — %d개 파일 / %.1f KB → %s" % (got, total / 1024, outdir))
    if arc.get("free_kb") is not None:
        print("기기 플래시 여유: %s KB (하한 %s KB)" % (arc["free_kb"], arc.get("min_free_kb")))
    return got > 0


def backup_usb(port, outdir):
    """USB 케이블 백업 — USB CDC(mpremote) 로 /data 를 통째로 복사한다.
    ★기기가 USB 드라이브로 보이는 게 아니라, mpremote 가 파일을 읽어 온다(결과는 같다)."""
    os.makedirs(outdir, exist_ok=True)
    cmd = ["mpremote", "connect", port, "fs", "cp", "-r", ":/data", outdir]
    print("$ " + " ".join(cmd))
    try:
        rc = subprocess.call(cmd)
    except OSError as e:
        print("mpremote 실행 실패: %r — `pip install mpremote` 확인" % e)
        return False
    if rc != 0:
        print("mpremote 종료코드 %d — 포트(%s)와 REPL 점유 여부를 확인" % (rc, port))
        return False
    print("USB 백업 완료 → %s" % outdir)
    return True


def main():
    ap = argparse.ArgumentParser(description="reefwiz-esp32 장기 백업(LAN / USB)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--http", metavar="HOST", help="기기 주소 (예: reefwiz.local, 192.168.0.50)")
    g.add_argument("--usb", metavar="PORT", help="USB 시리얼 포트 (예: COM3, /dev/ttyACM0)")
    ap.add_argument("--out", default="backups", help="저장 폴더 (기본 backups/)")
    a = ap.parse_args()

    outdir = os.path.join(a.out, _stamp())
    ok = backup_http(a.http, outdir) if a.http else backup_usb(a.usb, outdir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
