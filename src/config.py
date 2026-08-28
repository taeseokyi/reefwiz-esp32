# reefwiz-esp32 설정 — 원본: reefwiz/bin/measure_kh_once.py(V4), doser_adjust.py, dkh_server.py
# 상수 근거(사고 이력·튜닝 사유)는 원본 파일 헤더/주석 참조. 값은 원본과 동일하게 유지.

# ── 보드 (★확정 2026-08-18) ──
# VCC-GND Studio **ESP32-S3 N16R8** (Type-C 2포트·44핀, 디바이스마트 VND019 21,000원)
#   = ESP32-S3-WROOM-1 N16R8 : 240MHz 듀얼코어 + 16MB 플래시 + **8MB 옥탈(OPI) PSRAM**
#   Type-C 2개: 하나는 S3 네이티브 USB, 하나는 CH343P USB-시리얼(UART0). 둘 다 REPL 가능.
# ★펌웨어는 MicroPython **ESP32_GENERIC_S3 / SPIRAM_OCT** 변종이어야 한다 —
#   기본(no-SPIRAM/quad) 이미지를 올리면 8MB PSRAM 이 안 잡히거나 부팅 루프에 빠진다.
#   플래시는 `esptool --chip esp32s3 write_flash 0 <fw>.bin` (구형 ESP32 의 0x1000 아님).
# ★사용 금지 핀 — 아래 배정은 전부 이 목록을 피해 잡았다. 구형 ESP32 배선을 그대로
#   옮기면 조용히 안 되거나 부팅을 못 한다:
#     26~32  내장 SPI 플래시(16MB)
#     33~37  옥탈 PSRAM(8MB) — ★헤더에 33·34 가 나와 있어도 쓸 수 없다(R8=OPI)
#     19·20  네이티브 USB D-/D+   |   43·44  UART0(CH343 브리지)
#     0 BOOT 버튼(스트래핑), 45·46 스트래핑   |   48 온보드 RGB LED(WS2812)
#   ※S3 에는 GPIO22~25 가 아예 존재하지 않는다.
#   ★화면·SD 를 뺐으므로(2026-08-18) SPI 를 안 쓴다 — HC-05 4핀 외 전부 여유:
#     1~13, 15, 16, 38~42, 47 (필요해지면 여기서 골라 쓴다)
# ★내장 BT 경로는 이 보드에서 영구히 닫혔다 — S3 는 하드웨어가 Bluetooth Classic(SPP)
#   미지원이다. 외부 HC-05(결정 #2)가 유일한 길이며 재논의 대상이 아니다.

# ── WiFi / 시각 ──
WIFI_SSID = "CHANGE_ME"
WIFI_PASS = "CHANGE_ME"
TZ_OFFSET_S = 9 * 3600          # KST
NTP_HOST = "pool.ntp.org"

# ── 로컬 웹서버 (dkh_server.py 대체 — 대시보드 + API 서빙) ──
HTTP_PORT = 80                  # 대시보드가 상대경로 fetch 를 쓰므로 80 이 가장 편함
WWW_DIR = "/www"                # index.html, ops.html, vendor/ 등 정적 자산
DATA_DIR = "/data"              # dkh.dat, *.json 데이터
# POST 본문 상한 — 종전 4KB 하드코딩은 설정 저장(수십 바이트)만 상정한 값이었다.
# 백업 복원(/api/restore)은 설정 3종 + 메타라 4KB 를 넘길 수 있어 넉넉히 잡는다.
# 8MB PSRAM 보드라 16KB 문자열은 부담이 아니다(구형 4MB 보드였다면 8KB 로 낮춰야 한다).
HTTP_MAX_BODY = 16 * 1024
# ★리스너 견고성(2026-08-26) — 자가 치유 웹서버 튜닝.
WEB_POLL_MS = 30000             # 유휴 시 accept poll 타임아웃(ms) — 이 주기로 하트비트 갱신
WEB_RELISTEN_S = 2              # 리스닝 소켓 사망 시 재생성 백오프(초) — 스핀 방지
WEB_HEARTBEAT_STALE_S = 120     # 이 시간 넘게 하트비트가 멎으면 메인 루프가 리스너 이상 경고

