# ReefWiz KH 측정 V4 이식 — 원본 reefwiz/bin/measure_kh_once.py (1,197줄).
# 측정 시퀀스·평탄 판정·안전 레일(전제조건 검증, 진행 상태 추적, 비상정리, 에러 래치,
# 호스트 구제 계산)을 그대로 유지한다. 제거: argparse/CLI(함수 인자로), MQTT(reefCore 발행,
# 사용자 지시 2026-08-13), pythonw 로깅 리다이렉트(datalog.log 로), pyserial(link.Link 로).
# 각 안전장치의 사고 이력·설계 근거는 원본 파일 주석 참조 — 여기서는 요지만 남긴다.
import re
import time

import config
import datalog
import rwtime
import state
import link
import watchdog

p = datalog.log

# 진행 상태 추적 — 비상정리 레시피 선택용. 이동 도중 실패 = UNKNOWN(sticky) → 동결.
_liquid = {"chamber": "KCL", "holding": "EMPTY"}


# ─────────────────────────────────────────────
# 폭기·이송 전제조건 (응답 검증 — '거짓 성공' 방지, 원본 2026-07-24/28)
# ─────────────────────────────────────────────

def ensure_aeration_on(lk, where=""):
    """ron 송신·'참조ON' 확인. 미확인 시 재연결 후 1회 재확인.
    경고(**)는 '링크 생존인데 응답만 유실'에만 — 링크 사망은 별도 경로가 처리."""
    tag = " (%s)" % where if where else ""
    for attempt in (1, 2):
        try:
            lines = lk.send("ron", stop_pattern="참조ON")
        except OSError:
            lines = []
        if any("참조ON" in ln for ln in (lines or [])):
            return True
        if attempt == 1:
            p("    [RF] ron 재폭기 미확인%s → 재연결 후 재확인" % tag)
            try:
                lk.reconnect("ron 재폭기 미확인%s" % tag)
            except OSError:
                pass
    if lk.ensure_link():
        p("    **[경고] ron 재폭기 최종 미확인%s — 응답만 유실, 폭기 꺼진 채 진행 가능성" % tag)
    else:
        p("    [RF] ron 재폭기 미확인%s — 링크 사망(호출부가 처리)" % tag)
    return False


def ensure_aeration_off(lk, where="", swallow=True):
    """airoff 송신·'OFF' 확인 — ensure_aeration_on 의 대칭. swallow=False 면 통신 예외를
    호출부 except 로 올린다(정리 구간의 링크 사망 경로 보존)."""
    tag = " (%s)" % where if where else ""
    for attempt in (1, 2):
        try:
            lines = lk.send("airoff", stop_pattern="OFF")
        except OSError:
            if not swallow:
                raise
            lines = []
        if any("OFF" in ln for ln in (lines or [])):
            return True
        if attempt == 1:
            p("    [RF] airoff 미확인%s → 재연결 후 재확인" % tag)
            try:
                lk.reconnect("airoff 미확인%s" % tag)
            except OSError:
                if not swallow:
                    raise
    if lk.ensure_link():
        p("    **[경고] airoff 최종 미확인%s — 응답만 유실(read 노이즈/이송 무효 주의)" % tag)
    else:
        p("    [RF] airoff 미확인%s — 링크 사망(호출부가 처리)" % tag)
    return False


def _cleanup_precond(lk):
    """airoff+ton 둘 다 응답 확인돼야 True — 밀폐계라 미확인이면 모터 이송이 헛돈다."""
    ok = True
    for cmd, stop in (("airoff", "OFF"), ("ton", "수조ON")):
        try:
            lines = lk.send(cmd, stop_pattern=stop, timeout=5)
            ok = any(stop in ln for ln in (lines or [])) and ok
        except Exception:
            ok = False
    return ok


