#!/bin/bash
#
# Cowork Archive Manager - macOS .app インストーラー
# ダブルクリックで起動できる .app バンドルを生成します
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="Cowork Archive Manager"
APP_DIR="$HOME/Applications/${APP_NAME}.app"
BUNDLE_ID="com.github.cowork-archive-manager"

echo "=== Cowork Archive Manager Installer ==="
echo ""

# Xcode Command Line Tools の確認
if ! xcode-select -p &>/dev/null; then
    echo "Error: Xcode Command Line Tools が必要です。"
    echo "  xcode-select --install を実行してください。"
    exit 1
fi

# ~/Applications がなければ作成
mkdir -p "$HOME/Applications"

# 既存の .app があれば削除
if [ -d "$APP_DIR" ]; then
    echo "既存の ${APP_NAME}.app を上書きします..."
    rm -rf "$APP_DIR"
fi

# .app バンドル構造を作成
echo ".app バンドルを作成中..."
mkdir -p "$APP_DIR/Contents/MacOS"
mkdir -p "$APP_DIR/Contents/Resources"

# Python スクリプトをコピー
cp "$SCRIPT_DIR/cowork_archive_manager.py" "$APP_DIR/Contents/Resources/"

# C ランチャーをコンパイル
cat > /tmp/cwm_launcher.c << 'LAUNCHER_EOF'
#include <unistd.h>
#include <libgen.h>
#include <string.h>
#include <stdio.h>

int main(int argc, char *argv[]) {
    char path[4096];
    char script[4096];
    unsigned int size = sizeof(path);

    // 実行ファイルのパスを取得
    if (_NSGetExecutablePath(path, &size) != 0) return 1;
    char *dir = dirname(path);

    // Resources/cowork_archive_manager.py のパスを構築
    snprintf(script, sizeof(script), "%s/../Resources/cowork_archive_manager.py", dir);

    // Python3 で実行
    char *args[] = {"python3", script, NULL};
    execvp("python3", args);
    return 1;
}
LAUNCHER_EOF

cc -O2 -o "$APP_DIR/Contents/MacOS/CWManager" /tmp/cwm_launcher.c
rm /tmp/cwm_launcher.c

# Info.plist を作成
cat > "$APP_DIR/Contents/Info.plist" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>CWManager</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST_EOF

# PkgInfo を作成
echo -n "APPL????" > "$APP_DIR/Contents/PkgInfo"

echo ""
echo "インストール完了！"
echo "  場所: $APP_DIR"
echo ""
echo "使い方:"
echo "  1. Finder で ~/Applications を開く"
echo "  2. 「${APP_NAME}」をダブルクリック"
echo "  3. ブラウザに管理画面が表示されます"
echo ""
echo "アンインストール:"
echo "  rm -rf \"$APP_DIR\""
echo ""
