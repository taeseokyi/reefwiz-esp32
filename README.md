# reefwiz-esp32

reefwiz 자동 KH 측정·도저 조정 시스템의 ESP32(MicroPython) 이식.
Windows PC + 작업 스케줄러 + WSL 동기화 + GitHub Pages 를 ESP32 한 대로 대체한다.

원본: https://github.com/taeseokyi/reefwiz (bin/measure_kh_once.py V4, bin/doser_adjust.py,
bin/dkh_server.py, bin/parse_plateau_log.py, docs/index.html)

## 확정 설계

- 장비 측 **HC-06 은 그대로 유지** (측정·도저 펌웨어 배선 무변경)
- ESP32 에 **HC-05 1개(마스터 모드)** 를 UART 유선 연결 → 두 HC-06 에 `AT+BIND` 를
  바꿔가며 번갈아 접속 (아래 "왜 HC-05 1개인가" 참조)
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
| **BT 대상 전환** | HC-05 1개를 측정기/도저에 다시 바인드하고 **신원을 검증**. 불일치면 동결(오장비 명령 방지), 해제는 운영자 확인 후에만 | 웹 / 화면 `BT 전환` |
| **명령 콘솔** | 에러 후 수동 정리의 핵심 — 링크 회복(ensure_link 자동) 후 직접 명령으로 정리. 모터 명령(mXf/b:N)은 '[모터N] 완료'까지 대기, 일반 명령은 지정 시간 응답 수집. 복구 순서 빠른 버튼(status/airoff/ton/배출/KCl) + 응답 이력 표시. 전량 로깅 | 웹 |
| **즉시 측정 / 참조 교정** | 실패 회차 재시도, calref(ref dKH 역산) | 웹 / 화면 `MEASURE` |
| **로그 확인** | 진행 상황·`**경고` 확인 | 웹 / 화면 `LOG` |

**UART 안전**: 장비를 만지는 작업은 전부 `state.put_job()` 으로 큐잉되고 **메인 루프에서만**
실행된다. 웹 스레드가 측정 중 UART 에 끼어들면 응답이 뒤섞여 오측정이 되기 때문이다.
결과는 `/api/ops/result` 폴링으로 표시된다.

## 왜 HC-05 1개인가 (ESP32 내장 블루투스로 안 되는 이유)

**★2026-08-18 확정: HC-05 를 1개만 쓰고 두 장비에 번갈아 접속한다.** 측정기와 도저는
동시에 붙들 필요가 없다(main 루프가 순차 실행). 전환에 8~12초 걸리지만 속도는 요구사항이
아니다. 종전의 "장비당 전용 모듈 1개" 안을 대체한다.

**1. ESP32 하드웨어는 Bluetooth Classic(SPP)을 지원한다 — 그런데 MicroPython 이 안 열어준다.**
ESP32-WROOM-32 칩에는 BR/EDR(Classic) + BLE 가 다 있고, Arduino/ESP-IDF 의 `BluetoothSerial` 을
쓰면 SPP 마스터로 HC-06 에 접속할 수 있다. 하지만 **MicroPython ESP32 포트는 BLE 만 노출**하고
Classic/SPP API 가 없다(비공식 커스텀 펌웨어만 존재). 파이썬으로 가려면 외부 라디오가 필요하다.
※S3/C3/C6 계열은 하드웨어 자체가 Classic 미지원이라 **보드를 바꾸면 이 길은 영영 닫힌다** —
내장 BT 를 쓰기로 하면 구형 ESP32 에 영구히 묶이므로 "더 고성능 보드로 교체" 계획과 충돌한다.

**2. 그래서 외부 HC-05 를 쓰되, 개수는 1개다.** HC-05 는 SPP 점대점이라 한 번에 한 슬레이브만
붙든다. `AT+BIND` 를 바꿔가며 번갈아 쓰는 방식은 **바인드가 어긋나면 도저 명령이 측정기로 가는
사고**가 가능하다는 것이 유일한 위험이었는데, 이를 코드로 막았다(아래).

### 오장비 명령을 막는 3중 레일

원본의 "거짓 성공은 있으면 안 된다"(이송 전 airoff·ton 검증) 원칙을 링크 계층에 적용한 것이다.

1. **신원 검증** — 전환 후 첫 구동 명령 전에 부작용 없는 조회를 보내 응답 서명을 확인한다
   (측정기 `status` → `============` / 도저 `ls` → `왼쪽 동작·왼쪽 휴지`). 서명이 확인되기
   전에는 어떤 명령도 나가지 않는다. 다른 장비의 서명이 오면 **즉시 동결**하고, 동결 상태에서는
   `send()` 가 `LinkFrozen` 을 던진다. 자동 해제는 없다 — 배선·BIND 주소 확인 없이 풀면 같은
   오접속을 반복하므로 정비페이지의 명시적 '동결 해제'로만 푼다.
