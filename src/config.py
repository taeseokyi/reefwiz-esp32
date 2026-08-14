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

# ── 스케줄 (KST 시각) ──
MEASURE_HOURS = (5, 13, 21)     # 하루 3회 측정 (dkh.dat 8h 간격 전제와 정합)
DOSER_SLOT_HOUR = 13            # 매일 13시 회차만 도저 자동 조정(--slot-adjust 상당)

# ── UART / HC-05 브리지 ──
# ESP32-HC-05(마스터) 유선, HC-05~HC-06(장비측 슬레이브) 무선.
# HC-05 1회 설정: AT+ROLE=1, AT+CMODE=0, AT+BIND=<HC06주소>, AT+UART=9600,0,0
BAUD = 9600
MEAS_UART_ID = 1
MEAS_TX, MEAS_RX = 25, 26       # ESP32 TX→HC-05 RXD / HC-05 TXD→ESP32 RX
MEAS_RESET_PIN = 32             # HC-05 EN(또는 VCC 스위치용 MOSFET 게이트). None=하드리셋 없음
DOSER_UART_ID = 2
DOSER_TX, DOSER_RX = 17, 16
DOSER_RESET_PIN = 33

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
ROWS = 21                       # 최근 21행 ≈ 7일
MIN_VALID = 10
VALID_LO, VALID_HI = 4.0, 12.0
ROW_DAYS = 8.0 / 24.0
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
DAT_MAX_ROWS = RETENTION_DAYS * 3   # dkh.dat = 42행(하루 3회) — 기록 시마다 초과분 앞에서 제거
# ★힙 안전(PSRAM 없는 보드 기준 가용 ~100KB): 저장소 실물이 51KB 인 plateau 이력을
#   파이썬 객체로 올리면 힙이 터진다 → JSONL(런 1개=1줄)로 저장하고, 대시보드가 받는
#   dkh_plateau_history.json 은 웹서버가 줄 단위로 스트리밍해 배열로 조립한다
#   (전체를 메모리에 올리는 지점이 없음). PSRAM 보드로 가도 그대로 유효한 설계.
SERIES_MAX = DAT_MAX_ROWS       # dkh_series.json 최근 42행(14일) — 5KB 라 파싱 OK
PLATEAU_MAX = DAT_MAX_ROWS      # plateau.jsonl 최근 42런(14일)
PLATEAU_KEEP_MAX = 100          # 런당 phase 별 판독 보관 상한 — 초과 시 간격 솎음(곡선 형태 유지).
                                #   MEAS_MAX(180)×2phase 를 그대로 들면 힙 위험
LOG_MAX_BYTES = 512 * 1024      # measure_kh.log 상한(초과 시 새로 시작)
