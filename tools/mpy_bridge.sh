#!/usr/bin/env bash
# WSL -> Windows COM 포트 MicroPython REPL 브릿지.
#
# 왜 있나: 이 개발 PC 는 WSL2 인데 usbipd 가 없어 ESP32(네이티브 USB CDC)가 /dev/ttyUSB* 로
#   넘어오지 않는다. 대신 Windows 쪽 powershell.exe 의 System.IO.Ports.SerialPort 로 붙는다.
#   장치는 Windows 에서 "USB 직렬 장치(COMx)" 로 잡힌다(장치관리자에서 번호 확인).
#
# 사용:
#   source tools/mpy_bridge.sh
#   mpy 'print("hi")'                 # paste 모드로 코드 실행 후 출력 반환
#   MPY_PORT=COM4 mpy '...'           # 포트 지정(기본 COM4)
#
# ★주의: mpy 는 실행 전 Ctrl-C 를 보내 main.py 를 멈춘다(REPL 확보). 측정/도징 루프가
#   멈추므로 벤치 테스트용이다. 정상 운전 복귀는 mpy_reset(소프트 리셋)으로.
#
# ★대용량 전송 교훈(2026-08-26): paste 모드로 한 번에 보내는 스니펫이 크면(≈4KB 초과) 장치
#   USB 수신버퍼가 넘쳐 뒷부분이 잘린다. 파일 배포처럼 큰 걸 보낼 땐 base64 를 2000자 청크로
#   쪼개 "청크 2개(=스니펫 ~4KB)씩" 나눠 보내고, 매 호출마다 os.stat 크기로 누적을 확인한다.
#   (tools/deploy.py 의 mpremote 경로가 되면 그걸 쓰는 게 정석이다 — 이건 usbipd 없는 폴백.)
PS_EXE="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
: "${MPY_PORT:=COM4}"