def ensure_move_precond(lk, where, recovery_secs=None):
    """액체 이동 전제조건(airoff+ton) — 미확인이면 상한까지 재접속·재시도 후 False.
    호출부는 False 면 모터를 돌리지 않는다(헛도는 이송 + '완료' 로그 = 거짓 성공 방지)."""
    if recovery_secs is None:
        recovery_secs = config.MOVE_PRECOND_RECOVERY_SECS
    if _cleanup_precond(lk):
        return True
    deadline = time.time() + recovery_secs
    p("    *[이송 전제조건] airoff·ton 미확인 (%s) — 최대 %ds 재시도" % (where, recovery_secs))
    while time.time() < deadline:
        watchdog.feed()
        time.sleep(min(config.LINK_RETRY_INTERVAL, max(1, deadline - time.time())))
        lk.reconnect("이송 전제조건 재시도 (%s)" % where)
        if _cleanup_precond(lk):
            p("    [이송 전제조건] 확인됨 (%s) — 이송 진행" % where)
            return True
    if lk.ensure_link():
        p("    **[경고] 이송 전제조건 끝내 미확인 (%s) — 응답만 유실 → 모터 생략" % where)
    else:
        p("    [RF] 이송 전제조건 미확인 (%s) — 링크 사망 → 모터 생략" % where)
    return False


def _motor_ok(lines, idx):
    return any(("[모터%d] 완료" % idx) in ln for ln in (lines or []))


def _move_liquid(lk, motor_idx, cmd, chamber_after, holding_after):
    """send_motor + 진행 상태 갱신. '완료' 미수신이면 UNKNOWN 유지(sticky)."""
    was_known = "UNKNOWN" not in (_liquid["chamber"], _liquid["holding"])
    _liquid["chamber"] = _liquid["holding"] = "UNKNOWN"
    lines = lk.send_motor(motor_idx, cmd)
    if was_known and _motor_ok(lines, motor_idx):
        _liquid["chamber"], _liquid["holding"] = chamber_after, holding_after
    return lines


def _sync_firmware_hour(lk):
    """펌웨어 시계(시)를 맞춘다 — 표기·이력 전용, 실패해도 측정 진행."""
    try:
        lines = lk.send("settime:%02d" % rwtime.hour(), stop_pattern="시각(시)", timeout=5)
    except OSError as e:
        p("    [경고] settime 실패(%s) — 시각 표기만 ?? (측정은 진행)" % e)
        return
    if not any("[OK]" in ln for ln in lines):
        p("    [경고] settime 응답 미확인 — 시각 표기만 ?? (측정은 진행)")


# ─────────────────────────────────────────────
# 평형(plateau) 추종 측정
# ─────────────────────────────────────────────

def parse_ph(lines, label):
    for line in lines:
        m = re.search(r"\[" + label + r"\].*pH:([\d.]+)", line)
        if m:
            return float(m.group(1))
    return None


def _append_reading(readings, n, ph, elapsed):
    """판독 누적 — 상한 초과 시 앞쪽 절반을 2:1 로 솎는다(힙 보호).
    최신 구간(평탄 판정·CO2 판정이 보는 곳)은 촘촘히 남고 곡선 형태도 유지된다.
    ※CO2 판정의 net 은 첫 판독을 쓰는데, 솎기는 첫 항목을 항상 보존하므로 영향 없다."""
    readings.append({"n": n, "ph": ph, "elapsed": elapsed})
    if len(readings) > config.PLATEAU_KEEP_MAX:
        half = len(readings) // 2
        readings[:] = readings[:half:2] + readings[half:]


def _wait_link_recovery(lk, phase_t0):
    """링크 사망 시 phase 마감까지 재접속 대기 — 폭기 유지=평형 보존이라 무해."""
    deadline = phase_t0 + config.PHASE_MAX_SECS
    remain = int(deadline - time.time())
    if remain <= 0:
        return False
    p("    [RF] 링크 사망 — phase 마감까지(잔여 %ds) %ds 간격 재접속 대기"
      % (remain, config.LINK_RETRY_INTERVAL))
    while time.time() < deadline:
        watchdog.feed()
        time.sleep(min(config.LINK_RETRY_INTERVAL, max(1, deadline - time.time())))
        if lk.reconnect("링크 복구 대기"):
            return True
    return False


