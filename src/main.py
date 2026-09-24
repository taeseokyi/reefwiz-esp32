# main.py — webota 범용 런처(mpy-webota device/main.py 와 같다). 앱 코드는 app.py 에 있다.
#   WiFi 최소 접속 → 원격 배포 서버(:8266) → app.main(). 앱이 죽어도 원격 배포는 살아 있다.
#   ★이 파일·boot.py·webota*.py 는 잘못 바꾸면 USB 로만 복구된다.
import webota
c = webota.load_config()
webota.wifi_up(c)
webota.start(c)
webota.run_app(c)