mpy() {
  local CODE="$1"; local WAIT="${2:-3}"
  local CODE_B64; CODE_B64=$(printf '%s' "$CODE" | base64 -w0)
  local PORT="$MPY_PORT"
  local PSS
  PSS=$(cat <<EOF
\$ProgressPreference='SilentlyContinue'
\$ErrorActionPreference='Stop'
\$code=[System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$CODE_B64'))
\$code=\$code.Replace([string][char]13,'')
try{ \$p=New-Object System.IO.Ports.SerialPort('$PORT',115200,'None',8,'One') }
catch{ Write-Output ('PORT_FAIL '+\$_.Exception.Message); exit 2 }
\$p.ReadTimeout=300;\$p.WriteTimeout=2000
\$p.Encoding=[System.Text.Encoding]::UTF8
try{ \$p.Open() }catch{ Write-Output ('OPEN_FAIL '+\$_.Exception.Message); exit 3 }
Start-Sleep -Milliseconds 120
\$p.Write([string][char]3);\$p.Write([string][char]3)
Start-Sleep -Milliseconds 150
\$p.Write([string][char]5)
Start-Sleep -Milliseconds 150
\$p.ReadExisting() | Out-Null
\$i=0; \$len=\$code.Length; \$step=200
while(\$i -lt \$len){ \$n=[Math]::Min(\$step,\$len-\$i); \$p.Write(\$code.Substring(\$i,\$n)); \$i+=\$n; Start-Sleep -Milliseconds 15 }
\$p.Write([string][char]10)
\$p.Write([string][char]4)
\$sb=New-Object System.Text.StringBuilder
\$deadline=(Get-Date).AddSeconds($WAIT)
while((Get-Date) -lt \$deadline){ try{\$c=\$p.ReadExisting(); if(\$c){[void]\$sb.Append(\$c)}}catch{}; Start-Sleep -Milliseconds 60 }
\$p.Close()
[Console]::Out.Write(\$sb.ToString())
EOF
)
  local B64; B64=$(printf '%s' "$PSS" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
  "$PS_EXE" -NoProfile -EncodedCommand "$B64" 2>/dev/null
}

# 소프트 리셋 — main.py 재시작(정상 운전 복귀)
mpy_reset() {
  local PSS
  PSS=$(cat <<EOF
\$p=New-Object System.IO.Ports.SerialPort('$MPY_PORT',115200,'None',8,'One')
\$p.Open(); Start-Sleep -Milliseconds 100
\$p.Write([string][char]3); Start-Sleep -Milliseconds 100
\$p.Write([string][char]4)   # Ctrl-D 소프트 리셋
Start-Sleep -Milliseconds 300; \$p.Close(); Write-Output 'reset sent'
EOF
)
  local B64; B64=$(printf '%s' "$PSS" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
  "$PS_EXE" -NoProfile -EncodedCommand "$B64" 2>/dev/null
}

# HC-05 자체진단 실행 헬퍼 (장치에 hc05_selftest.py 가 배포돼 있어야 함)
mpy_hc05() {  # 사용: mpy_hc05            (저수준)
              #       mpy_hc05 full meas  (전체, 원격장비 필요)
  if [ "$1" = "full" ]; then
    mpy "import hc05_selftest as t
t.full('${2:-meas}')" 20
  else
    mpy "import hc05_selftest as t
t.lowlevel()" 12
  fi
}

# 파일 업로드 — usbipd 가 없어 mpremote 를 못 쓸 때의 폴백(base64 청크 전송).
#
# ★청크 크기의 근거(2026-08-26 교훈): paste 모드 스니펫이 ≈4KB 를 넘으면 장치 USB 수신버퍼가
#   넘쳐 뒷부분이 잘린다. 그래서 2000자 청크 2개(=스니펫 ~4KB)씩 보내고, **매 호출마다
#   os.stat 크기로 누적을 확인**한다(조용히 잘리는 것이 가장 위험한 실패다).
#
# 사용: mpy_put tools/hc05_cmode1.py            # 기기 루트에 hc05_cmode1.py 로
#       mpy_put src/link.py link.py
mpy_put() {
  local SRC="$1"; local DST="${2:-$(basename "$1")}"
  [ -f "$SRC" ] || { echo "없는 파일: $SRC"; return 1; }
  local B64; B64=$(base64 -w0 "$SRC")
  local LEN=${#B64}
  local SIZE; SIZE=$(stat -c%s "$SRC")
  echo "[put] $SRC -> :/$DST  ($SIZE bytes, base64 $LEN chars)"
  mpy "f=open('/$DST.b64','w'); f.close(); print('start')" 5 >/dev/null
  local i=0 n=0
  while [ "$i" -lt "$LEN" ]; do
    local part="${B64:$i:4000}"
    i=$((i + 4000)); n=$((n + 1))
    local out
    out=$(mpy "import os
f=open('/$DST.b64','a')
f.write('''$part''')
f.close()
print('acc', os.stat('/$DST.b64')[6])" 8)
    local acc; acc=$(echo "$out" | grep -o 'acc [0-9]*' | tail -1 | cut -d' ' -f2)
    if [ -z "$acc" ]; then echo "  ! 청크 $n 확인 실패 — 중단"; return 1; fi
    echo "  청크 $n: 누적 $acc / $LEN"
    if [ "$acc" != "$i" ] && [ "$acc" != "$LEN" ]; then
      echo "  ! 누적 불일치(기대 $i) — 전송이 잘렸다. 중단"; return 1
    fi
  done
  # ★파일을 반드시 close 한다: MicroPython 은 버퍼를 들고 있어서 open(..).write(..) 만 하면
  #   os.stat 이 0 을 돌려주고(실측 2026-08-29), .b64 를 지운 뒤라 복구도 안 된다.
  mpy "import ubinascii, os, sys
d = open('/$DST.b64').read()
f = open('/$DST','wb')
f.write(ubinascii.a2b_base64(d))
f.close()
n = os.stat('/$DST')[6]
if n > 0:
    os.remove('/$DST.b64')
sys.modules.pop('${DST%.py}', None)   # 낡은/빈 모듈이 캐시에 남아 있으면 다시 import 되지 않는다
print('wrote', n, 'bytes')" 12
}

# CMODE=1 접속 실험 (장치에 hc05_cmode1.py 가 올라가 있어야 함 — mpy_put 참조)
mpy_cmode1() {  # 사용: mpy_cmode1 show / scan / 'by_bind("98:DA:..")' ...
  local CALL="${1:-show}"
  case "$CALL" in *"("*) ;; *) CALL="$CALL()";; esac   # 인자 없는 호출이면 () 를 붙여 준다
  mpy "import hc05_cmode1 as c
c.$CALL" "${2:-70}"
}
