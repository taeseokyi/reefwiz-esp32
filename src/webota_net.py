# ★vendored: mpy-webota v1.2.0 (device/webota_net.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota_net — WiFi 는 webota 가 전담한다(앱은 WiFi 를 만지지 않는다).
#
# ★왜 webota 인가(사용자 결정 2026-09-24): WiFi 는 원격 배포·설치 화면이 기기에 닿는 길 그 자체다.
#   앱마다 접속·AP 폴백을 다시 만들 필요가 없고, 앱이 죽어도 이 길은 살아 있어야 한다.
#
# 설정(/webota.json):
#   "wifi": {"ssid": "...", "pass": "..."}      접속할 공유기 — 설치 화면 WiFi 카드에서 저장한다
#   "ap":   {"ssid": "webota-XXXX", "pass": "webota1234"}   설정용 AP(기본값: MAC 뒤 4자리)
#   "hostname": "myapp"                          mDNS 이름(http://myapp.local) — 선택
#   (옛 "wifi_file": 앱이 쓰던 {ssid, pass} JSON — 첫 부팅에 "wifi" 로 한 번 옮겨 온다)
#
# 동작:
#   부팅   STA 접속을 wifi_timeout_s 동안 시도 → 실패면 AP 를 올린다(설정 없으면 곧바로 AP).
#   유지   net 스레드: 끊기면 30초마다 재접속, 접속이 AP_AFTER_S 넘게 안 되면 AP. STA 가 붙고
#          AP_LINGER_S 가 지나면 AP 를 내린다(방금 설정한 휴대폰이 새 주소를 볼 시간).
#   설정   AP 로 접속한 기기(192.168.4.x)는 **토큰 없이** WiFi 설정만 할 수 있다 — AP 비밀번호가
#          인증이다. 처음 설정할 때 토큰을 몰라도 된다.
# 앱용(읽기 전용): is_connected() · ip() · ap_active() · status()
import time

import webota_boot as wb

AP_AFTER_S = 45          # 이만큼 STA 가 안 붙으면 AP 를 올린다(부팅 뒤·끊긴 뒤)
AP_LINGER_S = 60         # STA 가 붙은 뒤 AP 를 이만큼 더 둔다
RETRY_S = 30             # 끊겨 있을 때 재접속 간격
_sta = None
_ap = None
_cfg = None
_down_since = None
_up_since = None
_last_try = None
_kick = False            # 새 자격증명 저장 → 곧바로 재접속
_ntp_at = None           # 마지막 NTP 성공(가동 초)
_ntp_try = None


def ntp():
    """NTP 로 RTC 를 UTC 로 맞춘다 — ★GitHub TLS 인증서의 유효 기간을 보려면 시각이 필요하다
    (1.0.0: 네트워크의 주인이 webota 이므로 NTP 도 webota 가 한다). 앱이 따로 맞춰도 둘 다 UTC 라
    어긋나지 않는다. 실패는 False."""
    global _ntp_at, _ntp_try
    _ntp_try = _now()
    if not is_connected():
        return False
    try:
        import ntptime
        try:
            ntptime.timeout = 2
        except Exception:
            pass
        ntptime.settime()
        _ntp_at = _now()
        print("[webota] NTP 시각 맞춤 — %04d-%02d-%02d %02d:%02d UTC" % time.localtime()[:5])
        return True
    except Exception as e:
        print("[webota] NTP 실패: %r" % e)
        return False


def _now():
    try:
        return time.ticks_ms() // 1000
    except AttributeError:
        return int(time.monotonic())


def _net():
    try:
        import network
        return network
    except ImportError:
        return None              # PC(CPython) — 네트워크 없음


def sta():
    global _sta
    n = _net()
    if n is None:
        return None
    if _sta is None:
        _sta = n.WLAN(n.STA_IF)
    return _sta


def ap():
    global _ap
    n = _net()
    if n is None:
        return None
    if _ap is None:
        _ap = n.WLAN(n.AP_IF)
    return _ap


def is_connected():
    try:
        s = sta()
        return bool(s and s.isconnected())
    except OSError:
        return False


def ip():
    try:
        return sta().ifconfig()[0] if is_connected() else None
    except OSError:
        return None


def ap_active():
    try:
        a = ap()
        return bool(a and a.active())
    except OSError:
        return False


def ap_ip():
    try:
        return ap().ifconfig()[0] if ap_active() else None
    except OSError:
        return None


def from_ap(peer_ip):
    """요청이 설정용 AP 로 붙은 기기에서 왔나 — 같은 /24 대역이면 그렇다."""
    a = ap_ip()
    if not a or not peer_ip:
        return False
    return peer_ip.rsplit(".", 1)[0] == a.rsplit(".", 1)[0]


# ── 설정 ──

def creds(c):
    """(ssid, pass) — "wifi" 가 없고 옛 "wifi_file" 이 있으면 옮겨 온다(한 번)."""
    w = c.get("wifi") or {}
    if w.get("ssid"):
        return w["ssid"], w.get("pass") or ""
    wf = c.get("wifi_file")
    if wf:
        d = wb.read_json(wf, {}) or {}
        keys = c.get("wifi_keys") or ["ssid", "pass"]
        if d.get(keys[0]):
            save(c, d[keys[0]], d.get(keys[1]) or "", kick=False)
            print("[webota] WiFi 설정을 %s 에서 /webota.json 으로 옮겼다" % wf)
            return d[keys[0]], d.get(keys[1]) or ""
    return None, None


