# ★vendored: mpy-webota v1.0.3 (device/boot.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# boot.py — webota: 부팅 때 배포 적용 · 롤백(앱 코드보다 먼저, 앱과 무관하게 돈다).
#   ★webota 의 파일이다 — 앱은 이 파일을 갖지 않는다(원본 그대로 쓴다).
import webota_boot
webota_boot.apply()
