# 작업 인계 (2026-08-18)

다음 세션에서 이어서 작업하기 위한 현재 상태 요약. 상세 설계·배선·API 는 README.md 참조.

## 저장소

- **GitHub(private): https://github.com/taeseokyi/reefwiz-esp32**
- 로컬 경로: `E:\cygwin64\home\ower\work\reefwiz-esp32`
- 원본(public): https://github.com/taeseokyi/reefwiz — **2026-08-18 시점까지 반영 완료**
- git 자격증명은 Windows 에 캐시됨. git 사용자는 **저장소 로컬**로만 설정
  (taeseokyi / tsyi@kisti.re.kr) — 전역 설정 건드리지 않음

## 무엇을 만들었나

원본의 Windows PC + 작업 스케줄러 + WSL 동기화 + GitHub Pages 구성을
**ESP32(MicroPython) 한 대로 대체**.

| 모듈 | 내용 |
|---|---|
| `measure.py` | KH 측정 V4 이식 — 평탄 추종, 이송 전제조건 검증, 액체 위치 추적, 비상정리, 에러 래치, 호스트 구제 |
| `link.py` | **HC-05 1개** 링크 — 대상 전환(AT 재바인드) + ★신원 검증, keepalive/재연결/재송신, 전원 재투입 하드리셋 |
| `dkh_dat.py` | dkh.dat 한 줄의 파싱·포매팅 단일 규약 (날짜 컬럼) |
| `doser.py` | 도저 조정 — Theil-Sen(실측 시간 간격), 스텝캡/데드밴드/정지유지, 에코검증→refresh→롤백 |
| `storage.py` | SD 저장소(선택) — 로그 전문·보관 창 밖 아카이브·RF 이벤트 원장 |
| `ops.py` | 조치 도구 — 래치해제·측정중단·측정정리·KCl강제·액체위치지정·명령콘솔·BT연결점검·**BT 대상 전환** |
| `webserver.py` | LAN 전용 웹서버 — 대시보드·정비페이지·`/api/*`, plateau JSONL 스트리밍 |
| `display.py` | 한국어 터치 UI — kfont.bin 비트맵폰트, 픽셀 자동 줄바꿈, 해상도 자동적응 |
| `display_driver.py` | ★참조 구현(교체 대상) — ILI9341+XPT2046 |
| `wifinet.py` | WiFi 설정·AP 폴백(`reefwiz-setup` / `reefwiz1234`) |
| `datalog.py` | dkh.dat + 대시보드 JSON + plateau JSONL + CO₂ 편향 판정 |

## 확정된 사용자 결정 (변경 금지 — 재논의 불요)

