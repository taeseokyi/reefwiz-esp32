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
