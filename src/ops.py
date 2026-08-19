# 조치(recovery) 도구 — 측정이 끊기거나 이상 종료했을 때 웹(/ops.html)에서 복구한다.
# 원본 운영에서 손이 가던 일들을 그대로 도구화했다:
#   · 에러 래치 해제      — 원본은 "dkh.dat 마지막 에러 줄을 수동으로 지우기 전까지" 매 회차
#                           측정을 건너뛴다. 그 편집을 버튼 하나로.
#   · KCl 소크 강제 복원  — 비상정리가 실패하면 프로브가 KCl 없이 방치된다(원본 **경고).
#   · 액체 위치 수동 지정 — 위치 불명(UNKNOWN)이면 자동 정리가 동결된다. 눈으로 확인한
#                           실제 위치를 알려주고 정리를 재개.
#   · 측정 중단           — 매달린 회차를 끊는다(비상정리는 실행 → 프로브 보호).
#   · 링크 점검/HC-05 리셋 — 무선 구간 사망 판별과 하드 재기동.
#   · BT 대상 전환        — HC-05 가 1개라 측정기·도저를 번갈아 붙는다. 전환 후
#                           신원 서명을 확인하고, 불일치면 동결(오장비 명령 방지).
#   · 명령 콘솔           — 임의 펌웨어 명령 송신(최후 수단, 전량 로깅).
#
# UART 안전: 장비를 만지는 작업은 전부 state.put_job 으로 큐잉하고 메인 루프가 실행한다
# (웹 스레드가 측정 중 UART 에 끼어들면 응답이 뒤섞여 오측정이 된다).
import gc
import os
import time

import archive
import config
import datalog
import doser
import link
import measure
import rwtime
import state
import wifinet

JOB_KINDS = ("measure", "calref", "cleanup", "cmd", "link", "hc05_reset",
             "bt_target", "doser_query", "doser_apply", "doser_preview", "doser_clock")


# ─────────────────────────────────────────────
# 즉시 실행 (파일·메모리만 — UART 미접촉)
# ─────────────────────────────────────────────

def log_tail(n=40, path=None):
    """로그 마지막 n줄 — 정비페이지에서 진행 상황·경고 확인용. 파일 끝만 읽는다.
    읽기 창은 요청 행수에 비례(행당 ~100B 여유), 상한 32KB — n=300 도 커버."""
    path = path or datalog.LOG_FILE
    window = min(32768, max(8192, n * 100))
    try:
        size = os.stat(path)[6]
        with open(path) as f:
            if size > window:
                f.seek(size - window)
                f.readline()          # 잘린 첫 줄 버림
            lines = [ln.rstrip("\n") for ln in f.read().split("\n") if ln.strip()]
        return lines[-n:]
    except OSError:
        return []


def clear_error_latch():
    """dkh.dat 마지막 줄이 에러 표식(전부 0)이면 그 줄을 제거해 래치를 해제한다.
    원본의 '수동으로 마지막 에러 줄 제거' 절차 그대로 — 장비를 점검한 뒤 눌러야 한다.
    반환: (ok, msg)"""
    if not datalog.last_dat_is_error():
        return False, "에러 래치 상태가 아닙니다(마지막 줄이 정상 측정)"
    try:
        with open(datalog.DAT_FILE) as f:
            lines = [ln for ln in f.read().split("\n") if ln.strip()]
        removed = lines.pop()
        tmp = datalog.DAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            if lines:
                f.write("\n".join(lines) + "\n")
        os.rename(tmp, datalog.DAT_FILE)
    except OSError as e:
        return False, "dkh.dat 편집 실패: %r" % e
    datalog.log("[조치] 에러 래치 해제 — 제거한 줄: %s" % removed)
    return True, "래치 해제 — 제거: %s" % removed


def set_liquid(chamber, holding):
    """액체 위치를 눈으로 확인한 실제 상태로 지정(불명 동결 해제용).
    잘못 지정하면 잘못된 이송이 일어나므로 실물 확인 후에만 쓴다.
    값은 내부 상태 토큰(KCL/EMPTY/TANK/REF/UNKNOWN — 원본 코드와 동일) 그대로 받는다."""
    ok_ch = ("KCL", "EMPTY", "TANK", "REF", "UNKNOWN")
    ok_hd = ("EMPTY", "TANK", "UNKNOWN")
    if chamber not in ok_ch or holding not in ok_hd:
        return False, "측정 챔버 %s / 홀딩 챔버 %s 중 하나여야 합니다" % (ok_ch, ok_hd)
    if state.measuring:
        return False, "측정 중에는 변경할 수 없습니다(측정 중단 후 시도)"
    measure._liquid["chamber"], measure._liquid["holding"] = chamber, holding
    datalog.log("[조치] 액체 위치 수동 지정 — 측정 챔버=%s 홀딩 챔버=%s" % (chamber, holding))
    return True, "지정됨 — 측정 챔버=%s / 홀딩 챔버=%s" % (chamber, holding)