# ── 워치독 (2026-08-26) ──
# ★진짜 행(hang)에서만 자동 하드리셋. 개발 중 REPL 작업이 잦으면 False 로 굽고 운영 전환 시 켠다.
WDT_ENABLED = True
# 120초 — 코드상 최장 '무피드 단일 블로킹'(measure 링크복구 sleep 60초)의 2배 여유.
# 실측: 이 보드에서 machine.WDT(timeout=120000) 허용 확인.
WDT_TIMEOUT_MS = 120000

# ── 네트워크 유지 스레드 (2026-08-26 — WiFi 를 측정 스레드에서 완전 분리) ──
NET_MAINT_INTERVAL_S = 12       # WiFi/NTP 유지 스레드 주기(측정과 격리돼 있어 넉넉해도 무해)
WIFI_ENSURE_TIMEOUT = 15        # netmaint 의 STA 접속 대기(초) — 이 대기는 측정을 막지 않는다

# ── 온보드 RGB LED 경고등 (2026-08-26 — 헤드리스 로컬 표시. 평상시 소등) ──
LED_PIN = 48                    # 온보드 WS2812 — 실측 확인(GPIO48, (r,g,b) 매핑 정상)
LED_BRIGHT = 60                 # 밝기(0-255) — 눈부심 방지

# ── 장기 저장소 (SD 대체 — ★2026-08-19) ──
# ★S3 의 USB 는 스톡 MicroPython 에서 저장소로 쓸 수 없다: MSC 디바이스는 esp32 포트
#   미구현(micropython#8426), USB 호스트는 MicroPython 전체 미구현(discussions#15477).
#   둘 다 TinyUSB C 코드로 커스텀 펌웨어를 빌드해야 해서 "공식 펌웨어 무수정" 전제를 깬다.
#   → 대체 3층: ①플래시 아카이브(archive.py) ②LAN 백업(/api/backup·/api/files)
#     ③USB CDC 로 PC 로 뽑기(mpremote fs cp -r :/data ...). 상세는 archive.py 헤더.
ARCHIVE_ENABLED = True
ARCHIVE_DIR = DATA_DIR + "/archive"     # 14일 창 밖 기록·설정 스냅샷의 무기한 보관처
# 파일별 상한 — 16MB 플래시에서 펌웨어(1.7MB)·앱·자산을 빼면 10MB 이상 남는다.
# 2MB × 3파일이면 dkh.dat 은 수십 년치, plateau 는 수년치가 들어간다(1런 ≈ 1.3KB).
ARCHIVE_MAX_KB = 2048
# 플래시 여유 하한 — 이 아래로 내려가면 아카이브를 절반으로 줄인다(측정 데이터는 불가침).
# 로그(512KB 순환)·tmp 파일·plateau 성장분이 동시에 몰려도 쓰기가 실패하지 않을 여유.
ARCHIVE_MIN_FREE_KB = 1024

# ── 디스플레이·SD 카드 (★제거 2026-08-18, 사용자 확정) ──
# 화면과 SD 를 모두 뺀다 — 현장 확인·조치는 **웹(대시보드 + /ops.html)으로만** 한다.
# 그래서 이 펌웨어에 SPI 페리페럴이 하나도 없다: 남은 하드웨어는 HC-05(UART1) 뿐이다.
#   - 화면 관련: display.py / display_driver.py / kfont.bin, 폰트 도구 일체 삭제
#   - SD 관련: storage.py 삭제, 로그 전문 미러·RF 원장 파일 폐지
#     (RF 이벤트는 measure_kh.log 에 `[rf] ...` 로 남는다 — 백오프 튜닝 근거는 유지)
#   ★보관 창 밖 아카이브는 2026-08-19 에 **플래시로 되살렸다** — 위 '장기 저장소' 블록.
# ★보관 정책(14일)은 그대로다. 16MB 플래시라 /data 여유는 충분하다.

# ── 스케줄 (KST 시각) ──
# ★2026-08-21: 회차는 **정비페이지에서 바꾼다** — `/data/schedule.json` 에 저장되고 그 값이
#   아래 값보다 **우선**한다(schedule.py). 여기 값은 폴백이다(파일이 없거나 깨졌을 때).
MEASURE_HOURS = (5, 13, 21)     # 하루 3회 측정 (기본값 — 8h 간격)
DOSER_SLOT_HOUR = 13            # 그중 도저 자동 조정 회차(--slot-adjust 상당). 회차 목록 안이어야 한다
# 회차 제약 — ★최소 간격 2시간(사용자 확정 2026-08-21: "측정이 2시간을 넘긴 적이 없다").
#   간격은 자정을 넘는 쪽도 같이 본다(원형) — [23, 0] 은 1시간이라 거부된다.
MEASURE_MIN_GAP_H = 2
MEASURE_HOURS_MAX = 12          # 24h / 2h — 최소 간격에서 따라오는 상한