def measure_until_flat(lk, what, readings):
    """반복 측정 — read 직전 airoff+정치, 샘플 사이 폭기 유지. 평탄 판정은 정수 milli-pH
    윈도우(span AND net). readings 리스트에 {n, ph, elapsed} 를 누적(plateau JSON·CO2 판정용).
    반환: (ph, n_reads, flat_ok)."""
    label = "수조수" if what == "tank" else "참조수"
    min_n = config.FLAT_MIN_N_TANK if what == "tank" else 0
    win = []
    last_ph = None
    fails = 0
    t0 = time.time()
    n = 0
    n_ok = 0
    while True:
        watchdog.feed()
        if state.abort_requested:
            raise state.Aborted("%s 측정 중 중단 요청(%d회 진행)" % (what, n))
        n += 1
        ensure_aeration_off(lk, "%s read 직전" % what)
        lk.keepalive_sleep(config.SETTLE_SECS)
        try:
            lines = lk.send(what, stop_pattern="[OK]", timeout=config.MEAS_READ_TIMEOUT)
        except OSError:
            lines = []
        ph = parse_ph(lines, label)
        if ph is None:
            if not lk.ensure_link():
                if _wait_link_recovery(lk, t0):
                    p("    [RF] 링크 복구 — %s 측정 재개" % what)
                    continue
                p("    [상한] %s 링크 복구 실패(phase 마감) — 미평탄, 마지막값 %s 채택"
                  % (what, last_ph))
                return last_ph, n, False
            fails += 1
            p("    [측정실패 %d/%d] %s" % (fails, config.FAIL_MAX, what))
            if fails >= config.FAIL_MAX:
                p("    [실패] %s 연속 %d회 응답 이상 — phase 중단" % (what, config.FAIL_MAX))
                return last_ph, n, False
        else:
            fails = 0
            last_ph = ph
            n_ok += 1
            elapsed = int(time.time() - t0)
            _append_reading(readings, n, ph, elapsed)
            win.append(round(ph * 1000))
            if len(win) > config.FLAT_NET_N:
                win.pop(0)
            if len(win) >= config.FLAT_SPAN_N:
                tail = win[-config.FLAT_SPAN_N:]
                span = max(tail) - min(tail)
                net = abs(win[-1] - win[0]) if len(win) >= config.FLAT_NET_N else None
                p("    [%s] %d회 pH:%.3f span%d:%dmpH net%d:%smpH (%ds)"
                  % (what, n, ph, config.FLAT_SPAN_N, span, config.FLAT_NET_N,
                     net if net is not None else "-", elapsed))
                if span <= config.FLAT_SPAN_MPH and net is not None and net <= config.FLAT_NET_MPH:
                    if n_ok < min_n:
                        p("    [평탄보류] %s %d회 — MIN_N(%d) 미달(%d회), 계속 관찰"
                          % (what, n, min_n, n_ok))
                    else:
                        p("    [평탄] %s %d회 — span=%d net=%d → 평형 (pH %.3f)"
                          % (what, n, span, net, ph))
                        return ph, n, True
            else:
                p("    [%s] %d회 pH:%.3f (윈도우 %d/%d, %ds)"
                  % (what, n, ph, len(win), config.FLAT_SPAN_N, elapsed))

        if time.time() - t0 >= config.PHASE_MAX_SECS:
            p("    [상한] %s %ds 초과 — 미평탄, 마지막값 %s 채택"
              % (what, config.PHASE_MAX_SECS, last_ph))
            return last_ph, n, False
        if n >= config.MEAS_MAX:
            p("    [상한] %s 측정 %d회 초과 — 미평탄, 마지막값 %s 채택"
              % (what, config.MEAS_MAX, last_ph))
            return last_ph, n, False
        ensure_aeration_on(lk, "sample")
        lk.keepalive_sleep(config.MEAS_INTERVAL)


# ─────────────────────────────────────────────
# 결과 파싱 / 비상 정리
# ─────────────────────────────────────────────

def parse_results(kh_lines, calref=False):
    if calref:
        patterns = {"ref_ph": r"참조pH:([\d.]+)", "tank_ph": r"수조pH:([\d.]+)",
                    "ref_kh": r"새refDKH:([\d.]+)", "tank_kh": r"수조dKH:([\d.]+)",
                    "temp": r"온도:([\d.]+)"}
    else:
        patterns = {"ref_ph": r"참조pH:([\d.]+)", "tank_ph": r"수조pH:([\d.]+)",
                    "ref_kh": r"refKH:([\d.]+)", "tank_kh": r"수조KH:([\d.]+)",
                    "temp": r"온도:([\d.]+)"}
    vals = {k: None for k in patterns}
    for line in kh_lines:
        for key, pat in patterns.items():
            if vals[key] is None:
                m = re.search(pat, line)
                if m:
                    vals[key] = float(m.group(1))
    return (vals["ref_ph"], vals["tank_ph"], vals["ref_kh"], vals["tank_kh"], vals["temp"])


