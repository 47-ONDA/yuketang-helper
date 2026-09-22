# 雨课堂助手

长江雨课堂随堂桌面助手：**自动答题 · 课件扫描 · 按章节导出 PDF**。支持 macOS 和 Windows。

> 本项目衍生自 [jxylisty/yuketang-auto-answer](https://github.com/jxylisty/yuketang-auto-answer)（MIT License），感谢原作者开源。

## 功能

- **自动答题**：监听课堂，检测到随堂测验自动作答并提交（单选 / 多选 / 填空 / 主观题）
- **课件扫描**：老师翻页自动保存课件原图并识别文字（开关式，不开零调用）
- **课件导出**：AI 按章节合并 PDF + 文本文件，自动清理重复页与界面残留
- **容灾**：模型多级备选；课件识别首选失败自动切备用；日志落盘便于排障

## 下载安装

到 [Releases](../../releases/latest) 下载：

**macOS**（13+，Intel / Apple Silicon）
1. 下载 `*-macos-universal.zip`，解压，把「雨课堂助手.app」拖进「应用程序」
2. 首次打开：右键 → 打开（无付费签名）；若提示「已损坏」，终端执行 `xattr -cr /Applications/雨课堂助手.app`

**Windows**（10/11，需 Google Chrome）
1. 下载 `*-windows-setup.zip`，解压出 setup.exe，双击安装
2. SmartScreen 拦截时：更多信息 → 仍要运行
3. 无需安装 Python

> Windows 免安装目录可从 [Actions 构建产物](../../actions) 获取（需登录 GitHub）。

## 开始使用

1. 打开应用，「设置」里填入模型 API Key（OpenAI 兼容格式；答题要读题图，推荐多模态模型）
2. 点「扫码登录」，微信扫码
3. 开「监听」自动答题，「扫描课件」按需开启
4. 下课点「导出课件」，按章节生成 PDF 和文本

## 配置

配置文件在 `~/.yuketang-helper/config.json`（Windows 为 `%USERPROFILE%\.yuketang-helper\config.json`），界面上改的是同一份：

| 字段 | 说明 |
|---|---|
| `api_base` / `api_key` / `models` | 模型接口、Key、模型名（逗号分隔） |
| `enable_multimodal` / `multimodal_models` | 视觉作答开关与模型 |
| `auto_submit` | 自动提交，默认开 |
| `ocr_primary` / `ocr_backup` | 课件识别服务的地址 / Key / 模型 |
| `slide_dir` | 课件导出目录 |

## 常见问题

- **Mac 提示已损坏** → `xattr -cr /Applications/雨课堂助手.app`
- **Win 浏览器打不开** → 程序会自动下载与 Chrome 匹配的驱动（需联网）；仍失败时查看 `~/.yuketang-helper/engine.log`，多为网络问题或杀毒软件拦截
- **只有 Edge 没有 Chrome** → 需自行放置 msedgedriver，或安装 Chrome

## 源码运行

```bash
python3 -m venv ~/.yuketang-helper/venv
~/.yuketang-helper/venv/bin/pip install -r engine/requirements.txt   # Windows 用 Scripts\pip
python engine/engine.py scan            # 扫码登录
python engine/engine.py listen --scan   # 监听答题 + 课件扫描
python engine/engine.py cache-list      # 查看待导出课件
python engine/engine.py merge --dir <会话目录>
```

构建：macOS 跑 `./build.sh`；Windows 跑 `packaging/build-windows.ps1` 或在 Actions 页手动触发。

## 免责声明

仅供学习交流，请遵守所在学校与课程的规定，使用后果自负。