# ── UART / HC-05 브리지 (★HC-05 1개 — 사용자 확정 2026-08-18) ──
# ESP32-HC-05(마스터) 유선, HC-05~HC-06(장비측 슬레이브) 무선. 장비는 측정기·도저 2대이고
# 동시에 붙들 필요가 없으므로(main 루프가 순차 실행) 모듈 1개로 AT+BIND 를 바꿔가며
# 번갈아 접속한다. 전환에 8~12초 걸리지만 속도는 요구사항이 아니다.
#
# ★배선(ZS-040 캐리어 기준 — 예: 디바이스마트 VLT-BT018):
#   - BT_KEY_PIN(GPIO14) → 코어 모듈 PIN34(PIO11). ZS-040 헤더에는 안 나와 있고 **온보드 버튼**이
#     VCC→PIN34 로 물려 있으므로 버튼 패드의 PIN34 쪽에서 선을 따야 한다(납땜 1군데).
#   - BT_POWER_PIN(GPIO21) → 헤더의 `EN` 핀. ZS-040 의 EN 은 온보드 LDO(LP2985)의 ON/OFF 에
#     연결돼 있어(EN—1K—노드—220K—VCC) LOW 로 내리면 모듈 전원이 꺼진다. **MOSFET 불요.**
#     220K 풀업 때문에 부동 상태 기본값이 ON 이라 부팅 중 의도치 않은 차단도 없다.
#   - BT_STATE_PIN(GPIO38) → 헤더의 `STATE`. 연결=HIGH / 미연결=LOW (2026-08-28 실측 검증).
# ★★실장 정정(2026-08-28 실측): 이 보드의 헤더 `EN` 은 **전원 차단이 아니라 KEY(PIN34)** 였다.
#   위 "EN—1K—노드—220K—VCC(LDO enable)" 설명은 **다른 ZS-040 리비전** 이야기이고 이 보드에는
#   해당 회로가 없다. 그래서 전원 재투입 복구는 불가능하고, 그 자리를 **AT+RESET** 이 대신한다.
BAUD = 9600
BT_AT_BAUD = 38400              # 전원 경로의 AT 보레이트 — KEY HIGH 로 전원 인가 시 고정값
BT_UART_ID = 1
# ★핀은 S3 기준으로 재배정됐다(2026-08-18) — 종전 25/26/32/33 은 S3 에서 각각 '없는 핀'
#   (22~25) 이거나 플래시/PSRAM 전용(26~37)이라 그대로 쓰면 부팅조차 못 한다.
BT_TX, BT_RX = 17, 18           # ESP32 TX→HC-05 RXD / HC-05 TXD→ESP32 RX (UART1)
# ★★2026-08-28 실측 결론 — 헤더 `EN` 은 **전원 차단 핀이 아니다**(KEY 계열이지만 쓸 수 없다):
#   ①EN 을 LOW 로 내려도 모듈 전원은 절대 안 꺼진다(ESP32 전체 전원을 내려야 끊김) → 전원 경로 무효.
#   ②EN 으로 KEY 를 HIGH(AT 모드)로 올릴 수는 있으나, **LOW 로 확실히 내리지 못한다**(보드 풀업이
#     이겨 AT 모드에 갇힌다 — 실측). 그래서 EN 만으로는 데이터 모드 복귀가 안 된다.
#   ⇒ **KEY 는 코어 PIN34 납땜선(GPIO14)으로 제어한다**(현 배선). EN 은 연결하지 않는다.
#   ★KEY 선을 두 개(GPIO14+EN) 물리면 서로 싸워 AT 진입이 깨진다 — 반드시 하나만 연결한다.
BT_KEY_PIN = 14                 # HC-05 KEY = 코어 PIN34 납땜선. None 이면 대상 전환 불가
# 전원 차단 수단이 없다(EN 은 KEY 였다) → 전원 경로 비활성. 대신 **AT+RESET** 이 그 역할을 한다
# (link.at_reset — 실측: 'OK/+DISC:SUCCESS' 와 함께 STATE 1→0, 실제로 링크가 끊긴다).
# VCC 를 MOSFET 으로 게이팅하는 배선을 추가하면 그때 그 핀 번호를 여기에 넣는다.
BT_POWER_PIN = None