2. **교차 프로브** — 대상의 조회에 침묵이 오면 다른 장비의 조회 형식으로 한 번 더 묻는다.
   장비마다 줄 종단이 달라(측정기 CRLF / 도저 LF only) 엉뚱한 장비에 붙으면 아예 응답이 없는
   경우가 많은데, 그러면 '상대 전원 꺼짐'과 'BIND 주소 뒤바뀜'이 구분되지 않는다. 둘 다
   안전하기는 같지만 운영자가 할 일이 완전히 다르다.
3. **모터 구동 중 전환 금지** — 전환은 곧 라디오 전원 차단이다. 모터가 도는 중에 끊으면 정지
   명령을 보낼 수단이 사라져 **시약이 계속 주입된다**. `Link.motor_running` 이 세팅된 동안은
   전환·리셋이 모두 거부된다.

`tools/test_measure_sim.py` 의 시나리오 E 가 이 셋을 전부 검증한다(오접속 동결, 동결 중 송신
차단, 모터 중 전환 거부, BIND 주소 미설정 시 거부).

### 전환 시퀀스

전원을 끊었다 넣는 것이 모드 전환의 유일한 수단이므로 제어선이 2개 필요하다.

```
전원 OFF(0.4s) → KEY HIGH → 전원 ON → AT 모드 부팅(38400)
  → AT / AT+ROLE=1 / AT+CMODE=0 / AT+BIND=<주소> / AT+UART=9600,0,0
  → KEY LOW → 전원 재투입 → 데이터 모드(9600) → 자동 페어링
  → ★신원 검증(조회 응답 서명) → 통과해야 비로소 명령 송신 허용
```

ROLE/CMODE/UART 를 매번 다시 넣는 이유: 전원 이상으로 설정이 날아간 모듈을 조용히 잘못된
역할로 쓰는 것보다, 전환할 때마다 확정하는 편이 안전하다.

**★`config.BIND_ADDR_MEAS` / `BIND_ADDR_DOSER` 는 실장 전 반드시 채울 것.** 비어 있으면
전환이 즉시 실패한다(빈 주소로 아무 데나 붙는 것을 막기 위해 의도적으로 그렇게 했다).
주소는 `AT+INQ` 로 검색하며 형식은 콜론이 아니라 콤마다: `98d3,31,fb1234`.

## 배선

| ESP32 핀 | 연결 | 용도 |
|---|---|---|
| GPIO25 (TX1) / GPIO26 (RX1) | HC-05 RXD/TXD | 장비 링크(측정기·도저 공용) |
| GPIO32 | **HC-05 VCC 스위치**(MOSFET 게이트) | ★전원 재투입 = 모드 전환·하드 리셋의 유일한 수단 |
| GPIO33 | **HC-05 KEY/EN** | HIGH 로 두고 전원 인가 → AT 명령 모드(38400) |
| GPIO16 | SD CS | 도저 UART 폐지로 비게 된 핀(13/17/21/22 도 가능) |
| GPIO18/23/19 | TFT+터치+SD SCK/MOSI/MISO | SPI 공유(CS 로 분리) |
| GPIO5 / GPIO2 / GPIO4 / GPIO15 | TFT CS/DC/RST/BL | |
| GPIO27 / GPIO34 | 터치 CS / IRQ | 34 는 입력 전용 핀 |
| 3V3/5V, GND | HC-05·SD VCC/GND | 모듈 사양 확인(대부분 3.6~6V, 로직 3.3V) |

**★GPIO32 는 반드시 VCC 스위치여야 한다(EN 이 아니다).** HC-05 의 EN/KEY 핀은 AT 모드 진입에
써야 하므로, 하드 리셋은 전원을 실제로 끊는 방식뿐이다 — MOSFET 이 필요하다.

**★SD 는 SPI 모드로만 붙인다.** SDMMC(네이티브) 모드는 ESP32 slot1 핀이
CLK=14 / CMD=15 / D0=2 / D1=4 로 고정인데 위 TFT_BL=15 · TFT_DC=2 · TFT_RST=4 와 정면 충돌한다.
SD 는 삽입 순간 돌입 전류가 크므로 3.3V 레귤레이터 여유를 확인할 것 — 여유가 없으면 HC-05 가
같이 흔들려 측정 중 순단으로 나타난다.

핀 배정은 전부 `src/config.py`. **디스플레이 핀은 잠정값 — 실제 배선 확인 후 수정.**

## HC-05 준비

**펌웨어가 매 전환마다 ROLE/CMODE/BIND/UART 를 다시 넣으므로 수동 초기 설정은 필요 없다.**
필요한 것은 상대 HC-06 주소 2개뿐이다. AT 모드(KEY HIGH + 전원 인가, 38400)에서 `AT+INQ` 로
검색해 `config.py` 에 적는다:

