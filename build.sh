#!/bin/bash
# 雨课堂助手打包安装脚本
# 产物: build/雨课堂助手.app -> /Applications/雨课堂助手.app
set -e
cd "$(dirname "$0")"

APP_NAME="雨课堂助手"
EXE="ykt-helper"
APP="build/${APP_NAME}.app"
TARGET_MACOS="13.0"

echo "[1/5] 编译 SwiftUI 界面 (Universal: x86_64 + arm64) ..."
mkdir -p "build"

# 按当前 CPU 架构准备 chromedriver: 缺失或架构不符时下载对应版本
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then CD_VARIANT="mac-arm64"; else CD_VARIANT="mac-x64"; fi
DRV_INFO=$(lipo -info engine/chromedriver 2>/dev/null || echo missing)
if [ ! -x engine/chromedriver ] || ! echo "$DRV_INFO" | grep -q "$ARCH"; then
    echo "[驱动] 下载 chromedriver 153.0.8010.50 (${CD_VARIANT}) ..."
    curl -sL --max-time 180 -o /tmp/ykt_chromedriver.zip \
        "https://storage.googleapis.com/chrome-for-testing-public/153.0.8010.50/${CD_VARIANT}/chromedriver-${CD_VARIANT}.zip" || \
    curl -sL --max-time 180 -o /tmp/ykt_chromedriver.zip \
        "https://registry.npmmirror.com/-/binary/chrome-for-testing/153.0.8010.50/${CD_VARIANT}/chromedriver-${CD_VARIANT}.zip"
    rm -rf /tmp/ykt_cdt && unzip -o -q /tmp/ykt_chromedriver.zip -d /tmp/ykt_cdt
    cp "/tmp/ykt_cdt/chromedriver-${CD_VARIANT}/chromedriver" engine/chromedriver
    chmod +x engine/chromedriver
    xattr -d com.apple.quarantine engine/chromedriver 2>/dev/null || true
fi

swiftc -parse-as-library -O -target "x86_64-apple-macos${TARGET_MACOS}" \
    -o "build/${EXE}-x64" swift/App.swift
swiftc -parse-as-library -O -target "arm64-apple-macos${TARGET_MACOS}" \
    -o "build/${EXE}-arm64" swift/App.swift
lipo -create -output "build/${EXE}" "build/${EXE}-x64" "build/${EXE}-arm64"

echo "[2/5] 组装 .app 包 ..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "build/${EXE}" "$APP/Contents/MacOS/${EXE}"
cp -p engine/engine.py engine/requirements.txt "$APP/Contents/Resources/"
cp -p engine/chromedriver "$APP/Contents/Resources/"
if [ -f assets/AppIcon.icns ]; then
    cp -p assets/AppIcon.icns "$APP/Contents/Resources/"
fi

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>雨课堂助手</string>
    <key>CFBundleDisplayName</key><string>雨课堂助手</string>
    <key>CFBundleIdentifier</key><string>cn.robbanks.yuketang-helper</string>
    <key>CFBundleVersion</key><string>1.0.2</string>
    <key>CFBundleShortVersionString</key><string>1.0.2</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>ykt-helper</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.education</string>
</dict>
</plist>
PLIST

echo "[3/5] ad-hoc 签名 ..."
codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "  (codesign 跳过，本地运行不受影响)"

echo "[4/5] 安装到 /Applications ..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP" "/Applications/${APP_NAME}.app"

echo "[5/5] 完成 ✅"
echo "    App: /Applications/${APP_NAME}.app"
echo "    源码: $(pwd)"
echo "    运行数据: ~/.yuketang-helper/"
