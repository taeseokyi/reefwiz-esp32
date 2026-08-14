# reefwiz-esp32

reefwiz 자동 KH 측정·도저 조정 시스템의 ESP32(MicroPython) 이식.
Windows PC + 작업 스케줄러 + WSL 동기화 + GitHub Pages 를 ESP32 한 대로 대체한다.

원본: https://github.com/taeseokyi/reefwiz (bin/measure_kh_once.py V4, bin/doser_adjust.py,
bin/dkh_server.py, bin/parse_plateau_log.py, docs/index.html)

## 확정 설계

- 장비 측 **HC-06 은 그대로 유지** (측정·도저 펌웨어 배선 무변경)
- ESP32 에 **HC-05 2개(마스터 모드)** 를 UART 유선 연결 → 각 HC-06 과 무선 페어링
  (아래 "왜 HC-05 2개인가" 참조)
- 대시보드(docs/index.html)는 ESP32 **로컬 웹서버**로 발행 — **같은 공유기(LAN) 안에서만** 접속
- **MQTT(reefCore) 발행 제거** (사용자 지시 2026-08-13)
- 설정 저장(도징량·목표 dKH·pH 보정)은 GitHub API 커밋 대신 **로컬 POST** (토큰 불요)
- **WiFi 는 기기에서 설정** — 저장된 설정 우선, 실패 시 AP 폴백(`reefwiz-setup`)
- **디스플레이·터치**로 현장 확인·조치. 보드/화면은 교체 예정이라 드라이버만 분리

## 요구사항 → 구현 대응

| 요구 | 모듈 |
|---|---|
| 측정 도구 | `measure.py` (V4 평탄 추종) + `link.py` (RF 순단 대응) |
| 측정 결과 웹 발행 | `datalog.py` (dkh.dat + 대시보드 JSON 직접 생성) |
| 도징 점검·설정 변경(수동/자동) | `doser.py` (권고·수동 오버라이드·안전 레일) |
| 각종 로그 유지 | `datalog.log` (measure_kh.log, 상한 순환) + plateau JSONL + doser 이력 |
| 발행 페이지 서빙 | `webserver.py` → `/www/index.html`, `/www/ops.html` |
| dKH 서버 응답 API | `webserver.py` → `/api/dkh` (원본 dkh_server 와 동일 응답) |
| **측정 중단 시 조치** | `ops.py` + `/www/ops.html` (정비 페이지) + 화면 터치 |
| **WiFi 설정** | `wifinet.py` + 정비 페이지 WiFi 카드 (AP 폴백) |
| **디스플레이·터치 관리** | `display.py` (해상도 자동 적응) + `display_driver.py` (교체 대상) |

## 조치(복구) 도구 — 측정이 끊겼을 때

측정은 무인 반복이라 한 번 어긋나면 사람이 개입해야 한다. 원본 운영에서 손이 가던 일들을
**웹 정비 페이지(`/ops.html`)와 장비 화면**에서 바로 할 수 있게 도구화했다.

