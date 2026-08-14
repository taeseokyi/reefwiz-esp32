#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AquaWiz 펌웨어 시뮬레이터 (소켓 가상 포트)

실제 펌웨어(aquawiz_ph_meter_final.ino)의 명령별 응답을 충실히 흉내내, 하드웨어 없이
measure_kh_once.py 의 측정 흐름·RF 순단 재연결/keepalive/모터정지 로직을 상황별로 검증한다.

규칙(요청):
  1) 소켓(TCP) 가상 포트로 연결 — measure 쪽은 serial.serial_for_url('socket://host:port').
  2) 측정(tank/ref)은 *상수 pH* 로 응답 → 호스트 평탄 판정(FLAT_NET_N=8)상 정확히 8회째 평탄 latch.
  3) 모터(mNf/mNb)는 실제 지연 없이 즉시 펌웨어 메시지([Mx] 방향 / [모터x] 완료)로 응답.

★펌웨어 상태(refDKH·측정완료 플래그·저장 pH)는 *연결을 가로질러 유지*된다(실제 펌웨어는 RF 드롭
  중에도 살아있으므로). 드롭은 self.drops 스펙으로 특정 명령 시점에 소켓을 강제로 닫아 시뮬레이션.

단독 실행(수동 테스트): python3 firmware_sim.py [port]
  → 포트를 출력하고 계속 서빙. 다른 터미널에서 measure_kh_once.py 로 socket:// 접속 가능.
