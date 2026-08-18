# reefwiz-esp32 설정 — 원본: reefwiz/bin/measure_kh_once.py(V4), doser_adjust.py, dkh_server.py
# 상수 근거(사고 이력·튜닝 사유)는 원본 파일 헤더/주석 참조. 값은 원본과 동일하게 유지.

# ── WiFi / 시각 ──
WIFI_SSID = "CHANGE_ME"
WIFI_PASS = "CHANGE_ME"
TZ_OFFSET_S = 9 * 3600          # KST
NTP_HOST = "pool.ntp.org"

# ── 로컬 웹서버 (dkh_server.py 대체 — 대시보드 + API 서빙) ──
HTTP_PORT = 80                  # 대시보드가 상대경로 fetch 를 쓰므로 80 이 가장 편함
WWW_DIR = "/www"                # index.html, ops.html, vendor/ 등 정적 자산
DATA_DIR = "/data"              # dkh.dat, *.json 데이터

# ── 디스플레이 ──
# ★교체 예정(사용자 2026-08-14): 최종 보드는 더 고성능 + 더 큰 화면. 아래 값과
#   display_driver.py 는 *참조 구현*이며 교체 시 이 블록과 그 파일만 바꾼다.
#   display.py(레이아웃·조치 UI)는 해상도에 자동 적응하므로 수정 불요:
#   폰트 배율 = 가로폭/320 으로 자동 결정, 버튼·행 수도 화면 크기에 맞춰 재배치된다.
# 임시 참조 하드웨어(reefCore Checker R2 구성):
#   보드 = WEMOS ESP32 18650 (ESP32-WROOM-32, 4MB flash, PSRAM 없음)
#   화면 = 2.4" ILI9341 240x320 + XPT2046 저항막 터치(SPI 공유)
# ★PSRAM 없는 보드는 전체 프레임버퍼(320x240 RGB565=153KB)를 못 잡는다 → 참조 드라이버는
#   직접 그리기(윈도우 전송)이고 show() 는 no-op. PSRAM 보드로 가면 framebuf 방식 드라이버를
#   써도 되며(그때 show() 가 실제 전송), display.py 는 둘 다 지원한다.
# ★핀 배치는 잠정값 — 실제 배선 확인 후 수정. UART/HC-05 핀과 겹치지 않게 잡았다.
DISPLAY_DRIVER = "display_driver"    # None 이면 헤드리스(측정·웹·도저는 그대로 동작)
DISP_ROTATION = 90                   # 90=가로 320x240 (0=세로 240x320)
SPI_ID, SPI_SCK, SPI_MOSI, SPI_MISO = 1, 18, 23, 19
TFT_CS, TFT_DC, TFT_RST, TFT_BL = 5, 2, 4, 15    # TFT_BL=None 이면 백라이트 상시 ON 배선
TOUCH_CS, TOUCH_IRQ = 27, 34         # 34 는 입력 전용 핀(OK)
# 터치 캘리브레이션 — 원시 ADC 범위. 화면 네 귀퉁이를 눌러 로그의 raw 값으로 조정한다.
TOUCH_CAL = (300, 3800, 300, 3800)   # (x_min, x_max, y_min, y_max)
TOUCH_SWAP_XY = True                 # 가로 회전 시 보통 True
TOUCH_INVERT_X, TOUCH_INVERT_Y = False, True

# ── SD 카드 (SPI 모드, 디스플레이와 버스 공유) ──
# ★SDMMC(네이티브) 모드는 쓰지 않는다: ESP32 slot1 핀이 CLK=14/CMD=15/D0=2/D1=4 로 고정인데
#   위 TFT_DC=2 · TFT_RST=4 · TFT_BL=15 와 정면 충돌한다. SPI 모드로 기존 버스에 CS 만 더한다.
# ★SD 는 초기화 때 저속(~400kHz), 이후 고속을 요구하고 XPT2046 은 ~2MHz 라 장치마다 클럭이
#   다르다 — 드라이버가 트랜잭션마다 baudrate 를 잡으므로 공유해도 된다(CS 로 분리).
# ★돌입 전류: SD 삽입 순간 전류가 크다. 3.3V 레귤레이터 여유가 없으면 HC-05 가 같이 흔들리니
#   전원 여유를 확인할 것(측정 중 순단으로 나타난다).
SD_ENABLED = True
SD_CS = 16                      # 도저 UART 폐지로 비게 된 핀. 13/17/21/22 도 가능
SD_MOUNT = "/sd"
SD_DIR = "/sd/reefwiz"          # 이 아래에 로그·아카이브·RF 원장
SD_BAUD = 1320000               # 공유 버스 안정 우선(고속이 필요 없는 용도)
# SD 는 '있으면 좋은' 저장소다 — 없거나 실패하면 전부 플래시 동작으로 degrade 하고
# 측정·도저·웹은 그대로 돈다. 절대 측정을 막지 않는다.

