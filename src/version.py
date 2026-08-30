# 펌웨어 판(版) — 장비명·버전의 **단일 진실**. 코드·API·화면·백업·로그가 전부 여기서 읽는다.
#
# ★왜 이 파일이 생겼나(2026-08-30): 종전에는 어디에도 버전이 없어서 "지금 기기에 올라가 있는
#   게 언제 코드인지"를 확인할 방법이 없었다. 재배포가 싸서(mpremote 가 같은 해시를 건너뛴다)
#   자주 올리는데, 올라간 판을 되짚을 근거가 하나도 남지 않았다 — 화면이 이상할 때 "그 수정이
#   올라간 기기인가"를 사람이 기억으로 판단하고 있었다.
#   화면·백업·로그가 각자 문자열을 들고 있으면 반드시 갈라지므로 여기 한 곳만 고친다.
#
# ── 판 올리는 법(3단계) ──
#   1) VERSION 을 올린다 — MAJOR.MINOR.PATCH
#        MAJOR : 데이터 형식·API 가 깨져 예전 대시보드/백업과 안 맞을 때
#        MINOR : 기능 추가(측정·도저·화면 동작이 늘어남)
#        PATCH : 버그 수정·문구·튜닝
#      RELEASED 도 같이 고친다(그 판을 굳힌 날).
#   2) CHANGELOG.md 맨 위에 그 판의 항목을 적는다.
#   3) 커밋 → `git tag v<VERSION>`. 빌드 스탬프가 커밋 해시를 가리키므로 기기에서 읽은
#      버전으로 저장소의 그 시점을 정확히 되짚을 수 있다.
#
# ── 빌드 스탬프(buildinfo.py) ──
#   `tools/deploy.py` 가 배포할 때 커밋 해시·배포 시각을 담은 `buildinfo.py` 를 만들어 코드와
#   함께 올린다. **저장소에는 없다**(생성물이라 커밋하지 않는다). 없으면 build 가 "dev" 로
#   표시된다 — mpremote 로 손수 올린 코드, 즉 어느 커밋인지 보증할 수 없다는 뜻이다.
#
# ── 조회 경로 ──
#   기기:  GET /api/version           (전체)   ·  GET /api/ops/status 의 "version" (요약)
#   화면:  정비페이지 상태 카드 + 맨 아래 장비 정보 줄, 대시보드 푸터
#   로그:  부팅 시 `[boot] ReefWiz Controller C-1 v1.0.0 #A1B2C3 | 빌드 ...` 한 줄
#   백업:  reefwiz-backup.json 의 "device" — 어느 판에서 뜬 백업인지 남는다
import rwtime

# ── 장비명 ──
# 대시보드 브랜드가 'ReefWiz' 이므로 장비도 그 이름을 쓴다(저장소·호스트명 reefwiz 와도 같다).
# ★AquaWiz 는 쓰지 않는다 — 동명의 상업 브랜드가 이미 있다(2026-08-30 사용자 지시).
# ★C-1 = 1세대 **제어기**(Controller). 이 기기 자체는 KH 를 재지 않고 장비들을 물고 돌며
#   시키는 쪽이라, 측정 대상(KH)을 모델명에 넣으면 하는 일과 어긋난다(2026-08-30 사용자 지적).
#   제어 구성이 바뀌면 C-2 가 된다.
# ★계열 — 제어기 C-1(RWC1) / 측정기 Meter M-1(RWM1) / 도징기 Doser D-1(RWD1) /
#   에어 분배기 Air A-1(RWA1). 등록부와 `ver` 한 줄 규약은 README '장비 펌웨어 ver 규약'.
MODEL = "ReefWiz Controller C-1"
MODEL_CODE = "RWC1"            # 로그·파일명처럼 공백이 곤란한 자리용

VERSION = "1.0.0"               # ★판을 올릴 때 여기와 CHANGELOG.md 를 같이 고친다
RELEASED = "2026-08-30"

# 개체 식별자 — 같은 모델을 여러 대 돌릴 때 백업 파일·로그가 어느 기기 것인지 구분하는 값.
# MAC 뒤 3바이트를 쓴다(공장 고유값이라 재부팅·재배포와 무관하게 같다).
_serial = None
_uname = None
_BOOT_MS = rwtime.mono_ms()

try:
    import buildinfo as _b       # 배포가 만들어 올린 스탬프(저장소에는 없다)
except ImportError:
    _b = None


