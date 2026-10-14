#!/bin/bash
# ditl_sbp installer (macOS arm64). Run from the unpacked release folder: ./install.sh
# Installs the program under ~/.local/share/ditl-sbp and the command under ~/.local/bin.
# Research data (~/Library/Application Support/ditl_sbp) is never touched.
set -eu
set -o pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL_ROOT="${DITL_SBP_INSTALL_ROOT:-$HOME/.local/share/ditl-sbp}"
BIN_DIR="${DITL_SBP_BIN_DIR:-$HOME/.local/bin}"
STAGING=""
TMP_DL=""
DONE=0

say() { printf '%s\n' "$*"; }

cleanup() {
  [ -n "$TMP_DL" ] && rm -rf "$TMP_DL"
  if [ "$DONE" -ne 1 ] && [ -n "$STAGING" ]; then rm -rf "$STAGING"; fi
}
trap cleanup EXIT

fail() {
  say "" >&2
  say "安裝沒有完成：$1" >&2
  say "$2" >&2
  exit 1
}

# 1. platform ---------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  say "目前只支援 Apple 晶片（arm64）的 Mac，這台電腦無法安裝。" >&2
  exit 1
fi

[ -f "$HERE/bootstrap/pins.sh" ] || fail "找不到 bootstrap/pins.sh。" "請從完整解壓的發布資料夾執行 ./install.sh。"
# shellcheck disable=SC1091
. "$HERE/bootstrap/pins.sh"
[ -f "$HERE/requirements.lock" ] && [ -f "$HERE/pyproject.toml" ] && [ -d "$HERE/tools" ] ||
  fail "發布資料夾不完整。" "請重新下載並完整解壓後再執行 ./install.sh。"

# 2. folders ----------------------------------------------------------------
mkdir -p "$BIN_DIR" 2>/dev/null && [ -w "$BIN_DIR" ] ||
  fail "沒有權限寫入 $BIN_DIR。" "請確認這個資料夾可寫入，或用 DITL_SBP_BIN_DIR 指定另一個資料夾後重跑。"
mkdir -p "$INSTALL_ROOT/versions" "$INSTALL_ROOT/tools" 2>/dev/null && [ -w "$INSTALL_ROOT" ] ||
  fail "沒有權限寫入 $INSTALL_ROOT。" "請確認這個資料夾可寫入後重跑，重跑是安全的。"
rm -rf "$INSTALL_ROOT"/versions/.staging-* 2>/dev/null || true

NETWORK_MSG="下載失敗。網路恢復後重新執行 ./install.sh 即可，重跑是安全的。"
UV="$INSTALL_ROOT/tools/uv"