```python
BIND_ADDR_MEAS  = "98d3,31,fb1234"   # 측정 장비 HC-06
BIND_ADDR_DOSER = "98d3,31,fb5678"   # 도저 HC-06
```

형식은 콜론이 아니라 **콤마 3구간**이다(`AT+BIND` 규약). 비워 두면 전환이 즉시 실패한다 —
빈 주소로 아무 데나 붙는 것을 막기 위한 의도적 동작이다.

## SD 카드 (선택)

없어도 전부 동작한다. 있으면 플래시의 보관 한계가 사라진다:

| 경로 | 내용 |
|---|---|
| `/sd/reefwiz/log/measure-YYYY-MM.log` | 측정 로그 전문(플래시는 512KB 에서 돌려쓴다) |
| `/sd/reefwiz/archive/dkh.dat` | 14일 창 밖으로 밀려난 측정 행 |
| `/sd/reefwiz/archive/plateau.jsonl` | 창 밖으로 밀려난 평탄 궤적(사후 분석 표본) |
| `/sd/reefwiz/rf.jsonl` | **RF/BT 이벤트 원장** — 전환·재연결 시도별 소요·신원 불일치 |

마지막 항목이 실질적인 목적이다. 지금은 "왜 끊겼는지"가 기록에 남지 않아
`RECONNECT_BACKOFF` 가 추정치인데, 원장이 쌓이면 실측 분포로 튜닝할 수 있다.

**드라이버**: SPI 모드 SD 는 micropython-lib 의 `sdcard.py` 가 표준이고 이 저장소에는 포함하지
않았다. 배포 시 함께 올려야 하며, 없으면 조용히 비활성으로 남는다(측정은 그대로 동작).
미장착·마운트 실패·중간 탈착 — 어떤 경우에도 예외를 밖으로 내보내지 않는다.

## 설치

