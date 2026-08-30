# 조치(recovery) 도구 — 측정이 끊기거나 이상 종료했을 때 웹(/ops.html)에서 복구한다.
# 원본 운영에서 손이 가던 일들을 그대로 도구화했다:
#   · 에러 래치 해제      — 원본은 "dkh.dat 마지막 에러 줄을 수동으로 지우기 전까지" 매 회차
#                           측정을 건너뛴다. 그 편집을 버튼 하나로.
#   · KCl 소크 강제 복원  — 비상정리가 실패하면 프로브가 KCl 없이 방치된다(원본 **경고).
#   · 액체 위치 수동 지정 — 위치 불명(UNKNOWN)이면 자동 정리가 동결된다. 눈으로 확인한
#                           실제 위치를 알려주고 정리를 재개.
#   · 측정 중단           — 매달린 회차를 끊는다(비상정리는 실행 → 프로브 보호).
#   · 링크 점검/HC-05 리셋 — 무선 구간 사망 판별과 하드 재기동.
#   · BT 대상 전환        — HC-05 가 1개라 측정기·도징기들을 번갈아 붙는다. 전환 후
#                           신원 서명을 확인하고, 불일치면 동결(오장비 명령 방지).
#   · 도징기 시계 동기    — 등록된 도징기 전체 또는 1대. 명령·값이 동일해 자동 전환 허용.
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
import devices
import doser
import link
import measure
import rwtime
import schedule
import state
import version
import watchdog
import wifinet

JOB_KINDS = ("measure", "calref", "cleanup", "cmd", "link", "hc05_reset",
             "bt_target", "bt_scan", "dev_ver", "doser_query", "doser_apply",
             "doser_preview", "doser_clock")


# ─────────────────────────────────────────────
# 즉시 실행 (파일·메모리만 — UART 미접촉)
# ─────────────────────────────────────────────

def _safe_text(b):
    """UTF-8 안전 디코드 — 잘린 멀티바이트가 있어도 죽지 않는다(link._decode 와 같은 정책)."""
    try:
        return b.decode("utf-8")
    except (UnicodeError, ValueError):
        return "".join(chr(c) if c < 0x80 else "?" for c in b)


def log_tail(n=40, path=None):
    """로그 마지막 n줄 — 정비페이지에서 진행 상황·경고 확인용. 파일 끝만 읽는다.
    읽기 창은 요청 행수에 비례(행당 ~150B 여유), 상한은 **로그 파일 크기 상한**(LOG_MAX_BYTES).

    ★창 상한을 32KB→512KB 로 올렸다(2026-08-30): 32KB 는 한국어 로그 기준 300줄 남짓이라
      **측정 1회를 통째로 볼 수 없었다**. 측정 1회는 판독마다 `-> read` + 응답 여러 줄 +
      평탄 판정 1줄이 쌓여 **판독 1회당 10줄 안팎**이고(MEAS_INTERVAL 30초, 2 phase),
      실기 첫 완주(67분)가 700~1500줄, 상한(MEAS_MAX 180×2 phase)까지 가면 3000줄대다.
      읽기 창이 요청보다 작으면 **말없이 적게** 돌아오므로(잘린 앞부분은 버린다) 창이
      먼저 커져야 한다. 8MB PSRAM 이라 512KB 문자열은 부담이 아니다.

    ★**바이트로 읽는다**(2026-08-29 실측 버그): 종전에는 텍스트 모드로 `seek(size-window)` 를
      했는데, 로그가 한국어(문자당 3바이트)라 그 위치가 **문자 중간**이면 `UnicodeError` 가 났다.
      `except OSError` 로는 안 잡혀 웹 핸들러가 통째로 죽었고, 브라우저에는 응답이 아예 안 가
      **정비페이지의 로그 창이 비었다**(ERR_EMPTY_RESPONSE). 창 크기가 n 에 비례하므로
      n=150 은 되고 n=40/50 은 안 되는 식으로 **간헐적으로** 보였다.
      바이트로 seek 한 뒤 readline 으로 잘린 첫 줄을 버리면 남는 것은 온전한 줄들이고,
      그래도 파일이 도중에 잘려 있을 수 있으니 디코드는 안전판을 쓴다."""
    path = path or datalog.LOG_FILE
    window = min(config.LOG_MAX_BYTES, max(8192, n * 150))
    try:
        size = os.stat(path)[6]
        with open(path, "rb") as f:
            if size > window:
                f.seek(size - window)
                f.readline()          # 잘린 첫 줄 버림(바이트 단위라 안전)
            data = f.read()
    except OSError:
        return []
    lines = [_safe_text(b).rstrip("\n") for b in data.split(b"\n")]
    return [ln for ln in lines if ln.strip()][-n:]


def log_offset(path=None):
    """로그 파일 끝의 바이트 위치 — 증분 조회(log_since)의 시작점.
    datalog.log 는 줄마다 flush 하므로 파일 끝이 곧 줄 경계다."""
    try:
        return os.stat(path or datalog.LOG_FILE)[6]
    except OSError:
        return 0


