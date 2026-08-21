#!/usr/bin/env python3
"""archive.py 단위 검증 — 하드웨어 없이 CPython 으로 돈다.

검증 대상은 "SD 없이 플래시로 장기 보관"의 계약이다:
  ① 14일 창 밖으로 밀려난 줄이 아카이브에 그대로 남는가(datalog 연동 포함)
  ② 파일 상한(ARCHIVE_MAX_KB)을 넘으면 **오래된 것부터** 줄고 최신은 남는가
  ③ 줄 경계를 깨지 않는가(JSONL 이 깨지면 사후 분석이 불가능하다)
  ④ 설정 스냅샷이 같은 값으로 무한 증식하지 않는가
  ⑤ 백업 번들 → 복원 왕복, 그리고 **범위 밖 값은 거부**하는가(잘못된 백업 방어)
  ⑥ 아카이브가 불가능한 상황에서도 예외를 밖으로 내보내지 않는가(측정 불가침)

실행: python3 tools/test_archive.py
"""
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import config                                    # noqa: E402

FAILS = []


def check(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAILS.append(label)


def setup(tmp):
    """config 의 경로를 임시 디렉토리로 돌린 뒤 archive 를 새로 import 한다."""
    config.DATA_DIR = tmp
    config.ARCHIVE_DIR = tmp + "/archive"
    config.ARCHIVE_MAX_KB = 1                     # 1KB — 상한 동작을 빨리 확인
    config.ARCHIVE_MIN_FREE_KB = 0                # 여유 하한은 이 테스트에서 끈다
    for mod in ("archive", "datalog"):
        sys.modules.pop(mod, None)
    import archive
    archive.log = lambda msg: None                # 테스트 출력 조용히
    return archive


def write(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    tmp = tempfile.mkdtemp(prefix="reefwiz-arc-")
    try:
        archive = setup(tmp)
        print("\n[1] 디렉토리 확보·보관")
        check(archive.ensure(), "ensure() 성공")
        check(archive.store(tmp + "/dkh.dat", "2026-08-01 05 7.7 7.6 8.8 7.7 28.7"),
              "dkh.dat 줄 보관")
        check(archive.store(tmp + "/plateau.jsonl", '{"run_started": "2026-08-01 05:00:00"}'),
              "plateau 줄 보관")
        check(not archive.store(tmp + "/wifi.json", "{}"),
              "★대상 아닌 파일은 보관하지 않는다(설정·자격증명 유출 방지)")
        body = io.open(archive.path("dkh.dat"), encoding="utf-8").read()
        check(body.endswith("\n") and "2026-08-01" in body, "줄바꿈 보정 후 기록됨")

        print("\n[2] 상한 초과 시 오래된 것부터 정리 (줄 경계 유지)")
        with io.open(archive.path("plateau.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            for i in range(200):                  # 200줄 × ~40B ≈ 8KB > 1KB 상한
                f.write(json.dumps({"n": i, "pad": "x" * 20}) + "\n")
        archive.guard()
        lines = [ln for ln in io.open(archive.path("plateau.jsonl"), encoding="utf-8")
                 if ln.strip()]
        check(len(lines) < 200, "상한 초과분이 정리됐다 (%d줄 남음)" % len(lines))
        check(os.path.getsize(archive.path("plateau.jsonl")) <= config.ARCHIVE_MAX_KB * 1024,
              "상한(1KB) 이하로 줄었다")
        parsed = [json.loads(ln) for ln in lines]  # 깨진 줄이 있으면 여기서 예외
        check(parsed[-1]["n"] == 199, "★최신 줄이 남는다(오래된 것부터 버린다)")
        check(parsed[0]["n"] > 0, "앞쪽 오래된 줄이 잘렸다")

        print("\n[3] 설정 스냅샷 — 같은 값은 다시 쓰지 않는다")
        write(tmp + "/doser_config.json", '{"target_dkh": 7.2}')
        write(tmp + "/doser_override.json", '{"ml_day": 6.0, "id": 1}')
        write(tmp + "/ph_cal.json", '{"offset": 0.02}')
        check(archive.snapshot("test-1"), "첫 스냅샷 기록됨")
        check(not archive.snapshot("test-2"), "★값이 같으면 기록하지 않는다(무한 증식 방지)")
        write(tmp + "/doser_config.json", '{"target_dkh": 7.4}')
        check(archive.snapshot("test-3"), "값이 바뀌면 다시 기록된다")
        snaps = [json.loads(ln) for ln in
                 io.open(archive.path(archive.CONFIG_SNAP), encoding="utf-8") if ln.strip()]
        check(len(snaps) == 2 and snaps[-1]["why"] == "test-3", "스냅샷 2건·이유 기록")

        print("\n[4] 백업 번들 → 복원 왕복")
        write(tmp + "/dkh.dat", "2026-08-18 05 7.723 7.663 8.830 7.701 28.7\n")
        b = archive.bundle()
        check(b["kind"] == "reefwiz-backup" and b["v"] == 1, "번들 형식")
        check(b["config"]["doser_config.json"]["target_dkh"] == 7.4, "설정이 담겼다")
        check("2026-08-18" in b["dkh_dat"], "dkh.dat 본문이 담겼다")
        check(isinstance(b["archive"].get("files"), list), "아카이브 상태가 담겼다")

        write(tmp + "/doser_config.json", '{"target_dkh": 6.5}')     # 값을 흐트려 놓고
        ok, msg = archive.restore(b)
        cur = json.load(io.open(tmp + "/doser_config.json", encoding="utf-8"))
        check(ok and cur["target_dkh"] == 7.4, "복원됨: %s" % msg)
        dat_now = io.open(tmp + "/dkh.dat", encoding="utf-8").read()
        check("2026-08-18" in dat_now and dat_now.count("\n") == 1,
              "★측정 데이터(dkh.dat)는 복원으로 덮이지 않는다")

        print("\n[5] 잘못된 백업 거부")
        ok, msg = archive.restore({"kind": "something-else"})
        check(not ok, "형식 불일치 거부: %s" % msg)
        bad = {"kind": "reefwiz-backup", "v": 1,
               "config": {"doser_config.json": {"target_dkh": 99.0}}}
        ok, msg = archive.restore(bad)
        check(not ok and "범위" in msg, "★목표 dKH 범위 밖 거부: %s" % msg)
        cur = json.load(io.open(tmp + "/doser_config.json", encoding="utf-8"))
        check(cur["target_dkh"] == 7.4, "거부된 값은 파일에 쓰이지 않았다")
        bad2 = {"kind": "reefwiz-backup", "v": 1,
                "config": {"doser_override.json": {"ml_day": 99}}}
        ok, msg = archive.restore(bad2)
        check(not ok and "범위" in msg, "★수동 도징량 범위 밖 거부: %s" % msg)
        okz, _ = archive.restore({"kind": "reefwiz-backup", "v": 1,
                                  "config": {"doser_override.json": {"ml_day": 0}}})
        check(okz, "ml_day=0(도징 정지)은 허용된다 — 원본 규약")

        # ★BT 접속 정보(2026-08-19) — 기기를 새로 굽고 복원할 때 가장 먼저 필요한 설정이다.
        #   형식이 깨진 주소는 엉뚱한 상대에 붙으려다 실패하므로 복원 단계에서 막는다.
        okbt, msgbt = archive.restore({"kind": "reefwiz-backup", "v": 1,
                                       "config": {"bt.json": {"meas": "98da,60,0fc57a"}}})
        check(okbt, "★BT 주소 복원: %s" % msgbt)
        got = json.load(io.open(tmp + "/bt.json", encoding="utf-8"))
        check(got.get("meas") == "98da,60,0fc57a", "복원된 주소가 그대로 저장됐다")
        okbad, msgbad = archive.restore({"kind": "reefwiz-backup", "v": 1,
                                         "config": {"bt.json": {"meas": "98da60 0fc57a"}}})
        check(not okbad and "형식" in msgbad, "★깨진 주소 거부: %s" % msgbad)
        check(json.load(io.open(tmp + "/bt.json", encoding="utf-8")).get("meas")
              == "98da,60,0fc57a", "거부된 주소는 파일에 쓰이지 않았다")
        b = archive.bundle()
        check("bt.json" in b["config"], "백업 번들에 bt.json 이 포함된다")

        # ★장치 목록·측정 회차(2026-08-21) — 깨진 값을 되돌리면 장비에 못 붙거나 측정 시각이
        #   조용히 기본값으로 돌아간다. 검증은 devices/schedule 이 하고 여기서 위임한다.
        okd, msgd = archive.restore({"kind": "reefwiz-backup", "v": 1, "config": {
            "devices.json": {"devices": [{"kind": "meas", "addr": "98da,60,0fc57a"},
                                         {"kind": "doser", "addr": "98da,60,056895",
                                          "sync_hours": [0]}]}}})
        check(okd, "★장치 목록 복원: %s" % msgd)
        okdbad, msgdbad = archive.restore({"kind": "reefwiz-backup", "v": 1, "config": {
            "devices.json": {"devices": [{"kind": "doser", "addr": ""}]}}})   # 측정기 없음
        check(not okdbad and "측정 장비" in msgdbad, "★측정기 없는 목록 거부: %s" % msgdbad)
        oks, msgs = archive.restore({"kind": "reefwiz-backup", "v": 1, "config": {
            "schedule.json": {"measure_hours": [5, 13, 21], "doser_slot_hour": 13}}})
        check(oks, "★측정 회차 복원: %s" % msgs)
        oksbad, msgsbad = archive.restore({"kind": "reefwiz-backup", "v": 1, "config": {
            "schedule.json": {"measure_hours": [5, 6], "doser_slot_hour": 5}}})
        check(not oksbad and "간격" in msgsbad, "★간격 2h 미만 회차 거부: %s" % msgsbad)
        b = archive.bundle()
        check("devices.json" in b["config"] and "schedule.json" in b["config"],
              "백업 번들에 devices.json·schedule.json 이 포함된다")

        print("\n[6] 아카이브 불가 상황에서도 측정을 막지 않는다")
        shutil.rmtree(archive.path("").rstrip("/"), ignore_errors=True)
        check(archive.store(tmp + "/dkh.dat", "x") in (True, False), "예외 없이 반환")
        st = archive.status()
        check(isinstance(st, dict) and st["dir"].endswith("archive"), "status() 정상 반환")
        config.ARCHIVE_ENABLED = False
        check(not archive.store(tmp + "/dkh.dat", "x"), "비활성화면 보관하지 않는다")
        check(not archive.snapshot("off"), "비활성화면 스냅샷도 안 남긴다")
        archive.guard()                                        # 예외 없이 통과해야 한다
        check(True, "비활성 상태에서 guard() 무해")
        config.ARCHIVE_ENABLED = True

        print("\n[7] datalog 연동 — 14일 창 밖 줄이 아카이브로 간다")
        config.ARCHIVE_DIR = tmp + "/archive"
        archive.ensure()
        import datalog
        datalog.log = lambda msg: None
        rows = []
        for d in range(1, 21):                     # 8/01~8/20 = 20일치(창 14일보다 길다)
            rows.append("2026-08-%02d 05 7.700 7.600 8.800 7.700 28.7" % d)
        write(tmp + "/dkh.dat", "\n".join(rows) + "\n")
        dropped = datalog._trim_dat()
        kept = [ln for ln in io.open(tmp + "/dkh.dat", encoding="utf-8") if ln.strip()]
        check(len(kept) == config.RETENTION_DAYS, "본 파일은 14일치만 남았다(%d줄)" % len(kept))
        arc = [ln for ln in io.open(archive.path("dkh.dat"), encoding="utf-8") if ln.strip()]
        check(len(dropped) == 6 and len(arc) >= 6,
              "★밀려난 6줄이 아카이브에 남았다(dropped=%d, archive=%d줄)" % (len(dropped), len(arc)))
        check(any("2026-08-01" in ln for ln in arc), "가장 오래된 기록이 보존됐다")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("ALL PASS — 실패 0건" if not FAILS
                  else "실패 %d건: %s" % (len(FAILS), FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