1. ESP32 에 MicroPython 펌웨어 플래싱 (micropython.org, ESP32 generic)
2. 코드 + 한글 폰트 업로드:
   ```bash
   mpremote connect COM3 fs cp src/*.py :
   mpremote connect COM3 fs cp src/kfont.bin :
   # SD 를 쓸 경우에만 — micropython-lib 의 SPI SD 드라이버
   mpremote connect COM3 fs cp sdcard.py :
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
4. **데이터 이관** — 저장소 실물이 이미 `data/` 에 준비돼 있다(2026-08-18 기준: 신형식
   dkh.dat 40행/14일, plateau.jsonl 40런, doser_history 등). 통째로 올리면 이력이 이어진다:
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
  link.py            장비 링크 계층 — keepalive/재연결/재송신 (RF 순단 대응), HC-05 전원
                     재투입 하드리셋, ★대상 전환 + 신원 검증(오장비 명령 방지)
  dkh_dat.py         dkh.dat 한 줄의 파싱·포매팅 단일 규약 (날짜 컬럼, 원본 2026-08-16 이식)
  storage.py         SD 저장소(선택) — 로그 전문·보관 창 밖 아카이브·RF 이벤트 원장
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

**결과(2026-08-18): 57체크 ALL PASS**

| 시나리오 | 확인한 것 |
|---|---|
| A. 정상 calkh | 상수 pH → 정확히 8회째 평탄 latch, dKH=ref×10^ΔpH(8.142) 일치, dkh.dat/series/plateau(첫점 n=0 포함) 기록, 이송 8단계 순서, 정리 후 챔버=KCL, CO₂ 미의심 |
| B. RF 순단 | tank 측정 중 소켓 강제 드롭 → 자동 재연결(connection_count≥2) 후 완주, 값 동일, 래치 없음 |
| C. 에러 래치 | pH 누락→FAIL_MAX→0.0 기록→비상정리(m2b→m1b→m3f)→다음 회차 측정 생략(명령 0건)→`ops.clear_error_latch()`→측정 재개 |
| D. 조치 콘솔 | status 응답 수집, 모터 명령 '[모터N] 완료' 대기, 완료 누락 시 "이송 성공 불명" 실패 보고 |
| E. BT 대상 전환 | 측정기↔도저 전환·신원 서명 확인, 도저 `ls` 왕복(LF only 규약), **오접속 감지→동결**, 동결 중 송신 차단(`LinkFrozen`)·장비 미도달·다른 대상 전환도 거부, 해제 후 재전환, **모터 구동 중 전환 거부**, BIND 주소 미설정 시 거부 |

시나리오 E 의 하네스는 HC-05 자체를 모델링한다 — KEY 를 올린 채 전원을 넣으면 AT 모드,
내리고 넣으면 데이터 모드로 부팅해 *바인드된 주소*의 장비에 붙는다. 그래서 "주소가 뒤바뀐
상황"을 실제로 재현해 신원 검증이 잡아내는지 시험할 수 있다.

## dkh.dat 형식 — 날짜 컬럼 (원본 2026-08-16 반영)

```
YYYY-MM-DD HH ref_pH tank_pH ref_kh tank_kh temp
2026-08-18 05 7.723 7.663 8.830 7.701 28.7
```

원본이 날짜 컬럼을 도입한 이유를 그대로 따른다. 파일에 시각(HH)만 있으면 "최근 N일"을 하루
3회 측정 가정의 **회차 근사**로 셀 수밖에 없는데, 측정이 빠진 날이 있으면 창이 과거로 늘어나고
추가 측정을 돌린 날이 있으면 창 안쪽이 밀려 잘렸다. 이식본에 반영한 것:

| 항목 | 종전 (회차 근사) | 현재 (날짜) |
|---|---|---|
| 도저 수준·추세 창 | `ROWS=21행 ≈ 7일` | `WINDOW_DAYS=7` — 날짜로 컷 |
| Theil-Sen 시간축 | `ROW_DAYS=8h 균일 가정` | 실제 측정 시각 간격(`build_times`) |
| dkh.dat 보관 | 42행 컷 | 14일 날짜 컷(기준일 = 마지막 기록일) |
| series·plateau 보관 | 42행/42런 컷 | 14일 날짜 컷 + 행수는 힙 백스톱 |
| 대시보드 기간 버튼 | `slice(-N*3)` | `recentDays()` 날짜 컷 |
| 날짜를 모를 때 | 근사로 폴백 | **중단**(`record_abort`) — 근사하지 않는다 |

**★위치 인덱싱 금지**: 종전 코드는 `parts[4]=tank_kh` 처럼 위치로 읽었는데, 날짜가 붙으면
한 칸씩 밀려 **tank_kh 대신 ref_kh 를 반환한다**(원본도 같은 사고를 겪어 파서 경유로 바꿨다).
그래서 dkh.dat 을 읽는 모든 지점은 `dkh_dat.parse_parts()` 를 거친다 — 기기 코드뿐 아니라
`tools/devserver.py` 의 에러 래치 판정도 같이 고쳤다. 신·구 형식 모두 정확히 읽힌다
(구형식 백업본의 에러 래치도 계속 인식된다).

**교차 검증**: 원본 커밋 `1dd5020` 이 기록한 실측 출력(창 2026-08-10~08-16 / 19점 /
수준 7.746 / 추세 -0.0430 per day)을 이식본이 **완전히 동일하게** 재현한다.

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

1. **BIND 주소 입력** — `config.BIND_ADDR_MEAS` / `BIND_ADDR_DOSER` 를 실제 HC-06 주소로
   채운다(`AT+INQ` 로 검색). 비어 있으면 전환이 거부된다.
2. **실장 검증** — 전환 소요 실측(현재 타이밍 상수는 여유 있게 잡은 추정치), HC-05 재페어링
   시간에 맞춘 `RECONNECT_BACKOFF` 튜닝(SD 의 RF 원장이 실측 분포를 준다), 터치 캘리브레이션
   (`TOUCH_CAL` 은 네 귀퉁이 raw 값으로 조정), 장시간 힙 모니터링.
3. **배선** — GPIO32 를 VCC 스위치(MOSFET)로, GPIO33 을 KEY 로. SD 는 SPI 버스에 CS(16) 추가.
4. **하드웨어 확정 후** — 디스플레이 핀·드라이버 교체.
5. (선택) 대시보드에 calref(참조 교정) 버튼 — 현재는 정비 페이지 명령/`ops` job 으로 실행.
6. (선택) SD 의 RF 원장·아카이브를 웹에서 내려받는 링크.

## 원본 대비 제거된 것

- pyserial/COM 포트, HC-06 직결 (→ HC-05 브리지 UART 1개를 번갈아 사용)
- Windows 작업 스케줄러, pythonw 로깅 리다이렉트, WSL sync_dkh_dat.py 호출
- GitHub API 커밋(설정 저장) — **접미 정렬(suffix alignment) 핵 포함**: 로컬 dkh.dat 에
  날짜가 없어 원격 series 와 값 시퀀스로 맞추던 코드가, 날짜 있는 로컬 기록으로 불필요해졌다
- MQTT/reefCore 발행 (paho-mqtt, TLS 예외 처리 전부)
- dkh_server.py 의 subprocess 폴러 (→ 이벤트 기반 즉시 적용)
- 측정 로그 재파싱(parse_plateau_log) — 측정 직후 판독 시계열로 바로 판정·기록
