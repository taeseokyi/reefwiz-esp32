# ★vendored: mpy-webota v1.2.0 (device/webota_auth.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota_auth — 기기를 바꾸는 작업마다 **GitHub 로그인 확인**(OAuth 기기 흐름, 1.2.0).
#
# ★왜(사용자 결정 2026-09-25): 기기 토큰이 새면 서명된 옛 판으로 되돌리기 · 데이터 초기화 · WiFi 를
#   딴 망으로 돌리기가 된다. 그래서 설치 · 되돌리기 · 초기화 · 정리 · (공유기 쪽) WiFi 변경은 기기
#   토큰에 더해 **허용된 GitHub 계정의 승인**을 매번 받는다. 한 번 승인은 한 작업에만 쓴다.
#
# 흐름(RFC 8628 · GitHub Device Flow):
#   start  → 기기가 github.com/login/device/code 에 물어 확인 코드(user_code)를 받아 화면에 보인다
#   사람   → github.com/login/device 에서 코드를 넣고 승인(평소 GitHub 로그인 · 2단계 인증 그대로)
#   poll   → 기기가 access_token 을 받아 api.github.com/user 로 **누가** 승인했는지만 보고, 허용 목록
#            (USB 로 심은 github_auth.owners)에 있으면 승인 — 토큰은 버린다(파일에 쓰지 않는다)
#   consume→ 작업이 그 승인을 한 번 쓴다(작업 이름이 같아야 · 승인 뒤 APPROVED_TTL_S 안에)
# ★모든 통신은 인증서를 검증한 TLS(webota_pkg) — 가짜 GitHub 으로 속일 수 없다. 토큰은 권한 범위를
#   요청하지 않는다(scope 없음) — 누가 승인했는지 읽는 것밖에 못 한다.
# 설정(/webota.json — USB 로만): "github_auth": {"client_id": "<OAuth App Client ID — 공개 정보>",
#                                               "owners": ["github-login", ...]}
import time

import webota_pkg as pkg

WEB = "https://github.com"
API = "https://api.github.com"
APPROVED_TTL_S = 180          # 승인 뒤 이 안에 작업을 해야 한다
MAX_PENDING = 8
_pending = {}                 # auth_id → 상태(메모리에만)


def _now():
    try:
        return time.ticks_ms() // 1000
    except AttributeError:
        return int(time.monotonic())


def conf(cfg):
    ga = cfg.get("github_auth") or {}
    if ga.get("client_id") and ga.get("owners"):
        return ga
    return None


def _id():
    import os
    import binascii
    return binascii.hexlify(os.urandom(8)).decode()


def start(cfg, action):
    """승인 요청을 연다 → {auth_id, user_code, verification_uri, interval, expires_in}."""
    ga = conf(cfg)
    if not ga:
        raise OSError("GitHub 확인이 설정되지 않았다(github_auth — USB 로 심는다)")
    r = pkg.post_form(ga.get("web", WEB) + "/login/device/code", {"client_id": ga["client_id"], "scope": ""})
    if "device_code" not in r:
        raise OSError("GitHub 이 확인 코드를 주지 않았다: %s" % (r.get("error_description") or r.get("error") or r))
    now = _now()
    for k in [k for k, v in _pending.items() if v["expires"] < now]:
        del _pending[k]
    while len(_pending) >= MAX_PENDING:
        del _pending[next(iter(_pending))]
    aid = _id()
    _pending[aid] = {"device_code": r["device_code"], "interval": int(r.get("interval", 5)), "action": action,
                     "expires": now + int(r.get("expires_in", 900)), "login": None, "approved_at": None,
                     "used": False, "next_poll": 0}
    return {"auth_id": aid, "user_code": r.get("user_code"), "action": action,
            "verification_uri": r.get("verification_uri", "https://github.com/login/device"),
            "interval": int(r.get("interval", 5)), "expires_in": int(r.get("expires_in", 900))}


def poll(cfg, aid):
    """→ {"state": pending | approved | denied | expired, "login"?, "err"?}"""
    p = _pending.get(aid)
    if not p:
        return {"state": "expired", "err": "알 수 없는 승인 요청(기기가 재부팅됐거나 오래됐다)"}
    now = _now()
    if p["approved_at"] is not None:
        return {"state": "approved", "login": p["login"]}
    if now > p["expires"]:
        del _pending[aid]
        return {"state": "expired", "err": "확인 코드가 만료됐다 — 다시 시작한다"}
    if now < p["next_poll"]:
        return {"state": "pending"}                 # GitHub 이 정한 간격보다 자주 묻지 않는다
    ga = conf(cfg)
    r = pkg.post_form(ga.get("web", WEB) + "/login/oauth/access_token",
                      {"client_id": ga["client_id"], "device_code": p["device_code"],
                       "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
    err = r.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            p["interval"] = int(r.get("interval", p["interval"] + 5))
        p["next_poll"] = now + p["interval"]
        return {"state": "pending"}
    if err:
        del _pending[aid]
        return {"state": "denied" if err == "access_denied" else "expired",
                "err": r.get("error_description") or err}
    token = r.get("access_token")
    if not token:
        del _pending[aid]
        return {"state": "denied", "err": "GitHub 응답에 토큰이 없다"}
    user = pkg.get_json(ga.get("api", API) + "/user", accept="application/vnd.github+json", token=token,
                        token_hosts=None)   # 주소는 USB 로 심은 것(기본 api.github.com)
    token = None                                    # 버린다 — 누가 승인했는지만 필요하다
    login = (user or {}).get("login")
    owners = [o.lower() for o in ga.get("owners") or []]
    if not login or login.lower() not in owners:
        del _pending[aid]
        return {"state": "denied", "err": "허용되지 않은 GitHub 계정이다: %s" % login}
    p["login"], p["approved_at"] = login, now
    return {"state": "approved", "login": login}


def consume(cfg, aid, action):
    """작업 직전에 부른다 — (ok, 메시지, login). 승인은 한 번만 · 같은 작업에만 · 승인 뒤 제한 시간 안에."""
    if not conf(cfg):
        return True, "", None                       # GitHub 확인을 쓰지 않는 기기(설정 없음)
    p = _pending.get(aid or "")
    if not p or p["approved_at"] is None:
        return False, "GitHub 확인이 필요하다(승인되지 않았다)", None
    if p["used"]:
        return False, "이미 쓴 승인이다 — 작업마다 새로 승인한다", None
    if p["action"] != action:
        return False, "다른 작업을 위한 승인이다(%s ≠ %s)" % (p["action"], action), None
    if _now() - p["approved_at"] > APPROVED_TTL_S:
        del _pending[aid]
        return False, "승인이 오래됐다 — 다시 승인한다", None
    p["used"] = True
    del _pending[aid]
    return True, "", p["login"]