def save(c, ssid, pw, kick=True):
    """자격증명을 /webota.json 에 저장(다른 키는 그대로) · 곧바로 재접속."""
    global _kick
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "SSID 가 비었다"
    disk = wb.read_json("/webota.json", {}) or {}
    disk["wifi"] = {"ssid": ssid, "pass": pw or ""}
    wb.write_json("/webota.json", disk)
    c["wifi"] = disk["wifi"]
    if kick:
        _kick = True
    return True, "저장됨 — '%s' 로 접속을 시도한다" % ssid


def _ap_conf(c):
    a = c.get("ap") or {}
    ssid = a.get("ssid")
    if not ssid:
        try:
            import binascii
            mac = binascii.hexlify(ap().config("mac")).decode()
            ssid = "webota-" + mac[-4:]
        except Exception:
            ssid = "webota-setup"
    return ssid, a.get("pass") or "webota1234"


# ── 동작 ──

def connect(c, wait_s=0):
    """STA 접속 시작. wait_s>0 이면 그만큼 기다려 결과를 돌려준다."""
    global _last_try
    s = sta()
    if s is None:
        return False
    ssid, pw = creds(c)
    if not ssid:
        return False
    _last_try = _now()
    try:
        s.active(True)
        if c.get("hostname"):
            try:
                _net().hostname(c["hostname"])
            except (AttributeError, OSError, ValueError):
                pass
        if s.isconnected():
            try:
                if s.config("essid") == ssid:
                    return True
            except (OSError, ValueError):
                return True
            s.disconnect()
        print("[webota] WiFi '%s' 접속 중…" % ssid)
        s.connect(ssid, pw)
    except OSError as e:
        print("[webota] WiFi 오류: %r" % e)
        return False
    t = _now()
    while wait_s and _now() - t < wait_s:
        if s.isconnected():
            print("[webota] WiFi 접속 — %s" % ip())
            return True
        time.sleep(0.25)
    return s.isconnected()


def start_ap(c):
    a = ap()
    if a is None or ap_active():
        return
    ssid, pw = _ap_conf(c)
    n = _net()
    try:
        a.active(True)
        try:
            a.config(essid=ssid, password=pw, authmode=n.AUTH_WPA_WPA2_PSK)
        except (OSError, ValueError, AttributeError):
            a.config(essid=ssid, password=pw)
        print("[webota] AP 모드 — '%s' (비번 %s) → http://%s:%s/" % (ssid, pw, ap_ip(), c.get("port")))
    except OSError as e:
        print("[webota] AP 실패: %r" % e)


def stop_ap():
    try:
        if ap_active():
            ap().active(False)
            print("[webota] AP 종료(WiFi 접속됨)")
    except OSError:
        pass


def boot(c):
    """부팅 때(런처): 접속을 wifi_timeout_s 동안 시도, 안 되면 AP."""
    global _cfg, _down_since
    _cfg = c
    if sta() is None:
        return False
    ok = connect(c, wait_s=int(c.get("wifi_timeout_s") or 20)) if creds(c)[0] else False
    if ok:
        ntp()
    if not ok:
        _down_since = _now() - AP_AFTER_S          # 부팅 때 못 붙었으면 곧바로 AP
        start_ap(c)
    return ok


def tick(c):
    """net 스레드가 몇 초마다 — 재접속 · AP 올리기/내리기."""
    global _down_since, _up_since, _kick
    if sta() is None:
        return
    now = _now()
    if is_connected():
        _down_since = None
        if _up_since is None:
            _up_since = now
        if ap_active() and now - _up_since >= AP_LINGER_S:
            stop_ap()
        # 시각: 아직 못 맞췄으면 1분마다, 맞췄으면 하루에 한 번
        if (_ntp_at is None and (_ntp_try is None or now - _ntp_try >= 60)) or \
                (_ntp_at is not None and now - _ntp_at >= 86400):
            ntp()
        return
    _up_since = None
    if _down_since is None:
        _down_since = now
    if _kick or _last_try is None or now - _last_try >= RETRY_S:
        _kick = False
        connect(c)
    if not ap_active() and now - _down_since >= AP_AFTER_S:
        start_ap(c)


def scan():
    """주변 AP [{ssid, rssi, secure}] — 신호 순, 중복 SSID 제거."""
    s = sta()
    if s is None:
        return [], "네트워크 없음"
    try:
        s.active(True)
        nets = s.scan()
    except OSError as e:
        return [], "스캔 실패: %r" % e
    seen = {}
    for n in nets:
        try:
            ssid = n[0].decode("utf-8")
        except (UnicodeError, AttributeError):
            continue
        if ssid and (ssid not in seen or n[3] > seen[ssid]["rssi"]):
            seen[ssid] = {"ssid": ssid, "rssi": n[3], "secure": n[4] != 0}
    return sorted(seen.values(), key=lambda d: -d["rssi"]), None


def status(c=None):
    c = c or _cfg or {}
    st = {"connected": is_connected(), "ip": ip(), "ssid": (c.get("wifi") or {}).get("ssid"),
          "ap_active": ap_active(), "ap_ip": ap_ip(), "ap_ssid": _ap_conf(c)[0] if ap() else None,
          "hostname": c.get("hostname"), "ntp": _ntp_at is not None}
    if st["connected"]:
        try:
            st["rssi"] = sta().status("rssi")
        except (OSError, ValueError, AttributeError):
            pass
    return st
