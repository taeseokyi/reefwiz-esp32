#!/usr/bin/env bash
# tools/deploy_wsl.sh — WSL 에서 한 명령으로 기기에 배포한다.
#
# 왜 래퍼인가. WSL2 에는 usbipd 가 없어 COM 포트가 안 보인다. 그래서 mpremote 는 Windows
# 쪽 파이썬으로 돌려야 하는데, Windows 프로세스가 UNC 경로(\\wsl.localhost\...)의 저장소를
# 직접 읽게 하면 git 이 없어 dirty 를 모르고, PowerShell 창을 따로 열어야 한다. 다른 작업
# (r2-measure8)과 같은 방식으로 푼다: 필요한 것만 C:\Temp 로 복사하고, WSL 에서 Windows
# 파이썬을 바로 부른다. 커밋 해시·dirty 는 여기(WSL 의 git)에서 구해 넘긴다.
#
#   ./tools/deploy_wsl.sh --port COM4            # 코드 + 자산 (기기 /data 는 건드리지 않는다)
#   ./tools/deploy_wsl.sh --port COM4 --dry-run  # 실행할 mpremote 명령만
#   ./tools/deploy_wsl.sh --list                 # 보이는 COM 포트 목록
#   ./tools/deploy_wsl.sh --port COM4 --reset    # 배포 뒤 기기 리셋까지
#
# 그 밖의 인자는 tools/deploy.py 로 그대로 넘어간다. ★운영 중인 기기에 --with-data 금지 —
#   실측 dkh.dat·이력을 저장소 픽스처(과거)로 덮는다. 이 스크립트는 그래서 그 인자를 막는다.
#
# 환경변수로 경로를 바꿀 수 있다: WINPY=… STAGE=… ./tools/deploy_wsl.sh
# Windows 쪽 환경 만들기(한 번만, 2026-09-24 생성됨):
#   /mnt/c/dkh/python313/python.exe -m venv 'C:\Temp\reefwiz-tools'
#   /mnt/c/Temp/reefwiz-tools/Scripts/python.exe -m pip install mpremote
set -euo pipefail

WINPY="${WINPY:-/mnt/c/Temp/reefwiz-tools/Scripts/python.exe}"   # Windows venv (mpremote)
STAGE="${STAGE:-/mnt/c/Temp/reefwiz-esp32}"                      # 배포용 복사본
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[ -x "$WINPY" ] || { echo "Windows 파이썬이 없다: $WINPY (헤더의 '환경 만들기' 참조)" >&2; exit 1; }

reset=0
args=()
for a in "$@"; do
  case "$a" in
    --list)
      cd "$(dirname "$STAGE")"
      exec "$WINPY" -m serial.tools.list_ports -v ;;
    --reset) reset=1 ;;
    --with-data)
      echo "✗ --with-data 는 막아 두었다 — 운영 중인 기기의 실측 데이터를 과거 픽스처로 덮는다." >&2
      echo "  첫 설치라면 Windows 에서 deploy.py --with-data 를 직접 부른다." >&2
      exit 1 ;;
    *) args+=("$a") ;;
  esac
done

# ── 복사본 만들기 — 기기에 가는 것(src·www)과 배포 스크립트만 ─────────────────
#   --delete: 저장소에서 지운 파일이 복사본에 남아 기기로 딸려 가지 않게.
#   __pycache__ 는 deploy.py 가 어차피 .py 만 고르지만 복사할 이유도 없다.
mkdir -p "$STAGE/tools"
rsync -a --delete --exclude __pycache__ --exclude buildinfo.py "$ROOT/src/" "$STAGE/src/"
rsync -a --delete "$ROOT/www/" "$STAGE/www/"
cp "$ROOT/tools/deploy.py" "$STAGE/tools/deploy.py"

# ── 버전 스탬프는 원본 저장소의 git 이 정한다 ───────────────────────────────
commit="$(git -C "$ROOT" rev-parse --short=7 HEAD)"
dirty=0
[ -z "$(git -C "$ROOT" status --porcelain)" ] || dirty=1

# ★cwd 는 C:\Temp 쪽 — Windows 프로세스의 작업 디렉토리가 UNC(\\wsl.localhost)면 안 된다.
cd "$STAGE"
"$WINPY" tools/deploy.py --commit "$commit" --dirty "$dirty" "${args[@]}"

if [ "$reset" = 1 ]; then
  case " ${args[*]} " in *" --dry-run "*) exit 0 ;; esac
  port=""
  for ((i = 0; i < ${#args[@]}; i++)); do
    [ "${args[$i]}" = "--port" ] && port="${args[$((i + 1))]:-}"
  done
  echo "== 리셋"
  "$WINPY" -m mpremote ${port:+connect "$port"} exec "import machine; machine.reset()" || true
fi