def log_since(off, path=None):
    """★tail -f — off 바이트 이후에 **새로 붙은 줄만** 돌려준다(2026-08-30).

    측정 1회를 보려면 수천 줄이 필요한데(log_tail 주석 참조) 그걸 폴링마다 다시 보내면
    기기가 그 일만 한다. 처음 한 번만 통째로 받고, 그 뒤로는 새 줄만 받는다 — 조용한 동안의
    응답은 `{"lines":[],...}` 로 사실상 0바이트다.

    반환: (lines, new_off, reset)
      reset=True 는 **이어 붙일 수 없다**는 뜻 — 로그가 회전(2-파일 링)해 파일이 작아졌을
      때다. 호출부는 전체를 다시 받아야 한다. 회전 직후 파일은 0 에서 시작하므로 '크기가
      내가 읽은 위치보다 작다'로 안전하게 잡힌다(한 폴링 사이에 512KB 를 쓸 수는 없다).

    ★마지막 줄이 개행으로 끝나지 않으면 그 줄은 **넘기지 않고 다음 회차로 미룬다** — 반쪽
      줄을 보내면 화면에 잘린 글자가 남는다(한글은 3바이트라 특히 눈에 띈다)."""
    path = path or datalog.LOG_FILE
    try:
        size = os.stat(path)[6]
    except OSError:
        return [], 0, False
    if off > size:
        return [], size, True          # 회전·잘림 — 전체를 다시 받아야 한다
    if off == size:
        return [], size, False         # 새 줄 없음(가장 흔한 경우 — 응답이 거의 비어 있다)
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except OSError:
        return [], off, False
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], off, False          # 아직 한 줄도 완결되지 않았다
    lines = [_safe_text(b).rstrip("\n") for b in data[:cut].split(b"\n")]
    return [ln for ln in lines if ln.strip()], off + cut + 1, False


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


def _meas_side():
    """지금 붙어 있는 대상이 **측정 장비 쪽인가** — (측정쪽?, 대상 이름).

    ★왜 필요한가(사용자 지적 2026-08-29): `latched`(dkh.dat 의 에러 표식)와
      `liquid_unknown`(측정 챔버·홀딩의 액체 위치)은 **측정 장비의 사정**이다. 도저에 붙어
      있는데 그것으로 콘솔을 막으면, 아무 상관 없는 장비의 상태 때문에 도징 조작을 못 한다.
    ★대상 미확정(None)이면 '측정 장비일 수도 있다'로 보고 **막는 쪽**을 택한다 — 어디로 가는지
      모르는 채 명령을 여는 것보다 안전하다('연결 점검'이나 전환으로 확정하면 열린다)."""
    t = current_target()
    if t is None:
        return True, "미확정"
    spec = link.TARGETS.get(t)
    if spec is None:
        return True, t
    return spec["kind"] == "meas", spec["name"]


def device_state():
    """★측정 장비의 현재 상태를 한 마디로 — 판정은 여기 한 곳에서만 한다(2026-08-19).

    화면과 게이트가 서로 다른 기준으로 판단하면 "버튼은 눌리는데 서버가 거부"하거나 그 반대가
    된다. 그래서 상태 이름·설명·명령 콘솔 허용 여부를 한 dict 로 만들어 UI 와 서버가 같이 쓴다.

    상태(우선순위 순 — 위험한 것부터):
      measuring       측정 중(수 시간). 이 동안 장비 조작은 전부 금지.
      job             조치 작업 실행/대기 중. 끝나면 풀린다.
      frozen          BT 링크 동결(신원 불일치) — 어떤 명령도 나가지 않는다.
      latched         에러 래치(dkh.dat 마지막 줄이 전부 0) — 다음 정시 측정이 멈춰 있다.
      liquid_unknown  액체 위치 불명 — 자동 정리가 동결됐다.
      idle            대기. 정상 상태.

    명령 콘솔 규칙(사용자 지시 2026-08-19): **Idle 에서만 그냥 열린다.**
      · measuring / job → 잠금(우회 불가). 측정 중 임의 명령은 회차를 조용히 망친다.
      · frozen → 잠금(우회 무의미 — link 계층이 어차피 LinkFrozen 을 던진다). 해제가 먼저.
      · latched / liquid_unknown → 기본 잠금, **운영자 확인(ack)** 시에만 열린다. 이 둘은
        원래 콘솔로 수동 정리하는 상황이라(사용자 결정 #10) 완전히 막으면 복구 수단이 사라진다.
    """
    if state.measuring:
        return {"state": "measuring", "label": "측정 중",
                "detail": "측정 회차가 진행 중입니다(수 시간). 중단하려면 '측정 중단'.",
                "console_allowed": False, "console_override": False,
                "console_reason": "임의 명령이 측정을 망칩니다 — '측정 중단' 후 사용하세요"}
    if state.job_busy or state.job is not None:
        kind = (state.job or {}).get("kind") or "실행 중"
        return {"state": "job", "label": "조치 작업 %s" % kind,
                "detail": "조치 작업이 실행/대기 중입니다. 끝나면 자동으로 풀립니다.",
                "console_allowed": False, "console_override": False,
                "console_reason": "다른 조치 작업이 끝나기를 기다리세요"}
    lk = link.get_if_created()
    if lk is not None and lk.frozen:
        return {"state": "frozen", "label": "BT 링크 동결",
                "detail": "신원 불일치 — %s" % lk.frozen,
                "console_allowed": False, "console_override": False,
                "console_reason": "동결 중에는 어떤 명령도 나가지 않습니다 — '동결 해제' 먼저"}
    if datalog.last_dat_is_error():
        on_meas, tname = _meas_side()
        return {"state": "latched", "label": "에러 래치 — 측정 정지됨",
                "detail": "마지막 회차가 에러로 끝났습니다. 정시 측정은 래치를 풀 때까지 "
                          "건너뜁니다(프로브 보호).",
                "console_allowed": not on_meas, "console_override": on_meas,
                "console_reason": ("수동 정리 목적이면 아래 잠금을 해제하세요" if on_meas else
                                   "래치는 측정 장비의 상태입니다 — 지금 대상(%s) 조작은 "
                                   "그대로 가능합니다" % tname)}
    liq = measure._liquid
    if "UNKNOWN" in (liq["chamber"], liq["holding"]):
        on_meas, tname = _meas_side()
        return {"state": "liquid_unknown", "label": "액체 위치 불명 — 정리 동결",
                "detail": "이송 도중 중단돼 위치를 모릅니다(측정 챔버=%s / 홀딩=%s). "
                          "실물 확인 후 '액체 위치 수동 지정'." % (liq["chamber"], liq["holding"]),
                "console_allowed": not on_meas, "console_override": on_meas,
                "console_reason": ("수동 정리 목적이면 아래 잠금을 해제하세요" if on_meas else
                                   "액체 위치는 측정 장비의 상태입니다 — 지금 대상(%s) 조작은 "
                                   "그대로 가능합니다" % tname)}
    # ★측정 보류(정비 래치) — 고장이 아니라 **의도된** 상태다. 그래서 우선순위가 낮고
    #   (에러 래치·동결이 함께 있으면 그쪽이 먼저 보여야 한다) 콘솔도 막지 않는다:
    #   보류를 거는 상황이 곧 콘솔로 장비를 만지는 상황이다.
    hold = schedule.hold_status()
    if hold.get("active"):
        return {"state": "hold", "label": "측정 보류 중 — 정시 측정 멈춤",
                "detail": "정시 측정을 의도적으로 멈춰 두었습니다(%s). 해제: %s. "
                          "수동 '지금 측정'은 그대로 됩니다."
                          % (hold.get("reason") or "사유 없음",
                             hold.get("until") or "수동 해제 전까지"),
                "console_allowed": True, "console_override": False,
                # ★사유를 비워 두면 화면이 "대기(Idle) 상태 — 콘솔 사용 가능"으로 폴백해
                #   배너의 '측정 보류 중'과 서로 다른 말을 한다(래치 때 겪은 것과 같은 함정).
                "console_reason": "측정 보류 중 — 콘솔은 그대로 쓸 수 있습니다(정비가 곧 콘솔 작업)"}
    return {"state": "idle", "label": "대기 (Idle)",
            "detail": "정상 대기 상태입니다. 다음 정시 회차를 기다립니다.",
            "console_allowed": True, "console_override": False, "console_reason": ""}


