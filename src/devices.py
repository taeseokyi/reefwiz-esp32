# 장치 레지스트리 — 측정기 1대 + 도징기 N대. BT 주소·이름·시계 동기 시각을 장치별로 든다.
#
# ★왜 생겼나(2026-08-21): 종전 `bt.json` 은 {"meas": 주소, "doser": 주소} 두 칸이 전부여서
#   도징기를 2대 이상 둘 수 없었다. 도징기는 여러 대일 수 있고(알칼리/칼슘/미량원소) 각자
#   자체 타이머로 도징하므로 **시계 동기를 장치별로** 걸어야 한다. 그래서 주소를 목록으로
#   바꾸고, 장치마다 동기 시각(sync_hours)을 붙였다.
#
# ★★추가 도징기는 '시계 동기 전용'이다(사용자 확정 2026-08-21, 재논의 불요).
#   도저 펌웨어의 응답 서명은 모든 도징기가 동일하다(`ls` → "왼쪽 동작"/"왼쪽 휴지").
#   즉 link 의 신원 검증은 "도저인가"까지만 알 수 있고 "몇 번 도저인가"는 구분하지 못한다.
#   - `set time HH:MM:SS` 는 모든 도징기에 같은 값이라 주소가 뒤바뀌어도 무해하다.
#   - `lrt`(도징량)는 뒤바뀌면 곧 오장비 사고다.
#   → dKH 자동조정·수동 mL/일·lrt 적용은 **기본 도저 1대**에만 허용한다(ops.py) —
#     id 는 `doser`, 표시 이름은 실물 명칭인 "올포리프 도저"(PRIMARY_DOSER_NAME).
#
# ★목록을 dict 이 아니라 **리스트**로 저장하는 이유: MicroPython dict 은 삽입 순서를
#   보장하지 않는다. 화면에 보이는 순서와 '첫 도저 = 기본 도저' 규칙이 흔들리면 안 된다.
import json

import config

DEVICES_FILE = config.DATA_DIR + "/devices.json"
BT_FILE = config.DATA_DIR + "/bt.json"      # 구형식(2026-08-19) — 마이그레이션 원본

# 종류별 규약 — probe = 부작용 없는 조회, sig = 그 종류만 내는 응답 조각,
# eol = 줄 종단(도저 펌웨어는 CR 이 붙으면 명령을 실행하지 않는다 — 원본 확인).
# ★값은 종전 link.TARGETS 에서 그대로 옮겨온 것이다(동작 변경 없음).
KINDS = {
    "meas":  {"probe": "status", "sig": ("============",), "eol": b"\r\n",
              "label": "측정 장비"},
    "doser": {"probe": "ls", "sig": ("왼쪽 동작", "왼쪽 휴지"), "eol": b"\n",
              "label": "도저"},
}

MEAS_ID = "meas"          # 측정기 id 는 고정 — measure.py/ops.py 가 이 이름으로 부른다
PRIMARY_DOSER_ID = "doser"  # 기본 도저 id 도 고정 — doser.py 의 기본 대상
# 기본 도저의 **표시 이름** — 실물이 올포리프(AllForReef) 도저다(사용자 지시 2026-08-21).
# ★id 는 바꾸지 않는다(`doser`) — 이력·API·테스트가 그 이름으로 부른다. KINDS 의 label 은
#   '종류'를 가리키는 일반명이라 그대로 "도저"다(교차 프로브 로그 등에서 쓴다).
PRIMARY_DOSER_NAME = "올포리프 도저"

_cache = None             # 정규화된 장치 리스트. None = 아직 안 읽음


# ── 주소 정규화 ──

def normalize_addr(s):
    """입력 주소 → HC-05 AT+BIND 형식 'nnnn,nn,nnnnnn'. 반환 (주소, 오류사유).

    ★사람이 아는 형태를 그대로 받는다: 콜론 MAC(98:DA:60:0F:C5:7A), 붙여쓴 MAC
    (98DA600FC57A), 이미 콤마 3구간인 값 모두 같은 결과가 된다(원본 bt_config.json 은
    MAC 을 붙여쓰기로 적어 뒀다). 빈 값은 '지움'이라 오류가 아니다.
    ※2026-08-21 에 link.py 에서 이곳으로 옮겼다(link → devices 단방향 import 유지).
      link.normalize_addr 는 그대로 쓸 수 있다(재노출)."""
    s = (s or "").strip()
    if not s:
        return "", None
    hexs = ""
    for ch in s:
        if ch in ":-. ,":
            continue
        c = ch.lower()
        if not (("0" <= c <= "9") or ("a" <= c <= "f")):
            return None, "16진수·구분자 외 문자가 있습니다: %r" % ch
        hexs += c
    if len(hexs) != 12:
        return None, "16진수 12자리여야 합니다(입력 %d자리)" % len(hexs)
    return "%s,%s,%s" % (hexs[0:4], hexs[4:6], hexs[6:12]), None