# ★대상 전환 방식 — 데이터시트(ZG1643)에 AT 모드 진입 경로가 둘 있다:
#   "key"   = 전원을 켠 채 KEY 를 올려 AT 모드 진입(Way 1). 보레이트는 통신값(9600) 그대로다.
#             AT+DISC → AT+BIND → AT+LINK 으로 붙고 KEY 를 내리면 끝. 회당 1~2초.
#             데이터시트 주 (3): "When PIN34 keeps high level, all commands can be used"
#             — 전환 내내 KEY 를 올려둔 채로 진행하므로 전 명령을 쓸 수 있다.
#   "power" = KEY 를 올린 채 전원 재투입으로 AT 모드(38400) 진입 → AT+BIND → 전원 재투입.
#             회당 8~12초. 펌웨어 리비전에 따라 Way 1 이 안 먹을 때의 확실한 경로다.
#   "auto"  = key 먼저, 실패하면 power 로 폴백(기본).
# ★실장에서 Way 1 이 확인되기 전까지는 auto 가 안전하다 — 두 경로 모두 끝에서 신원 검증을
#   똑같이 통과해야 하므로, 어느 쪽으로 붙었든 오장비 명령 위험은 달라지지 않는다.
BT_SWITCH_MODE = "auto"

# EN 극성 — ZS-040 은 HIGH=ON 이다. (MOSFET 스위치를 따로 만든 경우에만 배선에 맞춰 바꾼다.)
BT_POWER_ACTIVE_HIGH = True

# STATE 핀(코어 PIN32) — 미연결 LOW / 연결 HIGH. 배선하면 순단 감지가 핑 타임아웃(3초)을
# 기다리지 않고 즉시 된다. ★연결 여부만 알려줄 뿐 '누구와' 붙었는지는 모르므로 신원 검증을
# 대체하지 않는다. (S3 는 전 GPIO 가 입출력 겸용 — 구형의 '입력 전용 34~39' 구분이 없다.)
BT_STATE_PIN = 38               # HC-05 STATE — ★2026-08-28 실측 검증(연결=1 / 미연결=0).
                                # 배선했으므로 활성화한다: 링크 계층이 RF 연결 여부를 하드웨어로
                                # 즉시 알아(핑 타임아웃 3초를 기다리지 않는다). ★'누구와' 붙었는지는
                                # 모르므로 신원 검증(응답 서명)을 대체하지 않는다.

# 상대 HC-06 주소 — AT+BIND 형식 'NNNN,NN,NNNNNN'(콜론 대신 콤마). AT+INQ 로 검색 가능.
# ★2026-08-21: 주소는 **정비페이지(BT 연결 → 장치 목록)에서 넣는다** — `/data/devices.json`
#   에 저장되고 그 값이 아래 값보다 **우선**한다(MAC 을 붙여 넣으면 자동 변환).
#   여기 값은 폴백이다(파일이 없거나 해당 항목이 비었을 때만 쓰인다).
#   ※구형 `/data/bt.json`(2026-08-19) 도 그대로 읽는다 — devices.json 이 없으면 그쪽에서
#     조립한다(devices._legacy). 저장하면 새 형식으로 기록된다.
# 비어 있으면 전환이 즉시 실패한다(빈 주소로 아무 데나 붙는 것 방지).
BIND_ADDR_MEAS = ""             # 예: "98d3,31,fb1234" — 측정 장비
BIND_ADDR_DOSER = ""            # 예: "98d3,31,fb5678" — 기본 도저(올포리프 도저)

# ── 도징기 여러 대 (★2026-08-21 사용자 요구) ──
# 도징기는 여러 대일 수 있고(알칼리/칼슘/미량원소) 각자 자체 타이머로 도징하므로 시계 동기를
# 장치별로 걸어야 한다. `set time HH:MM:SS` 명령은 전 도징기 공통이다.
# ★★추가 도징기는 **시계 동기 전용**이다(사용자 확정, 재논의 불요): 도저 펌웨어의 응답
#   서명이 전부 같아서(`ls` → "왼쪽 동작") 신원 검증이 도징기끼리는 구분을 못 한다.
#   set time 은 값이 같아 뒤바뀌어도 무해하지만 lrt 는 오장비 사고가 된다 →
#   dKH 자동조정·수동 mL/일·lrt 적용은 **기본 도저 1대**에만 허용한다
#   (id="doser", 표시 이름 "올포리프 도저" — devices.PRIMARY_DOSER_NAME).
DOSER_MAX = 4                   # 등록 가능한 도징기 수(기본 도저 포함)
DOSER_SYNC_HOURS = (0,)         # 새 도징기의 기본 시계 동기 시각 — 종전 동작(자정 이후 첫 틱)
DOSER_SYNC_MAX = 4              # 장치당 동기 시각 개수 상한