def _safe_cleanup(lk):
    """에러/비정상 종료 시 비상 정리 — 전제조건 우선, 진행 지점(_liquid)별 레시피,
    위치 불명이면 동결(잘못된 이송보다 낫다). 원본 2026-07-10 원칙 그대로."""
    ch, hd = _liquid["chamber"], _liquid["holding"]
    p("\n[비상정리] 진행 지점 판단: 챔버=%s 홀딩=%s" % (ch, hd))
    if "UNKNOWN" in (ch, hd):
        p("**[비상정리] 액체 위치 불명 — 자동 정리 생략(동결). 챔버·홀딩·KCl 수동 확인 필요")
        p("**[경고] 비상 KCl 소크 미완료 — 프로브가 KCl 없이 방치됐을 수 있음! 수동 확인 필요")
        return
    if ch == "KCL":
        p("    [비상정리] 챔버=KCl 소크 상태(목표 상태) — 모터 조치 불필요")
        try:
            lk.send("airoff", stop_pattern="OFF", timeout=5)
        except Exception:
            pass
        return
    p("[비상정리] 에어 OFF + 챔버 배출/회수 + KCl 소크 복원 시도")
    pre_ok = _cleanup_precond(lk)
    if not pre_ok:
        deadline = time.time() + config.CLEANUP_RECOVERY_SECS
        p("    [비상정리] 전제조건 실패 — 링크 회복 대기(최대 %ds)" % config.CLEANUP_RECOVERY_SECS)
        while time.time() < deadline:
            watchdog.feed()
            time.sleep(min(config.LINK_RETRY_INTERVAL, max(1, deadline - time.time())))
            if lk.reconnect("비상정리 전제조건 재시도") and _cleanup_precond(lk):
                pre_ok = True
                break
    kcl_ok = False
    if pre_ok:
        if ch == "TANK":
            try:
                _move_liquid(lk, 2, "m2b:68", "EMPTY", "TANK")
            except Exception:
                pass
        elif ch == "REF":
            try:
                _move_liquid(lk, 4, "m4b:70", "EMPTY", hd)
            except Exception:
                pass
        if _liquid["holding"] == "TANK":
            try:
                _move_liquid(lk, 1, "m1b:82", "EMPTY", "EMPTY")
            except Exception:
                pass
        if (_liquid["chamber"], _liquid["holding"]) == ("EMPTY", "EMPTY"):
            try:
                kcl_lines = _move_liquid(lk, 3, "m3f:60", "KCL", "EMPTY")
                kcl_ok = _motor_ok(kcl_lines, 3)
            except Exception:
                pass
        else:
            p("**[비상정리] 배출/회수 미완 — KCl 재공급 생략(동결)")
        try:
            lk.send("airoff", stop_pattern="OFF", timeout=5)
        except Exception:
            pass
    else:
        p("**[비상정리] 전제조건 끝내 실패 — 이송 무효라 모터 생략(동결)")
    if not kcl_ok:
        p("**[경고] 비상 KCl 소크 미완료 — 프로브가 KCl 없이 방치됐을 수 있음! 수동 확인 필요")


# ─────────────────────────────────────────────
# 측정 루틴 (V4) — 원본 run_measurement 이식
# ─────────────────────────────────────────────

