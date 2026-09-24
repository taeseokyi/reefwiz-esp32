#!/usr/bin/env bash
# tools/release.sh — 태그된 판의 배포 패키지(.wpk)를 만들어 GitHub Releases 에 올린다.
#
# 올라간 패키지는 기기의 webota 설치 화면(http://<기기>:8266/)에 목록으로 뜨고, 골라서
# 바로 설치된다(기기가 직접 내려받아 검증 → 배포 → 재부팅 → 시험 → 확인/롤백).
# 판마다 릴리스가 하나씩 쌓인다 — 옛 판으로 돌아가고 싶으면 그 판을 골라 설치한다.
#
#   ./tools/release.sh            # HEAD 가 v<VERSION> 태그여야 한다(깨끗한 트리, 태그 푸시 완료)
#   ./tools/release.sh --dry-run  # 패키지만 만든다(dist/) — 올리지 않는다
#
# 판 올리는 절차: src/version.py(VERSION·RELEASED) → CHANGELOG.md → 커밋 → git tag -a v<판>
#   → git push origin main v<판> → 이 스크립트.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
dry=0
[ "${1:-}" = "--dry-run" ] && dry=1
die() { echo "✗ $*" >&2; exit 1; }

# ★gh(GitHub CLI) 는 서명 **전에** 찾는다 — 서명(암호 입력)까지 하고 나서 올리지 못하면 헛수고다.
#   PATH 에 없으면 흔한 설치 위치(miniconda 환경 등)도 본다. 끝내 없으면 서명된 패키지만 만들어
#   dist/ 에 남기고 올리는 명령을 안내한다(다른 셸에서 올리면 된다 — 서명은 이미 끝났다).
GH="$(command -v gh || ls "$HOME"/miniconda3/envs/*/bin/gh "$HOME"/miniconda3/bin/gh "$HOME"/.local/bin/gh 2>/dev/null | head -1)"

ver="$(sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' src/version.py)"
tag="v$ver"
if [ "$dry" = 0 ]; then
  [ -z "$(git status --porcelain)" ] || die "커밋 안 된 변경이 있다 — 릴리스 패키지는 깨끗한 트리에서 만든다"
  git rev-parse -q --verify "refs/tags/$tag" >/dev/null || die "태그 $tag 가 없다 — git tag -a $tag"
  [ "$(git rev-list -n1 "$tag")" = "$(git rev-parse HEAD)" ] || die "HEAD 가 $tag 가 아니다"
  git ls-remote --exit-code --tags origin "$tag" >/dev/null || die "태그가 원격에 없다 — git push origin $tag"
  [ -n "$GH" ] || echo "! gh 가 없다 — 서명된 패키지만 만들고, 올리는 명령을 알려 준다"
fi

rm -rf dist
python3 tools/deploy.py --pack dist
pkg="$(ls dist/*.wpk)"
[ "$dry" = 1 ] && { echo "(dry-run) $pkg"; exit 0; }

# 릴리스 노트 = CHANGELOG 의 그 판 절
notes="$(mktemp)"
trap 'rm -f "$notes"' EXIT
awk -v t="## $tag" 'index($0, t) == 1 {on=1; next} on && /^## v/ {exit} on' CHANGELOG.md > "$notes"
[ -s "$notes" ] || echo "$tag" > "$notes"
printf '\n---\n기기 설치: webota 설치 화면(http://<기기>:8266/)에서 이 판을 골라 설치한다.\n' >> "$notes"

title="$(sed -n 's/^MODEL = "\([^"]*\)".*/\1/p' src/version.py) $tag"
if [ -z "$GH" ]; then
  cp "$notes" "dist/NOTES-$tag.md"
  echo "✓ 서명된 패키지: $pkg"
  echo "  올리기(gh 가 있는 셸에서):"
  echo "    gh release create $tag '$pkg' --title '$title' --notes-file 'dist/NOTES-$tag.md'"
  exit 0
fi
if "$GH" release view "$tag" >/dev/null 2>&1; then
  "$GH" release upload "$tag" "$pkg" --clobber
  echo "✓ $tag 릴리스에 패키지 갱신: $(basename "$pkg")"
else
  "$GH" release create "$tag" "$pkg" --title "$title" --notes-file "$notes"
  echo "✓ $tag 릴리스 생성: $(basename "$pkg")"
fi