def snapshot():
    """현재 상태 종합 — 대시보드·정비페이지가 이 데이터를 쓴다(유일한 조작 경로)."""
    latest = datalog._read_json(datalog.LATEST_FILE, {})
    last_run = datalog.last_plateau()      # 마지막 1건만 파싱(힙 절약)
    hist = doser.load_history()
    last_dose = hist[-1] if hist else {}
    lines = datalog.read_dat_lines()
    return {
        "now": rwtime.stamp(),
        # 장비명·펌웨어 판 — 상태를 볼 때마다 "어느 기기의 어느 판인지"가 같이 보여야
        # 화면과 기기가 어긋난 채로 판단하는 일이 없다. 전체는 GET /api/version.
        "version": version.brief(),
        # ★장비 상태 한 마디 — 화면 표시와 명령 콘솔 게이트가 같은 판정을 쓴다(device_state).
        "device": device_state(),
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
        # 연속 검색 — 화면이 이걸로 진행 상황(찾은 주소)을 실시간으로 그린다.
        "scan": {"running": state.scan["running"], "found": list(state.scan["found"]),
                 "passes": state.scan["passes"], "started": state.scan["started"],
                 "phase": state.scan["phase"]},
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
        # 스케줄은 라이브 값이다(정비페이지에서 바꾼 즉시 반영) — 화면이 실제 동작과
        # 어긋나면 "왜 안 도나"를 로그에서 찾게 된다.
        "schedule": {"hold": schedule.hold_status(),
                     "hours": schedule.measure_hours(),
                     "doser_slot": schedule.doser_slot_hour(),
                     "next_hour": schedule.next_hour(rwtime.now_tuple()),
                     "min_gap_h": config.MEASURE_MIN_GAP_H,
                     "hours_max": config.MEASURE_HOURS_MAX,
                     "doser_max": config.DOSER_MAX,
                     "sync_max": config.DOSER_SYNC_MAX,
                     "source": schedule.source()},
        "wifi": wifinet.status(),
        "heap_free": gc.mem_free() if hasattr(gc, "mem_free") else None,
    }


# ─────────────────────────────────────────────
# 큐 실행 (메인 루프에서만 — UART 접촉)
# ─────────────────────────────────────────────

def _meas_link():
    """조치 도구용 측정기 링크 — ★measure.make_link() 를 쓰지 않는다.
    그건 측정 흐름 전용(allow_measuring=True)이라 '측정 중 전환 금지' 레일을 지나간다.
    조치 도구는 운영자 조작이므로 레일 안쪽에 있어야 한다(측정 중이면 거부)."""
    return link.acquire("meas", log=datalog.log)


def current_target():
    """지금 붙어 있다고 검증된 BT 대상 id(meas/doser/doser2…) 또는 None — 콘솔·도징 라우팅 기준."""
    lk = link.get_if_created()
    return None if lk is None or lk.frozen else lk.target


