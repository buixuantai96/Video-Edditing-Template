#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="$ROOT_DIR/.venv"

find_python() {
  local candidates=(
    python3.14
    python3.13
    python3.12
    python3.11
    /opt/homebrew/opt/python@3.14/bin/python3.14
    /opt/homebrew/opt/python@3.13/bin/python3.13
    /opt/homebrew/opt/python@3.12/bin/python3.12
    /opt/homebrew/opt/python@3.11/bin/python3.11
    /usr/local/opt/python@3.14/bin/python3.14
    /usr/local/opt/python@3.13/bin/python3.13
    /usr/local/opt/python@3.12/bin/python3.12
    /usr/local/opt/python@3.11/bin/python3.11
    python3
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 || [ -x "$candidate" ]; then
      if "$candidate" - <<'PY_CHECK' >/dev/null 2>&1
import ensurepip
import sys
if sys.version_info < (3, 11) or sys.version_info >= (3, 15):
    raise SystemExit(1)
PY_CHECK
      then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

install_python_with_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    return 1
  fi
  echo "⚠️  Chưa tìm thấy Python 3.11–3.14 có ensurepip."
  echo "   Script sẽ cài Python 3.14 bằng Homebrew..."
  brew install python@3.14
}

venv_ok() {
  [ -f "$VENV_DIR/bin/activate" ] && [ -x "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

append_path_to_zshrc() {
  local path_line='export PATH="$HOME/.local/bin:$PATH"'
  local zshrc="${ZDOTDIR:-$HOME}/.zshrc"
  touch "$zshrc"
  if ! grep -Fqs "$path_line" "$zshrc"; then
    printf '\n%s\n' "$path_line" >> "$zshrc"
  fi
}

ensure_yt_dlp() {
  if command_exists yt-dlp; then
    echo "✅ Đã có yt-dlp"
    return
  fi

  echo "📥 Cài yt-dlp để tải video X/Twitter, YouTube..."
  if command_exists brew; then
    brew install yt-dlp
  else
    "$PYTHON_BIN" -m pip install --user -U "yt-dlp[default]"
    export PATH="$HOME/.local/bin:$PATH"
    append_path_to_zshrc
  fi

  if ! command_exists yt-dlp; then
    echo "❌ Đã thử cài yt-dlp nhưng shell hiện tại chưa thấy lệnh."
    echo "   Thử mở terminal mới rồi chạy: yt-dlp --version"
    exit 1
  fi
  echo "✅ Đã cài yt-dlp"
}

ensure_ffmpeg() {
  if command_exists ffmpeg && command_exists ffprobe; then
    echo "✅ Đã có ffmpeg và ffprobe"
    return
  fi

  if ! command_exists brew; then
    echo "❌ Chưa có ffmpeg/ffprobe và không thấy Homebrew."
    echo "   Cài Homebrew rồi chạy lại, hoặc cài FFmpeg thủ công từ https://ffmpeg.org/download.html"
    exit 1
  fi

  echo "🎞️  Cài ffmpeg để remux/normalize video khi cần..."
  brew install ffmpeg

  if ! command_exists ffmpeg || ! command_exists ffprobe; then
    echo "❌ Đã cài FFmpeg nhưng shell hiện tại chưa thấy ffmpeg/ffprobe."
    echo "   Thử mở terminal mới rồi chạy: ffmpeg -version"
    exit 1
  fi
  echo "✅ Đã cài ffmpeg và ffprobe"
}

node_20_or_newer() {
  command_exists node && node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' >/dev/null 2>&1
}

ensure_node_for_bird() {
  if command_exists npm && node_20_or_newer; then
    return
  fi

  if command_exists brew; then
    echo "🟢 Cài Node.js >= 20 để cài bird..."
    brew install node
  fi

  if ! command_exists npm || ! node_20_or_newer; then
    echo "❌ bird cần Node.js >= 20 và npm."
    echo "   Hãy cài Node.js LTS rồi chạy lại: https://nodejs.org/"
    return 1
  fi
}

zshrc_export_value() {
  local key="$1"
  local value="$2"
  local quoted_value
  local zshrc="${ZDOTDIR:-$HOME}/.zshrc"
  local tmp_file
  quoted_value="${value//\'/\'\\\'\'}"
  touch "$zshrc"
  tmp_file="$(mktemp)"
  grep -vE "^export ${key}=" "$zshrc" > "$tmp_file" || true
  printf "export %s='%s'\n" "$key" "$quoted_value" >> "$tmp_file"
  mv "$tmp_file" "$zshrc"
}

has_bird_auth() {
  [ -n "${AUTH_TOKEN:-}" ] && [ -n "${CT0:-}" ]
}

configure_bird_auth() {
  if has_bird_auth; then
    echo "✅ Đã có AUTH_TOKEN và CT0 trong môi trường"
    return
  fi

  echo
  echo "⚠️  bird dùng cookie đăng nhập X/Twitter."
  echo "   Ai có auth_token và ct0 có thể dùng phiên đăng nhập của bạn."
  echo "   Nên dùng tài khoản phụ, không dán token vào chat/log công khai, và có thể logout X để thu hồi phiên."
  echo
  echo "Cách lấy:"
  echo "  1. Đăng nhập x.com trong trình duyệt."
  echo "  2. Mở DevTools: Cmd + Option + I."
  echo "  3. Vào Application → Cookies → https://x.com."
  echo "  4. Copy giá trị auth_token và ct0."
  echo
  echo "Khi dán token vào Terminal, ký tự sẽ không hiện lên màn hình; dán xong cứ bấm Enter."
  echo

  local auth_token
  local ct0
  read -r -s -p "Dán auth_token: " auth_token
  echo
  read -r -s -p "Dán ct0: " ct0
  echo

  if [ -z "$auth_token" ] || [ -z "$ct0" ]; then
    echo "❌ Thiếu auth_token hoặc ct0, bỏ qua cấu hình bird."
    return 1
  fi

  zshrc_export_value "AUTH_TOKEN" "$auth_token"
  zshrc_export_value "CT0" "$ct0"
  export AUTH_TOKEN="$auth_token"
  export CT0="$ct0"
  echo "✅ Đã lưu AUTH_TOKEN và CT0 vào ${ZDOTDIR:-$HOME}/.zshrc"
}

ensure_bird_optional() {
  echo
  read -r -p "Bạn có muốn làm nội dung với link X/Twitter không? Cài bird để đọc thread? [y/N] " use_bird
  case "$use_bird" in
    y|Y|yes|YES)
      ;;
    *)
      echo "↪️  Bỏ qua bird. Khi cần làm bài từ X/Twitter, chạy lại setup và chọn y."
      return
      ;;
  esac

  if ! command_exists bird; then
    echo "🐦 Cài bird để extract thread X/Twitter..."
    if command_exists brew; then
      brew install steipete/tap/bird || {
        ensure_node_for_bird
        npm install -g @steipete/bird
      }
    else
      ensure_node_for_bird
      npm install -g @steipete/bird
    fi
  fi

  if ! command_exists bird; then
    echo "❌ Đã thử cài bird nhưng shell hiện tại chưa thấy lệnh."
    echo "   Thử mở terminal mới rồi chạy: bird --version"
    exit 1
  fi

  echo "✅ Đã có bird"
  configure_bird_auth || return
  bird whoami || true
}

ensure_media_tools() {
  echo
  echo "🧰 Kiểm tra media tools cho workflow X/Twitter và video..."
  ensure_yt_dlp
  ensure_ffmpeg
  ensure_bird_optional
}

echo
echo "🚀 Cài đặt môi trường và chạy Web UI"
echo "   📂 Repo: $ROOT_DIR"
echo

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  install_python_with_brew || true
  PYTHON_BIN="$(find_python || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "❌ Không tìm thấy Python có ensurepip để tạo virtualenv."
  echo "   Hãy cài Python 3.14 rồi chạy lại: brew install python@3.14"
  exit 1
fi
if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v "$PYTHON_BIN")"
fi

if ! venv_ok; then
  if [ -d "$VENV_DIR" ]; then
    BROKEN_VENV="$ROOT_DIR/.venv.broken-$(date +%Y%m%d-%H%M%S)"
    echo "⚠️  .venv hiện tại bị thiếu activate/pip, chuyển sang $(basename "$BROKEN_VENV") và tạo lại..."
    mv "$VENV_DIR" "$BROKEN_VENV"
  fi
  echo "🐍 Tạo virtualenv .venv bằng $("$PYTHON_BIN" --version 2>&1)..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  echo "✅ Đã có virtualenv .venv"
fi

if ! venv_ok; then
  echo "❌ Tạo .venv thất bại hoặc pip chưa sẵn sàng."
  echo "   Thử cài Python 3.13 rồi chạy lại: brew install python@3.13"
  exit 1
fi

echo "🔌 Kích hoạt virtualenv..."
source "$VENV_DIR/bin/activate"

echo "📦 Cài Python dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

echo "🎭 Cài Playwright Chromium..."
python3 -m playwright install chromium

ensure_media_tools

echo
echo "🌐 Khởi động Web UI..."
exec python3 web_server.py "$@"
