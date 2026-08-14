# 디스플레이 드라이버 — ★참조 구현(임시 하드웨어용). 교체 대상 파일.
#
# 대상: reefCore Checker R2 구성 = ILI9341 240x320 SPI TFT + XPT2046 저항막 터치.
#       (사용자 2026-08-14: 최종은 더 고성능 보드 + 더 큰 화면 → 그때 이 파일만 교체한다.
#        display.py 는 계약만 보고 동작하며 해상도·폰트 배율에 자동 적응한다.)
#
# 왜 직접 구현했나: PSRAM 없는 ESP32-WROOM 은 전체 프레임버퍼(320x240 RGB565=153KB)를
# 못 잡는다. 그래서 framebuf 를 화면 전체가 아니라 *텍스트 한 조각*에만 잡고 즉시 전송한다
# (fill/fill_rect 는 색 반복 전송, show() 는 no-op). 외부 드라이버 의존도 없앴다.
#
# 계약(display.py 헤더 참조): get_display() -> (fb, touch)
#   fb: width/height/fill/fill_rect/rect/text/text_scaled/show
#   touch: get_touch() -> (x, y) | None
#
# ★PSRAM 보드로 교체하면 이 파일 대신 framebuf 기반 드라이버를 써도 된다 — 그 경우
#   show() 가 실제 전송이 되고 display.py 는 수정 없이 그대로 동작한다.
import framebuf
import time
from machine import Pin, SPI

import config

_CHUNK = 512          # fill 전송 청크(픽셀 수) — 1KB 버퍼


class ILI9341:
    """최소 기능 ILI9341 — 초기화 + 윈도우 전송 + 사각형/텍스트."""

    def __init__(self, spi, cs, dc, rst=None, bl=None, width=320, height=240, rotation=90):
        self.spi = spi
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.dc = Pin(dc, Pin.OUT, value=0)
        self.rst = Pin(rst, Pin.OUT, value=1) if rst is not None else None
        self.bl = Pin(bl, Pin.OUT, value=1) if bl is not None else None
        self.width = width
        self.height = height
        self._buf = bytearray(_CHUNK * 2)
        self._reset()
        self._init(rotation)

    # ── 저수준 ──

    def _cmd(self, c, data=None):
        self.cs(0)
        self.dc(0)
        self.spi.write(bytes([c]))
        if data:
            self.dc(1)
            self.spi.write(data)
        self.cs(1)

    def _reset(self):
        if self.rst is None:
            return
        self.rst(0)
        time.sleep_ms(50)
        self.rst(1)
        time.sleep_ms(120)

    def _init(self, rotation):
        # MADCTL: 0x48=세로, 0x28=가로(90도). BGR 비트 포함.
        madctl = 0x28 if rotation in (90, 270) else 0x48
        if rotation in (180, 270):
            madctl ^= 0xC0
        for cmd, data in (
            (0x01, None),                      # soft reset
            (0x28, None),                      # display off
            (0xC0, b"\x23"), (0xC1, b"\x10"),  # power control
            (0xC5, b"\x3e\x28"), (0xC7, b"\x86"),
            (0x36, bytes([madctl])),           # memory access control
            (0x3A, b"\x55"),                   # pixel format RGB565
            (0xB1, b"\x00\x18"),               # frame rate
            (0xB6, b"\x08\x82\x27"),
            (0x26, b"\x01"),
            (0x11, None),                      # sleep out
        ):
            self._cmd(cmd, data)
            if cmd in (0x01, 0x11):
                time.sleep_ms(120)
        self._cmd(0x29)                        # display on

    def _window(self, x, y, w, h):
        x1, y1 = x + w - 1, y + h - 1
        self._cmd(0x2A, bytes([x >> 8, x & 0xFF, x1 >> 8, x1 & 0xFF]))
        self._cmd(0x2B, bytes([y >> 8, y & 0xFF, y1 >> 8, y1 & 0xFF]))
        self.cs(0)
        self.dc(0)
        self.spi.write(b"\x2C")                # memory write
        self.dc(1)

    def _clip(self, x, y, w, h):
        if x < 0:
            w += x
            x = 0
        if y < 0:
            h += y
            y = 0
        w = min(w, self.width - x)
        h = min(h, self.height - y)
        return x, y, w, h

    # ── 계약 구현 ──

    def fill_rect(self, x, y, w, h, color):
        x, y, w, h = self._clip(int(x), int(y), int(w), int(h))
        if w <= 0 or h <= 0:
            return
        hi, lo = (color >> 8) & 0xFF, color & 0xFF
        for i in range(0, _CHUNK * 2, 2):
            self._buf[i] = hi
            self._buf[i + 1] = lo
        self._window(x, y, w, h)
        left = w * h
        mv = memoryview(self._buf)
        while left > 0:
            n = min(left, _CHUNK)
            self.spi.write(mv[:n * 2])
            left -= n
        self.cs(1)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def rect(self, x, y, w, h, color):
        self.fill_rect(x, y, w, 1, color)
        self.fill_rect(x, y + h - 1, w, 1, color)
        self.fill_rect(x, y, 1, h, color)
        self.fill_rect(x + w - 1, y, 1, h, color)

    def _blit_text(self, s, x, y, color, scale, bg):
        """8x8 내장 폰트를 마스크로 받아 (color, bg) 두 색으로 확대 전송.
        버퍼는 한 글자분만 잡는다(8*scale 정사각 — scale 3 이면 1.1KB. 전체 버퍼 불요).
        ★마스크 방식인 이유: 폰트를 color 로 직접 그리면 color=0(검정 글자)이 배경과
        구분되지 않아 배지·버튼의 '검정 글자 + 컬러 배경'이 사라진다."""
        cw = 8 * scale
        buf = bytearray(cw * cw * 2)
        fb = framebuf.FrameBuffer(buf, cw, cw, framebuf.RGB565)
        mask = framebuf.FrameBuffer(bytearray(8 * 8 * 2), 8, 8, framebuf.RGB565)
        for i, ch in enumerate(s):
            cx = x + i * cw
            if cx + cw > self.width:
                break
            mask.fill(0)
            mask.text(ch, 0, 0, 0xFFFF)
            for py in range(8):                       # 최근접 확대 + 색 매핑
                for px in range(8):
                    c = color if mask.pixel(px, py) else bg
                    fb.fill_rect(px * scale, py * scale, scale, scale, c)
            self._window(cx, y, cw, cw)
            self.spi.write(buf)
            self.cs(1)

    def text(self, s, x, y, color, bg=0):
        self._blit_text(s, int(x), int(y), color, 1, bg)

    def text_scaled(self, s, x, y, color, scale, bg=0):
        self._blit_text(s, int(x), int(y), color, max(1, int(scale)), bg)

    def show(self):
        pass                                          # 직접 그리기 — 전송할 백버퍼가 없다


