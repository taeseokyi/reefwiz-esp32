# WiFi 설정·연결 — 재플래싱 없이 현장에서 공유기를 바꿀 수 있게 한다.
#
# 동작:
#   1) /data/wifi.json 의 저장된 설정으로 접속 시도(없으면 config.WIFI_SSID/PASS 폴백).
#   2) 실패하면 AP 모드로 올린다 — 폰으로 'reefwiz-setup' 에 붙어 http://192.168.4.1/ops.html
#      의 WiFi 카드에서 공유기를 골라 저장하면 즉시 재접속한다(설정 페이지는 같은 웹서버가
#      AP 인터페이스에서도 서빙 — 소켓이 0.0.0.0 바인딩이라 추가 작업 불요).
#   3) 접속되면 AP 는 내린다. 운전 중 끊기면 메인 루프가 재접속하고, 계속 실패하면 다시 AP.
#
# ★AP 폴백이 중요한 이유: 이 장치는 LAN 전용(외부 접근 없음)이고 **화면도 없다**(2026-08-18
#   결정) — WiFi 가 안 붙으면 손댈 방법이 아예 없다. AP 모드가 유일한 백도어다.
import json
import os
import time

import network

import config

WIFI_FILE = config.DATA_DIR + "/wifi.json"
AP_SSID = "reefwiz-setup"
AP_PASS = "reefwiz1234"        # WPA2 최소 8자
AP_IP = "192.168.4.1"

_sta = None
_ap = None


def _load():
    """저장된 설정 — 없으면 config 폴백. 반환: (ssid, password)."""
    try:
        with open(WIFI_FILE) as f:
            d = json.load(f)
        ssid = (d.get("ssid") or "").strip()
        if ssid:
            return ssid, d.get("pass") or ""
    except (OSError, ValueError):
        pass
    return (config.WIFI_SSID or "").strip(), config.WIFI_PASS or ""


def save(ssid, password):
    """설정 저장 — 비밀번호는 평문(LAN 전용 기기, 로컬 파일). 반환: (ok, msg)."""
    ssid = (ssid or "").strip()
    if not ssid:
        return False, "SSID 가 비었습니다"
    if password and len(password) < 8:
        return False, "WPA 비밀번호는 8자 이상이어야 합니다(개방망이면 비워두세요)"
    tmp = WIFI_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"ssid": ssid, "pass": password or ""}, f)
        os.rename(tmp, WIFI_FILE)
    except OSError as e:
        return False, "저장 실패: %r" % e
    return True, "저장됨 — '%s' 로 접속을 시도합니다" % ssid


def sta():
    global _sta
    if _sta is None:
        _sta = network.WLAN(network.STA_IF)
    return _sta


def ap():
    global _ap
    if _ap is None:
        _ap = network.WLAN(network.AP_IF)
    return _ap


def is_connected():
    try:
        return sta().isconnected()
    except OSError:
        return False


def ip():
    try:
        return sta().ifconfig()[0] if is_connected() else None
    except OSError:
        return None


def scan():
    """주변 AP 목록 — [{ssid, rssi, secure}] (신호 순, 중복 SSID 제거)."""
    w = sta()
    w.active(True)
    try:
        nets = w.scan()
    except OSError as e:
        return [], "스캔 실패: %r" % e
    seen = {}
    for n in nets:
        try:
            ssid = n[0].decode("utf-8")
        except (UnicodeError, AttributeError):
            continue
        if not ssid:
            continue
        rssi, authmode = n[3], n[4]
        if ssid not in seen or rssi > seen[ssid]["rssi"]:
            seen[ssid] = {"ssid": ssid, "rssi": rssi, "secure": authmode != 0}
    out = sorted(seen.values(), key=lambda d: -d["rssi"])
    return out, None


def start_ap():
    """설정용 AP 를 올린다 — 접속 실패 시의 유일한 접근 경로."""
    a = ap()
    if a.active() and a.config("essid") == AP_SSID:
        return
    a.active(True)
    try:
        a.config(essid=AP_SSID, password=AP_PASS, authmode=network.AUTH_WPA_WPA2_PSK)
    except (OSError, ValueError, AttributeError):
        try:
            a.config(essid=AP_SSID, password=AP_PASS)
        except OSError:
            pass
    print("[wifi] AP 모드 — SSID '%s' / PW '%s' → http://%s/ops.html"
          % (AP_SSID, AP_PASS, AP_IP))


def stop_ap():
    try:
        if ap().active():
            ap().active(False)
            print("[wifi] AP 종료(STA 접속됨)")
    except OSError:
        pass


def connect(timeout=25, ssid=None, password=None):
    """STA 접속 시도. 성공 True. 자격은 인자 우선, 없으면 저장된 설정."""
    if ssid is None:
        ssid, password = _load()
    if not ssid:
        print("[wifi] 저장된 SSID 없음 — AP 모드로 설정 필요")
        return False
    w = sta()
    w.active(True)
    if w.isconnected():
        try:
            if w.config("essid") == ssid:
                return True
        except (OSError, ValueError):
            return True
        w.disconnect()
        time.sleep(1)
    print("[wifi] connecting to '%s' ..." % ssid)
    try:
        w.connect(ssid, password)
    except OSError as e:
        print("[wifi] connect 오류: %r" % e)
        return False
    for _ in range(timeout):
        if w.isconnected():
            break
        time.sleep(1)
    if not w.isconnected():
        print("[wifi] '%s' 접속 실패" % ssid)
        return False
    print("[wifi] %s (%s)" % (w.ifconfig()[0], ssid))
    try:
        network.hostname("reefwiz")            # http://reefwiz.local (mDNS)
    except (AttributeError, OSError):
        pass
    return True


def ensure(timeout=25):
    """접속 보장 — 되면 AP 를 내리고 True, 안 되면 AP 를 올리고 False.
    메인 루프가 매 틱 호출해도 안전하다(이미 접속돼 있으면 즉시 리턴)."""
    if is_connected():
        stop_ap()
        return True
    if connect(timeout=timeout):
        stop_ap()
        return True
    start_ap()
    return False


def ap_is_active():
    """AP 폴백이 떠 있는가 — netmaint 가 state.ap_active 를 갱신할 때 쓴다."""
    try:
        return bool(ap().active())
    except OSError:
        return False


def status():
    """웹 표시용 상태."""
    ssid, _pw = _load()
    st = {"connected": is_connected(), "ip": ip(), "saved_ssid": ssid,
          "ap_active": False, "ap_ssid": AP_SSID, "ap_pass": AP_PASS, "ap_ip": AP_IP}
    try:
        st["ap_active"] = bool(ap().active())
    except OSError:
        pass
    if st["connected"]:
        try:
            st["rssi"] = sta().status("rssi")
        except (OSError, ValueError, AttributeError):
            pass
    return st