# 전환 타이밍(초) — 속도보다 확실성 우선
BT_KEY_SETTLE_SECS = 0.1        # KEY 레벨 변경 후 모드 전환 안정화(고속 경로)
BT_LINK_TIMEOUT = 10.0          # AT+LINK 응답 대기 — 상대를 실제로 찾아 붙는 시간이라 길다
BT_POWER_OFF_SECS = 0.4         # 전원 차단 유지(모듈 완전 방전)
BT_AT_BOOT_SECS = 1.0           # AT 모드 부팅 대기
BT_DATA_BOOT_SECS = 1.0         # 데이터 모드 부팅 대기
BT_CONNECT_SECS = 12.0          # 자동 재접속(자동 페어링) 최대 대기
BT_SWITCH_TRIES = 3             # 전환 재시도 횟수
BT_AT_TIMEOUT = 2.0             # AT 명령 응답 대기
# ★AT+RESET(전원 재투입 대체) 후 모듈이 다시 올라오는 시간 — 실측상 1초로는 부족했다.
BT_RESET_WAIT_SECS = 1.5        # 리셋 후 1회 대기
BT_RESET_TRIES = 4              # 그 대기를 최대 몇 번 반복하며 AT 응답을 기다릴지(총 ~6초)

# ── 평형(평탄) 판정 — 정수 milli-pH 윈도우 (measure_kh_once.py V4) ──
FLAT_SPAN_N = 4                 # 흔들림(span) 판정 윈도우
FLAT_SPAN_MPH = 2               # 최근 N개 max-min ≤ 2 mpH
FLAT_NET_N = 8                  # 드리프트(net) 룩백 — span 보다 긴 창(느린 단조 꼬리 차단)
FLAT_NET_MPH = 1
FLAT_MIN_N_TANK = 0             # 고정 사전폭기 도입으로 게이트 off (원본 2026-07-23)
MEAS_INTERVAL = 30              # 측정 간 간격(초) — 폭기 유지
PREAERATE_SECS = {"tank": 1500, "ref": 210}   # 측정 전 고정 사전폭기
SETTLE_SECS = 10                # read 직전 airoff 후 정치
FIRST_POINT_AERATE_SECS = 60    # tank 첫 점(관리용) 폭기 시간

# ── 무한 대기 방지 상한 ──
PHASE_MAX_SECS = 5400           # phase 별 최대 측정 시간(사전폭기 제외)
MEAS_MAX = 180
FAIL_MAX = 5

# ── RF 순단 대응 (HC-05~HC-06 무선 구간 — 원본 HC-06 정책 유지) ──
RECONNECT_TRIES = 5
RECONNECT_BACKOFF = (2, 3, 4, 5, 6)   # HC-05 재페어링 소요(~2-6s) 반영해 원본보다 약간 완만
SEND_RETRY_MAX = 3
KEEPALIVE_SECS = 12             # HC-06 ~20s 무통신 드롭 임계 아래
MEAS_READ_TIMEOUT = 20
LINK_PING_TIMEOUT = 3
LINK_RETRY_INTERVAL = 60
CLEANUP_RECOVERY_SECS = 1800
MOVE_PRECOND_RECOVERY_SECS = 1800