# 3. pinned uv --------------------------------------------------------------
uv_ok() { [ -x "$UV" ] && [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; }
if ! uv_ok; then
  say "取得 uv $UV_VERSION …"
  TMP_DL="$(mktemp -d "${TMPDIR:-/tmp}/ditl-sbp-dl.XXXXXX")"
  curl --fail --silent --show-error --location --retry 2 -o "$TMP_DL/uv.tar.gz" "$UV_URL" ||
    fail "$NETWORK_MSG" "（來源：$UV_URL）"
  ACTUAL="$(shasum -a 256 "$TMP_DL/uv.tar.gz" | awk '{print $1}')"
  if [ "$ACTUAL" != "$UV_SHA256" ]; then
    rm -f "$TMP_DL/uv.tar.gz"
    fail "下載的 uv 無法通過完整性驗證，已丟棄。" "請稍後重試；若一再發生，請回報。"
  fi
  mkdir "$TMP_DL/x" && tar -xzf "$TMP_DL/uv.tar.gz" -C "$TMP_DL/x" ||
    fail "uv 壓縮檔無法解開。" "請稍後重試。"
  FOUND="$(find "$TMP_DL/x" -name uv -type f | head -n 1)"
  [ -n "$FOUND" ] || fail "uv 壓縮檔內容不符預期。" "請稍後重試；若一再發生，請回報。"
  cp "$FOUND" "$INSTALL_ROOT/tools/uv.new" && chmod 755 "$INSTALL_ROOT/tools/uv.new" &&
    mv -f "$INSTALL_ROOT/tools/uv.new" "$UV"
  rm -rf "$TMP_DL"; TMP_DL=""
fi

export UV_NO_CONFIG=1
export UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python"
export UV_CACHE_DIR="$INSTALL_ROOT/cache"
export UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON_DOWNLOADS=manual

# 4. pinned Python ----------------------------------------------------------
say "準備 Python $PYTHON_VERSION …"
"$UV" python install --no-bin "$PYTHON_VERSION" || fail "$NETWORK_MSG" "（取得 Python $PYTHON_VERSION 時失敗）"

# 5. stage the complete installation ----------------------------------------
STAGING="$INSTALL_ROOT/versions/.staging-$$"
mkdir -p "$STAGING/app"
for item in tools schema design-system pyproject.toml requirements.lock README.md; do
  [ -e "$HERE/$item" ] && cp -R "$HERE/$item" "$STAGING/app/$item"
done
find "$STAGING/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

APP_VERSION="$(awk -F'"' '/^version *=/ {print $2; exit}' "$STAGING/app/pyproject.toml")"
APP_HASH="$(cd "$STAGING/app" && find . -type f | LC_ALL=C sort | while IFS= read -r f; do shasum -a 256 "$f"; done | shasum -a 256 | cut -c1-8)"
VERSION_ID="${APP_VERSION:-0}-$APP_HASH"

say "建立執行環境並安裝依賴（第一次需要幾分鐘）…"
"$UV" venv --python "$PYTHON_VERSION" "$STAGING/.venv" >/dev/null ||
  fail "無法建立 Python 執行環境。" "請重跑 ./install.sh；重跑是安全的。"
"$UV" pip install --quiet --python "$STAGING/.venv/bin/python" --only-binary :all: \
  -r "$STAGING/app/requirements.lock" ||
  fail "安裝依賴套件失敗。$NETWORK_MSG" "原本可用的安裝沒有被更動。"

# PDF export prints the research deliverables with the Chromium build that matches
# the pinned Playwright. It goes to Playwright's shared browser cache.
say "下載輸出 PDF 需要的瀏覽器元件（第一次約 200 MB）…"
"$STAGING/.venv/bin/python" -m playwright install chromium-headless-shell >/dev/null 2>&1 ||
  fail "下載輸出 PDF 需要的瀏覽器元件失敗。$NETWORK_MSG" "原本可用的安裝沒有被更動。"

verify() { # $1 = version directory
  PYTHONDONTWRITEBYTECODE=1 "$1/.venv/bin/python" -c "import yaml, jsonschema" >/dev/null 2>&1 &&
    PYTHONDONTWRITEBYTECODE=1 "$1/.venv/bin/python" "$1/app/tools/sbp_review/launcher.py" --help >/dev/null 2>&1
}
verify "$STAGING" || fail "新安裝的環境沒有通過自我檢查。" "原本可用的安裝沒有被更動；請回報這個問題。"

# 6. activate through one pointer ------------------------------------------
PREVIOUS=""
if [ -L "$INSTALL_ROOT/current" ]; then PREVIOUS="$(basename "$(readlink "$INSTALL_ROOT/current")")"; fi
TARGET="$INSTALL_ROOT/versions/$VERSION_ID"
if [ -d "$TARGET" ] && verify "$TARGET"; then
  rm -rf "$STAGING"
else
  rm -rf "$TARGET"
  mv "$STAGING" "$TARGET" || fail "無法放置新版本。" "原本可用的安裝沒有被更動。"
fi
STAGING=""
ln -s "versions/$VERSION_ID" "$INSTALL_ROOT/current.new.$$" || fail "無法切換到新版本。" "原本可用的安裝沒有被更動。"
if ! "$TARGET/.venv/bin/python" -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' \
     "$INSTALL_ROOT/current.new.$$" "$INSTALL_ROOT/current"; then
  rm -f "$INSTALL_ROOT/current.new.$$"
  fail "無法切換到新版本。" "原本可用的安裝沒有被更動。"
fi
DONE=1

# keep the version we just left (for manual rollback); remove older ones
for d in "$INSTALL_ROOT"/versions/*; do
  [ -d "$d" ] || continue
  name="$(basename "$d")"
  [ "$name" = "$VERSION_ID" ] && continue
  [ -n "$PREVIOUS" ] && [ "$name" = "$PREVIOUS" ] && continue
  rm -rf "$d"
done

# 7. command ----------------------------------------------------------------
sed "s|@INSTALL_ROOT@|$INSTALL_ROOT|g" "$HERE/bootstrap/ditl-sbp.template" > "$BIN_DIR/ditl-sbp.new.$$" &&
  chmod 755 "$BIN_DIR/ditl-sbp.new.$$" && mv -f "$BIN_DIR/ditl-sbp.new.$$" "$BIN_DIR/ditl-sbp" ||
  fail "無法建立 ditl-sbp 指令。" "請確認 $BIN_DIR 可寫入後重跑。"

# 8. report -----------------------------------------------------------------
say ""
say "安裝完成。"
case ":$PATH:" in
  *":$BIN_DIR:"*)
    say "現在可以輸入：ditl-sbp start" ;;
  *)
    say "$BIN_DIR 目前不在你的 PATH 裡，所以還不能直接輸入 ditl-sbp。"
    say "現在先用完整路徑啟動：$BIN_DIR/ditl-sbp start"
    say "想以後直接輸入 ditl-sbp，可以把下面這行加進你的 shell 設定檔（例如 ~/.zshrc），再開新的終端機："
    say "  export PATH=\"$BIN_DIR:\$PATH\""
    say "（安裝程式不會自動修改你的設定檔。）" ;;
esac
say "研究資料放在 ~/Library/Application Support/ditl_sbp，重跑 ./install.sh 不會動它。"