# ── 로드 / 마이그레이션 ──

def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _legacy():
    """구형식에서 조립 — `bt.json` 우선, 없으면 config 폴백.

    ★파일을 즉시 새로 쓰지는 않는다: 읽기만으로 /data 에 쓰면 부팅 중 플래시를 건드리고,
    복원 직후처럼 '읽는 김에 덮어쓰면 곤란한' 상황이 생긴다. 저장(set_devices) 시점에
    새 형식으로 기록된다."""
    bt = _read_json(BT_FILE) or {}
    if not isinstance(bt, dict):
        bt = {}
    meas = (bt.get("meas") or config.BIND_ADDR_MEAS or "").strip()
    doser = (bt.get("doser") or config.BIND_ADDR_DOSER or "").strip()
    return [
        {"id": MEAS_ID, "kind": "meas", "name": KINDS["meas"]["label"], "addr": meas},
        {"id": PRIMARY_DOSER_ID, "kind": "doser", "name": PRIMARY_DOSER_NAME,
         "addr": doser, "sync_hours": list(config.DOSER_SYNC_HOURS)},
    ]


def normalize_pswd(v):
    """장치별 접속 암호(HC-06 의 PIN) 정규화 — (암호, 오류사유). 빈 값 = '기본값 그대로'.

    ★이 값은 **상대 HC-06 의 PIN** 이다. 붙기 직전에 마스터(HC-05)의 `AT+PSWD` 를 이 값으로
      맞춰야 페어링이 성사된다(2026-08-29 실측: 틀린 PIN 이면 20초 동안 못 붙는다).
      HC-05 자기 값이므로 대상마다 다시 넣어 준다 — 모듈에 저장은 되지만 대상이 바뀌면 무의미하다.
    ★따옴표·제어문자를 막는 이유: 명령이 `AT+PSWD="<값>"` 형태라 따옴표가 섞이면 명령이 깨진다."""
    v = (v or "").strip()
    if not v:
        return "", None
    if len(v) > 16:
        return None, "접속 암호는 16자 이내여야 합니다"
    for ch in v:
        if ch == '"' or ch == "'" or ord(ch) < 0x20 or ord(ch) > 0x7E:
            return None, "접속 암호에 쓸 수 없는 문자가 있습니다: %r" % ch
    return v, None


def _normalize_list(raw):
    """저장·마이그레이션 공통 정규화 — id 재부여, 기본값 채우기, 순서 확정.
    반환 (리스트, 오류사유). 오류가 있으면 아무것도 쓰지 않는다(부분 적용 금지).

    ★id 는 위치에서 파생한다: 측정기=meas, 첫 도저=doser(기본), 나머지=doser2.. 순.
      화면에 보이는 순서가 곧 id 라서 '기본 도저가 어느 것인지'가 화면과 어긋나지 않는다."""
    if not isinstance(raw, list) or not raw:
        return None, "장치 목록이 비어 있습니다"
    meas_seen, dosers, out = None, 0, []
    for item in raw:
        if not isinstance(item, dict):
            return None, "장치 항목 형식 오류"
        kind = item.get("kind")
        if kind not in KINDS:
            return None, "알 수 없는 장치 종류: %r" % (kind,)
        # 이름을 비워 두면 위치에 맞는 기본 이름을 넣는다 — 첫 도저는 실물 명칭(올포리프),
        # 그다음부터는 '도저2'·'도저3'(운영자가 실물 이름으로 고쳐 쓴다).
        if kind == "meas":
            default_name = KINDS["meas"]["label"]
        elif dosers == 0:
            default_name = PRIMARY_DOSER_NAME
        else:
            default_name = "%s%d" % (KINDS["doser"]["label"], dosers + 1)
        name = (item.get("name") or "").strip() or default_name
        if len(name) > 20:
            return None, "이름은 20자 이내여야 합니다: %r" % name
        addr, err = normalize_addr(item.get("addr"))
        if err:
            return None, "%s 주소: %s" % (name, err)
        pswd, err = normalize_pswd(item.get("pswd"))
        if err:
            return None, "%s: %s" % (name, err)
        if kind == "meas":
            if meas_seen is not None:
                return None, "측정 장비는 1대만 등록할 수 있습니다"
            meas_seen = {"id": MEAS_ID, "kind": "meas", "name": name, "addr": addr,
                         "pswd": pswd}
            continue
        dosers += 1
        if dosers > config.DOSER_MAX:
            return None, "도징기는 최대 %d대입니다" % config.DOSER_MAX
        hours, err = _norm_hours(item.get("sync_hours"), name)
        if err:
            return None, err
        out.append({"id": PRIMARY_DOSER_ID if dosers == 1 else "doser%d" % dosers,
                    "kind": "doser", "name": name, "addr": addr, "pswd": pswd,
                    "sync_hours": hours})
    if meas_seen is None:
        return None, "측정 장비가 없습니다 — 1대는 반드시 있어야 합니다"
    if not dosers:
        return None, "도징기가 없습니다 — 기본 도저 1대는 반드시 있어야 합니다"
    return [meas_seen] + out, None