| 조치 | 왜 필요한가 | 경로 |
|---|---|---|
| **에러 래치 해제** | 원본은 dkh.dat 마지막 에러 줄을 *수동으로 지우기 전까지* 매 회차 측정을 건너뛴다(프로브 보호). 그 편집을 버튼 하나로 | 웹 / 화면 `LATCH` |
| **측정 중단** | 매달린 회차를 끊는다. 비상정리(KCl 소크)까지 수행하고, 장비 이상이 아니므로 **에러 래치를 걸지 않는다** | 웹 / 화면 `ABORT` |
| **비상정리 실행** | 정리가 실패하면 프로브가 KCl 없이 방치된다(원본 `**경고`) | 웹 / 화면 `CLEAN` |
| **KCl 강제 공급** | 위치 불명으로 자동 정리가 동결됐고, 챔버가 빈 것을 눈으로 확인했을 때 | 웹 (확인 2단계) |
| **액체 위치 수동 지정** | 위치 불명(UNKNOWN)이면 자동 정리가 동결된다 — 실물 확인 후 알려주고 재개 | 웹 |
| **링크 점검 / HC-05 리셋** | 무선 구간 사망 판별과 라디오 하드 재기동 (Windows 에서 불가능했던 조치) | 웹 / 화면 `LINK` |
| **명령 콘솔** | 에러 후 수동 정리의 핵심 — 링크 회복(ensure_link 자동) 후 직접 명령으로 정리. 모터 명령(mXf/b:N)은 '[모터N] 완료'까지 대기, 일반 명령은 지정 시간 응답 수집. 복구 순서 빠른 버튼(status/airoff/ton/배출/KCl) + 응답 이력 표시. 전량 로깅 | 웹 |
| **즉시 측정 / 참조 교정** | 실패 회차 재시도, calref(ref dKH 역산) | 웹 / 화면 `MEASURE` |
| **로그 확인** | 진행 상황·`**경고` 확인 | 웹 / 화면 `LOG` |

**UART 안전**: 장비를 만지는 작업은 전부 `state.put_job()` 으로 큐잉되고 **메인 루프에서만**
실행된다. 웹 스레드가 측정 중 UART 에 끼어들면 응답이 뒤섞여 오측정이 되기 때문이다.
결과는 `/api/ops/result` 폴링으로 표시된다.

## 왜 HC-05 2개인가 (ESP32 내장 블루투스로 안 되는 이유)

**1. ESP32 하드웨어는 Bluetooth Classic(SPP)을 지원한다 — 그런데 MicroPython 이 안 열어준다.**
ESP32-WROOM-32 칩에는 BR/EDR(Classic) + BLE 가 다 있고, Arduino/ESP-IDF 의 `BluetoothSerial` 을
쓰면 SPP 마스터로 HC-06 에 접속할 수 있다. 하지만 **MicroPython ESP32 포트는 BLE 만 노출**하고
Classic/SPP API 가 없다(펌웨어 제약, 하드웨어 문제가 아니다). 파이썬으로 가려면 외부 라디오가
필요하다. ※S3/C3 계열은 하드웨어 자체가 Classic 미지원이므로 보드를 바꿔도 이 길은 안 열린다.

**2. 설령 C++ 로 가도 ESP32 내장 SPP 는 동시 1연결뿐이다.** 두 HC-06 을 동시에 붙들 수 없어
연결을 끊고 옮겨 붙어야 한다(회당 수 초, 실패 가능). 측정 링크는 몇 시간 연속 유지가 필요하므로
이 방식은 맞지 않는다.

**3. HC-05 도 1연결이라 장비 2대 = 모듈 2개.** HC-05 는 SPP 점대점이라 한 모듈이 한 HC-06 만
붙든다. 한 개로 `AT+BIND` 를 바꿔가며 번갈아 쓰는 것도 이론상 가능하지만(KEY 핀을 GPIO 로 제어),
AT 모드 진입·재바인드·재접속에 수 초가 걸리고 **바인드가 잘못되면 도저 명령이 측정기로 가는
사고**가 난다. 원본의 "거짓 성공은 있으면 안 된다" 원칙과 정면으로 어긋나므로, 몇천 원을 아끼려
위험을 만들지 않고 **장비 1대당 전용 모듈 1개**로 간다.

**모듈을 줄이고 싶다면**: ESP32 를 장비 옆에 두고 그 장비만 **UART 유선 직결**하면 그 링크의
라디오가 사라진다(RF 순단 자체가 없어져 가장 안정적). 예 — 도저를 유선으로, 측정기만 HC-05 1개.
`config.py` 의 핀 설정만 바꾸면 되고 코드는 동일하게 동작한다.

## 배선

