# 雨课堂助手

长江雨课堂 / 标准版雨课堂的随堂桌面助手：**自动答题 · 课件扫描 · 按章节导出 PDF**。

同一套 Python 引擎，两种桌面形态：

| | macOS | Windows |
|---|---|---|
| 界面 | SwiftUI 原生应用 | tkinter 界面（功能一致，形态更简单） |
| 分发 | `.app`（Intel + Apple Silicon 双架构） | 安装包 `.exe` / 绿色目录 zip（PyInstaller 打包，无需装 Python） |
| 浏览器 | Chrome | Chrome 或 Edge（Win10/11 自带 Edge 可直接用） |

## 致谢与许可

本项目衍生自 [jxylisty/yuketang-auto-answer](https://github.com/jxylisty/yuketang-auto-answer)——自动答题核心（课堂监听、题型判定、作答与提交逻辑）来自该项目，并在此基础上移植到桌面端、增加了课件扫描与章节导出等功能。遵循其 [MIT License](LICENSE)，本项目同样以 MIT License 发布。

## 功能

- **扫码登录**：微信扫码，界面显示当前登录人；登录态保存在本地，之后免扫码
- **自动答题**：监听课堂，检测到随堂测验后调用大模型作答并提交，支持单选、多选、多项填空、主观简答；未作答的题自动跳转过去处理（每题只跳一次，已截止的题不会反复拉扯页面）
- **课件扫描**（开关式）：开启后老师翻到新页面自动保存课件原图并识别文字；动画过渡页自动跳过；不开则零 AI 调用
- **课件导出**：AI 判定章节（章号变化 / 独立大字标题），同时顺手清理每页文字（去图片占位、界面残留、重复页眉），每章生成 PDF 和带页码的文本文件，导出后自动清理缓存图片
- **容灾设计**：答题模型多级容灾；课件识别首选失败自动切备用；识别转后台运行，老师快速翻页也不丢页
- **日志落盘**：运行日志写入本地 `engine.log`，遇到问题直接发送该文件即可

## 模型配置建议

程序不预设任何服务商，Key 自备（应用内「设置」填写，或直接编辑配置文件）：

- **模型 API**（答题 + 章节划分 + 文本清理共用）：推荐**多模态大语言模型**——答题时直接读取题目截图，准确率远高于纯文本
- **课件识别接口**：推荐 PaddleOCR 系免费视觉识别服务（首选 + 备用两级）；不填时课件仍会存图并合并 PDF，只是没有文字层

## macOS 安装

到 [Releases](../../releases) 下载 `yuketang-helper-*-macos-universal.zip`，解压后把「雨课堂助手.app」拖进「应用程序」：

1. 首次打开：应用无付费开发者签名，需在 Finder 里**右键 → 打开** → 再点「打开」；或到 系统设置 → 隐私与安全性 点「仍要打开」
2. 若提示「**已损坏，无法打开**」，终端执行一句即可：
   ```bash
   xattr -cr /Applications/雨课堂助手.app
   ```
3. 首次启动自动创建 Python 虚拟环境并安装依赖（需联网，约半分钟）；若系统提示安装「命令行开发者工具」，按提示装即可（免费）
4. 打开「设置」填入模型 API 与课件识别 Key

从源码构建：`git clone` 本仓库后运行 `./build.sh`（自动下载 chromedriver → 编译 Swift 界面 → 装入 /Applications）。

## Windows 安装

两种方式（内容相同，二选一）：

1. **安装包**：下载 `yuketang-helper-*-windows-setup.exe`，双击安装，开始菜单启动
2. **绿色目录**：下载 `yuketang-helper-windows.zip`，解压后直接运行 `ykt-helper-gui.exe`

- 需要 Windows 10/11；无需安装 Python
- 首次运行若被 SmartScreen 拦截：点「更多信息」→「仍要运行」（应用无付费签名）
- 首次打开「设置」填入模型 API 与课件识别 Key

Windows 构建产物由 GitHub Actions 自动生成：仓库每次引擎/界面代码变更都会触发构建，产物在各次运行页面的 Artifacts 区（`windows-setup` = 安装包，`yuketang-helper-windows` = 绿色目录）。

## 配置

配置文件位置：macOS `~/.yuketang-helper/config.json`，Windows `%USERPROFILE%\.yuketang-helper\config.json`。所有字段都可在应用「设置」界面修改；公开版本默认全部留空。

| 字段 | 说明 |
|---|---|
| `api_base` / `api_key` / `models` | 模型接口（OpenAI 兼容格式）、Key、模型名列表（逗号分隔） |
| `enable_multimodal` / `multimodal_models` | 是否启用视觉作答及视觉模型列表 |
| `auto_submit` | 是否自动提交（关掉则只作答不提交） |
| `ocr_primary` / `ocr_backup` | 课件识别服务的 `api_base` / `api_key` / `model` |
| `slide_dir` | 课件导出目录，默认 `~/Documents/雨课堂课件` |
| `yuketang_base_url` | 雨课堂站点（默认长江 `https://changjiang.yuketang.cn`） |
| `browser` | `chrome` 或 `edge`（默认自动探测） |

运行数据（配置、登录态、课件缓存、虚拟环境）统一在这个目录，删目录即完全重置。

## 源码直接运行（不构建 App）

macOS / Windows / Linux 通用，过程看终端日志：

```bash
# 1) 准备环境（一次性）
python3 -m venv ~/.yuketang-helper/venv          # Windows 用: py -3 -m venv %USERPROFILE%\.yuketang-helper\venv
~/.yuketang-helper/venv/bin/pip install -r engine/requirements.txt   # Windows 路径为 ...\Scripts\pip.exe
```

```bash
# 2) 写配置（一次性），字段见上表
#    然后把对应平台的 chromedriver 放进 engine/ 目录（Windows 为 chromedriver.exe）

# 3) 扫码登录（一次性）
python engine/engine.py scan

# 4) 监听答题（Ctrl-C 退出；--scan 同时开启课件扫描）
python engine/engine.py listen --scan

# 5) 课后导出课件（按章合并 PDF/文本，成功后清理已导出的缓存图片）
python engine/engine.py merge --dir ~/.yuketang-helper/课件缓存/<会话目录>
python engine/engine.py cache-list        # 查看待导出的课件会话
```

> chromedriver 与 Chrome 大版本需一致，默认内置 153.0.8010.50；Chrome 升级大版本后从 [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 下载对应版本替换。

## 使用

1. 扫码登录（首次），界面显示登录人；换人随时可重新扫码
2. 开「监听」→ 自动检测随堂测验并作答；「扫描课件」按需开启
3. 下课后点「导出课件」，选目录，按章节生成 PDF + 文本
4. macOS 版退出时若有未导出课件会弹窗询问；缓存里的课件下次启动仍可补导出

## 工作原理

```
桌面界面 (SwiftUI / tkinter)
   │  stdin/stdout JSON 行协议
   ▼
Python 引擎 (selenium + Chrome/Edge)
   ├─ 课堂监听: 读取页面 Vue 状态判定题目 (problemType 主判据 + DOM 双证据)
   ├─ 自动作答: 截图/题面 → 大模型 → 逐选项点击/填空 → 轮询提交
   ├─ 课件抓取: 翻页检测 → 原图下载(加载时间优先) → 失败转课件区域裁剪截图
   ├─ 文字识别: 首选/备用双通道, 后台运行不阻塞抓取
   └─ 导出: AI 章节划分 + 文本清理 → img2pdf 合并 → 清理缓存
```

## 目录结构

```
engine/engine.py        跨平台引擎 (监听/作答/扫描/导出, 平台差异已内置适配)
gui/gui.py              Windows tkinter 界面 (macOS 界面在另一工程的 swift/App.swift)
swift/App.swift         macOS SwiftUI 界面 (本仓库不参与构建)
packaging/              Windows 打包: build-windows.ps1 + installer.iss
build.sh                macOS 一键构建
.github/workflows/      Windows 自动构建
```

## 免责声明

本项目仅供学习交流，请遵守所在学校与课程的相关规定，因使用本工具产生的任何后果由使用者自行承担。