def request_abort():
    """측정 중단 요청 — 측정 루프가 12초 내 반응, 비상정리(KCl 소크)까지 수행한다.
    에러 표식을 쓰지 않으므로 래치가 걸리지 않는다(다음 회차 정상 진행)."""
    if not state.measuring:
        return False, "측정 중이 아닙니다"
    state.abort_requested = True
    datalog.log("[조치] 측정 중단 요청 — 비상정리 후 종료 예정")
    return True, "중단 요청됨 — 비상정리 진행(로그 확인)"


def snapshot():
    """현재 상태 종합 — 대시보드·정비페이지가 이 데이터를 쓴다(유일한 조작 경로)."""
    latest = datalog._read_json(datalog.LATEST_FILE, {})
    last_run = datalog.last_plateau()      # 마지막 1건만 파싱(힙 절약)
    hist = doser.load_history()
    last_dose = hist[-1] if hist else {}
    lines = datalog.read_dat_lines()
    return {
        "now": rwtime.stamp(),
        "measuring": state.measuring,
        "abort_requested": state.abort_requested,
        "job_busy": state.job_busy,
        "job_pending": state.job["kind"] if state.job else None,
        "job_result": state.job_result,
        "error_latch": datalog.last_dat_is_error(),
        # HC-05 1개를 번갈아 쓰므로 "지금 어느 장비에 붙어 있는지"가 조치 판단의 전제다.
        # frozen 이 비어 있지 않으면 신원 검증 실패 상태 — 어떤 명령도 나가지 않는다.
        "link": link.status(),
        # 장기 저장소 — SD 대신 플래시 아카이브(용량이 차면 아카이브가 먼저 줄어든다).
        "archive": archive.status(),
        "liquid": dict(measure._liquid),
        "dat_rows": len(lines),
        "last_dat": " ".join(lines[-1]) if lines else None,
        "latest": latest,
        "last_run": {
            "run_started": last_run.get("run_started"),
            "mode": last_run.get("mode"),
            "completed": last_run.get("completed"),
            "tank_flat_n": last_run.get("tank_flat_n"),
            "ref_flat_n": last_run.get("ref_flat_n"),
            "tank_reads": len(last_run.get("tank") or []),
            "ref_reads": len(last_run.get("ref") or []),
            "co2_suspect": last_run.get("co2_suspect"),
            "ref_net_mph": last_run.get("ref_net_mph"),
        },
        "doser": {
            "ts": last_dose.get("ts"), "mode": last_dose.get("mode"),
            "lrt_new": last_dose.get("lrt_new"), "applied": last_dose.get("applied"),
            "ml_day_new": last_dose.get("ml_day_new"), "note": last_dose.get("note"),
            "auto_apply": config.AUTO_APPLY,
        },
        "schedule": {"hours": list(config.MEASURE_HOURS),
                     "doser_slot": config.DOSER_SLOT_HOUR},
        "wifi": wifinet.status(),
        "heap_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
    }


# ─────────────────────────────────────────────
# 큐 실행 (메인 루프에서만 — UART 접촉)
# ─────────────────────────────────────────────

