# -*- coding: utf-8 -*-
"""
雨课堂助手引擎 (GUI 后端, JSON 行协议)
用法:
    engine.py scan                     # 扫码登录
    engine.py listen                   # 监听课堂 + 自动作答 (+ 课件扫描, 由 stdin 命令开关)
    engine.py merge --dir <会话目录>    # 将缓存中的课件按章节合并为 PDF+TXT, 成功后删除缓存
    engine.py cache-list               # 列出缓存中未导出的课件会话

stdin 命令 (listen 模式, 每行一个 JSON):
    {"cmd":"scan_on"} / {"cmd":"scan_off"}

输出事件 (每行一个 JSON):
    {"event":"log","msg":"..."}
    {"event":"status","listening":true|false}
    {"event":"login","ok":true|false,"name":"..."}
    {"event":"question","time","qtype","question","answer","submit"}
    {"event":"session","dir","course","date","count"}     # 课件会话信息
    {"event":"slide","index","page","file","ocr_head","ocr_ok"}
    {"event":"scan_state","on":true|false}
    {"event":"chapters","items":[...]}                    # merge 结果
    {"event":"merge_done","ok":true|false,"out_dir":...}

运行数据: ~/.yuketang-helper/ (config.json / venv / ykt_profile / 课件缓存/)
课件导出: config.slide_dir (默认 ~/Documents/雨课堂课件/)
"""

import sys
import os
import json
import time
import re
import glob
import signal
import argparse
import threading
import subprocess
import base64
import hashlib
import platform

import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, WebDriverException

if sys.platform.startswith("win"):
    # Windows 控制台默认 GBK, 统一 UTF-8 输出避免协议乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(os.path.expanduser("~"), ".yuketang-helper")
CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.json")
PROFILE_DIR = os.path.join(RUNTIME_DIR, "ykt_profile")
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")
QUIZ_SHOT = os.path.join(RUNTIME_DIR, "current_problem.png")
SLIDE_CACHE_ROOT = os.path.join(RUNTIME_DIR, "课件缓存")
ENGINE_LOG_PATH = os.path.join(RUNTIME_DIR, "engine.log")


