#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kfont 파이프라인의 ①문자셋 수집 + ③패킹 단계 (②래스터라이즈는 gen_kfont.ps1).

사용:
    python3 tools/pack_kfont.py charset     # src/*.py 스캔 → tools/charset.txt
    powershell tools/gen_kfont.ps1          # charset.txt → glyphs.jsonl (Windows 폰트)
    python3 tools/pack_kfont.py pack        # glyphs.jsonl → src/kfont.bin

형식(src/display.py KFont 도크스트링과 동일):
    b'KF1' + count(2B LE) + codes(count*2B LE, 정렬) + widths(count*1B)
    + glyphs(count*32B — 16행 x 2B big-endian, 상위비트가 왼쪽)
"""
import json
import os
import struct
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")


def charset():
    """화면에 표시될 수 있는 모든 비ASCII 문자 — src 전체 스캔(조치 결과·로그 문구 포함)."""
    chars = set()
    for f in os.listdir(SRC):
        if f.endswith(".py"):
            with open(os.path.join(SRC, f), encoding="utf-8") as fh:
                for ch in fh.read():
                    if ord(ch) > 126:
                        chars.add(ch)
    chars = sorted(c for c in chars if unicodedata.category(c)[0] not in "C")
    with open(os.path.join(HERE, "charset.txt"), "w", encoding="utf-8") as f:
        f.write("".join(chars))
    print("charset.txt: 비ASCII %d자 (+ASCII 95자는 gen_kfont.ps1 이 추가)" % len(chars))


def pack():
    glyphs = []
    with open(os.path.join(HERE, "glyphs.jsonl"), encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                glyphs.append(json.loads(ln))
    glyphs.sort(key=lambda g: g["c"])
    blank = [chr(g["c"]) for g in glyphs if g["c"] != 32 and not any(g["r"])]
    codes = b"".join(struct.pack("<H", g["c"]) for g in glyphs)
    widths = bytes(g["w"] for g in glyphs)
    data = b"".join(b"".join(struct.pack(">H", r) for r in g["r"]) for g in glyphs)
    out = b"KF1" + struct.pack("<H", len(glyphs)) + codes + widths + data
    path = os.path.join(SRC, "kfont.bin")
    with open(path, "wb") as f:
        f.write(out)
    print("kfont.bin: %d자, %d바이트" % (len(glyphs), len(out)))
    if blank:
        print("경고 — 빈 글리프(폰트에 없는 글자?): %s" % "".join(blank))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "charset":
        charset()
    elif cmd == "pack":
        pack()
    else:
        print(__doc__)
        sys.exit(1)
