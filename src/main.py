# ★vendored: mpy-webota v0.9.2 (device/main.py) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다.
# main.py — webota 런처. ★webota 의 파일이다 — 앱은 이 파일을 갖지 않는다(원본 그대로 쓴다).
#   부팅 분기(무엇을 띄울지)는 webota 가 정한다: /webota.json 의 "app"(모듈)·"entry"(함수),
#   앱 교체 때는 패키지 매니페스트의 app·entry 로 바뀐다. 앱은 app.py 의 main() 만 제공한다.
#   순서: WiFi(webota_net — 접속, 안 되면 설정용 AP) → 원격 배포 서버(:8266) → 앱.
#   앱이 죽어도 WiFi·원격 배포는 살아 있다(구조 모드).
import webota
c = webota.load_config()
webota.wifi_up(c)
webota.start(c)
webota.run_app(c)
