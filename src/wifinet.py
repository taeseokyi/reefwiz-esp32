# WiFi 상태(읽기 전용) — ★WiFi 는 webota 가 전담한다(mpy-webota webota_net, 2026-09-24).
#
# 종전에는 이 모듈이 /data/wifi.json 으로 접속하고, 실패하면 AP 'reefwiz-setup' 을 올리고,
# 정비페이지 WiFi 카드로 공유기를 바꿨다. WiFi 는 원격 배포·설치 화면이 기기에 닿는 길 그
# 자체라 앱이 아니라 webota 가 맡는다 — 앱이 죽어도 WiFi·설정용 AP 는 살아 있어야 한다.
#   설정: webota 설치 화면 http://<기기>:8266/ 의 WiFi 카드(설정용 AP 로 붙으면 토큰 없이).
#   AP:   /webota.json 의 ap(reefwiz-setup / reefwiz1234) → http://192.168.4.1:8266/
# 이 모듈은 앱 쪽(정비페이지 상태·LED·NTP 게이트)이 읽을 상태만 준다. 접속·AP 는 만지지 않는다.
try:
    import webota_net as _net
except ImportError:                 # webota 없이 돌 때(옛 배치) — 상태를 모른다
    _net = None


def is_connected():
    return bool(_net and _net.is_connected())


def ip():
    return _net.ip() if _net else None


def ap_is_active():
    return bool(_net and _net.ap_active())


def status():
    """정비페이지 표시용 — webota_net.status() 를 옛 필드 이름으로도 준다."""
    st = _net.status() if _net else {"connected": False}
    st["saved_ssid"] = st.get("ssid")
    st["setup"] = "webota 설치 화면 :8266 의 WiFi 카드"
    return st