def _norm_hours(v, name):
    """시계 동기 시각 목록 검증 — 0~23 정수, 중복 없음, 정렬. 빈 목록 = 자동 동기 안 함."""
    if v is None:
        return [], None
    if not isinstance(v, (list, tuple)):
        return None, "%s 동기 시각 형식 오류" % name
    hours = []
    for h in v:
        try:
            h = int(h)
        except (TypeError, ValueError):
            return None, "%s 동기 시각에 숫자가 아닌 값: %r" % (name, h)
        if not (0 <= h <= 23):
            return None, "%s 동기 시각은 0~23 사이여야 합니다: %d" % (name, h)
        if h not in hours:
            hours.append(h)
    if len(hours) > config.DOSER_SYNC_MAX:
        return None, "%s 동기 시각은 최대 %d개입니다" % (name, config.DOSER_SYNC_MAX)
    return sorted(hours), None


def all_devices():
    """장치 리스트(정규화된 상태). 측정기가 먼저, 그다음 도저 등록 순.
    ★반환값을 호출부가 고치면 안 된다(캐시 자체다) — 수정은 set_devices 로만."""
    global _cache
    if _cache is None:
        raw = _read_json(DEVICES_FILE)
        if isinstance(raw, dict):
            raw = raw.get("devices")
        devs = None
        if raw:
            devs, err = _normalize_list(raw)
            if err:
                # 파일이 깨졌으면 조용히 구형식으로 떨어진다 — 여기서 죽으면 부팅이 막힌다.
                print("[devices] devices.json 무시(%s) — 구형식/config 로 폴백" % err)
                devs = None
        _cache = devs or _normalize_list(_legacy())[0]
    return _cache


def reload():
    """캐시 무효화 — 파일이 밖에서 바뀐 뒤(복원 등) 다음 조회가 다시 읽게 한다."""
    global _cache
    _cache = None


def get(dev_id):
    for d in all_devices():
        if d["id"] == dev_id:
            return d
    return None


def dosers():
    return [d for d in all_devices() if d["kind"] == "doser"]


def primary_doser():
    return get(PRIMARY_DOSER_ID)


def is_primary_doser(dev_id):
    return dev_id == PRIMARY_DOSER_ID


def source():
    """주소의 출처 — 정비페이지가 '어디서 온 값인지'를 보여 준다."""
    return "file" if _read_json(DEVICES_FILE) else ("bt.json" if _read_json(BT_FILE)
                                                    else "config")


def validate(obj):
    """저장 형태({"devices": [...]})가 쓸 수 있는 값인지 — (ok, 사유).
    백업 복원(archive._validate)이 쓴다: 깨진 목록을 되돌리면 장비에 못 붙는다."""
    raw = obj.get("devices") if isinstance(obj, dict) else obj
    _devs, err = _normalize_list(raw)
    return (False, err) if err else (True, "")


def set_devices(raw, log=None):
    """웹에서 온 장치 목록 저장 — 전부 검증을 통과해야 하나라도 쓴다. (ok, 메시지).
    ★부분 적용을 하지 않는 이유: 주소 절반만 바뀐 상태로 전환이 일어나면 어느 장비에
    붙는지 예측할 수 없다."""
    if isinstance(raw, dict):
        raw = raw.get("devices")
    devs, err = _normalize_list(raw)
    if err:
        return False, err
    try:
        tmp = DEVICES_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"devices": devs}, f)
        import os
        os.rename(tmp, DEVICES_FILE)
    except OSError as e:
        return False, "저장 실패: %r" % e
    global _cache
    _cache = devs                    # 다음 전환부터 새 주소로 붙는다
    parts = []
    for d in devs:
        bits = d["name"] + ("=" + d["addr"] if d["addr"] else "=미설정")
        if d.get("pswd"):
            bits += "/암호 설정됨"      # ★값은 로그에 남기지 않는다
        if d["kind"] == "doser":
            bits += "/동기 " + (",".join("%d시" % h for h in d["sync_hours"])
                                if d["sync_hours"] else "없음")
        parts.append(bits)
    msg = "장치 목록 저장 — " + ", ".join(parts)
    if log:
        log("[조치] " + msg)
    return True, msg