# ── 스케줄 (KST 시각) ──
MEASURE_HOURS = (5, 13, 21)     # 하루 3회 측정 (dkh.dat 8h 간격 전제와 정합)
DOSER_SLOT_HOUR = 13            # 매일 13시 회차만 도저 자동 조정(--slot-adjust 상당)

# ── UART / HC-05 브리지 (★HC-05 1개 — 사용자 확정 2026-08-18) ──
# ESP32-HC-05(마스터) 유선, HC-05~HC-06(장비측 슬레이브) 무선. 장비는 측정기·도저 2대이고
# 동시에 붙들 필요가 없으므로(main 루프가 순차 실행) 모듈 1개로 AT+BIND 를 바꿔가며
# 번갈아 접속한다. 전환에 8~12초 걸리지만 속도는 요구사항이 아니다.
#
# ★전환에는 제어선이 2개 필요하다:
#   - BT_POWER_PIN: VCC 스위치(MOSFET 게이트). AT 모드 진입은 "KEY HIGH 상태로 전원 인가"라
#     전원을 실제로 끊었다 넣어야 한다. 종전처럼 EN 핀을 리셋용으로 쓸 수 없다(KEY 겸용).
#   - BT_KEY_PIN: HC-05 의 KEY/EN. HIGH 로 두고 전원을 넣으면 AT 명령 모드(38400 고정).
BAUD = 9600
BT_AT_BAUD = 38400              # AT 모드 보레이트 — KEY HIGH 전원인가 시 고정값
BT_UART_ID = 1
BT_TX, BT_RX = 25, 26           # ESP32 TX→HC-05 RXD / HC-05 TXD→ESP32 RX
BT_POWER_PIN = 32               # VCC 스위치(MOSFET). None 이면 전환·하드리셋 불가
BT_KEY_PIN = 33                 # HC-05 KEY/EN. None 이면 대상 전환 불가(단일 장비 전용)

# 상대 HC-06 주소 — AT+BIND 형식 'NNNN,NN,NNNNNN'(콜론 대신 콤마). AT+INQ 로 검색 가능.
# ★실장 전 반드시 실제 주소로 채울 것. 비어 있으면 전환이 즉시 실패한다(오접속 방지).
BIND_ADDR_MEAS = ""             # 예: "98d3,31,fb1234" — 측정 장비
BIND_ADDR_DOSER = ""            # 예: "98d3,31,fb5678" — 도저

# 전환 타이밍(초) — 속도보다 확실성 우선
BT_POWER_OFF_SECS = 0.4         # 전원 차단 유지(모듈 완전 방전)
BT_AT_BOOT_SECS = 1.0           # AT 모드 부팅 대기
BT_DATA_BOOT_SECS = 1.0         # 데이터 모드 부팅 대기
BT_CONNECT_SECS = 12.0          # 자동 재접속(자동 페어링) 최대 대기
BT_SWITCH_TRIES = 3             # 전환 재시도 횟수
BT_AT_TIMEOUT = 2.0             # AT 명령 응답 대기

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
# ★힙 안전(PSRAM 없는 보드 기준 가용 ~100KB): 저장소 실물이 51KB 인 plateau 이력을
#   파이썬 객체로 올리면 힙이 터진다 → JSONL(런 1개=1줄)로 저장하고, 대시보드가 받는
#   dkh_plateau_history.json 은 웹서버가 줄 단위로 스트리밍해 배열로 조립한다
#   (전체를 메모리에 올리는 지점이 없음). PSRAM 보드로 가도 그대로 유효한 설계.
# 아래 둘은 날짜 컷(RETENTION_DAYS) 뒤에 적용되는 백스톱이다 — 하루에 수동 측정을 여러 번
# 돌려도 힙·플래시가 터지지 않게 한다. 정상 운용(하루 3회)에서는 날짜 컷만 걸린다.
SERIES_MAX = RETENTION_DAYS * 6   # dkh_series.json — 5KB 라 파싱 OK, 여유 2배
PLATEAU_MAX = RETENTION_DAYS * 6  # plateau.jsonl 런 수 상한
PLATEAU_KEEP_MAX = 100          # 런당 phase 별 판독 보관 상한 — 초과 시 간격 솎음(곡선 형태 유지).
                                #   MEAS_MAX(180)×2phase 를 그대로 들면 힙 위험
LOG_MAX_BYTES = 512 * 1024      # measure_kh.log 상한(초과 시 새로 시작)