"""
import socket
import threading
import re
import math

# 측정 상수 — 매 read 동일값 반환 → 호스트가 정확히 8회째(FLAT_NET_N) 평탄 latch
TANK_PH = 7.940
REF_PH  = 7.956
TEMP    = 27.0
DEFAULT_REF_DKH = 8.448


class FirmwareSim:
    def __init__(self, host='127.0.0.1', port=0):
        self.host = host
        self.port = port
        self._srv = None
        self._thread = None
        self._running = False
        # 펌웨어 상태(연결 가로질러 유지)
        self.ref_dkh = DEFAULT_REF_DKH
        self.tank_ph = None
        self.ref_ph = None
        self.tank_meas_done = False
        self.ref_meas_done = False
        self.kh_hist = 0
        self.hour = None            # settime:HH 로 주입된 시(없으면 표기 '??' — 펌웨어와 동일)
        # 관측용
        self.received = []          # 수신한 모든 명령(빈 줄 keepalive 제외)
        self.connection_count = 0   # accept 횟수(=최초연결+재연결)
        # ── 정상/예외 시뮬레이션 스펙 ──
        # 드롭(소켓 강제 닫기): {'pat':str,'nth':int,'when':'before'|'after'[, 'kill':True]}
        #   kill=True 면 드롭과 함께 서버 자체를 내려 이후 재연결을 거부(완전 통신 두절 모사).
        self.drops = []
        # tank pH 프로파일(무딘 S커브 등 시계열 모사): None=상수 TANK_PH(기존 동작).
        #   리스트면 tank 명령마다 순서대로 반환, 소진 후엔 마지막 값 유지(=평형).
        self.tank_profile = None
        self._tank_idx = 0
        # 예외 모드(지속): 특정 명령 패턴을
        self.garble = set()         #   측정 응답에서 pH 라인 누락 → 호스트 parse 실패(FAIL_MAX 경로)
        self.no_done = set()        #   모터 응답에서 '[모터n] 완료' 누락 → 미완료(튜브 막힘 모사)
        self.hang = False           # True 면 모든 명령을 읽되 응답 안 함(펌웨어 먹통 모사)
        self.no_reply = {}          # {부분문자열: 생략할 응답 횟수} — 소켓 유지한 채 이 명령 응답만 n회 생략(조용한 유실 모사)

    # ── 서버 ──────────────────────────────────────────────
    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._srv.settimeout(0.2)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self._srv:
            try: self._srv.close()
            except Exception: pass

    def _serve(self):
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connection_count += 1
            self._handle(conn)

    def _handle(self, conn):
        conn.settimeout(0.2)
        buf = b''
        while self._running:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:           # 상대가 닫음(호스트 reconnect 가 옛 소켓 close)
                break
            buf += data
            while b'\n' in buf:
                raw, buf = buf.split(b'\n', 1)
                cmd = raw.decode('utf-8', errors='replace').strip()
                if not cmd:        # 빈 줄(keepalive) → 펌웨어 무시(무응답)
                    continue
                self.received.append(cmd)
                if self.hang:      # 펌웨어 먹통: 읽되 응답 안 함(소켓은 유지)
                    continue
                hit = next((p for p in self.no_reply if p in cmd and self.no_reply[p] > 0), None)
                if hit:            # 조용한 유실: 소켓은 살린 채 이 명령 응답만 생략(send 재시도까지 소진)
                    self.no_reply[hit] -= 1
                    continue
                d = self._match_drop(cmd, 'before')
                if d:
                    self._do_drop(conn, d)
                    return         # 응답 전 드롭(수신 중 끊김 시뮬)
                resp = self.respond(cmd)
                if resp:
                    try:
                        conn.sendall(('\r\n'.join(resp) + '\r\n').encode('utf-8'))
                    except OSError:
                        return
                d = self._match_drop(cmd, 'after')
                if d:
                    self._do_drop(conn, d)
                    return         # 응답 후 드롭(다음 명령이 죽은 링크 만남 시뮬)
        try: conn.close()
        except Exception: pass

    def _match_drop(self, cmd, when):
        for d in self.drops:
            if d.get('consumed'):
                continue
            if d['when'] != when or d['pat'] not in cmd:
                continue
            d['seen'] = d.get('seen', 0) + 1
            if d['seen'] >= d.get('nth', 1):
                d['consumed'] = True
                return d
        return None

    def _do_drop(self, conn, spec):
        try: conn.close()
        except Exception: pass
        if spec.get('kill'):       # 완전 통신 두절: 서버 내려 이후 재연결 거부
            self._running = False
            try: self._srv.close()
            except Exception: pass

    def _ts(self):
        """펌웨어 getTimeStr 모방 — settime 전이면 '??', 후엔 두 자리 시(경과 시간 가산은 생략)."""
        return '??' if self.hour is None else f'{self.hour:02d}'

    # ── 명령별 응답(펌웨어 충실 모방) ───────────────────────
    def respond(self, cmd):
        # 모터 구동: mNf:초 / mNb:초 → [MN] 방향 초 + [모터N] 완료 (즉시)
        m = re.match(r'm([1-4])([fb]):(\d+)$', cmd)
        if m:
            n, fb, sec = m.group(1), m.group(2), m.group(3)
            dirn = '정방향' if fb == 'f' else '역방향'
            if any(p in cmd for p in self.no_done):   # 완료 누락(튜브 막힘 모사)
                return [f'[M{n}] {dirn} {sec}초']
            return [f'[M{n}] {dirn} {sec}초', f'[모터{n}] 완료']
        # 모터 정지: mNs → [MN] 정지
        m = re.match(r'm([1-4])s$', cmd)
        if m:
            return [f'[M{m.group(1)}] 정지']
        # 에어/솔레노이드
        if cmd == 'airoff': return ['[에어] OFF']
        if cmd == 'ron':    return ['[SOL] 참조ON']
        if cmd == 'roff':   return ['[SOL] 참조OFF']
        if cmd == 'ton':    return ['[SOL] 수조ON']
        if cmd == 'toff':   return ['[SOL] 수조OFF']
        if cmd == 'stop':   return ['[STOP] 전체 정지(모터+핀)']
        # 측정: 상수 pH(8회째 평탄) 또는 tank_profile 시계열(무딘 S커브 모사)
        if cmd == 'tank':
            if self.tank_profile:
                ph = self.tank_profile[min(self._tank_idx, len(self.tank_profile) - 1)]
                self._tank_idx += 1
            else:
                ph = TANK_PH
            self.tank_ph = ph; self.tank_meas_done = True
            val = (f'[수조수] V:1234.567 T:{TEMP:.1f}C (pH 누락)' if 'tank' in self.garble
                   else f'[수조수] V:1234.567 pH:{ph:.3f} T:{TEMP:.1f}C')
            return ['', '[START] 수조수 측정(8초)...',
                    '  샘플링: 16/64', '  샘플링: 32/64', '  샘플링: 48/64', '  샘플링: 64/64',
                    '  [RAW] min=11072 mid=11073 max=11074 z=0',   # ★신펌웨어 진단줄(호스트 파싱 무영향 검증)
                    val, '[OK]']
        if cmd == 'ref':
            self.ref_ph = REF_PH; self.ref_meas_done = True
            val = (f'[참조수] V:1234.567 T:{TEMP:.1f}C (pH 누락)' if 'ref' in self.garble
                   else f'[참조수] V:1234.567 pH:{REF_PH:.3f} T:{TEMP:.1f}C')
            return ['', '[START] 참조수 측정(8초)...',
                    '  샘플링: 16/64', '  샘플링: 32/64', '  샘플링: 48/64', '  샘플링: 64/64',
                    '  [RAW] min=11072 mid=11073 max=11074 z=0',   # ★신펌웨어 진단줄(호스트 파싱 무영향 검증)
                    val, '[OK]']
        # settime:HH → 시각(시) 주입. 펌웨어 executeOneCmd 와 동일한 응답/범위(0~23).
        m = re.match(r'settime:(\d+)$', cmd)
        if m:
            h = int(m.group(1))
            if 0 <= h <= 23:
                self.hour = h
                return [f'[OK] 시각(시): {h:02d}']
            return ['[ERR] settime:HH (0~23)']
        # setref:x → refDKH 설정
        m = re.match(r'setref:([\d.]+)$', cmd)
        if m:
            v = float(m.group(1))
            if 0.5 <= v <= 30.0:
                self.ref_dkh = v
                return [f'[OK] refDKH:{v:.3f} dKH']
            return ['[ERR] 0.5~30.0']
        # calkh → dKH 계산 블록
        if cmd == 'calkh':
            if not (self.ref_meas_done and self.tank_meas_done and self.ref_dkh > 0):
                return ['[ERR] ref/tank 미측정 또는 refDKH 없음']
            delta = self.ref_ph - self.tank_ph
            tank_dkh = self.ref_dkh * math.pow(10.0, -delta)
            self.kh_hist += 1
            return ['===[dKH]===', f'  시각:{self._ts()}',
                    f'  참조pH:{self.ref_ph:.3f}', f'  수조pH:{self.tank_ph:.3f}',
                    f'  dPH:{delta:.4f}',
                    f'  refKH:{self.ref_dkh:.3f} dKH', f'  수조KH:{tank_dkh:.3f} dKH',
                    f'  온도:{TEMP:.1f}C', '===========',
                    f'[OK] 이력저장 총{self.kh_hist}개']
        # calref → 참조 dKH 역산·저장
        if cmd == 'calref':
            if not (self.ref_meas_done and self.tank_meas_done and self.ref_dkh > 0):
                return ['[ERR] ref/tank 미측정 또는 setref 없음']
            known = self.ref_dkh
            delta = self.tank_ph - self.ref_ph
            new_ref = known * math.pow(10.0, -delta)
            self.ref_dkh = new_ref
            return ['===[calref]===', f'  시각:{self._ts()}',
                    f'  참조pH:{self.ref_ph:.3f}', f'  수조pH:{self.tank_ph:.3f}',
                    f'  dPH:{delta:.4f}',
                    f'  새refDKH:{new_ref:.3f} dKH', f'  수조dKH:{known:.3f} dKH',
                    f'  온도:{TEMP:.1f}C', '===============',
                    f'[OK] refDKH 저장:{new_ref:.3f} dKH', '[INFO] ref/tank 재측정 필요']
        # status → 상태 블록(reconnect/ensure_link 의 핑 응답)
        if cmd == 'status':
            tp = self.tank_ph if self.tank_ph is not None else 0.0
            rp = self.ref_ph if self.ref_ph is not None else 0.0
            return ['=== 상태 ===', f'시각:{self._ts()}',
                    f'온도:{TEMP:.1f}C 오프셋:0.00 보정T:25.0C',
                    f'수조pH:{tp:.3f}', f'참조pH:{rp:.3f}', f'dPH:{(rp-tp):.4f}',
                    f'refKH:{self.ref_dkh:.3f} dKH', f'수조KH:0.000 dKH',
                    f'KH이력:{self.kh_hist}개',
                    '[M1] 정지', '[M2] 정지', '[M3] 정지', '[M4] 정지',
                    '[직접] 에어(D12):OFF PWM(D13):OFF', '============']
        # 알 수 없는 명령 → 무응답(펌웨어도 대부분 무시)
        return []


if __name__ == '__main__':
    import sys, time
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    sim = FirmwareSim(port=port)
    p = sim.start()
    print(f"[firmware_sim] 서빙 중: socket://127.0.0.1:{p}  (Ctrl+C 종료)")
    print(f"  측정 평탄: tank pH {TANK_PH}, ref pH {REF_PH} (8회째 latch), 모터 즉시 완료")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()
        print("\n[firmware_sim] 종료")