| ESP32 핀 | 연결 | 용도 |
|---|---|---|
| GPIO25 (TX1) / GPIO26 (RX1) | HC-05 #1 RXD/TXD | 측정 장비 링크 |
| GPIO32 | HC-05 #1 EN(또는 VCC 스위치) | 링크 사망 시 하드 리셋 (선택이지만 권장) |
| GPIO17 (TX2) / GPIO16 (RX2) | HC-05 #2 RXD/TXD | 도저 링크 |
| GPIO33 | HC-05 #2 EN | |
| GPIO18/23/19 | TFT+터치 SCK/MOSI/MISO | SPI 공유 |
| GPIO5 / GPIO2 / GPIO4 / GPIO15 | TFT CS/DC/RST/BL | |
| GPIO27 / GPIO34 | 터치 CS / IRQ | 34 는 입력 전용 핀 |
| 3V3/5V, GND | HC-05 VCC/GND | 모듈 사양 확인(대부분 3.6~6V, 로직 3.3V) |

핀 배정은 전부 `src/config.py`. **디스플레이 핀은 잠정값 — 실제 배선 확인 후 수정.**

## HC-05 1회 설정 (모듈당)

KEY(EN) 핀을 HIGH 로 두고 전원 인가 → AT 모드(38400 baud) 진입 후:

```
AT+ROLE=1          ← 마스터
AT+CMODE=0         ← 지정 주소에만 접속
AT+BIND=xxxx,xx,xxxxxx   ← 상대 HC-06 주소 (AT+INQ 로 검색 가능)
AT+UART=9600,0,0   ← 장비 측과 동일 보레이트
AT+RESET
```

이후 전원 인가만 하면 자동 페어링·자동 재접속.

## 설치

1. ESP32 에 MicroPython 펌웨어 플래싱 (micropython.org, ESP32 generic)
2. 코드 + 한글 폰트 업로드:
   ```bash
   mpremote connect COM3 fs cp src/*.py :
   mpremote connect COM3 fs cp src/kfont.bin :
   ```
3. 정적 자산 업로드 — 저장소 docs/ 에서 가져와 `/www` 에:
   ```bash
   mpremote connect COM3 fs mkdir :/www
   mpremote connect COM3 fs mkdir :/www/vendor
   mpremote connect COM3 fs mkdir :/www/icons
   mpremote connect COM3 fs cp index.html www/ops.html :/www/
   # chart.js 는 gzip 으로(206KB→~70KB, 서버가 Content-Encoding: gzip 서빙):
   gzip -k chart.umd.min.js && mpremote fs cp chart.umd.min.js.gz :/www/vendor/
   mpremote connect COM3 fs cp icons/icon-192.png :/www/icons/
   ```
4. **데이터 이관** — 저장소 실물이 이미 `data/` 에 준비돼 있다(2026-08-14 기준: dkh.dat 268행,
   plateau.jsonl 42런, doser_history 등). 통째로 올리면 이력이 이어진다:
   ```bash
   mpremote connect COM3 fs mkdir :/data
   mpremote connect COM3 fs cp data/* :/data/
   ```
5. 리셋 → **WiFi 설정**:
   - `config.py` 에 SSID/PW 를 미리 적었으면 그대로 접속
   - 아니면 폰으로 AP **`reefwiz-setup`** (비번 `reefwiz1234`) 에 붙어
     `http://192.168.4.1/ops.html` → WiFi 카드에서 스캔·선택·저장
6. `http://reefwiz.local` (또는 IP) → 대시보드 / `/ops.html` → 정비 페이지

## 구성