def _job_cmd(args):
    """임의 펌웨어 명령 송신 — 에러 후 수동 정리의 핵심 도구. 전량 로깅한다.

    ★대상은 **지금 붙어 있는 BT 대상**이다(2026-08-19, 사용자 지시). 종전에는 콘솔에 장비
    선택 드롭다운이 따로 있어서 "화면의 BT 대상"과 "명령이 실제로 가는 곳"이라는 두 개의
    진실이 생겼다. 이제 대상 전환은 BT 카드에서만 하고, 콘솔은 그 결과를 그대로 따른다.

    사용자 워크플로(2026-08-14): 에러 발생 → 링크 회복 → 직접 명령을 던져 정리.
    그래서 meas 명령은 ①송신 전 ensure_link(죽었으면 자동 재연결) ②모터 명령(mXf/b:N)은
    '[모터N] 완료'까지 대기(60~85초 — 안 기다리면 완료 여부를 모른 채 다음 명령을 던지게 됨)
    ③그 외는 timeout 동안 응답을 전부 수집해 돌려준다(0.3초 드레인이 아니라 — 펌웨어
    응답이 느려도 놓치지 않게)."""
    cmd = (args.get("cmd") or "").strip()
    if not cmd:
        return False, "명령이 비었습니다"
    target = current_target()
    if target is None:
        return False, "BT 대상 미확정(또는 동결) — 'BT 연결'에서 대상을 먼저 정하세요"
    timeout = min(120.0, max(1.0, float(args.get("timeout", 5))))
    datalog.log("[조치] 명령 콘솔(%s) <- %r" % (target, cmd))
    # ★대상 id 가 아니라 **종류**로 고른다(2026-08-29 실측 버그): 종전 `target == "doser"` 는
    #   기본 도저 하나만 잡아서, **도저2 에 붙은 채 콘솔에 `ls` 를 치면 측정기 경로로 떨어졌다**.
    #   그 경로의 `_meas_link()` 가 `link.acquire("meas")` 를 부르므로 연결이 측정 장비로
    #   **전환돼 버린다** — 운영자는 조회 하나 보냈을 뿐인데 대상이 바뀐다.
    #   (화면은 2026-08-21 에 이미 종류로 고르게 고쳤는데 서버가 안 따라와 있었다.)
    # ★target 을 그대로 넘긴다: 지금 붙어 있는 그 도징기로 보내야 한다(기본 도저가 아니라).
    #   이미 검증된 대상이라 acquire 는 라디오를 건드리지 않고 즉시 통과한다.
    if link.TARGETS.get(target, {}).get("kind") == "doser":
        lines = doser.send_cmd(cmd, wait=timeout, target=target)
        return bool(lines), "\n".join(lines) if lines else "(응답 없음)"

    lk, err = _meas_link()
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
    deadline = rwtime.deadline_ms(timeout)
    while rwtime.before(deadline):
        watchdog.feed()
        if lk.uart.any():
            ln = lk.readline()
            if ln:
                datalog.log("    " + ln)
                lines.append(ln)
        else:
            time.sleep_ms(50)
    return bool(lines), "\n".join(lines) if lines else "(응답 없음 — 명령/링크 확인)"


def _job_cleanup(args):
    """측정 정리(비상정리) 실행 — 프로브 KCl 소크 복원이 목적.

    ★레시피는 **액체 위치**가 고른다(_safe_cleanup): 챔버=KCl 이면 조치 불필요, 수조수면 배출,
    참조수면 5L 회수, 불명이면 동결. 그래서 정비페이지는 '액체 위치 지정'과 이 버튼을 같은
    행에 둔다 — 위치를 먼저 맞추고 정리를 돌리는 것이 하나의 절차다.

    ★'KCl 강제 공급'은 없앴다(2026-08-19, 사용자 지시): 챔버에 무엇이 들었는지 모르는 채
    시약을 밀어 넣는 조작이라 의미가 없다. 챔버가 빈 것을 눈으로 확인했다면 위치를
    '비움/비움'으로 지정하고 이 버튼을 누르면 같은 일(KCl 공급)이 안전하게 일어난다."""
    lk, err = _meas_link()
    if lk is None:
        return False, "측정 장비 링크 확보 실패 — %s" % err
    lk.log = datalog.log
    measure._safe_cleanup(lk)
    return True, "측정 정리 실행 — 결과는 로그 확인(챔버=%s)" % measure._liquid["chamber"]


def _job_dev_ver(args):
    """장비 판 조회 — 지금 붙어 있는 대상에게 `ver` 을 1회 보내 한 줄을 다시 읽는다.

    ★읽기 전용이고 **대상을 바꾸지 않는다**: 전환은 회차를 흐트러뜨리고 실측상 수동 전원
      조작까지 필요할 수 있다. "지금 붙어 있는 그 장비의 판"만 확인한다.
    ★캐시를 무시하고(force) 다시 읽는 것이 이 버튼의 존재 이유다 — 장비 펌웨어를 올린 직후
      제어기가 들고 있는 값은 옛 판이다(캐시는 대상당 1회만 읽는다).
    ★`ver` 이 없는 옛 펌웨어면 무응답이 정상이다 — 실패가 아니라 사실로 보고한다."""
    ok, msg, _s = guard_measure(0, "판 조회")
    if not ok:
        return False, msg
    lk = link.get()
    lk.log = datalog.log
    if lk.frozen:
        return False, "링크 동결됨(%s) — 먼저 '동결 해제'" % lk.frozen
    if lk.target is None:
        return False, "BT 대상 미확정 — 'BT 연결'에서 대상을 먼저 정하세요"
    spec = link.TARGETS.get(lk.target) or {}
    info = lk._capture_ver(lk.target, force=True)
    if not info or not info.get("ver"):
        return True, ("%s — `ver` 응답 없음(그 명령이 없는 펌웨어). 펌웨어를 올렸다면 "
                      "장비가 켜져 있고 이 대상에 붙어 있는지 확인하세요" % spec.get("name", lk.target))
    return True, "%s 판: %s" % (spec.get("name", lk.target), info["ver"])