class XPT2046:
    """최소 기능 저항막 터치 — 원시 ADC 를 화면 좌표로 변환한다.
    캘리브레이션은 config.TOUCH_CAL/SWAP/INVERT 로 조정(네 귀퉁이 raw 로그 확인).
    ★같은 SPI 버스를 TFT 와 공유한다: 터치는 저속(1MHz)이어야 안정적이므로 읽기 직전에
      버스 속도를 낮추고 끝나면 화면 속도로 되돌린다(SPI 객체를 두 개 만들면 같은 하드웨어
      페리페럴을 재설정해 서로의 설정을 덮어쓴다)."""

    CMD_X, CMD_Y = 0xD0, 0x90

    def __init__(self, spi, cs, irq=None, width=320, height=240,
                 fast_hz=40_000_000, slow_hz=1_000_000):
        self.spi = spi
        self.cs = Pin(cs, Pin.OUT, value=1)
        self.irq = Pin(irq, Pin.IN) if irq is not None else None
        self.width = width
        self.height = height
        self.fast_hz = fast_hz
        self.slow_hz = slow_hz
        self._buf = bytearray(2)

    def _read(self, cmd):
        self.cs(0)
        self.spi.write(bytes([cmd]))
        self.spi.readinto(self._buf, 0x00)
        self.cs(1)
        return ((self._buf[0] << 8) | self._buf[1]) >> 4      # 12bit

    def raw(self):
        """3회 중앙값 — 저항막은 지터가 크다. 압력 없으면 None."""
        if self.irq is not None and self.irq.value():
            return None                                        # IRQ High = 미접촉
        self.spi.init(baudrate=self.slow_hz)
        try:
            xs, ys = [], []
            for _ in range(3):
                xs.append(self._read(self.CMD_X))
                ys.append(self._read(self.CMD_Y))
        finally:
            self.spi.init(baudrate=self.fast_hz)               # 화면 전송 속도로 복귀
        x, y = sorted(xs)[1], sorted(ys)[1]
        if x < 100 or y < 100 or x > 4000 or y > 4000:
            return None
        return x, y

    def get_touch(self):
        r = self.raw()
        if r is None:
            return None
        rx, ry = r
        x0, x1, y0, y1 = config.TOUCH_CAL
        fx = (rx - x0) / float(x1 - x0)
        fy = (ry - y0) / float(y1 - y0)
        if config.TOUCH_INVERT_X:
            fx = 1.0 - fx
        if config.TOUCH_INVERT_Y:
            fy = 1.0 - fy
        if config.TOUCH_SWAP_XY:
            fx, fy = fy, fx
        x = int(min(max(fx, 0.0), 1.0) * (self.width - 1))
        y = int(min(max(fy, 0.0), 1.0) * (self.height - 1))
        return x, y


def get_display():
    """display.create() 가 호출 — (fb, touch) 반환. 예외는 호출부가 헤드리스로 처리.
    ★SPI 객체는 하나만 만든다 — 같은 페리페럴을 두 번 생성하면 나중 설정이 앞의 것을
      덮어쓴다. 터치가 읽기 직전/직후로 속도를 오가며 공유한다."""
    landscape = config.DISP_ROTATION in (90, 270)
    w, h = (320, 240) if landscape else (240, 320)
    fast = 40_000_000
    spi = SPI(config.SPI_ID, baudrate=fast, polarity=0, phase=0,
              sck=Pin(config.SPI_SCK), mosi=Pin(config.SPI_MOSI), miso=Pin(config.SPI_MISO))
    fb = ILI9341(spi, config.TFT_CS, config.TFT_DC, config.TFT_RST, config.TFT_BL,
                 width=w, height=h, rotation=config.DISP_ROTATION)
    touch = XPT2046(spi, config.TOUCH_CS, config.TOUCH_IRQ, width=w, height=h,
                    fast_hz=fast, slow_hz=1_000_000)
    return fb, touch