```
src/
  config.py          설정·상수 (원본 튜닝값 유지 — 근거는 원본 주석 참조)
  main.py            WiFi/NTP/스케줄 루프 + 조치 작업 실행 (작업 스케줄러 + dkh_server 폴러 대체)
  wifinet.py         WiFi 설정·연결·스캔·AP 폴백
  link.py            측정 링크 계층 — keepalive/재연결/재송신 (RF 순단 대응, HC-05 하드리셋)
  measure.py         KH 측정 V4 (평탄 추종, 전제조건 검증, 비상정리, 에러 래치, 호스트 구제)
  doser.py           도저 조정 (Theil-Sen, 스텝캡/데드밴드/정지유지, 에코검증→refresh→롤백)
  ops.py             조치(복구) 도구 — 래치 해제·중단·정리·위치 지정·명령 콘솔·링크 리셋
  datalog.py         dkh.dat + 대시보드 JSON + plateau JSONL + CO₂ 편향 판정
  webserver.py       로컬 웹서버 (정적 + /api/*)
  display.py         화면 UI·터치 조치 (해상도 자동 적응 — 하드웨어 무관)
  display_driver.py  ★참조 구현(교체 대상) — ILI9341 + XPT2046
  rwtime.py          KST 시각 헬퍼
  state.py           스레드 공유 상태·작업 큐
www/
  ops.html           정비·조치 페이지 (외부 의존 없음)
```

**스레드 배치**: 측정은 수 시간 블로킹하므로 웹·화면을 별도 스레드에 둔다 — 측정 중에도
상태 조회와 중단이 되고, UART 작업은 큐에서 순차 실행된다.

## API

| 경로 | 메서드 | 내용 |
|---|---|---|
| `/api/dkh` | GET | `{"dkh": 8.02}` — 원본 dkh_server.py 와 동일 |
| `/api/override` | GET/POST | 도징량 수동 설정 `{"ml_day": 0 또는 1.5~18}` — POST 시 id 부여·즉시 적용 |
| `/api/override/state` | GET | 마지막 적용 id/시각 |
| `/api/config` | GET/POST | `{"target_dkh": 6.0~9.0}` |
| `/api/ph_cal` | GET/POST | 한나 pH 보정(표시 전용) |
| `/api/ops/status` | GET | 상태 종합(측정중·래치·액체위치·마지막런·도저·WiFi·힙) |
| `/api/ops/log?n=` | GET | measure_kh.log 마지막 n줄 |
| `/api/ops/result` | GET | 마지막 조치 작업 결과 |
| `/api/ops/abort` \| `/clear_latch` \| `/liquid` | POST | 즉시 실행 조치 |
| `/api/ops/job` | POST | `{"kind": measure\|calref\|cleanup\|cmd\|link\|hc05_reset\|doser_query\|doser_apply, ...}` |
| `/api/wifi` | GET/POST | 상태 / `{"ssid","pass"}` 저장·재접속 |
| `/api/wifi/scan` | GET | 주변 AP 목록 |
| `dkh_series.json` 등 | GET | 대시보드 데이터(상대경로 그대로 동작) |

## 개발용 스텁 서버 (하드웨어 없이 대시보드 검증)

`tools/devserver.py` 가 `webserver.py` 와 **같은 경로·같은 응답 형태**를 CPython 으로 흉내낸다
(plateau JSONL → 배열 스트리밍까지 동일). 장비 조작은 그럴듯한 결과만 반환한다.

```bash
python3 tools/devserver.py --seed --port 8123   # 저장소 docs/ 실물 JSON 을 data/ 로 받고 시작
```

`--seed` 가 SSL 인증서 문제로 실패하면 `docs/*.json` 을 직접 `data/` 에 넣고 `--seed` 없이 실행.
`data/` 는 개발 픽스처 디렉토리이며 ESP32 에 올라가지 않는다(장치에서는 `/data`).

**검증한 것**(2026-08-14): 대시보드 읽기 경로 전부 200, 저장 3종(override·config·ph_cal) 왕복 +
범위 검증(99mL/일 거부), 정비 API 전부, plateau JSONL→배열 42런 파싱, 두 페이지 inline JS
`node --check` 통과.

## 디스플레이 미리보기 (하드웨어 없이 화면 UI 확인)

`tools/display_sim.py` — `display.UI` 의 그리기 호출을 기록하는 SVG 프레임버퍼를 드라이버
자리에 꽂아, 실데이터 기반 `ops.snapshot()` 으로 화면을 렌더링해 `www/display_sim.html` 로
저장한다(**display.py 무수정**). devserver 실행 중이면 http://localhost:8123/display_sim.html