def _job_link(args):
    """연결 점검 — **읽기 전용 진단**. 지금 붙어 있는 대상에게 부작용 없는 조회를 1회 보내고
    응답이 오는지만 본다(측정기 `status`=상태 출력 / 도저 `ls`=설정 출력 — 둘 다 액추에이터·
    샘플링을 건드리지 않는다. 원본 bt_health.py 가 같은 명령으로 링크 건강을 모니터했다).

    ★여기서 아무것도 바꾸지 않는다(2026-08-19): 종전 구현은 measure.make_link() →
    ensure_link() 를 탔는데, 그러면 ①대상이 도저면 측정기로 **전환**되고 ②무응답이면
    reconnect() 가 **전원 펄스를 최대 5회** 준다 — HC-05 리셋과 같은 위험(그 사이 정지 명령
    불가)을 확인 없이 저지르는 셈이었다. 진단 버튼은 상태를 바꾸지 말아야 한다.
    무응답이면 사실만 보고하고, 라디오 재기동은 위험을 고지하는 'HC-05 리셋' 버튼에 맡긴다.

    ★신원 검증은 살아 있다: 조회 응답이 *다른 장비 서명*이면 _ping 이 그 자리에서 동결한다."""
    lk = link.get()
    lk.log = datalog.log
    if lk.frozen:
        return False, "링크 동결됨(%s) — 배선·BIND 확인 후 '동결 해제'" % lk.frozen
    if lk.target is None:
        # ★대상 미확정도 진단해야 한다(2026-08-29): 부팅 자동연결을 끄고 명시적으로 붙이는
        #   운영에서는 부팅 직후가 늘 이 상태다. 그런데 HC-05 는 자체 전원이라 **직전 상대를
        #   그대로 물고 있다** — "지금 누가 저쪽에 있나"를 알고 싶을 뿐인데 종전 코드는
        #   'BT 대상 전환'을 하라고 돌려보냈다. 전환은 대상을 *바꿔 버리므로*, 알기 위해
        #   바꿔야 하는 모순이었다. 여기서는 조회만 보내 종류를 읽는다.
        kind = lk.identify()
        st = link.state_pin_value()
        where = ("STATE: 연결" if st else "STATE: 끊김") if st is not None else "STATE 미배선"
        if kind is None:
            return False, ("대상 미확정 — 조회에 응답이 없습니다(%s). 'BT 대상 전환'으로 "
                           "붙이세요" % where)
        label = devices.KINDS[kind]["label"]
        same = [t for t, v in link.TARGETS.items() if v["kind"] == kind]
        if len(same) == 1:
            # 그 종류가 한 대뿐이면 신원이 확정된다 — select_target 과 같은 근거(응답 서명)다.
            lk.target = same[0]
            lk.last_ok_at = rwtime.stamp()
            return True, "%s 응답 확인 — 대상 확정(%s). 아무것도 바꾸지 않았습니다" % (
                label, where)
        # ★같은 종류가 여러 대면 응답 서명만으로는 개체를 못 가린다 — BIND 주소로 좁힌다.
        #   서명(종류) + BIND(개체)를 **둘 다** 만족해야 확정한다: 서명만 보면 도저1/도저2 를
        #   구분 못 하고, BIND 만 보면 '바인드는 됐지만 실제로 그쪽에 붙었는지'를 모른다.
        #   조회(AT+BIND?)는 연결을 끊지 않는다(실측).
        addr = lk.bound_addr()
        hit = [t for t in same if link.bind_addr(t) == addr] if addr else []
        if len(hit) == 1:
            lk.target = hit[0]
            lk.last_ok_at = rwtime.stamp()
            tn = link.TARGETS[hit[0]]["name"]
            return True, ("%s 응답 확인 + BIND 주소 일치 — 대상 확정: %s (%s). "
                          "아무것도 바꾸지 않았습니다" % (label, tn, where))
        return True, ("%s 쪽에 붙어 있습니다(%s) — 다만 %s가 %d대이고 BIND 주소(%s)가 등록된 "
                      "장치와 맞지 않아 어느 것인지 확정할 수 없습니다. 'BT 대상 전환'을 쓰세요"
                      % (label, where, label, len(same), addr or "조회 실패"))
    name = link.TARGETS.get(lk.target, {}).get("name", lk.target)
    alive = lk._ping()          # 조회 1회 — 전환·전원 펄스 없음
    if alive:
        return True, "%s 응답 확인 — 링크 정상(아무것도 바꾸지 않음)" % name
    if lk.frozen:
        return False, "점검 중 다른 장비 서명 수신 — 링크 동결됨(%s)" % lk.frozen
    return False, ("%s 무응답 — 상대 전원·거리·페어링 확인. 그래도 안 되면 'HC-05 리셋'"
                   "(위험 고지 확인 후 실행)" % name)


