# 워치독 — 진짜 행(hang)에서만 자동 하드리셋한다(2026-08-26 장기운영 검토).
#
# ★불변식 ①: WDT 는 **메인 스레드**(측정·스케줄·잡)의 진행만 감시한다. 웹 스레드
#   (webserver._serve/_handle/_api, link.status)에서는 **절대 feed 하지 않는다** — 메인이
#   멈췄는데 웹만 살아서 feed 하면 WDT 가 안 터져 감시가 무의미해진다.
# ★불변식 ②: 한 번 켜면 끄거나 재설정할 수 없다(MicroPython WDT 는 deinit 없음). 그래서
#   enable() 은 부팅(WiFi/NTP/BT 자동연결)이 끝나고 스케줄 루프 진입 직후 **한 번만** 부른다
#   — 느린 부팅 구간에서의 오작동을 피한다.
# ★간접층인 이유: measure/link/doser/ops 가 machine 의존 없이 feed() 만 부르게 해서 PC
#   테스트 import 를 보존하고, config.WDT_ENABLED 로 on/off 한다.
import config

_wdt = None


def enable():
    """config.WDT_ENABLED 면 WDT 를 켠다(한 번만). 실패해도 그냥 진행한다(감시 없이 동작)."""
    global _wdt
    if _wdt is not None or not getattr(config, "WDT_ENABLED", False):
        return
    try:
        from machine import WDT          # 기기 전용 — CPython(테스트)엔 없다
        _wdt = WDT(timeout=config.WDT_TIMEOUT_MS)
        print("[wdt] 활성 — 타임아웃 %dms" % config.WDT_TIMEOUT_MS)
    except Exception as e:
        print("[wdt] 활성 실패(감시 없이 진행): %r" % e)


def feed():
    """WDT 가 켜져 있으면 급여, 아니면 no-op. ★메인 스레드의 장기 블로킹 루프 안에서만 부른다."""
    if _wdt is not None:
        try:
            _wdt.feed()
        except Exception:
            pass


def enabled():
    return _wdt is not None
