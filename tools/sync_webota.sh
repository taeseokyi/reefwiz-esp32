#!/usr/bin/env bash
# tools/sync_webota.sh — mpy-webota(원본)의 파일을 이 저장소로 다시 복사(vendor)한다.
#
# 왜: webota 는 다른 MicroPython 프로젝트와 함께 쓰는 범용 모듈이라 원본은 별도 저장소다
#   (github.com/taeseokyi/mpy-webota). 여기서 고치면 원본과 갈라진다 — 원본에서 고치고 이걸
#   돌린다. 각 파일 머리에 출처 커밋을 적어, 기기에 올라간 webota 가 어느 판인지 되짚는다.
#   `tools/deploy.py --http` 는 배포 전에 원본과 비교해 어긋나면 경고한다.
#
#   ./tools/sync_webota.sh            # WEBOTA_SRC(기본 ~/work/mpy-webota) 의 HEAD 를 복사
set -euo pipefail
SRC="${WEBOTA_SRC:-$HOME/work/mpy-webota}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$SRC/device" ] || { echo "원본이 없다: $SRC (git clone https://github.com/taeseokyi/mpy-webota)" >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain)" ] || { echo "원본에 커밋 안 된 변경이 있다 — 커밋 후 복사한다: $SRC" >&2; exit 1; }
rev="$(git -C "$SRC" describe --tags --always)"
hdr() { echo "# ★vendored: mpy-webota $rev ($1) — 여기서 고치지 말고 원본(~/work/mpy-webota)에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다."; }
# ★boot.py · main.py 도 webota 의 파일이다 — 부팅 분기는 webota 가 가진다(앱은 원본 그대로 쓴다).
for f in webota.py webota_boot.py webota_pkg.py webota_net.py webota_sig.py webota_auth.py boot.py main.py; do
  { hdr "device/$f"; cat "$SRC/device/$f"; } > "$ROOT/src/$f"
done
# 화면은 HTML 이라 출처를 맨 끝 주석으로 단다(<!doctype> 앞에는 아무것도 두지 않는다).
cp "$SRC/device/webota_ca.pem" "$ROOT/src/webota_ca.pem"      # PEM — 머리 주석 없이 원본 그대로(인증서 파서)
{ cat "$SRC/device/webota_ui.html"; echo "<!-- ★vendored: mpy-webota $rev (device/webota_ui.html) — 원본에서 고친 뒤 tools/sync_webota.sh 로 다시 복사한다. -->"; } > "$ROOT/src/webota_ui.html"
{ echo "#!/usr/bin/env python3"; hdr "client/webota.py"; tail -n +2 "$SRC/client/webota.py"; } > "$ROOT/tools/webota.py"
chmod +x "$ROOT/tools/webota.py"
echo "복사 완료 — mpy-webota $rev → src/{webota,webota_boot,webota_pkg,webota_net,webota_sig,webota_auth,boot,main}.py · src/webota_ca.pem · src/webota_ui.html · tools/webota.py"
