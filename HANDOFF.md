# 작업 인계 (2026-08-14)

다음 세션에서 이어서 작업하기 위한 현재 상태 요약. 상세 설계·배선·API 는 README.md 참조.

## 저장소

- **GitHub(private): https://github.com/taeseokyi/reefwiz-esp32** — 푸시 완료
- 커밋 `b1b8612` (38파일), 브랜치 `main`, upstream 설정됨, 작업트리 깨끗함
- git 자격증명은 Windows 에 캐시됨 → 다음 푸시는 바로 가능
- 로컬 경로: `E:\cygwin64\home\ower\work\reefwiz-esp32`
- git 사용자는 **저장소 로컬**로만 설정(taeseokyi / tsyi@kisti.re.kr) — 전역 설정 건드리지 않음

## 무엇을 만들었나

원본 https://github.com/taeseokyi/reefwiz 의 Windows PC + 작업 스케줄러 + WSL 동기화 +
GitHub Pages 구성을 **ESP32(MicroPython) 한 대로 대체**. src/ 13모듈 3,087줄.

| 모듈 | 내용 |
|---|---|
| `measure.py` | KH 측정 V4 이식 — 평탄 추종, 이송 전제조건 검증, 액체 위치 추적, 비상정리, 에러 래치, 호스트 구제 |
| `link.py` | HC-05(마스터)↔HC-06(장비) 무선 링크 — keepalive/재연결/재송신, HC-05 하드리셋 |
| `doser.py` | 도저 조정 — Theil-Sen, 스텝캡/데드밴드/정지유지, 에코검증→refresh→롤백 |
| `ops.py` | 조치 도구 — 래치해제·측정중단·측정정리·KCl강제·액체위치지정·명령콘솔·BT연결점검 |
| `webserver.py` | LAN 전용 웹서버 — 대시보드·정비페이지·`/api/*`, plateau JSONL 스트리밍 |
| `display.py` | 한국어 터치 UI — kfont.bin 비트맵폰트, 픽셀 자동 줄바꿈, 해상도 자동적응 |
| `display_driver.py` | ★참조 구현(교체 대상) — ILI9341+XPT2046 |
| `wifinet.py` | WiFi 설정·AP 폴백(`reefwiz-setup` / `reefwiz1234`) |
| `datalog.py` | dkh.dat + 대시보드 JSON + plateau JSONL + CO₂ 편향 판정 |

## 확정된 사용자 결정 (변경 금지 — 재논의 불요)

1. **HC-06 유지**(장비 배선 무변경). ESP32 쪽은 **HC-05 마스터 2개**
   — MicroPython 은 BT Classic/SPP 미지원, ESP32 내장 SPP 도 동시 1연결뿐
2. **LAN 전용 발행** — 외부 접근·GitHub Pages 미러 불필요
3. **MQTT(reefCore) 발행 제거**
4. 설정 저장은 GitHub API 커밋 → **로컬 POST**(토큰 불요)
5. **모든 정보 14일 보존** — dkh.dat 42행(14일×3회), series·plateau·도저이력 동일
6. **화면 전부 한국어** — 버튼: 측정 / 측정 중단 / 측정 정리 / 래치해제 / BT 연결 점검 / 로그
7. **줄 잘림 금지** — 화면 크기 무관 픽셀 단위 자동 줄바꿈
8. 용어 통일: **측정 챔버 / 홀딩 챔버 / KCl 보관액 / 위즈 수조**
9. 에러 시 **BT 연결 회복 후 명령 콘솔로 직접 정리**하는 워크플로 중시
10. 보드·디스플레이는 **더 고성능·더 큰 것으로 교체 예정**(임시 참조 = reefCore Checker R2:
    WEMOS ESP32 18650 + 2.4" ILI9341). 그래서 드라이버만 분리, UI 는 해상도 자동적응

**원본의 안전 레일은 반드시 유지** (실제 사고로 얻은 것들): 이송 전 airoff·ton 응답 검증
(거짓 성공 방지), 액체 위치 불명 시 동결, 비상정리 KCl 소크, 에러 래치, 평탄 미도달 음수 표식,
호스트 구제 계산. 근거는 원본 파일 주석의 날짜별 이력 참조.

## 검증 완료 (하드웨어 없이)

```bash
python3 tools/test_measure_sim.py     # 42체크 ALL PASS — 원본 firmware_sim.py 에 machine.UART
                                      # 심으로 이식본 무수정 연결. 정상 calkh(8회 latch·dKH 일치)/
                                      # RF 순단 재연결 완주/에러 래치 왕복/비상정리/콘솔 모터 대기
python3 tools/devserver.py --port 8123 # 스텁 서버 → 대시보드·정비페이지·API 계약 확인
python3 tools/display_sim.py           # 화면 UI 렌더링 → www/display_sim.html
```

- 대시보드(`www/index.html`): 실데이터 렌더링·차트 3종·plateau 42런·콘솔 오류 0
- 정비페이지(`www/ops.html`): 상태·조치·도징·WiFi·로그(최대 300줄) 전부 동작 확인
- 화면: 320×240 / 800×480 양쪽 한국어 렌더링 확인

## 남은 작업 (TODO)

1. **실장 검증** — HC-05 페어링·`RECONNECT_BACKOFF` 튜닝, 터치 캘리브레이션(`TOUCH_CAL` 은
   네 귀퉁이 raw 값으로), 장시간 힙 모니터링
2. **하드웨어 확정 후** — `config.py` 디스플레이 블록 + `display_driver.py` 교체
   (`display.py` 는 수정 불요). 디스플레이 핀은 현재 **잠정값**
3. (선택) 대시보드에 calref(참조 교정) 버튼 — 지금은 정비페이지 job 으로 실행
4. (선택) 로그 파일 다운로드 링크 — 14일 초과 이력 조회 필요해지면

## 배포 (실기기)

```bash
mpremote connect COM3 fs cp src/*.py :
mpremote connect COM3 fs cp src/kfont.bin :          # 한글 폰트 — 빠뜨리면 ASCII 폴백
mpremote connect COM3 fs mkdir :/www ; mpremote connect COM3 fs cp www/index.html www/ops.html :/www/
gzip -k www/vendor/chart.umd.min.js && mpremote connect COM3 fs cp www/vendor/chart.umd.min.js.gz :/www/vendor/
# 기존 dkh.dat 을 /data/dkh.dat 로 복사하면 도저 계산 이력이 이어짐
```

WiFi 는 `config.py` 에 적거나, 미설정 시 AP `reefwiz-setup`(비번 `reefwiz1234`) →
`http://192.168.4.1/ops.html` 에서 설정. 정상 접속 후 `http://reefwiz.local`.

## 주의사항

- **`data/wifi.json` 은 .gitignore 영구 제외** — 실제 WiFi 자격증명, 장치에만 존재
- `data/` 는 개발 픽스처(원본 저장소 실데이터). 장치에서는 `/data`
- 한글 문구를 코드에 새로 추가하면 폰트 재생성 필요:
  `python3 tools/pack_kfont.py charset` → `powershell tools/gen_kfont.ps1` → `pack_kfont.py pack`
- 이 세션 환경은 `GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never` 라 에이전트가 직접
  인증 프롬프트를 띄울 수 없음. 자격증명이 캐시됐으므로 이후 푸시는 문제 없음