def _default_browser_paths():
    """各平台常见浏览器可执行文件路径, 顺序即探测优先级"""
    if IS_MAC:
        return [("chrome", "/Applications/Google Chrome.app"),
                ("edge", "/Applications/Microsoft Edge.app")]
    if IS_WINDOWS:
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local = os.environ.get("LocalAppData", os.path.expanduser(r"~\AppData\Local"))
        return [
            ("chrome", os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe")),
            ("chrome", os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe")),
            ("chrome", os.path.join(local, "Google", "Chrome", "Application", "chrome.exe")),
            ("edge", os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe")),
            ("edge", os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")),
        ]
    return [("chrome", "/usr/bin/google-chrome"), ("chrome", "/usr/bin/chromium-browser"),
            ("edge", "/usr/bin/microsoft-edge")]


BROWSER_APPS = _default_browser_paths()

DEFAULT_CONFIG = {
    "yuketang_base_url": "https://changjiang.yuketang.cn",
    "browser": "chrome",
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "models": ["deepseek-flash"],
    "enable_multimodal": True,
    "multimodal_models": ["deepseek-flash"],
    "auto_submit": True,
    "listen_interval": 1.0,
    "ocr_primary": {"api_base": "https://paddleocr.aistudio-app.com", "api_key": "", "model": "PaddleOCR-VL-1.6"},
    "ocr_backup": {"api_base": "https://open.bigmodel.cn/api/paas/v4", "api_key": "", "model": "glm-4v-flash"},
    "slide_dir": "~/Documents/雨课堂课件",
}


_EMIT_LOCK = threading.Lock()


def emit(obj):
    # OCR 在后台线程运行, 日志行必须串行写出避免协议串行损坏
    with _EMIT_LOCK:
        print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_log(msg):
    # 同步落盘一份, 排障不再依赖界面截图
    try:
        if os.path.exists(ENGINE_LOG_PATH) and os.path.getsize(ENGINE_LOG_PATH) > 1024 * 1024:
            os.replace(ENGINE_LOG_PATH, ENGINE_LOG_PATH + ".1")
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(ENGINE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    emit({"event": "log", "msg": msg})


def ts():
    return time.strftime("%H:%M:%S")


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user = json.load(f)
            cfg.update(user)
            # 子对象单独合并, 避免用户配置里缺字段时整块丢失默认值
            for k in ("ocr_primary", "ocr_backup"):
                if isinstance(user.get(k), dict):
                    cfg[k] = dict(DEFAULT_CONFIG[k]); cfg[k].update(user[k])
        except Exception as e:
            emit_log(f"读取配置失败: {e}, 使用默认配置")
    return cfg


def sanitize_filename(name, max_len=30):
    name = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", str(name or "")).strip("_")
    return name[:max_len] or "未命名"


# ---------------------------------------------------------------------------
# 跨平台: 防休眠 / 浏览器进程清理 / 浏览器探测
# ---------------------------------------------------------------------------

_caffeinate_proc = None
_sleep_display_on = False


def prevent_system_sleep():
    global _caffeinate_proc, _sleep_display_on
    if IS_MAC:
        if _caffeinate_proc is None:
            try:
                _caffeinate_proc = subprocess.Popen(
                    ["caffeinate", "-dis"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    elif IS_WINDOWS:
        try:
            import ctypes
            # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
            _sleep_display_on = True
        except Exception:
            pass


def restore_system_sleep():
    global _caffeinate_proc, _sleep_display_on
    if IS_MAC and _caffeinate_proc is not None:
        try:
            _caffeinate_proc.terminate()
        except Exception:
            pass
        _caffeinate_proc = None
    elif IS_WINDOWS and _sleep_display_on:
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)  # ES_CONTINUOUS
            _sleep_display_on = False
        except Exception:
            pass


_KILL_PS_TEMPLATE = ("& { param($pat) Get-CimInstance Win32_Process | "
                     "Where-Object { $_.CommandLine -like ('*' + $pat + '*') "
                     "-and $_.ProcessId -ne $PID "
                     "-and ($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') } | "
                     "ForEach-Object { Stop-Process -Id $_.ProcessId -Force } }")


def kill_profile_browsers(force=False):
    """按 user-data-dir 杀掉残留的浏览器进程 (跨平台)
    Windows 用参数传递路径(避免引号/通配符注入), 排除自身并限定浏览器进程名"""
    pattern = f"--user-data-dir={PROFILE_DIR}"
    try:
        if IS_WINDOWS:
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            _KILL_PS_TEMPLATE, pattern],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            args = ["pkill", "-f", pattern]
            if force:
                args.insert(1, "-9")
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    except Exception:
        pass


def detect_browser(pref):
    order = ["chrome", "edge"]
    if pref in order:
        order = [pref] + [n for n in order if n != pref]
    for name in order:
        for want, path in BROWSER_APPS:
            if want == name and os.path.exists(path):
                return name
    return None


def resolve_driver_path(browser="chrome"):
    """按浏览器取驱动: Chrome 用 chromedriver, Edge 用 msedgedriver (Windows 带 .exe 后缀)"""
    p = os.environ.get("YKT_DRIVER")
    if p and os.path.exists(p):
        return p
    p = bundled_driver_path(browser)
    return p if p and os.path.exists(p) else None


def bundled_driver_path(browser="chrome"):
    if browser == "edge":
        name = "msedgedriver.exe" if IS_WINDOWS else "msedgedriver"
    else:
        name = "chromedriver.exe" if IS_WINDOWS else "chromedriver"
    return os.path.join(ENGINE_DIR, name)


DRIVER_CACHE_DIR = os.path.join(RUNTIME_DIR, "drivers")
CHROMETESTING_MIRROR = "https://registry.npmmirror.com/-/binary/chrome-for-testing"
CHROMETESTING_OFFICIAL = "https://googlechromelabs.github.io/chrome-for-testing"


def _driver_platform():
    if IS_WINDOWS:
        return "win64"
    if IS_MAC:
        return "mac-arm64" if platform.machine() == "arm64" else "mac-x64"
    return "linux64"


def _driver_exe_name(browser="chrome"):
    base = "msedgedriver" if browser == "edge" else "chromedriver"
    return base + (".exe" if IS_WINDOWS else "")


def _detect_browser_version(browser="chrome"):
    """读已安装浏览器的完整版本号, 读不到返回 None"""
    try:
        if IS_MAC:
            import plistlib
            if browser == "edge":
                plist = "/Applications/Microsoft Edge.app/Contents/Info.plist"
            else:
                plist = "/Applications/Google Chrome.app/Contents/Info.plist"
            if os.path.exists(plist):
                with open(plist, "rb") as f:
                    return (plistlib.load(f) or {}).get("CFBundleShortVersionString")
            return None
        if IS_WINDOWS:
            import winreg
            vendor = "Microsoft" if browser == "edge" else "Google"
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, f"Software\\{vendor}\\{browser.capitalize()}\\BLBeacon") as k:
                        val, _ = winreg.QueryValueEx(k, "version")
                        return val
                except OSError:
                    continue
            return None
        # linux
        exe = "/usr/bin/microsoft-edge" if browser == "edge" else "/usr/bin/google-chrome"
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _pick_version_for_milestone(versions, major):
    """从版本号列表里挑出该大版本下最新的一个"""
    cands = [v for v in versions if v.split(".")[0] == str(major)]
    if not cands:
        return None
    return max(cands, key=lambda v: [int(x) for x in v.split(".")])


def _download_and_validate(zip_url, exe_name, dest_dir):
    """下载 zip -> 解压目标驱动 -> 校验可执行; 成功返回最终路径, 失败返回 None"""
    os.makedirs(dest_dir, exist_ok=True)
    exe_path = os.path.join(dest_dir, exe_name)
    tmp_exe = exe_path + ".tmp"
    zpath = os.path.join(dest_dir, "driver.zip")
    try:
        r = requests.get(zip_url, timeout=120, stream=True)
        r.raise_for_status()
        with open(zpath, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        import zipfile
        hit = None
        with zipfile.ZipFile(zpath) as z:
            for info in z.namelist():
                # 精确匹配文件名, 避免 LICENSE.chromedriver 这类同名尾缀文件抢先
                if os.path.basename(info) == exe_name:
                    hit = info
                    break
        if not hit:
            return None
        with zipfile.ZipFile(zpath) as z:
            with z.open(hit) as src, open(tmp_exe, "wb") as dst:
                dst.write(src.read())
        if os.path.getsize(tmp_exe) < 1024 * 1024:
            return None   # 明显不是真驱动
        if not IS_WINDOWS:
            os.chmod(tmp_exe, 0o755)
        ver = _detect_driver_version(tmp_exe)
        if not ver:
            return None   # 无法执行/损坏
        os.replace(tmp_exe, exe_path)
        return exe_path
    except Exception:
        return None
    finally:
        for tmp in (zpath, tmp_exe):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _download_driver(major, browser="chrome"):
    """下载与大版本匹配的驱动: 每个源做「查版本→下载→校验」完整尝试, 失败换下一源"""
    if browser == "edge":
        return None   # Edge 走手动配置, 不做自动下载
    plat = _driver_platform()
    exe_name = _driver_exe_name()
    dest_dir = os.path.join(DRIVER_CACHE_DIR, f"chrome-{major}")
    exe_path = os.path.join(dest_dir, exe_name)
    if os.path.exists(exe_path) and os.path.getsize(exe_path) > 1024 * 1024:
        return exe_path
    try:
        os.remove(exe_path)
    except Exception:
        pass

    def mirror_source():
        r = requests.get(f"{CHROMETESTING_MIRROR}/", timeout=15)
        names = [item.get("name", "").rstrip("/")
                 for item in r.json() if isinstance(item, dict)]
        names = [n for n in names if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", n)]
        full = _pick_version_for_milestone(names, major)
        # npmmirror 镜像比官方桶多一层平台子目录
        return (full, f"{CHROMETESTING_MIRROR}/{full}/{plat}/chromedriver-{plat}.zip") if full else (None, None)

    def official_source():
        r = requests.get(f"{CHROMETESTING_OFFICIAL}/known-good-versions-with-downloads.json", timeout=20)
        versions = r.json().get("versions", [])
        vs = [v["version"] for v in versions
              if v.get("version", "").split(".")[0] == str(major)
              and any(d.get("platform") == plat for d in v.get("downloads", {}).get("chromedriver", []))]
        full = _pick_version_for_milestone(vs, major)
        for v in versions:
            if v.get("version") == full:
                for d in v["downloads"]["chromedriver"]:
                    if d.get("platform") == plat:
                        return full, d["url"]
        return None, None

    for source_name, source in (("npmmirror", mirror_source), ("官方", official_source)):
        try:
            full, zip_url = source()
        except Exception as e:
            emit_log(f"驱动源 {source_name} 查询失败: {str(e)[:80]}")
            continue
        if not zip_url:
            continue
        path = _download_and_validate(zip_url, exe_name, dest_dir)
        if path:
            emit_log(f"已自动下载 chrome 驱动 {full}（源: {source_name}）")
            return path
        emit_log(f"驱动源 {source_name} 下载或校验失败，换下一源")
    return None


def pick_driver(browser="chrome"):
    """驱动选择策略: 内置驱动版本匹配 → 直接用;
    不匹配 → 缓存/自动下载匹配版; 全失败 → 退回内置驱动并警告。
    返回 (驱动路径, 是否版本匹配)"""
    env = os.environ.get("YKT_DRIVER")
    if env and os.path.exists(env):
        return env, True
    bundled = bundled_driver_path(browser)
    bundled_ok = bundled and os.path.exists(bundled)
    if browser == "edge":
        return (bundled, True) if bundled_ok else (None, False)
    browser_ver = _detect_browser_version("chrome")
    want_major = browser_ver.split(".")[0] if browser_ver else None
    bundled_ver = _detect_driver_version(bundled) if bundled_ok else None
    if want_major and bundled_ver and want_major == bundled_ver.split(".")[0]:
        return bundled, True
    # 需要匹配: 先查缓存, 再下载
    cached = _cached_driver_for(want_major) if want_major else None
    if cached:
        return cached, True
    if want_major:
        dl = _download_driver(want_major)
        if dl:
            return dl, True
    if bundled_ok:
        emit_log(f"警告: 内置驱动 {bundled_ver or '?'} 与 Chrome {browser_ver or '?'} 大版本不一致, 尝试直接使用")
        return bundled, False
    return None, False


def _detect_driver_version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", out.stdout or "")
        return m.group(1) if m else None
    except Exception:
        return None


def _cached_driver_for(major):
    if not major:
        return None
    exe_name = _driver_exe_name()
    try:
        for d in sorted(glob.glob(os.path.join(DRIVER_CACHE_DIR, f"chrome-*")), reverse=True):
            if d.split("chrome-")[-1].split("-")[0] == str(major):
                p = os.path.join(d, exe_name)
                # 缓存必须可执行且版本对得上, 坏文件直接作废
                if os.path.exists(p) and os.path.getsize(p) > 1024 * 1024 \
                        and (eng_ver := _detect_driver_version(p)) and eng_ver.split(".")[0] == str(major):
                    return p
                try:
                    os.remove(p)
                except Exception:
                    pass
    except Exception:
        pass
    return None


def get_driver(cfg, headless=False):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    last_err = None
    for attempt in range(1, 4):
        kill_profile_browsers(force=False)
        try:
            return _launch_browser(cfg, headless)
        except Exception as e:
            last_err = e
            emit_log(f"浏览器启动失败 (第 {attempt}/3 次)")
            try:
                kill_profile_browsers(force=True)
                time.sleep(1)
                for lock in glob.glob(os.path.join(PROFILE_DIR, "Singleton*")):
                    try:
                        os.remove(lock)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(1)
    raise RuntimeError(f"浏览器启动失败(已重试 3 次): {last_err}")


def _launch_browser(cfg, headless):
    pref = str(cfg.get("browser", "chrome")).lower()
    name = detect_browser(pref)
    if not name:
        raise RuntimeError("未找到 Chrome/Edge 浏览器")

    if name == "chrome":
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        driver_cls = webdriver.Chrome
    else:
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service
        driver_cls = webdriver.Edge

    opts = Options()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--log-level=3")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    if headless:
        opts.add_argument("--headless=new")

    drv, matched = pick_driver(name)
    if not drv:
        if name == "edge":
            raise RuntimeError("未找到 Edge 驱动 (msedgedriver)，且未检测到 Chrome——请安装 Chrome 后重试")
        raise RuntimeError("未找到 chromedriver，请确认打包完整或重试（程序会自动下载匹配版本，需联网）")
    if not matched:
        emit_log("驱动与 Chrome 版本不一致且自动下载失败，尝试用现有驱动继续；若报错请检查网络后重启程序")
    service = Service(executable_path=drv)
    return driver_cls(options=opts, service=service)


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

def _chat(api_base, api_key, model, messages, timeout=15, max_tokens=500, temperature=0.1):
    """OpenAI 兼容调用; deepseek 接口关闭深度思考以保证响应速度。
    部分平台(如智谱)有 1024 上限: 先按请求值发送, 报参数错误再降级重试一次"""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if "deepseek" in api_base:
        payload["thinking"] = {"type": "disabled"}
    try:
        return _chat_request(api_base, api_key, payload, timeout)
    except requests.exceptions.HTTPError as e:
        body = ""
        if e.response is not None:
            body = (e.response.text or "")[:300]
        if "max_tokens" in body and max_tokens > 1024:
            payload["max_tokens"] = 1024
            return _chat_request(api_base, api_key, payload, timeout)
        raise


def _chat_request(api_base, api_key, payload, timeout):
    r = requests.post(f"{api_base.rstrip('/')}/chat/completions",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    res = r.json()
    msg = res['choices'][0]['message']
    ans = (msg.get('content') or '').strip()
    if not ans:
        reasoning = (msg.get('reasoning_content') or '').strip()
        if reasoning:
            tail = [l.strip() for l in reasoning.splitlines() if l.strip()]
            ans = re.sub(r'^[^A-Za-z0-9\u4e00-\u9fff]+', '', tail[-1]) if tail else ""
    return re.sub(r'^```[\w]*\n?', '', re.sub(r'\n?```$', '', ans)).strip()


def image_content(prompt, img_path):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]


class SolverUnavailable(RuntimeError):
    """模型不可用(未配置/全部调用失败): 调用方必须放弃作答而不是填假答案"""


def call_solver(cfg, question_text, q_type, options=None, image_path=None):
    api_base = cfg.get("api_base", DEFAULT_CONFIG["api_base"]).rstrip("/")
    api_key = cfg.get("api_key", "")
    models_pool = list(cfg.get("models", DEFAULT_CONFIG["models"]))
    enable_mm = cfg.get("enable_multimodal", True)
    mm_models = list(cfg.get("multimodal_models", DEFAULT_CONFIG["multimodal_models"]))

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        raise SolverUnavailable("模型 API 未配置，无法作答（请在设置中填写）")

    opt_str = f"\n可选选项: {', '.join(options)}" if options else ""
    prompt = f"""你是一个大学课堂随堂测验答题专家。请根据以下题目内容（若附带图像请分析其中的图表与排版）给出高准确率的回答：

【题目类型】: {q_type}
【题目内容】:
{question_text}
{opt_str}

【输出规则】:
1. 单选题: 只输出 1 个大写字母（例如 C）。
2. 多选题: 只输出所有正确选项的大写字母连写（例如 ACD）。
3. 填空题: 1 个空直接输出答案; 多个空按顺序输出, 各空之间用竖线 | 分隔（例如 2 | 3 | 5）。
4. 主观题: 直接输出答案内容。"""

    img_b64 = None
    if image_path and os.path.exists(image_path):
        try:
            with open(image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
        except Exception:
            pass

    attempt_queue = []
    if img_b64 and enable_mm:
        for vm in mm_models:
            if vm not in [m for m, _ in attempt_queue]:
                attempt_queue.append((vm, True))
    for m in models_pool:
        vc = bool(img_b64) and (m in mm_models or
                                any(k in m.lower() for k in ["vl", "vision", "4v", "4o"]))
        if (m, vc) not in attempt_queue:
            attempt_queue.append((m, vc))

    for model_name, with_image in attempt_queue:
        if with_image and img_b64:
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ]
            mode_tag = "视觉"
            timeout_sec = 15
        else:
            user_content = prompt
            mode_tag = "文本"
            timeout_sec = 9

        messages = [
            {"role": "system", "content": "你是随堂测试答题助手，严格按格式输出最终答案。"},
            {"role": "user", "content": user_content},
        ]
        for attempt in range(1, 3):
            try:
                t0 = time.time()
                ans = _chat(api_base, api_key, model_name, messages,
                            timeout=timeout_sec, max_tokens=500)
                if ans:
                    emit_log(f"答案 {ans}（{model_name}/{mode_tag} {round(time.time()-t0,1)}s）")
                    return ans
                emit_log(f"{model_name} 返回空结果，重试")
            except requests.exceptions.Timeout:
                emit_log(f"{model_name} 超时({timeout_sec}s)，切换备用")
                break
            except Exception as e:
                emit_log(f"{model_name} 调用失败: {str(e)[:80]}，切换备用")
                break
            time.sleep(0.5)

    raise SolverUnavailable("所有模型调用均失败（网络或服务异常），本题未作答")


OCR_PROMPT = "提取图片中的全部文字，按阅读顺序输出为纯文本，保留段落与标题换行，不要添加任何解释。"


def paddle_ocr(api_base, api_key, model, img_path, timeout=30):
    """PaddleOCR 云端异步接口: 提交任务 -> 轮询 state=done -> 下载结果取 markdown 文本"""
    base = api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    ctype = "image/jpeg" if img_path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    with open(img_path, "rb") as f:
        r = requests.post(f"{base}/api/v2/ocr/jobs", headers=headers,
                          files={"file": (os.path.basename(img_path), f, ctype)},
                          data={"model": model or "PaddleOCR-VL-1.6"},
                          timeout=15)
    r.raise_for_status()
    j = r.json()
    if j.get("code") not in (0, None):
        raise RuntimeError(f"提交失败: {j.get('msg')}")
    job_id = (j.get("data") or {}).get("jobId")
    if not job_id:
        raise RuntimeError("未返回 jobId")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.8)
        j2 = requests.get(f"{base}/api/v2/ocr/jobs/{job_id}", headers=headers, timeout=10).json()
        d = j2.get("data") or {}
        state = d.get("state")
        if state == "done":
            json_url = (d.get("resultUrl") or {}).get("jsonUrl")
            if not json_url:
                raise RuntimeError("结果链接缺失")
            res = requests.get(json_url, timeout=15).json()
            texts = []
            for item in ((res.get("result") or {}).get("layoutParsingResults") or []):
                t = ((item.get("markdown") or {}).get("text") or "").strip()
                if t:
                    texts.append(t)
            return "\n\n".join(texts)
        if state in ("failed", "error"):
            raise RuntimeError(f"任务失败: {j2.get('msg')}")
    raise TimeoutError(f"轮询超时({timeout}s)")


def ocr_image(cfg, img_path):
    """课件识别: 首选(PaddleOCR 云端或视觉模型), 失败自动用备用。返回 (text, ok)"""
    for tag, key in (("首选", "ocr_primary"), ("备用", "ocr_backup")):
        api = cfg.get(key) or {}
        base = (api.get("api_base") or "").rstrip("/")
        api_key = api.get("api_key") or ""
        model = api.get("model") or ""
        if not (base and api_key):
            if tag == "首选":
                continue
            emit_log("课件识别接口未配置")
            return "", False
        try:
            if "aistudio-app" in base or "paddleocr" in base.lower():
                text = paddle_ocr(base, api_key, model, img_path)
            else:
                text = _chat(base, api_key, model,
                             [{"role": "user", "content": image_content(OCR_PROMPT, img_path)}],
                             timeout=20, max_tokens=1500)
            if text:
                return text, True
            emit_log(f"课件识别({tag} {model})返回空")
        except Exception as e:
            emit_log(f"课件识别{tag}失败: {str(e)[:80]}" + ("，改用备用" if tag == "首选" else ""))
    return "", False


def split_blank_answers(ans, expected_count=0):
    ans = ans.strip()
    if expected_count == 1:
        return [ans]
    if '|' in ans:
        parts = [p.strip() for p in ans.split('|') if p.strip()]
        if len(parts) > 1:
            return parts
    labeled = re.findall(r'(?:\[?填空\d+\]?|\b\d+[\.、]|空\d+)[:：\s]*([^\s,，|]+)', ans)
    if labeled and len(labeled) > 1:
        if expected_count <= 1 or len(labeled) == expected_count:
            return labeled
    if expected_count > 1:
        if '\n' in ans:
            lines = [re.sub(r'^(?:\[?填空\d+\]?|\d+[\.、:：]|空\d+[:：])\s*', '', l.strip()).strip() for l in ans.split('\n') if l.strip()]
            if len(lines) == expected_count:
                return lines
        if any(c in ans for c in [',', '，', '、']):
            parts = [p.strip() for p in re.split(r'[,，、]', ans) if p.strip()]
            if len(parts) == expected_count:
                return parts
        spaces = [s.strip() for s in ans.split() if s.strip()]
        if len(spaces) == expected_count:
            return spaces
    if '\n' in ans:
        lines = [re.sub(r'^(?:\[?填空\d+\]?|\d+[\.、:：]|空\d+[:：])\s*', '', l.strip()).strip() for l in ans.split('\n') if l.strip()]
        if len(lines) > 1 and all(len(l) < 30 for l in lines):
            return lines
    return [ans]


# ---------------------------------------------------------------------------
# 页面探测 JS
# ---------------------------------------------------------------------------

LOGIN_STATE_JS = """
return (async function() {
    const info = { logged: false, name: null };
    try {
        const text = document.body ? document.body.innerText : '';
        const hasAvatar = !!document.querySelector('.avatar, .user-name, [class*="userInfo"], [class*="avatar"]');
        const hasMyCourse = text.includes('我听的课') || text.includes('我的课程') || text.includes('课堂动态');
        info.logged = hasAvatar || hasMyCourse;
        const nameSels = ['.user-name', '.username', '[class*="user-name"]',
                          '[class*="userInfo"] .name', '[class*="nickname"]'];
        for (const s of nameSels) {
            const el = document.querySelector(s);
            if (el && el.textContent.trim()) { info.name = el.textContent.trim(); break; }
        }
        if (!info.name) {
            for (const url of ['/api/v3/user/me', '/api/v3/user/basic-info', '/api/v3/account/profile']) {
                try {
                    const r = await fetch(url, { credentials: 'include' });
                    if (!r.ok) continue;
                    const j = await r.json();
                    const d = j.data || j;
                    for (const k of ['name', 'username', 'nickname', 'realName', 'userName']) {
                        if (d && typeof d[k] === 'string' && d[k].trim()) { info.name = d[k].trim(); break; }
                    }
                    if (info.name) break;
                } catch(e) {}
            }
        }
    } catch(e) {}
    return info;
})();
"""

QUIZ_PROBE_JS = """
const info = { hasQuiz: false };
const app = document.querySelector('#app');
const store = app && app.__vue__ && app.__vue__.$store ? app.__vue__.$store.state : null;
const currSlide = store ? store.currSlide : null;

const vis = el => !!(el && (el.offsetWidth > 0 || el.offsetHeight > 0));

// 报告「未完成」题的时间轴下标, 由 Python 侧限量跳转(截止的题永远是未完成, JS 内点击会无限跳回)
const tItems = Array.from(document.querySelectorAll('.timeline__item.J_slide, .timeline__item'));
info.unfinIndex = tItems.findIndex(el => el.innerText.includes('未完成'));

// ---- 证据收集 ----
// 1) 选项控件: 题目面板里可点击的选项容器(class 含 option/choice 等), 不收正文里的裸字母文本
const optWidgets = Array.from(document.querySelectorAll(
    '[class*="option"], [class*="choice"], [class*="answer-item"]'
)).filter(el => vis(el) && typeof el.className === 'string'
    && !/page|nav|slide|thumb|tab|menu|filter|sort/i.test(el.className));

// 2) 提交按钮: 文本恰为 提交答案/提交 的可见元素
const btnEls = Array.from(document.querySelectorAll('button, [class*="submit"], [class*="btn"], a, span, div'));
const submitBtn = btnEls.find(el => vis(el) && ['提交答案', '提交'].includes(el.textContent.trim()));

// 3) 倒计时: 可见叶子元素的文本含 倒计时
const timingText = btnEls.find(el => vis(el) && el.children.length === 0 && el.textContent.includes('倒计时'));

// ---- 触发判定: 单一弱特征不再触发 ----
const probType = currSlide ? Number(currSlide.problemType || 0) : 0;
let trigger = null;
if (probType >= 1 && probType <= 5) {
    trigger = 'problemType=' + probType;          // 题目页专有字段, 普通课件页没有
} else if (optWidgets.length >= 2 && (submitBtn || timingText)) {
    trigger = 'DOM:选项控件x' + optWidgets.length + (submitBtn ? '+提交按钮' : '+倒计时');
}

if (trigger) {
    info.hasQuiz = true;
    info.evidence = trigger;
    info.probId = currSlide ? (currSlide.problemID || currSlide.sid || currSlide.slideID) : null;
    const centerCanvas = document.querySelector('.ppt__wrapper, .lesson__page, .presentation, .center-area');
    const centerText = centerCanvas ? centerCanvas.innerText : (document.body.innerText || '');
    info.domText = (currSlide ? (currSlide.body || currSlide.title || '') : '') || centerText.slice(0, 300);

    // 选项字母: 优先取选项控件自身的字母标记(A / A. / A、/ A+空格), 控件没有再找面板内裸字母
    let letters = [];
    for (const w of optWidgets) {
        const t = (w.textContent || '').trim();
        const m = t.match(/^([A-F])(?:[.、．]\\s]|\\s|$)/);
        if (m) letters.push(m[1]);
    }
    if (letters.length < 2 && centerCanvas) {
        for (const p of centerCanvas.querySelectorAll('p, span, div, li')) {
            const t = p.textContent.trim();
            if (['A', 'B', 'C', 'D', 'E', 'F'].includes(t) && p.children.length === 0 && p.offsetWidth > 0) {
                letters.push(t);
            }
        }
    }
    const uniqueOpts = Array.from(new Set(letters));
    const hasChoiceOptions = uniqueOpts.length >= 2;

    let qType = null;
    if (probType === 1) qType = '单选题';
    else if (probType === 2) qType = '多选题';
    else if (probType === 3 || probType === 4) qType = '填空题';
    else if (probType === 5) qType = '主观题';
    if (!qType) {
        if (hasChoiceOptions) {
            if (centerText.includes('多选')) qType = '多选题';
            else qType = '单选题';
        } else if (centerText.includes('填空')) qType = '填空题';
        else if (centerText.includes('主观') || centerText.includes('简答')) qType = '主观题';
        else qType = '单选题';   // 已确认是题目, 分类不明时按单选处理
    }
    info.qType = qType;
    if (qType.includes('选')) info.options = uniqueOpts.length > 0 ? uniqueOpts : ['A', 'B', 'C', 'D'];
    else info.options = null;
}
return info;
"""

SLIDE_INFO_JS = """
return (function() {
    const app = document.querySelector('#app');
    const store = app && app.__vue__ && app.__vue__.$store ? app.__vue__.$store.state : null;
    const cs = store ? store.currSlide : null;
    const out = {};
    out.sid = cs ? (cs.sid || cs.slideID || cs.id || null) : null;
    out.page = cs ? (cs.page || cs.index || cs.pageNum || cs.num || null) : null;
    out.presId = store ? (store.presentationId || (store.presentation && store.presentation.id) || null) : null;

    // 动画页: 雨课堂盖着「当前页面有动画」提示层, 此时课件内容还没显示, 抓了也是提示图
    out.animated = false;
    try {
        for (const el of document.querySelectorAll('div, p, span')) {
            const t = el.textContent || '';
            if ((t.includes('当前页面有动画') || t.includes('请先听老师讲解')) && el.offsetWidth > 0 && el.children.length <= 2) {
                out.animated = true;
                break;
            }
        }
    } catch(e) {}

    // 搜索范围含 iframe (课件可能渲染在 iframe 里)
    const roots = [document];
    try {
        for (const f of document.querySelectorAll('iframe')) {
            try { if (f.contentDocument) roots.push(f.contentDocument); } catch(e) {}
        }
    } catch(e) {}

    // 图片最近一次加载完成的时间戳: 翻页新加载的图最新, 静态封面最老
    const rtime = {};
    try {
        const collect = (perf) => {
            for (const r of perf.getEntriesByType('resource')) {
                if (/\\.(png|jpe?g|webp)(\\?|$)/i.test(r.name)) {
                    const t = r.responseEnd || r.startTime || 0;
                    if (!(r.name in rtime) || t > rtime[r.name]) rtime[r.name] = t;
                }
            }
        };
        collect(performance);
        for (const f of document.querySelectorAll('iframe')) {
            try { if (f.contentWindow && f.contentWindow.performance) collect(f.contentWindow.performance); } catch(e) {}
        }
    } catch(e) {}

    const cands = [];
    const seen = new Set();
    const push = (url, area) => {
        if (!/^https?:/.test(url) || area < 40000 || seen.has(url)) return;
        seen.add(url);
        cands.push({ url: url, area: area, t: (url in rtime) ? rtime[url] : -1 });
    };

    for (const root of roots) {
        const center = root.querySelector('.ppt__wrapper, .lesson__page, .presentation, .center-area');
        const scopes = center ? [center, root] : [root];
        for (const scope of scopes) {
            for (const img of scope.querySelectorAll('img')) {
                const u = img.currentSrc || img.src;
                if (u) push(u, img.offsetWidth * img.offsetHeight);
            }
            const els = [scope].concat(Array.from(scope.querySelectorAll('div')).slice(0, 400));
            for (const el of els) {
                try {
                    const bg = getComputedStyle(el).backgroundImage;
                    const m = bg && bg.match(/url\\(["']?(.*?)["']?\\)/);
                    if (m) push(m[1], el.offsetWidth * el.offsetHeight);
                } catch(e) {}
            }
        }
    }

    // 排序: 加载时间新者优先(翻页刚加载的图排最前); 没有时间戳的按面积; 时间戳全是 -1 时退化为纯面积
    const anyTimed = cands.some(c => c.t >= 0);
    cands.sort((a, b) => anyTimed ? ((b.t < 0) - (a.t < 0)) || (b.t - a.t) || (b.area - a.area)
                                  : b.area - a.area);

    // store 的 url/pic 字段可能是不随翻页变化的封面, 只在 DOM 没有时垫底补入
    const storeUrls = [];
    if (cs) {
        for (const k of ['url', 'imgUrl', 'imageUrl', 'pageUrl', 'pic']) {
            if (typeof cs[k] === 'string' && /^https?:/.test(cs[k]) && !seen.has(cs[k])) {
                seen.add(cs[k]);
                storeUrls.push(cs[k]);
            }
        }
    }
    out.candidates = cands.map(c => c.url).concat(storeUrls);
    out.imgUrls = out.candidates;
    return out;
})();
"""


# ---------------------------------------------------------------------------
# 课件会话管理
# ---------------------------------------------------------------------------

class SlideSession:
    """一次监听中的课件扫描会话: 图片与识别文本缓存到运行目录, 导出后删除"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.date = time.strftime("%Y-%m-%d")
        self.course = None
        self.dir = None
        self.pages = []          # [{index,page,file,ocr,pres_switch}]
        self.captured_sids = set()
        self.last_pres_id = None
        self.last_img_url = None
        self.seen_hashes = set()   # 本课件已保存的内容哈希(按 pres 分组, 换 pres 清空)
        self.index = 0
        self.meta_lock = threading.Lock()   # OCR 后台线程与主线程都会写 slides.json

    def bind_course(self, course_name):
        if self.course is None and course_name:
            self.course = sanitize_filename(course_name)
            # 同课程同日多次监听各用独立目录, 重启不覆盖之前的扫描
            base = f"{self.date}-{time.strftime('%H%M')}-{self.course}"
            d, n = base, 2
            while os.path.exists(os.path.join(SLIDE_CACHE_ROOT, d)):
                d = f"{base}-{n}"
                n += 1
            self.dir = os.path.join(SLIDE_CACHE_ROOT, d)
            os.makedirs(self.dir, exist_ok=True)
            self.emit_session()

    def emit_session(self):
        n = 0
        if self.dir:
            n = len([p for p in self.pages if os.path.exists(os.path.join(self.dir, p["file"]))])
        emit({"event": "session", "dir": self.dir, "course": self.course,
              "date": self.date, "count": n})

    def save_slide(self, data_bytes, ext="png", page=None, pres_switch=False):
        """保存一张课件图片(优先原图字节), 返回文件名"""
        os.makedirs(self.dir, exist_ok=True)   # 导出清理后目录可能被删, 兜底重建
        self.index += 1
        fname = f"{self.date}-{self.course}-第{self.index:02d}张.{ext}"
        path = os.path.join(self.dir, fname)
        with open(path, "wb") as f:
            f.write(data_bytes)
        self.pages.append({
            "index": self.index,
            "page": page if page is not None else self.index,
            "file": fname,
            "ocr": "",
            "pres_switch": pres_switch,
        })
        return path, fname

    def save_screenshot_slide(self, driver, page=None, pres_switch=False):
        os.makedirs(self.dir, exist_ok=True)
        tmp = os.path.join(self.dir, ".tmp_shot.png")
        driver.save_screenshot(tmp)
        with open(tmp, "rb") as f:
            data = f.read()
        try:
            os.remove(tmp)
        except Exception:
            pass
        return self.save_slide(data, "png", page=page, pres_switch=pres_switch)

    def flush_meta(self):
        if self.dir:
            with self.meta_lock:
                os.makedirs(self.dir, exist_ok=True)
                with open(os.path.join(self.dir, "slides.json"), "w", encoding="utf-8") as f:
                    json.dump({"date": self.date, "course": self.course, "pages": self.pages},
                              f, ensure_ascii=False, indent=1)


def is_animation_notice(text):
    """OCR 出来的文本是否只是「当前页面有动画」提示层(雨课堂在动画页盖的覆盖层)。
    精确匹配 UI 文案, 不做长度+关键词模糊判断, 避免误伤含「动画」的正常课件"""
    t = (text or "").replace("\n", "").replace(" ", "")
    return "当前页面有动画" in t or "请先听老师讲解" in t


def download_image(driver, url, referer):
    """用浏览器会话的 Cookie 下载原图, 返回 (bytes, ext) 或 (None, None)"""
    try:
        s = requests.Session()
        for c in driver.get_cookies():
            s.cookies.set(c.get("name"), c.get("value"), domain=c.get("domain"))
        r = s.get(url, headers={"Referer": referer, "User-Agent":
                  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                  timeout=10)
        if r.status_code == 200 and len(r.content) > 5000:
            ct = (r.headers.get("Content-Type") or "").lower()
            ext = "jpg" if ("jpeg" in ct or "jpg" in ct) else "png"
            return r.content, ext
    except Exception:
        pass
    return None, None


# ---------------------------------------------------------------------------
# 模式一: 扫码登录
# ---------------------------------------------------------------------------

def force_logout(driver, base_url):
    """清除当前域的 Cookie 与本地存储并重开首页, 回到扫码登录页"""
    try:
        driver.delete_all_cookies()
    except Exception:
        pass
    try:
        driver.execute_script("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
    except Exception:
        pass
    driver.get(f"{base_url}/v2/web/index")
    time.sleep(2)


def run_scan():
    cfg = load_config()
    base_url = cfg.get("yuketang_base_url", DEFAULT_CONFIG["yuketang_base_url"]).rstrip("/")
    emit_log(f"打开登录页 {base_url}，等待扫码")
    try:
        driver = get_driver(cfg, headless=False)
    except Exception as e:
        emit({"event": "login", "ok": False, "name": None})
        emit({"event": "error", "msg": str(e)})
        return

    ok, name = False, None
    try:
        driver.get(f"{base_url}/v2/web/index")
        time.sleep(2)
        # 已有登录态时先退出当前账号, 回到扫码页等待重新扫码(可换人)
        try:
            st = driver.execute_script(LOGIN_STATE_JS)
            if st and st.get("logged"):
                prev = st.get("name") or "未取到姓名"
                emit_log(f"检测到已有登录态（{prev}），已退出当前账号，等待新扫码")
                force_logout(driver, base_url)
        except Exception:
            pass
        cancelled = False
        for _ in range(200):
            time.sleep(1.5)
            try:
                st = driver.execute_script(LOGIN_STATE_JS)
            except WebDriverException:
                # 浏览器被关闭: 不代表登录成功。重新拉起浏览器验证 profile 里的登录态
                emit_log("浏览器已关闭，正在验证登录状态…")
                try:
                    driver = get_driver(cfg, headless=False)
                    driver.get(f"{base_url}/v2/web/index")
                    time.sleep(3)
                    st2 = driver.execute_script(LOGIN_STATE_JS) or {}
                    if st2.get("logged"):
                        ok = True
                        name = st2.get("name")
                        break
                    cancelled = True
                    emit_log("未检测到登录态，登录已取消")
                    break
                except Exception:
                    cancelled = True
                    emit_log("无法验证登录态，登录已取消")
                    break
            except Exception:
                continue
            if st and st.get("logged"):
                ok = True
                name = st.get("name")
                break
        if ok:
            try:
                if name is None:
                    time.sleep(2)
                    name = (driver.execute_script(LOGIN_STATE_JS) or {}).get("name")
            except Exception:
                pass
            save_state(name)
            emit_log(f"登录成功: {name if name else '已登录'}")
            emit({"event": "login", "ok": True, "name": name})
        elif cancelled:
            emit({"event": "login", "ok": False, "name": None})
        else:
            emit_log("5 分钟未检测到登录")
            emit({"event": "login", "ok": False, "name": None})
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def save_state(name):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    state = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass
    state["logged_in_as"] = name or state.get("logged_in_as")
    state["last_login_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 模式二: 监听 + 答题 + 课件扫描
# ---------------------------------------------------------------------------

SLIDE_SETTLE_WAIT = 0.7     # 翻页后等渲染稳定的时长(秒); 期间翻页则作废本次
SLIDE_MAX_CANDIDATES = 4    # 原图候选最多尝试下载数
SUBMIT_WAIT_SECONDS = 5.0   # 等提交按钮出现的时长(部分课堂选中后才渲染)


def stdin_command_loop(handlers):
    """读 stdin 的 JSON 命令行, 分发给 handlers"""
    def loop():
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except Exception:
                    continue
                fn = handlers.get(cmd.get("cmd"))
                if fn:
                    try:
                        fn(cmd)
                    except Exception:
                        pass
        except BaseException:
            pass   # 退出信号到达时静默结束读取线程
    t = threading.Thread(target=loop, daemon=True)
    t.start()


class SlideCapture:
    """监听中的课件扫描: 翻页检测、抓取保存、后台 OCR。由 run_listen 驱动。

    协议事件: scan_state / slide / slide_ocr / slide_removed / session / log
    """

    def __init__(self, cfg, driver):
        self.cfg = cfg
        self.driver = driver
        self.on = False
        self.session = None   # 当前 SlideSession

    def set_enabled(self, state, reason=""):
        self.on = state
        emit({"event": "scan_state", "on": state})
        emit_log(("课件扫描已开启" + reason) if state else "课件扫描已关闭")

    def bind_course(self, course_name):
        """进入新课堂时换会话; 同一门课沿用(保留 captured_sids 与去重集合)"""
        if self.session is None or self.session.course != sanitize_filename(course_name):
            self.session = SlideSession(self.cfg)
            self.session.bind_course(course_name)

    def handle_slide(self):
        """监听循环每轮调用; 任何异常只记日志, 不拖垮答题链路"""
        if not self.on or self.session is None:
            return
        seq = self.session.index + 1
        try:
            self.capture_slide()
        except Exception as e:
            emit_log(f"第 {seq or '?'} 张: 扫描失败已跳过 ({str(e)[:80]})")

    def capture_slide(self):
        driver, s = self.driver, self.session
        info = driver.execute_script(SLIDE_INFO_JS)
        if not info or not info.get("sid"):
            return
        sid = info["sid"]
        if sid in s.captured_sids:
            return
        if info.get("animated"):
            return  # 动画提示层盖着, 内容没显示; 不标记 sid, 播完动画后下轮再抓
        pres_switch = (s.last_pres_id is not None and info.get("presId")
                       and info.get("presId") != s.last_pres_id)
        if pres_switch:
            s.seen_hashes.clear()   # 换课件后内容重新计
        s.last_pres_id = info.get("presId") or s.last_pres_id
        # 先标记防止本轮 settle 期间重复进入; 下方所有失败路径必须撤销标记允许重试
        s.captured_sids.add(sid)

        # 短暂等待渲染稳定后重取候选; 期间已翻页则作废本次, 下轮循环抓当前页
        time.sleep(SLIDE_SETTLE_WAIT)
        try:
            info2 = driver.execute_script(SLIDE_INFO_JS)
        except Exception:
            info2 = None
        if info2 and info2.get("sid") and info2["sid"] != sid:
            s.captured_sids.discard(sid)
            return
        if info2 and info2.get("animated"):
            s.captured_sids.discard(sid)
            return  # 等待期间出现动画层, 作废
        info = info2 or info

        data, ext, via, url_used, retryable = self._fetch_image(info, s)
        if data is None:
            if retryable:
                s.captured_sids.discard(sid)   # 失败可重试, 下轮再试这一页
            return
        content_hash = hashlib.sha1(data).hexdigest()
        s.seen_hashes.add(content_hash)
        if url_used:
            s.last_img_url = url_used
        emit_log(f"第 {s.index + 1} 张: {via}")
        path, fname = s.save_slide(data, ext, page=info.get("page"),
                                   pres_switch=pres_switch)
        # 页面记录先落盘: 立即导出/异常退出时 cache-list 也能发现这张图
        s.flush_meta()
        self._start_ocr(s, path, fname, content_hash)
        if pres_switch:
            emit_log("检测到课件切换")
        page_entry = s.pages[-1] if s.pages else {"index": s.index, "page": s.index}
        emit({"event": "slide", "index": s.index,
              "page": page_entry.get("page", s.index),
              "file": fname, "ocr_head": "", "ocr_ok": False})
        s.emit_session()

    def _fetch_image(self, info, s):
        """按候选顺序下载原图; 全部失败/重复时转课件区域裁剪截图。
        返回 (data, ext, via, url_used, retryable)——retryable 表示失败可下轮重试"""
        referer = self.driver.current_url
        for url in (info.get("candidates") or [])[:SLIDE_MAX_CANDIDATES]:
            if url == s.last_img_url:
                continue   # 上一张用过的地址不重复抓
            d2, e2 = download_image(self.driver, url, referer)
            if d2 and hashlib.sha1(d2).hexdigest() not in s.seen_hashes:
                return d2, e2, "原图下载", url, False
        shot = self._shot_bytes(s)
        if shot is None:
            emit_log(f"第 {s.index + 1} 张: 截图失败，跳过")
            return None, None, None, None, True   # 截图失败是临时性的, 允许重试
        if hashlib.sha1(shot).hexdigest() in s.seen_hashes:
            emit_log(f"第 {s.index + 1} 张: 画面与已抓内容相同，跳过")
            return None, None, None, None, False  # 内容重复, 不重试
        return shot, "png", "页面截图", None, False

    def _shot_bytes(self, s):
        """页面截图, 裁到课件区域(去掉侧边栏/弹幕); 取不到容器时全屏"""
        try:
            os.makedirs(s.dir, exist_ok=True)
            tmp = os.path.join(s.dir, ".tmp_shot.png")
            self.driver.save_screenshot(tmp)
            import io
            from PIL import Image
            img = Image.open(tmp)
            rect = None
            try:
                rect = self.driver.execute_script("""
                    const c = document.querySelector('.ppt__wrapper, .lesson__page, .presentation, .center-area');
                    if (!c) return null;
                    const r = c.getBoundingClientRect();
                    if (r.width < 100 || r.height < 100) return null;
                    return {x: r.left, y: r.top, w: r.width, h: r.height, vw: window.innerWidth};
                """)
            except Exception:
                rect = None
            if rect and img.width > 0:
                # save_screenshot 输出物理像素, rect 是逻辑像素, 按 viewport 宽度换算
                scale = img.width / float(rect["vw"]) if rect.get("vw") else 1.0
                box = (max(0, int(rect["x"] * scale)), max(0, int(rect["y"] * scale)),
                       min(img.width, int((rect["x"] + rect["w"]) * scale)),
                       min(img.height, int((rect["y"] + rect["h"]) * scale)))
                if box[2] - box[0] > 100 and box[3] - box[1] > 100:
                    cropped = img.crop(box)
                    buf = io.BytesIO()
                    cropped.save(buf, format="PNG")
                    with open(tmp, "wb") as f:
                        f.write(buf.getvalue())
            with open(tmp, "rb") as f:
                data = f.read()
            try:
                os.remove(tmp)
            except Exception:
                pass
            return data
        except Exception:
            return None

    def _start_ocr(self, s, path, fname, content_hash):
        """识别转后台线程: 主循环立刻返回继续盯翻页, 不让 OCR 拖慢抓取"""
        page_entry = s.pages[-1] if s.pages else {"index": s.index, "page": s.index}

        def ocr_worker():
            try:
                text, ok = ocr_image(self.cfg, path)
            except Exception:
                text, ok = "", False
            # OCR 兜底: JS 漏检的动画提示页(整页只有那两行字)在这里清除
            if ok and is_animation_notice(text):
                self._remove_page(s, page_entry, path, fname, content_hash)
                return
            page_entry["ocr"] = text
            try:
                s.flush_meta()
            except Exception:
                pass
            head = text.replace("\n", " ")[:40] if text else ""
            emit_log(f"第 {page_entry['index']} 张识别" + ("完成" if ok else "失败"))
            emit({"event": "slide_ocr", "index": page_entry["index"], "file": fname,
                  "ocr_head": head, "ocr_ok": ok})

        threading.Thread(target=ocr_worker, daemon=True).start()

    def _remove_page(self, s, page_entry, path, fname, content_hash):
        """删除动画提示页: 文件、pages 记录、去重集合一并撤销"""
        try:
            os.remove(path)
        except Exception:
            pass
        if page_entry in s.pages:
            s.pages.remove(page_entry)
            s.seen_hashes.discard(content_hash)
        try:
            s.flush_meta()
        except Exception:
            pass
        emit_log(f"第 {page_entry['index']} 张是动画提示页，已移除")
        emit({"event": "slide_removed", "index": page_entry["index"], "file": fname})
        s.emit_session()


def run_listen(scan_default=False):
    def _sigterm(signum, frame):
        raise KeyboardInterrupt()
    try:
        signal.signal(signal.SIGTERM, _sigterm)
    except Exception:
        pass

    cfg = load_config()
    base_url = cfg.get("yuketang_base_url", DEFAULT_CONFIG["yuketang_base_url"]).rstrip("/")
    auto_submit = cfg.get("auto_submit", True)
    enable_mm = cfg.get("enable_multimodal", True)
    interval = float(cfg.get("listen_interval", 1.0))

    try:
        driver = get_driver(cfg, headless=False)
    except KeyboardInterrupt:
        emit({"event": "status", "listening": False})
        emit_log("已停止")
        return
    except Exception as e:
        emit({"event": "status", "listening": False})
        emit({"event": "error", "msg": str(e)})
        return

    # 课件扫描
    capture = SlideCapture(cfg, driver)
    stop_flag = threading.Event()   # GUI 通过 stdin 的 stop 命令优雅退出 (Windows 无 SIGTERM)

    stdin_command_loop({
        "scan_on": lambda c: capture.set_enabled(True),
        "scan_off": lambda c: capture.set_enabled(False),
        "stop": lambda c: stop_flag.set(),
    })

    emit({"event": "status", "listening": True})
    if scan_default:
        capture.set_enabled(True, " (--scan)")
    prevent_system_sleep()
    answered = set()
    jumped_unfin = set()   # 已自动跳转过的「未完成」题, 每题只跳一次(截止题会永远未完成)

    try:
        driver.get(f"{base_url}/v2/web/index")
        time.sleep(2)
        emit_log("监听已启动")

        while not stop_flag.is_set():
            try:
                cur_url = driver.current_url or ""

                if "/lesson/" not in cur_url:
                    on_lesson = driver.execute_script("""
                        return (async function() {
                            try {
                                const res = await fetch('/api/v3/classroom/on-lesson', { credentials: 'include' });
                                if (res.ok) {
                                    const j = await res.json();
                                    const list = j.data?.onLessonClassrooms || [];
                                    if (list.length > 0) return list[0];
                                }
                            } catch(e) {}
                            return null;
                        })();
                    """)
                    if on_lesson:
                        l_id = on_lesson.get("lessonId") or on_lesson.get("lesson_id")
                        c_name = on_lesson.get("courseName") or "雨课堂"
                        emit_log(f"进入课堂: {c_name} (ID {l_id})")
                        capture.bind_course(c_name)
                        driver.get(f"{base_url}/lesson/fullscreen/v3/{l_id}")
                        time.sleep(4)
                        continue
                    else:
                        time.sleep(3)
                        continue

                quiz_info = driver.execute_script(QUIZ_PROBE_JS)

                # 「未完成」题限量跳转: 每题只自动跳一次去作答;
                # 截止的题会永远停在未完成, 无限量跳会不停把页面拽回去
                ui = (quiz_info or {}).get("unfinIndex", -1)
                if isinstance(ui, int) and 0 <= ui and ui not in jumped_unfin:
                    jumped_unfin.add(ui)
                    emit_log("检测到未作答的题，跳转过去")
                    try:
                        driver.execute_script("""
                            const i = arguments[0];
                            const items = document.querySelectorAll('.timeline__item.J_slide, .timeline__item');
                            if (items[i]) { items[i].click(); }
                        """, ui)
                        time.sleep(2)
                        continue
                    except Exception:
                        pass

                if quiz_info and quiz_info.get("hasQuiz"):
                    prob_id = quiz_info.get("probId")
                    if not prob_id:
                        dom_fp = (quiz_info.get("domText") or "").strip()[:50]
                        prob_id = f"gen_{abs(hash(dom_fp + quiz_info.get('qType', '')))}"
                    if prob_id in answered:
                        time.sleep(1)
                        continue
                    ev = handle_quiz(driver, cfg, quiz_info, auto_submit, enable_mm)
                    if prob_id:
                        answered.add(prob_id)
                    if ev:
                        emit(ev)

                capture.handle_slide()

                time.sleep(interval)

            except (NoSuchWindowException, WebDriverException) as e:
                emit_log(f"浏览器连接断开 ({type(e).__name__})，3 秒后重连")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(3)
                try:
                    driver = get_driver(cfg, headless=False)
                    capture.driver = driver   # 扫描仍持有旧连接, 必须同步替换
                    driver.get(f"{base_url}/v2/web/index")
                    time.sleep(2)
                    emit_log("已重连，继续监听")
                except Exception as relaunch_err:
                    emit_log(f"重连失败: {relaunch_err}")
                    time.sleep(5)
            except Exception as e:
                emit_log(f"监听异常: {e}")
                time.sleep(2)

    except KeyboardInterrupt:
        pass
    finally:
        restore_system_sleep()
        try:
            driver.quit()
        except Exception:
            pass
        if capture.session:
            capture.session.flush_meta()
        emit({"event": "status", "listening": False})


SUBMIT_FIND_CLICK_JS = """
const roots = [document];
try {
    for (const f of document.querySelectorAll('iframe')) {
        try { if (f.contentDocument) roots.push(f.contentDocument); } catch(e) {}
    }
} catch(e) {}
const vis = el => !!(el && el.offsetWidth > 0);
for (const root of roots) {
    const els = root.querySelectorAll('button, [class*="submit"], [class*="btn"], a, span, div');
    for (const el of els) {
        const t = el.textContent.trim();
        if ((t === '提交答案' || t === '提交') && vis(el)) {
            el.click();
            if (el.parentElement) el.parentElement.click();
            return { success: true, text: t };
        }
    }
}
const texts = [];
for (const root of roots) {
    for (const el of root.querySelectorAll('button, [class*="btn"], [class*="submit"], a')) {
        const t = el.textContent.trim().replace(/\\s+/g, ' ');
        if (t && t.length <= 12 && vis(el) && !texts.includes(t)) texts.push(t);
    }
}
return { success: false, buttons: texts.slice(0, 20) };
"""


def find_and_click_submit(driver, timeout=5.0):
    """轮询等提交按钮出现(部分课堂选中选项后才渲染), 返回 (是否成功, 按钮文字或可见按钮清单)"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = driver.execute_script(SUBMIT_FIND_CLICK_JS) or {}
        except Exception:
            last = {}
        if last.get("success"):
            return True, last.get("text")
        time.sleep(1)
    return False, (last or {}).get("buttons") or []


CLICK_OPTION_JS = """
const ch = arguments[0];
// 逐个字母调用: 每次点击后 Vue 会重渲染选项列表, 必须重新查询 DOM,
// 一次查好存数组再点的写法第二个选项起全是已脱离的旧元素, 点了无效
const widgets = Array.from(document.querySelectorAll(
    '[class*="option"], [class*="choice"], [class*="answer-item"]'
)).filter(el => el.offsetWidth > 0 && typeof el.className === 'string'
    && !/page|nav|slide|thumb|tab|menu/i.test(el.className));
let hit = widgets.find(el => new RegExp('^' + ch + '([.、．\\\\s]|$)').test((el.textContent || '').trim()));
if (!hit) {
    const allEls = Array.from(document.querySelectorAll('p, span, div, li'));
    hit = allEls.find(el => el.children.length === 0 && el.textContent.trim() === ch && el.offsetWidth > 0);
}
if (hit) {
    hit.click();
    if (hit.parentElement) hit.parentElement.click();
    return true;
}
return false;
"""

COUNT_SELECTED_JS = """
return Array.from(document.querySelectorAll(
    '[class*="option"], [class*="choice"], [class*="answer-item"]'
)).filter(el => {
    const c = typeof el.className === 'string' ? el.className : '';
    return el.offsetWidth > 0 && /select|active|checked|chosen/i.test(c);
}).length;
"""


def click_choice_options(driver, answer):
    """按答案字母逐个点击选项(每击重新查 DOM), 返回点击后选中的选项数"""
    letters = list(dict.fromkeys(c for c in answer.upper() if 'A' <= c <= 'Z'))
    for ch in letters:
        try:
            driver.execute_script(CLICK_OPTION_JS, ch)
        except Exception:
            continue
        time.sleep(0.15)
    time.sleep(0.8)
    try:
        return driver.execute_script(COUNT_SELECTED_JS) or 0
    except Exception:
        return 0


ZUODA_CLICK_JS = """
const all = Array.from(document.querySelectorAll('*'));
const zuoda = all.find(el => el.children.length === 0 && el.textContent.trim() === '作答' && el.offsetWidth > 0);
if (zuoda) {
    zuoda.click();
    if (zuoda.parentElement) zuoda.parentElement.click();
}
"""

COUNT_BLANKS_JS = """
const drawer = document.querySelector('[class*="drawer"], [class*="sheet"], [class*="sidebar"]');
const root = drawer || document;
return Array.from(root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"]'))
    .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0).length;
"""

FILL_BLANKS_JS = """
const answers = arguments[0];
const drawer = document.querySelector('[class*="drawer"], [class*="sheet"], [class*="sidebar"]');
const root = drawer || document;
let taList = Array.from(root.querySelectorAll('textarea.blank__input, input.blank__input, textarea, input[type="text"], [contenteditable="true"]'))
    .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0);
if (taList.length === 0) taList = Array.from(document.querySelectorAll('textarea, input[type="text"]'));
for (let i = 0; i < taList.length; i++) {
    const ta = taList[i];
    const val = i < answers.length ? answers[i] : (answers.length === 1 ? answers[0] : '');
    ta.focus();
    if (ta.tagName === 'TEXTAREA' || ta.tagName === 'INPUT') {
        ta.value = val;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        ta.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ' ' }));
    } else {
        ta.innerText = val;
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
    }
}
"""


def fill_blank_answer(driver, answer, q_type):
    """打开作答抽屉并填入填空/主观题答案; 多空按竖线拆分"""
    driver.execute_script(ZUODA_CLICK_JS)
    time.sleep(1.5)
    detected = driver.execute_script(COUNT_BLANKS_JS) or 0
    if "主观" in q_type:
        ans_list = [answer]
    else:
        ans_list = split_blank_answers(answer, expected_count=detected)
    driver.execute_script(FILL_BLANKS_JS, ans_list)
    time.sleep(1)


def submit_answer(driver, auto_submit):
    """提交作答; 返回提交状态描述"""
    if not auto_submit:
        emit_log("自动提交已关闭，答案已填入")
        return "未自动提交"
    ok, extra = find_and_click_submit(driver, timeout=SUBMIT_WAIT_SECONDS)
    if ok:
        emit_log("已提交")
        return "已提交"
    btns = "、".join((extra or [])[:12]) if extra else "无"
    emit_log(f"未找到提交按钮（部分题型选中即自动提交；页面可见按钮: {btns}）")
    return "未找到提交按钮"


def handle_quiz(driver, cfg, quiz_info, auto_submit, enable_mm):
    q_type = quiz_info.get("qType", "单选题")
    options = quiz_info.get("options", ["A", "B", "C", "D"])
    dom_text = (quiz_info.get("domText") or "").strip()
    evidence = quiz_info.get("evidence") or "DOM"
    t_stamp = ts()

    emit_log(f"[{t_stamp}] 检测到题目（{q_type}，触发依据: {evidence}）")
    try:
        driver.save_screenshot(QUIZ_SHOT)
        shot = QUIZ_SHOT
    except Exception:
        shot = None

    question_content = dom_text
    if len(question_content) < 10 or "PPT" in question_content:
        if enable_mm:
            question_content = question_content or "题面见图片，请读取题目与选项作答"

    try:
        ans = call_solver(cfg, question_content, q_type, options, image_path=shot)
    except SolverUnavailable as e:
        emit_log(f"本题未作答: {e}")
        return {
            "event": "question",
            "time": t_stamp,
            "qtype": q_type,
            "question": question_content[:120],
            "answer": "（未作答）",
            "submit": f"未作答: {e}",
        }

    submit_desc = "未自动提交"
    try:
        if "选" in q_type:
            n_sel = click_choice_options(driver, ans)
            if n_sel:
                emit_log(f"已选中 {n_sel} 个选项")

        if "填空" in q_type or "主观" in q_type:
            fill_blank_answer(driver, ans, q_type)

        submit_desc = submit_answer(driver, auto_submit)
    except Exception as e:
        submit_desc = f"作答异常: {str(e)[:60]}"
        emit_log(submit_desc)

    return {
        "event": "question",
        "time": t_stamp,
        "qtype": q_type,
        "question": question_content[:120],
        "answer": ans[:200],
        "submit": submit_desc,
    }


# ---------------------------------------------------------------------------
# 模式三: 合并导出 PDF + TXT
# ---------------------------------------------------------------------------

def detect_chapters(cfg, pages):
    """用文本模型划分章节, 并顺手清洗每页 OCR 文本(去图片标记/界面残留/重复页眉)。
    pages: [{index,page,ocr,pres_switch}], 清洗结果直接写回 p["ocr"] 并随 slides.json 持久化"""
    api_base = cfg.get("api_base", DEFAULT_CONFIG["api_base"]).rstrip("/")
    api_key = cfg.get("api_key", "")
    model = (cfg.get("models") or DEFAULT_CONFIG["models"])[0]
    if not api_key:
        emit_log("未配置模型 API Key，全部页面合并为一个文件，文本不清洗")
        return None

    parts = []
    for p in pages:
        mark = "|课件切换" if p.get("pres_switch") else ""
        parts.append(f"【第{p['index']}页{mark}】\n{(p.get('ocr') or '').strip()}")
    listing = "\n\n".join(parts)

    prompt = f"""你是课件整理助手。下面是一次课每页课件的 OCR 文本，请完成两件事。

一、清理每页文本（目标是 txt 阅读体验，不改内容只去噪音）：
1. 删除所有图片标记（如 <div...><img src="imgs/...">...</div>、![](...)、空段落）；
2. 删除课堂界面残留：课堂动态、正在放映、已签到、收藏、不懂、发送、说点什么、50/50、弹幕、「N 分钟前」「N 秒前」、第N页 等；
3. 每页重复出现的标题文字（课程名、章节名、学校院系名、教师姓名邮箱等页眉页脚）只保留第一次出现，后续页删除；
4. 正文、表格、公式原样保留，不要改写、不要总结、不要增删。

二、划分章节。判定规则：
1. 出现新的章号（如从「第4章」变为「第5章」）即开始新章节；
2. 某页开头出现明显的一行独立大字标题（与上一页内容主题明显不同）也视为新章节；
3. 标注「课件切换」的页是老师换了课件文件，可作参考；
4. 每章标题取该章起始页的标题文字，20 字以内；没有明显章节时输出单一章节，标题用课件主题或「课件」。

严格输出 JSON（不要输出任何其他内容）：
{{"chapters":[{{"title":"第1章 函数与极限","pages":[1,2,3]}}],"cleaned":{{"1":"该页清理后的文本","2":"..."}}}}
chapters 的 pages 用页序号，按顺序覆盖全部 {len(pages)} 页，不遗漏不重复；cleaned 的键是页序号字符串。

{listing}"""

    try:
        out = _chat(api_base, api_key, model,
                    [{"role": "user", "content": prompt}],
                    timeout=60, max_tokens=8000, temperature=0.1)
        m = re.search(r'\{.*\}', out, re.S)
        data = json.loads(m.group(0))

        cleaned = data.get("cleaned") or {}
        n_clean = 0
        for p in pages:
            t = cleaned.get(str(p["index"]))
            if isinstance(t, str) and len(t.strip()) >= 5:
                p["ocr"] = t.strip()
                n_clean += 1
        if n_clean:
            emit_log(f"已清洗 {n_clean}/{len(pages)} 页文本（去图片标记与重复标题）")

        chapters = data.get("chapters") or []
        # 动画页剔除后 index 可能不连续, 校验集合必须用真实页序号
        real_indexes = {p["index"] for p in pages}
        valid = []
        seen = set()
        for ch in chapters:
            pgs = [x for x in (ch.get("pages") or [])
                   if isinstance(x, int) and x in real_indexes and x not in seen]
            if pgs:
                for x in pgs:
                    seen.add(x)
                valid.append({"title": sanitize_filename(ch.get("title") or f"第{len(valid)+1}部分", 24),
                              "pages": sorted(pgs)})
        if not valid or len(seen) < len(real_indexes) // 2:
            emit_log("章节划分结果不完整，按单一文件处理")
            return None
        # 未覆盖的页并入最后一章
        missing = sorted(real_indexes - seen)
        if missing and valid:
            valid[-1]["pages"].extend(missing)
            valid[-1]["pages"].sort()
        return valid
    except Exception as e:
        emit_log(f"章节判定失败: {str(e)[:80]}，按单一文件处理")
        return None


def run_merge(session_dir):
    cfg = load_config()
    meta_path = os.path.join(session_dir, "slides.json")
    if not os.path.exists(meta_path):
        emit_log("会话目录中没有 slides.json，无法导出")
        emit({"event": "merge_done", "ok": False, "out_dir": None})
        return
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    raw_pages = [p for p in meta.get("pages", []) if os.path.exists(os.path.join(session_dir, p["file"]))]
    # 剔除动画提示页(雨课堂「当前页面有动画」覆盖层, 没有课件内容)
    pages = [p for p in raw_pages if not is_animation_notice(p.get("ocr"))]
    skipped = len(raw_pages) - len(pages)
    if skipped:
        emit_log(f"已剔除 {skipped} 张动画提示页")
    if not pages:
        emit_log("没有可用的课件图片")
        emit({"event": "merge_done", "ok": False, "out_dir": None})
        return

    date = meta.get("date") or time.strftime("%Y-%m-%d")
    course = meta.get("course") or "未命名"
    emit_log(f"开始合并 {course}，共 {len(pages)} 张")

    chapters = detect_chapters(cfg, pages)
    if not chapters:
        chapters = [{"title": "课件", "pages": [p["index"] for p in pages]}]

    out_root = os.path.expanduser(cfg.get("slide_dir") or DEFAULT_CONFIG["slide_dir"])
    out_dir = os.path.join(out_root, f"{date}-{course}")
    os.makedirs(out_dir, exist_ok=True)

    items = []
    ok_all = True
    try:
        import img2pdf
        by_index = {p["index"]: p for p in pages}
        for ch in chapters:
            base_name = f"{date}-{course}-{ch['title']}"
            # 不覆盖历史: 同名存在时追加批次序号
            seq = 1
            while os.path.exists(os.path.join(out_dir, base_name + ("" if seq == 1 else f"-第{seq}次") + ".pdf")):
                seq += 1
            if seq > 1:
                base_name += f"-第{seq}次"
            pdf_path = os.path.join(out_dir, base_name + ".pdf")
            txt_path = os.path.join(out_dir, base_name + "-文本.txt")

            imgs = [os.path.join(session_dir, by_index[i]["file"]) for i in ch["pages"] if i in by_index]
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(imgs))

            parts = []
            for i in ch["pages"]:
                p = by_index.get(i)
                if not p:
                    continue
                parts.append(f"【第{p['page']}页】\n{(p.get('ocr') or '').strip()}\n")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(parts))

            items.append({"title": ch["title"], "pages": ch["pages"], "pdf": os.path.basename(pdf_path)})
            emit_log(f"已生成 {base_name}.pdf（{len(imgs)} 页）及文本")
    except Exception as e:
        ok_all = False
        emit_log(f"合并失败: {str(e)[:100]}")

    if ok_all:
        # 只清理已导出的图片, 保留目录与 slides.json —— 监听可能仍在写入
        # (导出后继续扫描的新图落在同一目录, 下次导出按图片存在性只合并新页)
        removed = 0
        exported_names = {p["file"] for p in pages}
        excluded = [p for p in meta.get("pages", [])
                    if p.get("file") not in exported_names and is_animation_notice(p.get("ocr"))]
        for p in pages + excluded:   # 被剔除的动画提示页文件一并删除
            try:
                os.remove(os.path.join(session_dir, p["file"]))
                removed += 1
            except Exception:
                pass
        # 写回前重读磁盘: 监听可能在导出期间新增了页面, 直接覆盖会丢页
        # 合并策略: 以磁盘最新列表为准, 同名文件用内存版本(含 AI 清洗后的文本)
        try:
            with open(meta_path, encoding="utf-8") as f:
                disk_meta = json.load(f)
            by_file = {p.get("file"): p for p in meta.get("pages", [])}
            merged = [by_file.get(p.get("file"), p)
                      for p in disk_meta.get("pages", [])]
            merged += [p for p in meta.get("pages", [])
                       if p.get("file") not in {q.get("file") for q in merged}]
            meta["pages"] = merged
        except Exception:
            pass
        meta["pages"] = [p for p in meta.get("pages", [])
                         if os.path.exists(os.path.join(session_dir, p["file"]))]
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        emit_log(f"已导出的 {removed} 张缓存图片已清理，文件保存在 {out_dir}")
    emit({"event": "chapters", "items": items})
    emit({"event": "merge_done", "ok": ok_all, "out_dir": out_dir if ok_all else None})


def run_cache_list():
    sessions = []
    for meta_path in sorted(glob.glob(os.path.join(SLIDE_CACHE_ROOT, "*", "slides.json"))):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            d = os.path.dirname(meta_path)
            n_imgs = len([p for p in meta.get("pages", [])
                          if os.path.exists(os.path.join(d, p["file"]))])
            sessions.append({"dir": d, "course": meta.get("course"), "date": meta.get("date"), "count": n_imgs})
        except Exception:
            continue
    print(json.dumps({"sessions": sessions}, ensure_ascii=False), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["scan", "listen", "merge", "cache-list"])
    ap.add_argument("--dir", default=None, help="merge 模式的会话目录")
    ap.add_argument("--scan", action="store_true", help="listen 模式启动即开启课件扫描")
    args = ap.parse_args()

    if args.mode == "scan":
        run_scan()
    elif args.mode == "listen":
        run_listen(scan_default=args.scan)
    elif args.mode == "merge":
        if not args.dir:
            emit_log("merge 需要 --dir 参数")
            emit({"event": "merge_done", "ok": False, "out_dir": None})
            return
        run_merge(args.dir)
    else:
        run_cache_list()


if __name__ == "__main__":
    main()