def _job_cmd(args):
    """임의 펌웨어 명령 송신 — 에러 후 수동 정리의 핵심 도구. 전량 로깅한다.
    target=meas(측정 펌웨어) | doser(도저 펌웨어).

    사용자 워크플로(2026-08-14): 에러 발생 → 링크 회복 → 직접 명령을 던져 정리.
    그래서 meas 명령은 ①송신 전 ensure_link(죽었으면 자동 재연결) ②모터 명령(mXf/b:N)은
    '[모터N] 완료'까지 대기(60~85초 — 안 기다리면 완료 여부를 모른 채 다음 명령을 던지게 됨)
    ③그 외는 timeout 동안 응답을 전부 수집해 돌려준다(0.3초 드레인이 아니라 — 펌웨어
    응답이 느려도 놓치지 않게)."""
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return False, "명령이 비었습니다"
    target = args.get("target", "meas")
    timeout = min(120.0, max(1.0, float(args.get("timeout", 5))))
    datalog.log("[조치] 명령 콘솔(%s) <- %r" % (target, cmd))
    if target == "doser":
        lines = doser.send_cmd(cmd, wait=timeout)
        return bool(lines), "\n".join(lines) if lines else "(응답 없음)"

    lk, err = measure.make_link()
    if lk is None:
        return False, "측정 장비 링크 확보 실패 — %s" % err
    lk.log = datalog.log
    if not lk.ensure_link():
        return False, "링크 사망 — HC-05 리셋 후 재시도하세요"
    motor_idx = link._motor_index(cmd)
    if motor_idx is not None:
        lines = lk.send_motor(motor_idx, cmd)       # '[모터N] 완료'까지 대기(+keepalive)
        ok = measure._motor_ok(lines, motor_idx)
        tail = "\n".join(lines[-6:]) if lines else "(응답 없음)"
        return ok, tail + ("" if ok else "\n** 완료 응답 없음 — 이송 성공 불명")
    # 일반 명령 — timeout 동안 수집(도저 send_cmd 와 같은 방식)
    lk.flush_input()
    lk.write_line(cmd)
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        if lk.uart.any():
            ln = lk.readline()
            if ln:
                datalog.log("    " + ln)
                lines.append(ln)
        else:
            time.sleep_ms(50)
    return bool(lines), "\n".join(lines) if lines else "(응답 없음 — 명령/링크 확인)"


def _job_cleanup(args):
    """비상정리 강제 실행 — 프로브 KCl 소크 복원이 목적.
    force=True 면 위치 불명이어도 KCl 공급만 시도한다(챔버가 빈 것을 눈으로 확인했을 때)."""
    lk, err = measure.make_link()
    if lk is None:
        return False, "측정 장비 링크 확보 실패 — %s" % err
    lk.log = datalog.log
    if args.get("force"):
        datalog.log("[조치] KCl 소크 강제 공급 — 챔버 비어있음을 운영자가 확인")
        if not measure.ensure_move_precond(lk, "강제 KCl 소크", recovery_secs=120):
            return False, "전제조건(airoff·ton) 미확인 — 링크/장비 점검 필요"
        lines = measure._move_liquid(lk, 3, "m3f:60", "KCL", "EMPTY")
        ok = measure._motor_ok(lines, 3)
        return ok, "KCl 공급 %s" % ("완료" if ok else "미완료(모터 응답 없음)")
    measure._safe_cleanup(lk)
    return True, "비상정리 실행 — 결과는 로그 확인(챔버=%s)" % measure._liquid["chamber"]


def _job_link(args):
    """링크 점검 — status 핑으로 무선 구간·펌웨어 생존 확인."""
    lk, err = measure.make_link()
    if lk is None:
        return False, "측정 장비 링크 확보 실패 — %s" % err
    lk.log = datalog.log
    alive = lk.ensure_link()
    return alive, "링크 %s" % ("정상(펌웨어 응답 확인)" if alive else "사망 — HC-05 리셋 시도 권장")


def _job_hc05_reset(args):
    """HC-05 하드 리셋(전원 재투입) — 라디오 좀비 상태 복구. Windows 에서 불가능했던 조치.
    ★리셋은 현재 대상을 유지한 채 라디오만 되살린다. 대상을 바꾸려면 _job_bt_target 을 쓴다."""
    lk = link.get()
    lk.log = datalog.log
    if lk.target is None:
        return False, "연결 대상 미확정 — 'BT 대상 전환'으로 먼저 대상을 정하세요"
    if lk.motor_running is not None:
        return False, ("모터 %d 구동 중 — 리셋 금지(전원 차단 시 정지 명령 불가). "
                       "먼저 정지시키세요" % lk.motor_running)
    lk._pulse_reset()
    alive = lk.ensure_link()
    return alive, "HC-05 리셋 후 링크 %s" % ("복구됨" if alive else "여전히 무응답")


def _job_bt_target(args):
    """BT 대상 전환 — HC-05 를 측정기/도저 중 하나에 다시 바인드하고 신원을 검증한다.

    ★unfreeze=True 는 신원 불일치로 동결된 링크를 운영자 확인 후 푸는 경로다. 동결은
    "요청한 장비와 다른 장비가 응답했다"는 뜻이라 배선·BIND 주소를 확인하기 전에 풀면
    같은 오접속을 반복한다. 그래서 자동 해제는 없고 이 버튼으로만 푼다."""
    target = args.get("target", "meas")
    lk = link.get()
    lk.log = datalog.log
    if args.get("unfreeze"):
        was = lk.unfreeze()
        datalog.log("[조치] 링크 동결 해제 — 사유였던 것: %s" % was)
    ok, err = lk.select_target(target, force=bool(args.get("force")))
    spec = link.TARGETS.get(target, {})
    if ok:
        return True, "%s 연결 확인 — 신원 서명 일치" % spec.get("name", target)
    return False, err


