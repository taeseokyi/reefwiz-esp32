# KST 시각 헬퍼 — CPython datetime 대체
import time
import config


def now_tuple():
    """KST localtime 튜플 (y, m, d, hh, mm, ss, wd, yd)."""
    return time.localtime(time.time() + config.TZ_OFFSET_S)


def hour():
    return now_tuple()[3]


def stamp():
    """'YYYY-MM-DD HH:MM:SS' — 로그/이력 타임스탬프."""
    t = now_tuple()
    return "%04d-%02d-%02d %02d:%02d:%02d" % (t[0], t[1], t[2], t[3], t[4], t[5])


def date_str():
    t = now_tuple()
    return "%04d-%02d-%02d" % (t[0], t[1], t[2])


def iso_id():
    """오버라이드 id 용 — 원본은 브라우저 ISO 문자열이었음. 유일성만 있으면 됨."""
    t = now_tuple()
    return "%04d-%02d-%02dT%02d:%02d:%02d+09:00" % (t[0], t[1], t[2], t[3], t[4], t[5])