1. **HC-06 유지**(장비 배선 무변경).
2. **★HC-05 1개**(2026-08-18 확정) — 두 장비에 `AT+BIND` 를 바꿔가며 번갈아 접속.
   매번 새로 연결해도 되고 전환 속도(8~12초)는 요구사항이 아니다.
   *종전의 "장비당 전용 모듈 2개" 안을 대체한다.*
   내장 BT 는 불가: 공식 MicroPython 이 BT Classic/SPP 를 안 열어주고, Classic 은 구형
   ESP32 에만 있어 보드 업그레이드 계획(#11)과 충돌한다.
3. **LAN 전용 발행** — 외부 접근·GitHub Pages 미러 불필요
4. **MQTT(reefCore) 발행 제거**
5. 설정 저장은 GitHub API 커밋 → **로컬 POST**(토큰 불요)
6. **모든 정보 14일 보존** — ★회차 컷이 아니라 **날짜 컷**(기준일 = 마지막 기록일)
7. **화면 전부 한국어** — 버튼: 측정 / 측정 중단 / 측정 정리 / 래치해제 / BT 전환 / 로그
8. **줄 잘림 금지** — 화면 크기 무관 픽셀 단위 자동 줄바꿈
9. 용어 통일: **측정 챔버 / 홀딩 챔버 / KCl 보관액 / 위즈 수조**
10. 에러 시 **BT 연결 회복 후 명령 콘솔로 직접 정리**하는 워크플로 중시
11. 보드·디스플레이는 **더 고성능·더 큰 것으로 교체 예정**. 드라이버만 분리, UI 는 해상도 자동적응
12. **SD 카드 리더 추가**(2026-08-18) — 선택 장비. 없어도 전부 동작한다.

**원본의 안전 레일은 반드시 유지** (실제 사고로 얻은 것들): 이송 전 airoff·ton 응답 검증
(거짓 성공 방지), 액체 위치 불명 시 동결, 비상정리 KCl 소크, 에러 래치, 평탄 미도달 음수 표식,
호스트 구제 계산.

## ★HC-05 1개 — 오장비 명령을 막는 3중 레일

"바인드가 어긋나면 도저 명령이 측정기로 간다"가 이 구성의 유일한 위험이었고, 코드로 막았다.
원본의 "거짓 성공은 있으면 안 된다" 원칙을 링크 계층에 적용한 것이다.

1. **신원 검증** — 전환 후 첫 구동 명령 전에 부작용 없는 조회로 응답 서명을 확인
   (측정기 `status` → `============` / 도저 `ls` → `왼쪽 동작·왼쪽 휴지`). 다른 장비 서명이
   오면 **즉시 동결**하고 `send()` 가 `LinkFrozen` 을 던진다. **자동 해제 없음** — 배선·BIND
   주소 확인 없이 풀면 같은 오접속을 반복하므로 정비페이지의 명시적 '동결 해제'로만 푼다.
2. **교차 프로브** — 침묵이 오면 다른 장비의 조회 형식으로 한 번 더 묻는다. 장비마다 줄 종단이
   달라(측정기 CRLF / 도저 LF only) 오접속이 '무응답'으로만 보이는데, 원인이 '상대 전원 꺼짐'
   인지 'BIND 주소 뒤바뀜'인지에 따라 운영자가 할 일이 완전히 다르다.
3. **모터 구동 중 전환 금지** — 전환은 곧 라디오 전원 차단이다. 모터가 도는 중에 끊으면 정지
   명령을 보낼 수단이 사라져 시약이 계속 주입된다.

## 자동 연결

BIND 주소만 넣으면 조작 불요. 부팅 시 측정 장비로 자동 연결하고(main.py), 이후 measure /
doser 가 각자 필요할 때 `link.acquire()` 로 전환한다. 이미 그 대상이면 즉시 반환하므로 연속
측정에 전환 비용이 없다. 정비페이지의 'BT 전환' 버튼은 복구용이지 평상시용이 아니다.

## 검증 완료 (하드웨어 없이)

```bash
python3 tools/test_measure_sim.py      # ★57체크 ALL PASS (약 5분 소요)
python3 tools/devserver.py --port 8123 # 스텁 서버 → 대시보드·정비페이지·API 계약 확인
python3 tools/display_sim.py           # 화면 UI 렌더링 → www/display_sim.html
```

- 시나리오 A~D(정상/RF 순단/에러 래치/조치 콘솔) + **E(BT 대상 전환·신원 검증)**
  — 시나리오 E 하네스는 HC-05 자체를 모델링해(KEY+전원으로 AT/데이터 모드, 주소→포트 맵)
  "주소가 뒤바뀐 상황"을 실제로 재현한다
- **도저 계산 교차 검증**: 원본 커밋 `1dd5020` 이 기록한 실측 출력
  (창 8/10~8/16 / 19점 / 수준 7.746 / 추세 -0.0430 per day)을 이식본이 완전히 동일하게 재현
- 대시보드 날짜 헬퍼 node 검증 12건, `_trim_dat`·에러 래치 12건, 보관 정책 8건 전부 통과
- 브라우저 확인: 경도·수조 pH 그래프 모두 활성 버튼(7일)과 일치하는 19점(8/12~8/18),
  "전체(14일)" 라벨, 도저 14행, 콘솔 오류 0

## 원본(upstream) 반영 상태 — 2026-08-18까지 완료

| 원본 커밋 | 내용 | 반영 |
|---|---|---|
| `bc3e763` + `1dd5020` | **dkh.dat 날짜 컬럼** 도입, 회차 근사 전면 폐기 | `dkh_dat.py` 신설, datalog·doser·대시보드·보관정책 전면 전환 |
| `a2282b6` | 수조 pH 초기 범위를 활성 버튼과 일치 | 반영 |
| `016d634` | 카드 제목에서 "· 최근 14일" 제거 | 반영 |
| `f02b820` | "전체" 버튼에 누적 일수 표기 | 반영(경도 그래프에도) |
| `4a8fafc` | 경도 그래프 초기 렌더 깜빡임 제거 | 반영 |
| `d885745` | 측정 소스에서 reefCore 자동 발행 제거 | 이미 반영됨(결정 #4) |

원본의 데이터 동기화 커밋(`chore: dKH 최신값 자동 갱신`, `data: 측정 동기화`)은 코드 변경이
아니므로 대상 아님. **다음에 이어갈 때는 원본에서 `2026-08-18` 이후 커밋만 확인하면 된다.**

## 남은 작업 (TODO)

1. **★BIND 주소 입력** — `config.BIND_ADDR_MEAS` / `BIND_ADDR_DOSER` 가 지금 비어 있다.
   실제 HC-06 주소(`AT+INQ` 로 검색, 콤마 3구간 형식)를 넣어야 전환이 동작한다.
   비어 있으면 의도적으로 거부된다(빈 주소로 아무 데나 붙는 것 방지).
2. **배선** — GPIO32 = HC-05 **VCC 하이사이드 스위치**, GPIO33 = **KEY(PIO11)**, GPIO16 = SD CS.
   부품·회로도는 README "HC-05 전원 스위치 회로" 절. 요지:
   - 권장 = P-MOSFET(AO3401A) + N-FET(2N7002) 2석, 5V 급전, GPIO HIGH=ON
     (`BT_POWER_ACTIVE_HIGH=True`). 3.3V 급전이면 P-MOSFET 1석 + LOW=ON 도 가능
   - ★로우사이드(GND 끊기) 금지 — TX 핀 보호 다이오드로 back-power 돼 모드 전환이 실패한다
   - ★GPIO32 게이트에 100k 풀다운, GPIO33(KEY)에 10k 풀다운 필수(부팅 중 GPIO 부동 대비)
   - ★ZS-040 계열 보드는 헤더의 `EN` 이 레귤레이터 enable 이라 쓸 수 없다.
     PIO11 은 온보드 버튼 패드에 있으므로 거기서 따야 한다
3. **SD 드라이버** — micropython-lib 의 `sdcard.py` 를 함께 업로드해야 SD 가 활성화된다.
   없으면 조용히 비활성(측정은 그대로 동작).
4. **실장 검증** — 전환 소요 실측(현재 타이밍 상수는 여유 있게 잡은 추정치),
   `RECONNECT_BACKOFF` 튜닝(SD 의 RF 원장이 실측 분포를 준다), 터치 캘리브레이션
   (`TOUCH_CAL` 은 네 귀퉁이 raw 값), 장시간 힙 모니터링
5. **하드웨어 확정 후** — `config.py` 디스플레이 블록 + `display_driver.py` 교체
   (`display.py` 는 수정 불요). 디스플레이 핀은 현재 **잠정값**
6. (선택) 대시보드에 calref 버튼 / SD 아카이브·RF 원장 다운로드 링크

## 배포 (실기기)

```bash
mpremote connect COM3 fs cp src/*.py :
mpremote connect COM3 fs cp src/kfont.bin :          # 한글 폰트 — 빠뜨리면 ASCII 폴백
mpremote connect COM3 fs cp sdcard.py :              # SD 쓸 때만(micropython-lib)
mpremote connect COM3 fs mkdir :/www ; mpremote connect COM3 fs cp www/index.html www/ops.html :/www/
gzip -k www/vendor/chart.umd.min.js && mpremote connect COM3 fs cp www/vendor/chart.umd.min.js.gz :/www/vendor/
mpremote connect COM3 fs mkdir :/data ; mpremote connect COM3 fs cp data/* :/data/
```

`data/` 픽스처는 원본 실데이터의 최근 14일치(신형식)라 그대로 올리면 도저 계산 이력이 이어진다.

WiFi 는 `config.py` 에 적거나, 미설정 시 AP `reefwiz-setup`(비번 `reefwiz1234`) →
`http://192.168.4.1/ops.html` 에서 설정. 정상 접속 후 `http://reefwiz.local`.

## 주의사항

- **`data/wifi.json` 은 .gitignore 영구 제외** — 실제 WiFi 자격증명, 장치에만 존재
- **dkh.dat 를 읽을 때 위치 인덱싱 금지** — 반드시 `dkh_dat.parse_parts()` 경유.
  `parts[4]` 같은 코드는 날짜 컬럼 때문에 한 칸씩 밀려 tank_kh 대신 ref_kh 를 반환한다
- 한글 문구를 코드에 새로 추가하면 폰트 재생성 필요:
  `python3 tools/pack_kfont.py charset` → `powershell tools/gen_kfont.ps1` → `pack_kfont.py pack`
- 이 개발 환경의 파이썬은 **cygwin `/e/cygwin64/bin/python3` (3.6.4)** 이다.
  Git Bash 의 `python3` 는 Microsoft Store 스텁이라 동작하지 않는다 — PATH 를 앞에 붙여 쓸 것
- 에이전트는 `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never` 라 push 인증 프롬프트를 띄울 수
  없다. 자격증명이 캐시됐으므로 이후 푸시는 문제 없음
