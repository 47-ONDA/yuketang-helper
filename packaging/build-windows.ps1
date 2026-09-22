# 雨课堂助手 Windows 构建脚本 (GitHub Actions windows-latest 或本机 PowerShell 运行)
# 产物: dist\ykt-helper-gui\ (绿色目录) + dist\installer\*-setup.exe (Inno Setup 安装包)
$ErrorActionPreference = "Stop"
$CD_VERSION = "153.0.8010.50"
Start-Transcript -Path "build-log.txt" -Force

try {

Write-Host "[1/5] 下载 chromedriver win64 $CD_VERSION ..."
$ok = $false
foreach ($url in @(
    "https://storage.googleapis.com/chrome-for-testing-public/$CD_VERSION/win64/chromedriver-win64.zip",
    "https://registry.npmmirror.com/-/binary/chrome-for-testing/$CD_VERSION/win64/chromedriver-win64.zip")) {
    try {
        Invoke-WebRequest -Uri $url -OutFile "chromedriver.zip" -TimeoutSec 180
        $ok = $true; break
    } catch { Write-Host "  源失败: $url" }
}
if (-not $ok) { throw "chromedriver 下载失败" }
Expand-Archive chromedriver.zip -DestinationPath chromedriver_tmp -Force
Copy-Item "chromedriver_tmp\chromedriver-win64\chromedriver.exe" "engine\chromedriver.exe" -Force

Write-Host "[2/5] 安装依赖 ..."
python -m pip install --upgrade pip
pip install -r engine/requirements.txt
pip install pyinstaller

Write-Host "[3/5] PyInstaller 打包 ..."
pyinstaller --noconfirm --clean --name engine --console --onefile `
    --add-binary "engine/chromedriver.exe;." engine/engine.py
pyinstaller --noconfirm --clean --name ykt-helper-gui --windowed gui/gui.py

Write-Host "[4/5] 合并产物 ..."
Copy-Item "dist\engine\engine.exe" "dist\ykt-helper-gui\engine.exe" -Force

Write-Host "[5/5] Inno Setup 安装包 ..."
# Inno 6.3 之前不含官方中文语言文件, 缺了就从官方仓库补
$langDir = "C:\Program Files (x86)\Inno Setup 6\Languages"
if (-not (Test-Path "$langDir\ChineseSimplified.isl")) {
    Write-Host "  ChineseSimplified.isl 缺失, 从官方仓库下载..."
    New-Item -ItemType Directory -Force -Path $langDir | Out-Null
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/Unofficial/ChineseSimplified.isl" `
        -OutFile "$langDir\ChineseSimplified.isl" -TimeoutSec 60
}
$iscc = Get-Command iscc -ErrorAction SilentlyContinue
if (-not $iscc) { $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
& $iscc.ToString() "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败" }

Stop-Transcript
Write-Host "完成: dist\ykt-helper-gui\ 与 dist\installer\"

} catch {
    # 失败时把日志尾部打成分步注解(CI 无日志权限时也能看到原因)
    Stop-Transcript -ErrorAction SilentlyContinue
    $tail = Get-Content "build-log.txt" -Tail 30 -ErrorAction SilentlyContinue
    foreach ($line in $tail) { Write-Host "::error::$line" }
    exit 1
}