# ── 도저 (doser_adjust.py) ──
TARGET_DKH = 7.2
TARGET_LO, TARGET_HI = 6.0, 9.0
DAILY_RATE_CAP = 0.25
APPROACH_DAYS = 7.0
SENS = 0.0058                   # dKH/(원액mL·일) — 6/29 볼루스 실측 유도
MS_PER_ML = 4000
DOSES_PER_DAY = 6
DILUTION = 0.5
LRT_MIN = 2000
LRT_MAX = 24000
STEP_MAX_FRAC = 0.30
DEADBAND_MS = 200
# ★수준·추세 창 = 7일. dkh.dat 의 날짜 컬럼으로 자른다(원본 2026-08-16 반영).
#   종전 ROWS=21행(≈7일)·ROW_DAYS=8h 균일 가정은 하루 3회 측정을 전제한 회차 근사라,
#   측정이 빠진 날엔 창이 과거로 늘어나고 추가 측정을 돌린 날엔 창 안쪽이 밀려 잘렸다.
WINDOW_DAYS = 7
MIN_VALID = 10
VALID_LO, VALID_HI = 4.0, 12.0
CO2_EXCLUDE_MAX = 9
# CO2 편향 의심 판정(parse_plateau_log.classify_co2_suspect 이식, 원본 2026-07-13):
# 실내 CO2 축적이 ref 에 유입되면 ref 곡선이 하강 추적 + 평탄 지연 → dKH 저편향.
# 실측 35런 소급에서 두 지표(flat_n, 전구간 net)가 완전 분리(오분류 0) — AND 결합.
CO2_FLAT_N_MIN = 21             # ref_flat_n >= 21 (정상 최대 15 / 편향 최소 21)
CO2_REF_NET_MPH_MAX = -3        # ref 전구간 net <= -3mpH (정상 최저 -1 / 편향 최고 -7)
ADVISORY_RUNS = 2
AUTO_APPLY = False              # False=권고만(원본 사용자 지시 2026-07-06). 수동 오버라이드는 무관하게 적용
HISTORY_MAX = 42                # 도저 이력도 14일 정책에 맞춤(자동 1회/일 + 수동 여유. 원본 52)

# ── 데이터 보존 — ★모든 정보 14일 유지(사용자 지시 2026-08-14) ──
RETENTION_DAYS = 14
# ★보관 기준은 회차가 아니라 날짜다(원본 2026-08-16 반영). 종전 42회(=14일×3회) 컷은
#   하루 3회를 가정한 근사라, 측정이 빠진 날이 있으면 창이 14일보다 길어지고 추가 측정을
#   돌린 날이 있으면 14일 안쪽 데이터가 밀려나 잘렸다. "최근 14일"은 무조건 14일이어야 한다.
#   기준일은 오늘이 아니라 마지막 기록일 — 측정이 며칠 끊겨도 마지막 창은 보존된다.
#   아래 행수 값들은 이제 **힙 안전용 절대 상한**(백스톱)이고, 1차 컷은 항상 날짜다.
DAT_MAX_ROWS = RETENTION_DAYS * 3   # 날짜 없는 구형식 파일 전용 폴백 + 상한
# ★힙 안전: 이제 8MB PSRAM 이 있어 힙이 병목은 아니지만 **설계는 그대로 둔다** — 스트리밍
#   구조는 PSRAM 이 있어도 손해가 없고, PSRAM 초기화 실패 시에도 계속 동작한다.
#   (아래 설명은 구형 4MB·PSRAM 없는 보드 기준으로 왜 이렇게 만들었는지의 기록이다.)
# 저장소 실물이 51KB 인 plateau 이력을
#   파이썬 객체로 올리면 힙이 터진다 → JSONL(런 1개=1줄)로 저장하고, 대시보드가 받는
#   dkh_plateau_history.json 은 웹서버가 줄 단위로 스트리밍해 배열로 조립한다
#   (전체를 메모리에 올리는 지점이 없음). PSRAM 보드로 가도 그대로 유효한 설계.
# 행수 백스톱은 날짜 컷(RETENTION_DAYS) 뒤에 적용된다 — 하루에 수동 측정을 여러 번 돌려도
# 힙·플래시가 터지지 않게 한다. 정상 운용에서는 날짜 컷만 걸린다.
# ★2026-08-21: 상한을 **회차 수에 맞춰 늘린다**(schedule.rows_cap). 종전 고정값 `14×6`(=84)
#   은 하루 3회 전제였고, 회차를 웹에서 바꿀 수 있게 되면서 하루 6회를 넘기면 날짜 컷을
#   통과한 14일 창 안의 데이터가 여기서 잘렸다(하루 12회 = 168행 → 84행).
SERIES_BASE_PER_DAY = 6         # 하루 기준 보관 행수 하한(회차 3회 이하에서 종전과 동일)
PLATEAU_KEEP_MAX = 100          # 런당 phase 별 판독 보관 상한 — 초과 시 간격 솎음(곡선 형태 유지).
                                #   MEAS_MAX(180)×2phase 를 그대로 들면 힙 위험
LOG_MAX_BYTES = 512 * 1024      # measure_kh.log 상한(초과 시 새로 시작)