def _job_doser_query(args):
    lrt, lgt = doser.query_left()
    if lrt is None:
        return False, "ls 응답 파싱 실패(도저 링크 점검)"
    return True, "lrt=%dms lgt=%smin (원액 %.1fmL/일)" % (lrt, lgt, doser.lrt_to_ml_day(lrt))


def _job_doser_apply(args):
    """정비용 lrt 직접 적용 — 대시보드 오버라이드(mL/일)와 달리 ms 를 그대로 쓴다.
    안전 레일은 유지: 0(정지) 또는 [LRT_MIN, LRT_MAX]."""
    try:
        lrt = int(args.get("lrt"))
    except (TypeError, ValueError):
        return False, "lrt(ms) 정수 필요"
    if lrt != 0 and not (config.LRT_MIN <= lrt <= config.LRT_MAX):
        return False, "lrt 는 0 또는 %d~%dms" % (config.LRT_MIN, config.LRT_MAX)
    cur, _lgt = doser.query_left()
    if cur is None:
        return False, "현재값 조회 실패 — 적용 취소(도저 링크 점검)"
    if cur == lrt:
        return True, "이미 %dms — 변경 없음" % lrt
    ok = doser.apply_lrt(lrt, cur)
    doser.append_history({
        "ts": rwtime.stamp(), "mode": "manual", "requested_ml": doser.lrt_to_ml_day(lrt),
        "lrt_old": cur, "lrt_new": lrt, "applied": ok,
        "ml_day_old": round(doser.lrt_to_ml_day(cur), 2),
        "ml_day_new": round(doser.lrt_to_ml_day(lrt), 2),
        "note": "정비 도구에서 lrt 직접 적용" + ("" if ok else " | 적용 실패"),
    })
    return ok, "lrt %d→%dms 적용=%s" % (cur, lrt, ok)


def _job_doser_preview(args):
    """무접속 권고 미리보기 — 원본 `doser_adjust.py --dry-run` 상당(장비 미접촉).
    lrt 를 주면 그 값을 현재값으로 가정한다(원본 --lrt)."""
    lrt = args.get("lrt")
    if lrt not in (None, ""):
        try:
            lrt = int(lrt)
        except (TypeError, ValueError):
            return False, "lrt(ms) 정수 필요"
    else:
        lrt = None
    return doser.preview(lrt)


def _job_doser_clock(args):
    """도저 시계 수동 동기화 — 자동은 하루 1회(main 루프). 원본 set_time.py 상당."""
    return doser.sync_clock(), "도저 시계 동기화 시도 — 결과는 로그 확인"


def _job_measure(args):
    r = measure.run_once()
    return r is not None, ("측정 완료 — 수조 %.3f dKH" % r[3]) if r else "측정 실패(로그 확인)"


def _job_calref(args):
    """참조 교정 — 수조 실측 dKH 로 ref dKH 역산·EEPROM 저장(원본 --setref 상당)."""
    try:
        dkh = float(args.get("tank_dkh"))
    except (TypeError, ValueError):
        return False, "수조 실측 dKH 값 필요"
    if not (0.5 <= dkh <= 30.0):
        return False, "허용 범위 0.5~30.0 dKH"
    r = measure.run_once(tank_dkh=dkh)
    return r is not None, ("교정 완료 — 새 ref %.3f dKH" % r[2]) if r else "교정 실패(로그 확인)"


_DISPATCH = {"measure": _job_measure, "calref": _job_calref, "cleanup": _job_cleanup,
             "cmd": _job_cmd, "link": _job_link, "hc05_reset": _job_hc05_reset,
             "bt_target": _job_bt_target,
             "doser_query": _job_doser_query, "doser_apply": _job_doser_apply,
             "doser_preview": _job_doser_preview, "doser_clock": _job_doser_clock}


def run_pending_job():
    """메인 루프가 매 틱 호출 — 대기 중 작업 1건 실행하고 결과를 state 에 남긴다."""
    job = state.job
    if job is None:
        return False
    state.job = None
    state.job_busy = True
    kind = job["kind"]
    try:
        fn = _DISPATCH.get(kind)
        if fn is None:
            ok, msg = False, "알 수 없는 작업: %s" % kind
        else:
            ok, msg = fn(job["args"])
    except Exception as e:
        ok, msg = False, "예외: %r" % e
        datalog.log("[조치] %s 실행 예외: %r" % (kind, e))
    state.job_result = {"kind": kind, "ok": ok, "msg": msg, "at": rwtime.stamp()}
    state.job_busy = False
    return True
