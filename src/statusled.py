# 온보드 RGB LED 경고등 — 헤드리스 장비의 로컬 상태 표시(2026-08-26 사용자 요청).
#
# ★평상시 소등이 원칙. 경고가 있을 때만 켠다:
#   RED  깜빡임 = 치명(측정이 멈췄거나 위협받음) — 에러 래치·링크 동결·시각 미동기(자동측정 게이트 닫힘)
#   AMBER 상시  = 경고(측정은 되지만 무언가 저하) — WiFi 끊김/AP 모드·BT 대상 미검증
#   OFF         = 정상
# 색·핀은 실측 확인: GPIO48, (r,g,b) 매핑 정상(빨강/초록/파랑 순서 확인 2026-08-26).
#
# ★메인 스레드가 매 틱 render() 를 부른다(측정 중 장기 블로킹 구간에는 갱신이 멈추지만,
#   그때 상태는 잘 바뀌지 않고 종료 후 즉시 반영된다). LED 실패는 무해하게 삼킨다.
import config

_np = None


def init():
    """LED 준비 + 소등. 실패해도(핀 없음/모듈 없음) 조용히 비활성으로 둔다."""
    global _np
    try:
        import machine
        import neopixel
        _np = neopixel.NeoPixel(machine.Pin(config.LED_PIN), 1)
        off()
    except Exception as e:
        _np = None
        print("[led] 비활성(무해): %r" % e)


def _set(rgb):
    if _np is None:
        return
    try:
        _np[0] = rgb
        _np.write()
    except Exception:
        pass


def off():
    _set((0, 0, 0))


def boot_blip():
    """부팅 확인용 짧은 초록 점멸 1회 — LED 가 살아 있음을 알린다."""
    import time
    b = config.LED_BRIGHT
    _set((0, b, 0))
    time.sleep(0.25)
    off()


def render(critical, warning, blink_on):
    """우선순위: 치명(RED 깜빡) > 경고(AMBER 상시) > 정상(OFF)."""
    b = config.LED_BRIGHT
    if critical:
        _set((b, 0, 0) if blink_on else (0, 0, 0))
    elif warning:
        _set((b, (b * 2) // 3, 0))     # 앰버
    else:
        off()
