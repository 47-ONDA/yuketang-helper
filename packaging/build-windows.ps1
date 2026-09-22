# 雨课堂助手 Windows 构建脚本 (GitHub Actions windows-latest 或本机 PowerShell 运行)
# 产物: dist\ykt-helper-gui\ (绿色目录) + dist\installer\*-setup.exe (Inno Setup 安装包)
$ErrorActionPreference = "Stop"
$CD_VERSION = "153.0.8010.50"
Start-Transcript -Path "build-log.txt" -Force

# 原生命令失败不会触发 ErrorActionPreference, 必须逐步显式检查退出码
function Run-Step([string]$name, [scriptblock]$cmd) {
    Write-Host ">>> $name"
    & $cmd
    if ($LASTEXITCODE -ne 0) { throw "$name 失败 (exit $LASTEXITCODE)" }
}

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
python --version
Run-Step "pip 升级" { python -m pip install --upgrade pip }
Run-Step "pip 引擎依赖" { python -m pip install -r engine/requirements.txt }
Run-Step "pip 安装 PyInstaller" { python -m pip install pyinstaller }

Write-Host "[3/5] PyInstaller 打包 ..."
Run-Step "PyInstaller engine" { python -m PyInstaller --noconfirm --clean --name engine --console --onefile --add-binary "engine/chromedriver.exe;." engine/engine.py }
Run-Step "PyInstaller gui" { python -m PyInstaller --noconfirm --clean --name ykt-helper-gui --windowed gui/gui.py }
Write-Host "  dist 内容:"
Get-ChildItem -Recurse dist | Select-Object -ExpandProperty FullName

Write-Host "[4/5] 合并产物 ..."
if (-not (Test-Path "dist\engine.exe")) { throw "engine.exe 未生成" }
Copy-Item "dist\engine.exe" "dist\ykt-helper-gui\engine.exe" -Force

Write-Host "[5/5] Inno Setup 安装包 ..."
# Inno 6.3 之前不含官方中文语言文件, 缺了就从官方仓库补; 补不到则退回英文界面
$langDir = "C:\Program Files (x86)\Inno Setup 6\Languages"
if (-not (Test-Path "$langDir\ChineseSimplified.isl")) {
    Write-Host "  ChineseSimplified.isl 缺失, 尝试从官方仓库下载..."
    New-Item -ItemType Directory -Force -Path $langDir | Out-Null
    try {
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/Unofficial/ChineseSimplified.isl" `
            -OutFile "$langDir\ChineseSimplified.isl" -TimeoutSec 60
    } catch {
        Write-Host "  下载失败, 安装器界面退回英文"
        (Get-Content "packaging\installer.iss") |
            Where-Object { $_ -notmatch 'Languages|ChineseSimplified' } |
            Set-Content "packaging\installer.no-zh.iss"
    }
}
$issFile = if (Test-Path "packaging\installer.no-zh.iss") { "packaging\installer.no-zh.iss" } else { "packaging\installer.iss" }
$isccPath = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (Get-Command iscc -ErrorAction SilentlyContinue) { $isccPath = (Get-Command iscc).Source }
Run-Step "ISCC $issFile" { & $isccPath $issFile }

Stop-Transcript
Write-Host "完成: dist\ykt-helper-gui\ 与 dist\installer\"

} catch {
    # 失败时把日志尾部打成分步注解(CI 无日志权限时也能看到原因)
    Stop-Transcript -ErrorAction SilentlyContinue
    $tail = Get-Content "build-log.txt" -Tail 30 -ErrorAction SilentlyContinue
    foreach ($line in $tail) { Write-Host "::error::$line" }
    exit 1
}