def run_measurement(lk, tank_dkh=None, plateau=None):
    """tank_dkh None=calkh / 값 있음=calref. plateau dict 에 판독 이력을 채운다.
    반환: (ref_ph, tank_ph, ref_kh, tank_kh, temp) — 실패 항목 None."""
    calref = tank_dkh is not None
    completed = False
    _liquid["chamber"], _liquid["holding"] = "KCL", "EMPTY"
    tank_readings = plateau["tank"] if plateau else []
    ref_readings = plateau["ref"] if plateau else []
    try:
        _sync_firmware_hour(lk)

        if calref:
            p("\n[calref] 수조 실측 dKH 를 setref 로 기록: %.3f dKH" % tank_dkh)
            sr = lk.send("setref:%.3f" % tank_dkh, stop_pattern="refDKH", timeout=5)
            if not any("[OK] refDKH" in ln for ln in sr):
                raise RuntimeError("setref 실패(범위 0.5~30.0 확인)")

        # 구제 캐시 — 링크 사망 시 호스트(ESP32)가 동일 차동식으로 dKH 계산(calkh 전용)
        refkh_cached = temp_cached = None
        if not calref:
            for ln in lk.send("status", stop_pattern="============", timeout=5):
                m = re.search(r"refKH:([\d.]+)", ln)
                if m and refkh_cached is None:
                    refkh_cached = float(m.group(1))
                m = re.search(r"온도:([\d.]+)", ln)
                if m and temp_cached is None:
                    temp_cached = float(m.group(1))
            p("\n[구제캐시] refKH=%s 온도=%sC" % (refkh_cached, temp_cached))

        # ── 준비: KCl 배출 → tank 이송 (전제조건 필수) ──
        if not ensure_move_precond(lk, "준비 이송"):
            raise RuntimeError("준비 이송 전제조건 미확인 — 이송 생략(거짓 성공 방지)")
        p("\n[준비] KCl 배출 (측정 챔버)")
        _move_liquid(lk, 3, "m3b:68", "EMPTY", "EMPTY")
        p("\n[tank] 본수조수 -> 홀딩 (m1)")
        _move_liquid(lk, 1, "m1f:70", "EMPTY", "TANK")
        p("\n[tank] 홀딩 -> 측정 챔버 (m2)")
        _move_liquid(lk, 2, "m2f:60", "TANK", "EMPTY")

        # ── [A] 폭기 ON — tank 평탄까지 (5L ref 동시폭기 = co-aeration) ──
        ensure_aeration_off(lk, "tank 사전폭기 진입 전(ton 해제)")
        ensure_aeration_on(lk, "tank 사전폭기")
        p("\n[폭기] ON (측정챔버 tank + 5L 위즈수조 동시)")

        # 첫 점(관리용) — 실패해도 본 측정 계속, 폭기 재점화는 필수
        try:
            p("[첫점] tank — 사전폭기 전 %ds 폭기 후 정치 read" % config.FIRST_POINT_AERATE_SECS)
            lk.keepalive_sleep(config.FIRST_POINT_AERATE_SECS)
            ensure_aeration_off(lk, "첫점 read 직전")
            lk.keepalive_sleep(config.SETTLE_SECS)
            fp = lk.send("tank", stop_pattern="[OK]", timeout=config.MEAS_READ_TIMEOUT)
            first_ph = parse_ph(fp, "수조수")
            if first_ph is not None:
                p("    [tank] 0회 pH:%.3f (첫점 사전폭기전, 0s)" % first_ph)
                tank_readings.append({"n": 0, "ph": first_ph, "elapsed": 0})
            else:
                p("    [첫점] 파싱 실패 — 첫 점 없이 본 측정 계속")
        except OSError as e:
            p("    [첫점] 측정 실패(%s) — 첫 점 없이 본 측정 계속" % e)
        finally:
            ensure_aeration_on(lk, "tank 사전폭기(첫점 후 재폭기)")

        pt = config.PREAERATE_SECS["tank"]
        p("[사전폭기] tank %ds — 평형 도달용 고정 폭기(미측정)" % pt)
        lk.keepalive_sleep(pt)
        p("[측정] tank 평탄까지 — read 직전에만 폭기 off")
        tank_ph, tank_n, tank_flat = measure_until_flat(lk, "tank", tank_readings)
        if tank_ph is None:
            raise RuntimeError("tank 측정 실패(응답 없음)")

        # ── 전이: tank 홀딩 파킹 → ref 이송 ──
        if not ensure_move_precond(lk, "ref 이송"):
            raise RuntimeError("ref 이송 전제조건 미확인 — 이송 생략(거짓 성공 방지)")
        p("\n[tank] 측정챔버 -> 홀딩 임시 파킹 (m2 역방향)")
        _move_liquid(lk, 2, "m2b:68", "EMPTY", "TANK")
        p("\n[ref] 참조수 5L -> 측정 챔버 (m4)")
        _move_liquid(lk, 4, "m4f:60", "REF", "TANK")

        # ── [B] 폭기 ON — ref 평탄까지 ──
        ensure_aeration_off(lk, "ref 사전폭기 진입 전(ton 해제)")
        ensure_aeration_on(lk, "ref 사전폭기")
        p("\n[폭기] ON (측정챔버 ref + 5L 위즈수조 동시)")
        pr = config.PREAERATE_SECS["ref"]
        p("[사전폭기] ref %ds" % pr)
        lk.keepalive_sleep(pr)
        p("[측정] ref 평탄까지 — read 직전에만 폭기 off")
        ref_ph, ref_n, ref_flat = measure_until_flat(lk, "ref", ref_readings)
        if ref_ph is None:
            raise RuntimeError("ref 측정 실패(응답 없음)")

        # ── KH 계산 — calkh 모드는 링크 사망을 죽이지 않는다(호스트 구제로 진행) ──
        try:
            ensure_aeration_off(lk, "측정 종료(calkh·정리 이동 전)", swallow=False)
            if calref:
                p("\n[calref] ref dKH 역산·저장")
                kh_lines = lk.send("calref", stop_pattern="refDKH 저장", timeout=10)
            else:
                p("\n[KH] 계산")
                kh_lines = lk.send("calkh", stop_pattern="===========", timeout=10)
        except OSError:
            if calref:
                raise
            p("    [RF] 링크 사망 — calkh 불능, 호스트 구제 계산으로 진행")
            kh_lines = []

        # 호스트 구제 — calkh 응답이 없어도 phase 데이터가 온전하면 동일 차동식으로 계산
        if (not calref) and (not any("수조KH:" in ln for ln in kh_lines)) \
                and refkh_cached is not None and tank_ph is not None and ref_ph is not None:
            tk = refkh_cached * 10.0 ** (tank_ph - ref_ph)
            if 0.0 < tk <= 50.0:
                temp_s = temp_cached if temp_cached is not None else 0.0
                p("\n[측정 결과 — 호스트 구제] ref pH %.3f / tank pH %.3f / refKH %.3f"
                  % (ref_ph, tank_ph, refkh_cached))
                p("  수조 dKH: %.3f (음수=구제 표식) / 온도 %.1fC — 비상정리 수동 확인 필요"
                  % (-tk, temp_s))
                return (ref_ph, tank_ph, refkh_cached, -tk, temp_s)
            p("    [WARN] 구제 dKH 이상(%.3f) — 구제 포기(기존 에러 경로)" % tk)

        # ── 정상 정리 (전제조건 우선, calkh 는 링크 사망 시 음수 표식으로 결과만 살림) ──
        link_lost = False
        if not ensure_move_precond(lk, "정리 이송"):
            if calref:
                raise RuntimeError("정리 이송 전제조건 미확인 — 이송 생략(거짓 성공 방지)")
            p("**[정리] 전제조건 미확인 — 정상 정리 생략. 측정값은 음수 표식으로 기록, "
              "KCl 소크는 비상정리·수동 확인")
            link_lost = True
        if not link_lost:
            try:
                p("\n[정리] 참조수 회수 (m4 역방향)")
                _move_liquid(lk, 4, "m4b:70", "EMPTY", "TANK")
                p("\n[정리] 파킹 수조수 배출 (m1 역방향)")
                _move_liquid(lk, 1, "m1b:82", "EMPTY", "EMPTY")
                p("\n[정리] KCl 공급 (프로브 소크)")
                kcl_lines = _move_liquid(lk, 3, "m3f:60", "KCL", "EMPTY")
                ensure_aeration_off(lk, "정리 완료", swallow=False)
                if not _motor_ok(kcl_lines, 3):
                    raise RuntimeError("KCl 소크(m3f) 미완료 — 프로브 소크 실패 → 에러(0.0) 기록")
                completed = True
            except OSError:
                if calref:
                    raise
                link_lost = True
                p("**[RF] 링크 사망 — 정상 정리 미완료. 측정값은 음수 표식으로 기록")

        # ── 파싱·결과 ──
        ref_ph_r, tank_ph_r, ref_kh, tank_kh, temp = parse_results(kh_lines, calref=calref)
        plateau_ok = bool(tank_flat and ref_flat) and not link_lost
        if plateau is not None:
            plateau["tank_flat"] = tank_flat
            plateau["ref_flat"] = ref_flat

        if calref:
            if ref_kh is None:
                raise RuntimeError("calref 실패 — 새 refDKH 파싱 실패")
            tank_kh = tank_dkh if plateau_ok else -abs(tank_dkh)
            p("\n[calref 결과] 새 ref dKH %.3f (EEPROM 저장) / 입력 수조 %.3f / 평탄 %s"
              % (ref_kh, tank_dkh, "O" if plateau_ok else "X"))
            return (ref_ph_r, tank_ph_r, ref_kh, tank_kh, temp)

        if tank_kh is not None and not plateau_ok:
            tank_kh = -abs(tank_kh)
        p("\n[측정 결과 V4] tank %d회 %s / ref %d회 %s%s"
          % (tank_n, "O" if tank_flat else "X(상한)", ref_n, "O" if ref_flat else "X(상한)",
             " / 미평탄 음수 표식" if not plateau_ok else ""))
        if tank_kh is not None:
            p("  수조 dKH: %.3f / 참조 dKH: %s / 온도 %sC" % (tank_kh, ref_kh, temp))
        else:
            p("  dKH 파싱 실패")
        return (ref_ph_r, tank_ph_r, ref_kh, tank_kh, temp)
    finally:
        if not completed:
            _safe_cleanup(lk)


