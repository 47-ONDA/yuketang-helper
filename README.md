# 雨课堂助手 (macOS)

长江雨课堂 / 标准版雨课堂的随堂课桌面助手：自动答题、课件扫描、按章节导出 PDF。

原生 macOS 应用（SwiftUI 界面 + Python 引擎)，支持微信扫码登录、随堂测验自动作答、课件逐页识别、课后按章节合并导出。

## 致谢与许可

本项目衍生自 [jxylisty/yuketang-auto-answer](https://github.com/jxylisty/yuketang-auto-answer)——自动答题核心（课堂监听、题型判定、作答与提交逻辑）来自该项目，并在此基础上移植到 macOS、增加了桌面应用、课件扫描与章节导出等功能。遵循其 [MIT License](LICENSE)，本项目同样以 MIT License 发布。

## 功能

- **扫码登录**：微信扫码，界面显示当前登录人；登录态保存在本地，之后免扫码
- **自动答题**：监听课堂，检测到随堂测验后调用大模型作答并提交，支持单选、多选、多项填空、主观简答
- **课件扫描**（开关式）：开启后老师翻到新页面自动保存课件原图并识别文字；不开则零调用
- **课件导出**：AI 按章号变化、独立大字标题划分章节，每章生成 PDF 和带页码备注的文本文件，导出后自动清理缓存图片
- **容灾设计**：答题模型多级容灾；课件识别首选失败自动切备用
- **退出保护**：关闭应用时若有未导出的课件，弹窗询问导出还是删除

## 环境要求

| 依赖 | 说明 |
|---|---|
| macOS 13+ | Intel 与 Apple Silicon 均原生支持（构建产物为双架构 Universal） |
| Google Chrome | 引擎驱动的浏览器 |
| Python 3 | 系统自带 `/usr/bin/python3` 即可，首次运行自动创建虚拟环境 |
| Xcode Command Line Tools | 编译 Swift 界面（`swiftc`） |

模型与识别服务需要自行准备 API Key（见下方「模型配置建议」），程序内不预设任何服务商。

## 模型配置建议

程序支持任意 OpenAI 兼容格式的接口，全部在应用「设置」中填写：

| 用途 | 建议 |
|---|---|
| 模型 API（答题 + 章节判定） | 推荐**多模态大语言模型**——答题时可以直接看题目截图，章节判定复用同一模型，无需分开配置 |
| 课件识别 · 首选 | 推荐 **PaddleOCR 云端**等视觉理解服务，对课件版面、公式的结构化识别效果好；也支持任意视觉模型接口 |
| 课件识别 · 备用 | 建议填一家**不同厂商**的视觉理解模型，作为首选故障时的兜底 |

## 安装

**方式一：直接下载（普通用户）**

到 [Releases](../../releases) 下载最新的 zip，解压得到 `雨课堂助手.app`，拖进「应用程序」：

1. 首次打开：因为应用没有付费开发者签名，需在 Finder 里**右键 → 打开** → 再点「打开」；或到 系统设置 → 隐私与安全性 里点「仍要打开」
2. 若提示「**已损坏，无法打开**」（从浏览器下载的文件带隔离属性时会出现），在终端执行一句即可：
   ```bash
   xattr -cr /Applications/雨课堂助手.app
   ```
3. 首次启动会自动创建 Python 虚拟环境并安装依赖（需联网，约半分钟）；若提示安装「命令行开发者工具」，按提示安装即可（免费）
4. 打开应用「设置」填入模型 API 和课件识别接口的 Key

**方式二：从源码构建（开发者）**


```bash
git clone <本仓库>
cd <项目目录>
./build.sh
```

`build.sh` 会自动完成：下载 chromedriver（若缺失）→ 编译 SwiftUI 界面 → 组装 `.app` → 安装到 `/Applications`。

> chromedriver 与 Chrome 大版本需一致，默认下载 153.0.8010.50。Chrome 升级大版本后，从 [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) 下载对应版本覆盖 `engine/chromedriver` 后重新构建。

## 源码直接运行（不构建 App）

不装界面，直接跑引擎，功能完整（答题监听 / 课件扫描 / 导出），过程看终端日志：

```bash
# 1) 准备环境（一次性）
python3 -m venv ~/.yuketang-helper/venv
~/.yuketang-helper/venv/bin/pip install -r engine/requirements.txt

# 2) 写配置（一次性），字段说明见下方「配置」一节
mkdir -p ~/.yuketang-helper
cat > ~/.yuketang-helper/config.json <<'EOF'
{
  "yuketang_base_url": "https://changjiang.yuketang.cn",
  "browser": "chrome",
  "api_base": "填写模型 API 地址",
  "api_key": "填写 Key",
  "models": ["填写模型名"],
  "enable_multimodal": true,
  "multimodal_models": ["填写视觉模型名"],
  "ocr_primary": {"api_base": "填写课件识别接口", "api_key": "填写 Key", "model": "填写模型名"},
  "ocr_backup": {"api_base": "", "api_key": "", "model": ""},
  "auto_submit": true
}
EOF

# 3) 扫码登录（一次性，登录态保存在本地）
~/.yuketang-helper/venv/bin/python engine/engine.py scan

# 4) 监听答题，Ctrl-C 退出；加 --scan 则同时开启课件扫描
~/.yuketang-helper/venv/bin/python engine/engine.py listen
~/.yuketang-helper/venv/bin/python engine/engine.py listen --scan

# 5) 课后导出课件（按章合并 PDF/文本，成功后清缓存）
~/.yuketang-helper/venv/bin/python engine/engine.py merge --dir ~/.yuketang-helper/课件缓存/<日期-课程名>

# 查看待导出的课件会话
~/.yuketang-helper/venv/bin/python engine/engine.py cache-list
```

说明：

- **chromedriver**：引擎优先使用 `engine/chromedriver`（存在时），其次读环境变量 `YKT_DRIVER`，都没有则由 Selenium Manager 在线解析（部分网络下可能很慢，建议手动下载放到 `engine/` 目录）。版本需与本机 Chrome 大版本一致
- 监听过程中也可以直接在终端输入 `{"cmd":"scan_on"}` 或 `{"cmd":"scan_off"}` 回车，实时开关课件扫描
- 运行数据（配置、登录态、课件缓存、虚拟环境）统一在 `~/.yuketang-helper/`，与源码目录无关

## 使用

1. 打开「雨课堂助手」→ **扫码登录**（微信扫码）
2. **设置** → 填入模型 API 和课件识别接口的 Key
3. 上课前点 **开始监听**，进入课堂后自动检测答题
4. 需要留存课件时打开 **扫描课件** 开关，翻页自动保存识别
5. 课后点 **导出课件**，按章节的 PDF 和文本输出到 `~/Documents/雨课堂课件/`
6. 关闭应用时如有未导出课件会弹窗询问

## 工作原理

架构：SwiftUI 界面进程 ↔ JSON 行协议 ↔ Python 引擎子进程（Selenium/ChromeDriver 驱动独立 profile 的 Chrome）。

- **答题**：读取课堂大屏的页面状态与 DOM（题型、选项、倒计时标记）→ 截图交视觉模型求解 → 模拟点击选项/填入答案/触发提交。不需要屏幕录制等系统权限，截图全部来自浏览器内部渲染
- **课件**：轮询当前页标识，变化即抓取课件原图（`<img>` 源 / 背景图，失败退回页面截图）；识别优先支持 PaddleOCR 云端异步接口（提交任务 → 轮询 → 取 Markdown 结果），也兼容 OpenAI 格式视觉模型
- **章节划分**：把各页识别文本交给文本模型判定（章号变化如 4→5、明显独立标题行），老师切换课件文件作为参考信号
- **导出**：`img2pdf` 按章合并图片，同章文本按页码合并为 TXT

## 配置

配置文件在 `~/.yuketang-helper/config.json`，均可在应用「设置」界面修改，默认全部留空需自行填写：

| 配置 | 默认值 | 说明 |
|---|---|---|
| `yuketang_base_url` | `https://changjiang.yuketang.cn` | 长江雨课堂；标准版改 `https://www.yuketang.cn` |
| `browser` | `chrome` | `chrome` / `edge` |
| `api_base` / `api_key` / `models` | 空 | 模型 API（答题 + 章节判定） |
| `enable_multimodal` / `multimodal_models` | `true` / 空 | 题目截图交视觉模型 |
| `ocr_primary` | 空 | 课件识别首选（Base / Key / 模型） |
| `ocr_backup` | 空 | 课件识别备用 |
| `slide_dir` | `~/Documents/雨课堂课件` | 课件导出目录 |
| `auto_submit` | `true` | 是否自动提交答案 |

运行数据（登录态、配置、课件缓存、虚拟环境）都在 `~/.yuketang-helper/`，与工程目录分离。

## 目录结构

```
├── swift/App.swift           # SwiftUI 界面
├── engine/engine.py          # Python 引擎（监听/答题/课件扫描/导出）
├── engine/requirements.txt   # 引擎依赖
├── assets/                   # 图标及生成脚本
├── build.sh                  # 一键构建安装
└── LICENSE
```

## 免责声明

本项目仅供编程学习与技术交流。使用者应遵守所在学校的学术规范与课堂纪律，勿用于正规考试或刷分等场景；因使用本工具产生的一切后果由使用者自行承担。