렌더링: 320×240 메인 / 확인 대기(CLEAN 1탭 후 CONFIRM?) / 로그 화면, 그리고 800×480 메인
(해상도 자동 적응 — 배율 2 — 이 실제로 동작함을 보여준다).

## 시뮬레이터 검증 (하드웨어 없이 측정 시퀀스 전체)

`tools/firmware_sim.py`(원본 bin/ 그대로 — 펌웨어 프로토콜을 TCP 로 흉내냄)에
`tools/test_measure_sim.py` 로 **이식본을 무수정으로** 붙여 검증한다. MicroPython
`machine.UART` 를 TCP 클라이언트로 구현한 심(shim)이 무선 링크의 성질(write 는 항상 성공,
링크 사망 시 수신만 끊김, 백그라운드 재접속)을 재현하고, 타이밍 상수만 초 단위로 압축한다
(판정 로직은 원본 값 그대로의 코드가 돈다).

```bash
python3 tools/test_measure_sim.py
```

**결과(2026-08-14): 42체크 ALL PASS**

| 시나리오 | 확인한 것 |
|---|---|
| A. 정상 calkh | 상수 pH → 정확히 8회째 평탄 latch, dKH=ref×10^ΔpH(8.142) 일치, dkh.dat/series/plateau(첫점 n=0 포함) 기록, 이송 8단계 순서, 정리 후 챔버=KCL, CO₂ 미의심 |
| B. RF 순단 | tank 측정 중 소켓 강제 드롭 → 자동 재연결(connection_count≥2) 후 완주, 값 동일, 래치 없음 |
| C. 에러 래치 | pH 누락→FAIL_MAX→0.0 기록→비상정리(m2b→m1b→m3f)→다음 회차 측정 생략(명령 0건)→`ops.clear_error_latch()`→측정 재개 |
| D. 조치 콘솔 | status 응답 수집, 모터 명령 '[모터N] 완료' 대기, 완료 누락 시 "이송 성공 불명" 실패 보고 |

## 힙 안전 설계 (PSRAM 없는 보드 기준 가용 ~100KB)

측정 자체는 몇 시간 돌고 데이터는 계속 쌓이므로, "파일을 통째로 파싱"하는 지점을 없앴다.

- **plateau 이력**: 저장소 실물이 51KB — 파이썬 객체로 올리면 힙이 터진다.
  → `plateau.jsonl`(런 1개 = 1줄)로 저장하고, 대시보드가 받는 `dkh_plateau_history.json`
  배열은 웹서버가 **줄 단위로 스트리밍해 조립**한다(메모리에 올리는 지점 없음).
  상태 표시는 마지막 1줄만 파싱, 트림도 한 줄씩 옮긴다.
- **런 중 판독 누적**: `MEAS_MAX`(180)×2 phase 를 다 들면 위험 → `PLATEAU_KEEP_MAX`(100)
  초과 시 앞쪽 절반을 2:1 로 솎는다(최신 구간·첫 판독 보존 → 평탄·CO₂ 판정 영향 없음).
- **정적 자산**: chart.js 등은 파일에서 청크 스트리밍(gzip 그대로 전달).

## 하드웨어 메모

**임시 참조**(reefCore Checker R2 구성): WEMOS ESP32 18650 (ESP32-WROOM-32, 4MB, PSRAM 없음)
+ 2.4" ILI9341 240×320 + XPT2046 저항막 터치. `display_driver.py` 가 이 구성의 참조 구현이다.

**교체 예정**: 더 고성능 보드 + 더 큰 화면. 교체 시 바꿀 것은 `config.py` 디스플레이 블록과
`display_driver.py` 뿐 — `display.py` 는 계약만 보고 동작하며 **폰트 배율 = 가로폭/320** 으로
자동 확대되고 버튼·로그 줄 수도 화면 크기에 맞춰 재배치된다. PSRAM 보드라면 framebuf 기반
드라이버를 써도 되고(그때 `show()` 가 실제 전송), 코드 수정은 필요 없다.