def make_link():
    """측정 장비로 전환된(=신원 검증된) 공유 링크. 실패하면 (None, 사유).
    ★HC-05 1개 체제(2026-08-18): 종전에는 호출마다 전용 UART 를 새로 잡았지만, 이제는
    측정기·도저가 같은 모듈을 번갈아 쓰므로 전환·검증을 거친 싱글턴을 받는다.
    ★allow_measuring: '측정 중 전환 금지' 레일을 지나가는 유일한 경로다 — 측정 흐름이
    자기 장비를 잡는 것이므로 막으면 측정 자체가 시작되지 않는다(다른 경로는 전부 막힌다)."""
    return link.acquire("meas", log=p, allow_measuring=True)


def run_once(tank_dkh=None, lk=None):
    """1회 측정 후 기록 — 원본 main() 상당(에러 래치·음수 표식·JSON 갱신·CO2 판정 포함).
    calref 는 REPL 에서 measure.run_once(tank_dkh=8.5) 로 수동 실행."""
    calref = tank_dkh is not None
    if calref and not (0.5 <= tank_dkh <= 30.0):
        p("[ERR] --setref 값 %s 는 허용 범위(0.5~30.0) 밖" % tank_dkh)
        return None
    hour = rwtime.hour()
    day = rwtime.date_str()      # ★측정 시작일 — 자정을 넘겨 끝나도 시작일에 귀속(원본 규칙)
    p("\n===== ReefWiz KH 측정 V4 (ESP32) %s [%s] =====" % (rwtime.stamp(),
      "calref" if calref else "calkh"))

    # 에러 래치 — 마지막 줄이 에러 표식이면 측정 생략(프로브 보호). 해제는 dkh.dat 수동 편집.
    if datalog.last_dat_is_error():
        p("[중단] dkh.dat 마지막 줄이 에러 표식 — 측정 생략, 에러 표식 재기록")
        datalog.log_kh(day, hour, 0.0, 0.0, 0.0, 0.0, 0.0)
        return None

    plateau = {"tank": [], "ref": [], "tank_flat": False, "ref_flat": False}
    run_started = rwtime.stamp()
    result = None
    aborted = False
    if lk is None:
        lk, err = make_link()
        if lk is None:
            # ★링크를 못 잡았으면 측정을 시작하지 않는다 — 어느 장비에 붙었는지 모르는 채
            #   모터를 돌리면 안 된다. 에러 표식을 남겨 래치를 걸고 운영자 확인을 요구한다.
            p("[중단] 측정 장비 링크 확보 실패 — %s" % err)
            datalog.log_kh(day, hour, 0.0, 0.0, 0.0, 0.0, 0.0)
            return None
    lk.log = p
    try:
        time.sleep(2)
        lk.flush_input()
        result = run_measurement(lk, tank_dkh, plateau)
    except state.Aborted as e:
        # 운영자 중단 — 장비 이상이 아니므로 에러 표식(0.0)을 쓰지 않는다(래치 안 걸림).
        # 비상정리는 run_measurement 의 finally 에서 이미 실행됨(프로브 KCl 복원 시도).
        aborted = True
        p("[중단] %s — 에러 표식 미기록(래치 없음), 비상정리 결과 확인 필요" % e)
    except Exception as e:
        p("[ERR] 예외 발생: %r" % e)
    finally:
        state.abort_requested = False

    ok = result is not None and all(v is not None for v in result)
    if ok:
        datalog.log_kh(day, hour, *result)
    elif not aborted:
        datalog.log_kh(day, hour, 0.0, 0.0, 0.0, 0.0, 0.0)
    co2_suspect = datalog.append_plateau(run_started,
                                         ("aborted" if aborted else
                                          ("calref" if calref else "calkh")),
                                         ok, plateau["tank"], plateau["ref"],
                                         plateau["tank_flat"], plateau["ref_flat"])
    if ok:
        datalog.append_series(day, hour, result[2], result[3], result[4],
                              is_flat=(result[3] > 0), co2_suspect=co2_suspect)
    return result if ok else None