def serial():
    """개체 시리얼 — 'A1B2C3'. 하드웨어에서 못 읽으면 'SIM'(PC 스텁·시뮬)."""
    global _serial
    if _serial is None:
        try:
            import machine
            uid = machine.unique_id()
            _serial = "".join("%02X" % b for b in uid[-3:])
        except Exception:
            _serial = "SIM"
    return _serial


def name():
    """장비명 — 'ReefWiz Controller C-1 #A1B2C3'. 사람이 기기를 지목할 때 쓰는 이름.
    ★개체 구분자는 '#' — 측정기·도징기의 `ver` 응답과 같은 표기다(아래 ver 참조)."""
    return "%s #%s" % (MODEL, serial())


def build():
    """빌드 스탬프 — {"commit","dirty","at","by"}. 배포 스탬프가 없으면 전부 None."""
    if _b is None:
        return {"commit": None, "dirty": None, "at": None, "by": None}
    return {"commit": getattr(_b, "COMMIT", None), "dirty": getattr(_b, "DIRTY", None),
            "at": getattr(_b, "BUILT_AT", None), "by": getattr(_b, "BUILT_BY", None)}


def full():
    """전체 버전 문자열 — '1.0.0+3f2a1c9' / 커밋 위에 미커밋 변경이 있으면 '...-dirty' /
    배포 스탬프가 없으면 '1.0.0+dev'(손으로 올린 코드)."""
    b = build()
    if not b["commit"]:
        return VERSION + "+dev"
    return VERSION + "+" + b["commit"] + ("-dirty" if b["dirty"] else "")


def platform():
    """펌웨어 바닥 — MicroPython 판과 보드. 펌웨어를 갈아 끼운 뒤 확인용."""
    global _uname
    if _uname is None:
        try:
            import os
            u = os.uname()
            _uname = {"sys": u[0], "release": u[2], "version": u[3], "machine": u[4]}
        except Exception:
            _uname = {"sys": None, "release": None, "version": None, "machine": None}
    return _uname


def uptime_s():
    """부팅 후 경과(초) — 이 모듈이 import 된 시점 기준(부팅 극초반)."""
    return int(rwtime.elapsed_s(_BOOT_MS))


def brief():
    """요약 — /api/ops/status 처럼 자주 폴링되는 응답에 얹는 최소 정보."""
    return {"model": MODEL, "name": name(), "version": VERSION, "full": full(), "ver": ver()}


def info():
    """전체 — GET /api/version. 사람이 '이 기기가 무엇이고 어떤 판인지'를 한 번에 본다."""
    d = brief()
    d["model_code"] = MODEL_CODE
    d["serial"] = serial()
    d["released"] = RELEASED
    d["build"] = build()
    d["platform"] = platform()
    d["uptime_s"] = uptime_s()
    return d


def ver():
    """장비 3종 공통 한 줄 — '<이름> v<판> #<개체>'.

    ★측정기(ReefWiz Meter M-1)·도징기(ReefWiz Doser D-1) 펌웨어의 `ver` 응답과 **글자 그대로
      같은 모양**이다(README '장비 펌웨어 ver 규약'). 제어기는 `ver` 명령을 받는 쪽이 아니라
      **묻는 쪽**이라 시리얼 명령이 없다 — 대신 이 줄을 `GET /api/version` 의 "ver" 로 낸다.
      셋이 같은 모양이어야 나중에 세 대를 나란히 표시할 때 파서가 하나로 끝난다.
    ★빌드 커밋은 여기 넣지 않는다: 규약이 정한 칸이 셋(이름·판·개체)뿐이고, 커밋은
      build 필드에 그대로 남아 있다(잃는 정보가 없다)."""
    return "%s v%s #%s" % (MODEL, VERSION, serial())


def line():
    """부팅 로그용 — 규약 한 줄 + 빌드 상세.
    ★로그는 '언제 어떤 커밋이 올라와 돌기 시작했나'를 사후에 되짚는 자리라 커밋을 뺄 수 없다.
      규약 한 줄을 앞에 두고 빌드를 뒤에 붙여 둘 다 만족시킨다."""
    b = build()
    detail = (b["commit"] + ("-dirty" if b["dirty"] else "")) if b["commit"] else "dev(스탬프 없음)"
    if b["at"]:
        detail += " · %s 배포" % b["at"]
    return "%s | 빌드 %s" % (ver(), detail)