**화면은 전부 한국어**(버튼 측정/중단/정리/래치해제/링크/로그, 메시지·로그 포함 — 사용자
지시 2026-08-14). 내장 8×8 폰트에 한글이 없어 자체 비트맵 폰트 `src/kfont.bin`(21KB,
598자 — 소스에 등장하는 글자만 수록)을 로드해 `fill_rect` 수평 런으로 직접 그린다.
드라이버는 텍스트 기능이 없어도 되고, kfont.bin 이 없으면 ASCII 폴백으로 계속 동작한다.
**긴 줄은 픽셀 폭 기준 자동 줄바꿈 — 화면 크기와 무관하게 가로로 잘리지 않는다.**

폰트 재생성(새 한글 문구를 코드에 추가했을 때):
```bash
python3 tools/pack_kfont.py charset     # ① 소스 전체 스캔 → tools/charset.txt
powershell tools/gen_kfont.ps1          # ② Windows 폰트 래스터라이즈(맑은 고딕/Consolas)
python3 tools/pack_kfont.py pack        # ③ src/kfont.bin 패킹
```
업로드 시 `mpremote fs cp src/kfont.bin :` 로 코드와 함께 올린다.

## index.html 이식 내용 (완료)

`www/index.html` = 원본 `docs/index.html` + 아래 변경. 그래프·통계·도저 카드 로직은 그대로다.

| 원본 | ESP32 |
|---|---|
| `ghCommitJson()` — 토큰 localStorage 보관 → sha 조회 → base64 → GitHub PUT | `saveJson()` — 로컬 `POST /api/*` (토큰·sha·인증 실패 처리 전부 삭제) |
| GitHub 토큰 입력칸 2개(`doserPat`, `phCalPat`) | 제거 (서버가 같은 기기 = 인증 불요) |
| `api.github.com/...?ref=master` + raw Accept 헤더로 읽기 3곳 | `/api/ph_cal`, `/api/override`, `/api/config` GET |
| "몇 분 내(최대 ~5분) 적용됩니다" | "곧 적용됩니다(측정 중이면 종료 후)" — 폴러 없이 즉시 |
| `navigator.serviceWorker.register("sw.js")` | 제거 (LAN HTTP 에선 브라우저가 등록 거부) |
| 문서 링크 `user-manual.html` 등 상대경로 | GitHub Pages 절대 URL (`.md` 렌더링은 ESP32 에 없음) |
| — | **`ops.html` 정비·조치 링크 추가**(문서 링크 맨 앞) |

## 남은 작업 (TODO)

1. **실장 검증** — HC-05 재페어링 시간에 맞춘 `RECONNECT_BACKOFF` 튜닝, 터치 캘리브레이션
   (`TOUCH_CAL` 은 네 귀퉁이 raw 값으로 조정), 장시간 힙 모니터링.
2. **하드웨어 확정 후** — 디스플레이 핀·드라이버 교체, 화면 라벨 한글화(선택).
3. (선택) 대시보드에 calref(참조 교정) 버튼 — 현재는 정비 페이지 명령/`ops` job 으로 실행.

## 원본 대비 제거된 것

- pyserial/COM 포트, HC-06 직결 (→ HC-05 브리지 UART)
- Windows 작업 스케줄러, pythonw 로깅 리다이렉트, WSL sync_dkh_dat.py 호출
- GitHub API 커밋(설정 저장) — **접미 정렬(suffix alignment) 핵 포함**: 로컬 dkh.dat 에
  날짜가 없어 원격 series 와 값 시퀀스로 맞추던 코드가, 날짜 있는 로컬 기록으로 불필요해졌다
- MQTT/reefCore 발행 (paho-mqtt, TLS 예외 처리 전부)
- dkh_server.py 의 subprocess 폴러 (→ 이벤트 기반 즉시 적용)
- 측정 로그 재파싱(parse_plateau_log) — 측정 직후 판독 시계열로 바로 판정·기록
