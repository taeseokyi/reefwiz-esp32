# dkh.dat 한 줄의 단일 파서·포매터 — 원본 reefwiz/bin/dkh_dat.py 이식(2026-08-16, bc3e763).
#
# 형식 (공백 구분, 한 줄에 한 측정):
#   ★신형식(2026-08-16~): YYYY-MM-DD HH ref_pH tank_pH ref_kh tank_kh temp
#     2026-08-16 05 7.735 7.678 8.830 7.746 28.9
#   구형식(그 이전):      HH ref_pH tank_pH ref_kh tank_kh temp
#     05 7.735 7.678 8.830 7.746 28.9
#
# 날짜 컬럼을 넣은 이유(원본): 파일에 시각(HH)만 있으면 "최근 N일"을 하루 3회 가정의
# **회차 근사**로 셀 수밖에 없다 — 측정이 빠진 날이 있으면 창이 과거로 늘어나고, 추가
# 측정을 돌린 날이 있으면 창 안쪽이 밀려나 잘린다. 날짜를 원본에 적으면 모든 소비자가
# 같은 사실을 직접 읽는다. 날짜를 되살리는 추측(회차 근사)은 전부 폐기한다.
#
# ★이식본에서 특히 중요한 이유: 종전 포트 코드는 parts[4]=tank_kh 처럼 **위치로** 읽었다.
#   날짜가 붙은 줄을 그 코드가 읽으면 한 칸씩 밀려 tank_kh 대신 ref_kh 를 반환한다
#   (원본도 같은 사고를 겪어 파서 경유로 바꿨다 — bc3e763 커밋 메시지 참조).
#   그래서 dkh.dat 를 읽는 모든 지점은 반드시 이 모듈을 거친다.
#
# 쓰기는 항상 신형식이다. 읽기만 구형식을 함께 받는다 — 구형식 행은 date=None.
# 관용 파싱을 남기는 이유: 손대지 않은 구형식 백업 사본이 마지막 줄에 에러 표식을 갖고
# 있어도 에러 래치가 그걸 읽어내야 한다.
#
# 특수 표식(값 규약은 종전과 동일):
#   - 5개 값 전부 0.000 → 에러 표식(측정 실패/타임아웃/KCl 소크 실패)
#   - tank_kh 가 음수   → 평탄(평형) 미도달. 크기는 유지되므로 abs() 로 값만 취한다
#
# ★MicroPython 주의: re 모듈이 {n} 반복 수량자를 지원하지 않아 원본의 DATE_RE 대신
#   자릿수·구분자 직접 검사로 날짜를 판별한다(동작은 동일).

FIELDS = ("hh", "ref_ph", "tank_ph", "ref_kh", "tank_kh", "temp")
_VALUE_KEYS = ("ref_ph", "tank_ph", "ref_kh", "tank_kh", "temp")


def is_date(tok):
    """'YYYY-MM-DD' 모양인가 — 형식 판별의 단일 지점(원본 DATE_RE 상당)."""
    if not tok or len(tok) != 10 or tok[4] != "-" or tok[7] != "-":
        return False
    for i in (0, 1, 2, 3, 5, 6, 8, 9):
        if not ("0" <= tok[i] <= "9"):
            return False
    return True


def split_date(parts):
    """(날짜 문자열 또는 None, 날짜를 뺀 나머지 필드)."""
    if parts and is_date(parts[0]):
        return parts[0], list(parts[1:])
    return None, list(parts)


def parse_parts(parts):
    """이미 split() 된 한 줄 → dict. 필드 부족·형식 오류면 None.

    반환: {date(str|None), hh(int), ref_ph, tank_ph, ref_kh, tank_kh, temp(float),
           is_error(bool: 5개 값 전부 0), is_flat(bool: tank_kh 가 음수가 아님)}
    """
    day, rest = split_date(parts)
    if len(rest) < 6:
        return None
    try:
        row = {
            "date": day, "hh": int(rest[0]),
            "ref_ph": float(rest[1]), "tank_ph": float(rest[2]),
            "ref_kh": float(rest[3]), "tank_kh": float(rest[4]), "temp": float(rest[5]),
        }
    except ValueError:
        return None
    row["is_error"] = all(row[k] == 0.0 for k in _VALUE_KEYS)
    row["is_flat"] = row["tank_kh"] >= 0
    return row


def parse(line):
    """한 줄(문자열) → dict 또는 None."""
    return parse_parts(line.split())


def format_line(day, hour, ref_ph, tank_ph, ref_kh, tank_kh, temp):
    """한 줄 문자열(개행 없음). day=None 이면 구형식으로 쓴다(테스트·호환용)."""
    body = "%02d %.3f %.3f %.3f %.3f %.1f" % (hour, ref_ph, tank_ph, ref_kh, tank_kh, temp)
    return ("%s %s" % (day, body)) if day else body


def day_ordinal(day):
    """'YYYY-MM-DD' → 일 단위 정수(그레고리력 서수). 날짜 차이 계산용.

    ★MicroPython 에는 datetime.date 가 없다. 창을 자를 때 필요한 건 두 날짜의 **차이**
    뿐이므로, CPython date.toordinal() 과 같은 값을 직접 계산한다(윤년 규칙 포함).
    형식이 깨졌으면 None.
    """
    if not is_date(day):
        return None
    y, m, d = int(day[0:4]), int(day[5:7]), int(day[8:10])
    if not (1 <= m <= 12) or not (1 <= d <= 31):
        return None
    # 3월을 연초로 옮겨 윤년 분기를 없애는 표준 기법(달 길이 누적식이 단조가 된다).
    a = (14 - m) // 12
    y2 = y + 4800 - a
    m2 = m + 12 * a - 3
    jdn = d + (153 * m2 + 2) // 5 + 365 * y2 + y2 // 4 - y2 // 100 + y2 // 400 - 32045
    return jdn - 1721425      # 0001-01-01 을 1 로 맞춤 = date.toordinal()


def load(path):
    """파일 전체 → [dict] (파싱 실패 줄은 건너뜀). 각 행에 1-base 줄번호 line 포함."""
    rows = []
    try:
        with open(path) as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                row = parse(line)
                if row is not None:
                    row["line"] = lineno
                    rows.append(row)
    except OSError:
        return []
    return rows