def _job_hc05_reset(args):
    """HC-05 하드 리셋 — **ESP32 쪽 HC-05 모듈의 전원(EN)을 0.4초 끊었다 켠다**.
    장비(측정기·도저) 자체는 건드리지 않는다. 라디오가 좀비가 돼(AT 조차 응답 없음) 재연결로도
    안 살아날 때의 최후 수단이며, Windows 에서는 불가능했던 조치다.
    리셋 뒤 모듈은 BIND 주소로 자동 재접속하고, ensure_link 가 **신원 서명을 다시 확인**한다
    (다른 장비가 붙었으면 그 자리에서 동결된다). 현재 대상은 유지된다 — 대상을 바꾸려면
    'BT 대상 전환'을 쓴다.

    ★위험(호출부가 반드시 경고할 것): 전원 차단~재접속까지 수 초 동안 **어떤 명령도 보낼 수
    없다**. 그 사이 장비가 모터를 돌고 있으면 정지 명령을 못 보낸다. ESP32 가 아는 구동은
    motor_running 가드가 막지만, '완료 응답만 유실되고 펌프는 계속 도는' 경우나 폰 BT
    터미널로 직접 내린 명령까지는 알 수 없다 — 그래서 운영자 확인을 받고 실행한다."""
    # 측정 보호는 공통 가드 한 곳에서 판정한다(guard_measure) — 리셋은 링크를 끊는다.
    ok, msg, _s = guard_measure(0, "HC-05 리셋")
    if not ok:
        return False, msg
    lk = link.get()
    lk.log = datalog.log
    if lk.target is None:
        return False, "연결 대상 미확정 — 'BT 대상 전환'으로 먼저 대상을 정하세요"
    datalog.log("[조치] HC-05 하드 리셋 — 라디오 전원 재투입(대상=%s, 운영자 확인)" % lk.target)
    lk._pulse_reset()
    alive = lk.ensure_link()
    if alive:
        return True, "HC-05 리셋 완료 — 라디오 재부팅 후 링크 복구됨(신원 재확인 통과)"
    if lk.frozen:
        return False, "HC-05 리셋 후 다른 장비가 응답 — 링크 동결됨(%s)" % lk.frozen
    return False, ("HC-05 리셋했지만 여전히 무응답 — 장비 전원·거리·배선(EN/KEY) 확인. "
                   "모듈 자체가 아니라 상대 장비가 꺼져 있을 수도 있습니다")


def _job_bt_target(args):
    """BT 대상 전환 — HC-05 를 측정기/도저 중 하나에 다시 바인드하고 신원을 검증한다.

    ★unfreeze=True 는 신원 불일치로 동결된 링크를 운영자 확인 후 푸는 경로다. 동결은
    "요청한 장비와 다른 장비가 응답했다"는 뜻이라 배선·BIND 주소를 확인하기 전에 풀면
    같은 오접속을 반복한다. 그래서 자동 해제는 없고 이 버튼으로만 푼다."""
    target = args.get("target", "meas")
    ok, msg, _s = guard_measure(0, "대상 전환")
    if not ok:
        return False, msg
    lk = link.get()
    lk.log = datalog.log
    if args.get("unfreeze"):
        # ★동결이 아니면 거부한다(2026-08-30, 웹 버튼 잠금과 이중 방어). unfreeze() 는 사유와
        #   함께 **검증된 대상까지 지우므로**, 멀쩡한 상태에서 부르면 화면상 변화 없이 불필요한
        #   재바인드를 돌린다(그 몇 초 동안 어떤 명령도 못 나간다). 해제할 게 없으면 안 푼다.
        if not lk.frozen:
            return False, ("지금은 동결 상태가 아닙니다 — 해제할 것이 없습니다. "
                           "대상을 바꾸려면 장치 목록의 '…로 전환'을 쓰세요")
        was = lk.unfreeze()
        datalog.log("[조치] 링크 동결 해제 — 사유였던 것: %s" % was)
    ok, err = lk.select_target(target, force=bool(args.get("force")))
    spec = link.TARGETS.get(target, {})
    if ok:
        return True, "%s 연결 확인 — 신원 서명 일치" % spec.get("name", target)
    return False, err


SLOT_MARGIN_S = 120        # 다음 정시 회차 앞에 남겨 두는 여유(초) — 전환·준비 시간


