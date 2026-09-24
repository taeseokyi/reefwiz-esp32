# ★vendored: mpy-webota v1.2.0 (device/webota_sig.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# webota_sig — 배포 패키지 서명 검증(RSA PKCS#1 v1.5 · SHA-256).
#
# ★왜(사용자 결정 2026-09-25): 변형된 패키지(좀비)를 설치하지 않는 것이 가장 중요하다. TLS 와
#   GitHub 토큰은 **전송 경로**를 지키지만, 릴리스 체부파일이 바뀌면(계정 탈취·협업자·CI 실수)
#   매니페스트 해시도 함께 맞춰져 온다. 그래서 매니페스트(파일마다 SHA256 · app_id · 판)를
#   사용자만 가진 개인키로 서명하고, 기기는 USB 로 심은 공개키로만 믿는다.
#   - 개인키: PC 의 ~/.config/webota/signing-key.pem (webota.py signing-key init) — 기기로 가지 않는다
#   - 공개키: /webota.json 의 "pkg_keys" — **USB 로만** 심는다(device-config). 웹으로 바꿀 길이 없다
#   - 서명 없는 패키지 · 공개키 없는 기기 · 서명 불일치는 모두 설치 거부
# 순수 파이썬: 검증은 큰 수 거듭제곱 한 번(pow(s, e, n))이라 MicroPython 에서도 빠르다.
import hashlib

# SHA-256 DigestInfo (RFC 8017 §9.2 주 1)
_DI = b"\x30\x31\x30\x0d\x06\x09\x60\x86\x48\x01\x65\x03\x04\x02\x01\x05\x00\x04\x20"


def _klen(nhex):
    return (len(nhex.lstrip("0")) + 1) // 2


def verify(msg, sig, keys):
    """msg 에 대한 sig 가 keys 중 하나로 맞으면 그 키의 id, 아니면 None.
    keys: [{"id", "n": 모듈러스 hex, "e": 공개 지수}]"""
    h = hashlib.sha256(msg).digest()
    for k in keys or []:
        try:
            nhex = k["n"]
            n = int(nhex, 16)
            e = int(k.get("e", 65537))
            size = _klen(nhex)
            if len(sig) != size:
                continue
            m = pow(int.from_bytes(sig, "big"), e, n)
            em = m.to_bytes(size, "big")
            pad = size - 3 - len(_DI) - len(h)
            if pad < 8:
                continue
            if em == b"\x00\x01" + b"\xff" * pad + b"\x00" + _DI + h:
                return k.get("id") or "?"
        except Exception:
            continue
    return None