def guard_measure(hold_secs=0, what="이 작업"):
    """측정 보호 — 라디오·메인 루프를 붙잡는 조치의 **공통 전제**. (ok, 메시지, 허용 초).

    ★한 곳에서 판정한다(사용자 지시 2026-08-30). 종전에는 작업마다 조건과 문구가 제각각이었다
      — `dev_ver` 에는 측정 중 검사가 아예 없었고, `hc05_reset` 과 `bt_scan` 은 같은 상황에서
      다른 말을 했다. 규칙이 흩어져 있으면 새 작업을 붙일 때 하나를 빠뜨린다.
    규칙:
      ① 측정 중이면 금지 — 회차가 깨진다.
      ② 모터 구동 중이면 금지 — 라디오를 뺏으면 정지 명령을 보낼 수단이 사라진다.
      ③ `hold_secs` 만큼 메인 루프를 붙잡는 작업(연속 검색)은 **다음 정시 회차를 침범하면
         안 된다** — 회차 %d초 전까지로 줄이고, 그마저 안 되면 금지한다. 검색이 회차를 물고
         있으면 측정이 밀리고, 그 시(hour)를 넘기면 회차를 통째로 건너뛴다.
      ★측정 보류(정비 래치) 중에는 ③을 적용하지 않는다(사용자 확정): 정시 측정이 없으므로
        침범할 것이 없다.
    ★`link.select_target` 에도 측정 중 검사가 있다 — 그건 측정 흐름 자신이 지나가는 깊은
      게이트(allow_measuring)라 남겨 둔다. 여기는 **조치 버튼들의 공통 문지기**다.""" % SLOT_MARGIN_S
    if state.measuring:
        return False, "측정 중 — %s는 회차를 깨뜨립니다. '측정 중단' 후 시도하세요" % what, 0
    lk = link.get_if_created()
    if lk is not None and lk.motor_running is not None:
        return False, ("모터 %s 구동 중 — %s 중에는 정지 명령을 보낼 수 없습니다"
                       % (lk.motor_running, what)), 0
    if hold_secs <= 0:
        return True, "", 0
    left = _secs_to_next_slot()
    if left is None:                       # 회차 없음 또는 측정 보류 중 — 침범할 것이 없다
        return True, "", hold_secs
    if left <= SLOT_MARGIN_S:
        return False, ("다음 측정 회차까지 %d분뿐입니다 — %s는 회차가 끝난 뒤에 하세요"
                       % (max(0, int(left // 60)), what)), 0
    return True, "", min(hold_secs, left - SLOT_MARGIN_S)


def _secs_to_next_slot():
    """다음 측정 회차까지 남은 초 — 회차가 없으면 None. 보류 중이면 무한(정시 측정이 없다).

    ★왜 필요한가: 연속 검색은 **메인 루프를 붙잡고 도는 유일한 작업**이다. 검색이 도는 동안
      정시 회차가 오면 측정이 그만큼 밀리고, 그 시(hour)를 넘겨 버리면 회차를 통째로 건너뛴다.
      그래서 검색은 다음 회차를 침범하지 않는 길이로만 허용한다."""
    if schedule.hold_status().get("active"):
        return None                      # 측정 보류 중 — 정시 측정이 없으니 침범할 것도 없다
    t = rwtime.now_tuple()
    nh = schedule.next_hour(t)
    if nh is None:
        return None
    ahead = (nh - t[3]) % 24 or 24       # 지금 시각과 같으면 하루 뒤 회차다(이번 건 이미 지났다)
    return ahead * 3600 - (t[4] * 60 + t[5])


def _job_bt_scan(args):
    """주변 BT 장치 **연속** 검색(`AT+INQ`) — 화면을 보다가 원하는 주소가 뜨면 멈춘다.

    ★왜 연속인가(사용자 요구 2026-08-30): 한 패스로는 다 안 잡힌다. 신호가 약하거나 그때
      응답하지 않는 장비는 빠지고, 특히 **지금 HC-05 가 붙어 있는 상대는 아예 응답하지
      않는다**. 그래서 계속 돌리면서 새 주소가 뜨는 대로 화면에 채우고, 사용자가 멈춘다.
    ★찾은 주소는 `state.scan` 에 실시간으로 쌓인다 — 정비페이지가 상태 폴링으로 그걸 그린다.
      중지는 `POST /api/ops/scan_stop`(웹 스레드가 stop 을 올리고 이 루프가 읽는다).
    ★**측정을 침범하지 않는다**: ①측정 중이면 거부 ②모터 구동 중이면 거부 ③다음 정시 회차가
      가까우면 거부하고, 아니면 **회차 2분 전까지로 길이를 잘라** 실행한다. 검색은 메인 루프를
      붙잡고 도는 유일한 작업이라, 이 가드가 없으면 회차가 밀리거나 통째로 건너뛰어진다.
    ★조회 **전**에는 리셋하지 않는다(리셋하면 오히려 ERROR:(1F) 로 거부된다). 대신 끝나면
      **데이터 모드로 되돌리는 리셋**을 한 번 한다 — 검색은 KEY 를 몇 분간 올린 채 돌아서,
      그 사이 모듈이 재부팅하면 AT 모드에 갇힌 채로 끝날 수 있다(link.inquire 헤더).
      그래서 검색 후 BT 대상은 **미확정**이 되고, 다음 조작이 신원 검증으로 다시 확인한다."""
    try:
        want = float(args.get("max_secs", 300))
    except (TypeError, ValueError):
        want = 300.0
    want = max(20.0, min(600.0, want))
    ok, msg, max_secs = guard_measure(want, "장치 검색")
    if not ok:
        return False, msg
    if max_secs < want:
        datalog.log("[조치] 검색 시간을 %d초로 줄임 — 다음 회차 침범 방지" % int(max_secs))
    lk = link.get()
    lk.log = datalog.log

    sc = state.scan
    sc["running"], sc["stop"] = True, False
    sc["found"], sc["passes"], sc["phase"] = [], 0, "검색"
    sc["started"] = rwtime.stamp()
    known = {}
    for tid, spec in link.TARGETS.items():
        a = spec["bind"]()
        if a:
            known[a] = spec["name"]

    def on_pass(n):
        sc["passes"] = n

    def on_phase(p):
        sc["phase"] = p

    def on_found(e):
        # ★같은 dict 을 그대로 담는다 — 이름(RNAME)이 뒤에 채워지면 화면에도 저절로 반영된다.
        sc["found"].append(e)
        a = e["addr"]
        datalog.log("  [INQ] 새 주소 %s%s"
                    % (a, ("  ← " + known[a]) if a in known else "  (미등록)"))

    datalog.log("[조치] 주변 BT 장치 연속 검색 시작 — 최대 %d초(정리에 2~4초 더), "
                "중지 버튼으로 멈춥니다" % int(max_secs))
    try:
        found, err = lk.inquire(25.0, max_secs=max_secs, on_found=on_found,
                                on_pass=on_pass, on_phase=on_phase,
                                should_stop=lambda: sc["stop"])
    finally:
        sc["running"], sc["phase"] = False, "" 
    if err:
        return False, err
    cur = link.TARGETS.get(lk.target, {}).get("name") if lk.target else None
    note = ("\n※지금 HC-05 가 붙어 있는 '%s' 는 조회에 잡히지 않습니다(연결 중인 장비는 "
            "응답하지 않습니다)." % cur) if cur else ""
    tail = " (사용자 중지)" if sc["stop"] else ""
    if not found:
        return True, ("주변에서 아무 장치도 찾지 못했습니다%s — 대상 장비의 전원과 거리를 "
                      "확인하세요" % tail + note)
    lines = ["찾은 장치 %d대%s:" % (len(found), tail)]
    for e in found:
        a = e["addr"]
        # 광고 이름(RNAME) → 등록 여부 순으로 붙인다. 이름은 못 받을 수도 있다(실패 아님).
        lines.append("  %-16s %-14s %s" % (a, e.get("name") or "(이름 없음)",
                                           ("← " + known[a]) if a in known else "(미등록)"))
    lines.append("미등록 주소를 아래 '장치 목록'에 넣으면 등록됩니다."
                 + "\n※검색 후 라디오를 데이터 모드로 되돌렸습니다 — BT 대상은 다음 조작에서 "
                   "다시 확인합니다." + note)
    datalog.log("[조치] 검색 결과: %s"
                % ", ".join("%s(%s)" % (e["addr"], e.get("name") or "?") for e in found))
    return True, "\n".join(lines)


def stop_scan():
    """연속 검색 중지 — 웹 스레드가 부른다(메인 루프가 검색 루프 안에서 읽는다)."""
    if not state.scan.get("running"):
        return False, "검색 중이 아닙니다"
    state.scan["stop"] = True
    datalog.log("[조치] 검색 중지 요청")
    return True, "중지 요청됨 — 이번 패스를 마치는 대로 멈춥니다"


def _require_primary_doser():
    """도징 조작 전제 — ★BT 대상이 **기본 도저**여야 한다.
    ①(2026-08-19) 종전에는 doser.send_cmd 가 알아서 전환했지만, 그러면 화면의 'BT: 측정 장비'
      표시와 실제 동작이 순간 어긋난다. 전환은 BT 카드에서 명시적으로 하고 도징은 그 뒤에 쓴다.
    ②(2026-08-21) 도징기가 여러 대여도 **도징량 조작은 기본 도저 1대에만** 허용한다. 도저
      펌웨어 응답 서명이 전부 같아 신원 검증이 도징기끼리를 구분하지 못하므로, 추가 도징기에
      lrt 를 보내는 경로를 아예 만들지 않는다(시계 동기는 값이 같아 무해 — 그건 허용).
    ※메인 루프의 자동 경로(post_measure·sync_clock)는 ops 를 거치지 않으므로 영향 없다."""
    t = current_target()
    if t == devices.PRIMARY_DOSER_ID:
        return None
    name = link.TARGETS.get(t, {}).get("name", "미확정") if t else "미확정"
    primary = link.TARGETS.get(devices.PRIMARY_DOSER_ID, {}).get("name", "기본 도저")
    return ("BT 대상이 '%s' 가 아닙니다(현재 %s) — '%s로 전환' 후 실행하세요"
            % (primary, name, primary))


def _job_doser_query(args):
    err = _require_primary_doser()
    if err:
        return False, err
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
    err = _require_primary_doser()
    if err:
        return False, err
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
    """도징기 시계 수동 동기화 — 자동은 장치별 시각(devices sync_hours). 원본 set_time.py 상당.

    `device` 를 주면 그 도징기 1대, 생략하면 **등록된 전 도징기**를 순회한다.
    ★이 작업만은 `_require_primary_doser` 를 걸지 않는다(2026-08-21): `set time` 은 값이 전
    도징기 동일해서 주소가 뒤바뀌어도 결과가 같고(무해), 여러 대를 수동 전환으로 돌리게 하면
    쓸 수 없는 도구가 된다. 전환과 대상은 전부 로그에 남는다."""
    dev_id = args.get("device")
    if dev_id:
        dev = devices.get(dev_id)
        if dev is None or dev["kind"] != "doser":
            return False, "등록된 도징기가 아닙니다: %r" % (dev_id,)
        ok = doser.sync_clock(dev_id)
        return ok, "%s 시계 동기화 %s" % (dev["name"], "성공" if ok else "실패(로그 확인)")
    ok_n, total, note = doser.sync_clock_all()
    return ok_n == total and total > 0, "시계 동기화 %d/%d대 성공 — %s" % (ok_n, total, note)


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
             "bt_target": _job_bt_target, "bt_scan": _job_bt_scan,
             "dev_ver": _job_dev_ver,
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
