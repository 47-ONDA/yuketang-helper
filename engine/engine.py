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
import shutil
import argparse
import threading
import subprocess
import base64

import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, WebDriverException

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(os.path.expanduser("~"), ".yuketang-helper")
CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.json")
PROFILE_DIR = os.path.join(RUNTIME_DIR, "ykt_profile")
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")
QUIZ_SHOT = os.path.join(RUNTIME_DIR, "current_problem.png")
SLIDE_CACHE_ROOT = os.path.join(RUNTIME_DIR, "课件缓存")

BROWSER_APPS = [
    ("chrome", "/Applications/Google Chrome.app"),
    ("edge", "/Applications/Microsoft Edge.app"),
]

DEFAULT_CONFIG = {
    "yuketang_base_url": "https://changjiang.yuketang.cn",
    "browser": "chrome",
    "api_base": "",
    "api_key": "",
    "models": [],
    "enable_multimodal": True,
    "multimodal_models": [],
    "auto_submit": True,
    "listen_interval": 1.0,
    "ocr_primary": {"api_base": "", "api_key": "", "model": ""},
    "ocr_backup": {"api_base": "", "api_key": "", "model": ""},
    "slide_dir": "~/Documents/雨课堂课件",
}


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def emit_log(msg):
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
# macOS 适配 / 浏览器
# ---------------------------------------------------------------------------

_caffeinate_proc = None

def prevent_system_sleep():
    global _caffeinate_proc
    if sys.platform == "darwin" and _caffeinate_proc is None:
        try:
            _caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-dis"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def restore_system_sleep():
    global _caffeinate_proc
    if _caffeinate_proc is not None:
        try:
            _caffeinate_proc.terminate()
        except Exception:
            pass
        _caffeinate_proc = None


def ensure_browser_clean():
    try:
        subprocess.run(["pkill", "-f", f"--user-data-dir={PROFILE_DIR}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.5)
    except Exception:
        pass


def detect_browser(pref):
    order = [n for n, _ in BROWSER_APPS]
    if pref in ("chrome", "edge"):
        order = [pref] + [n for n in order if n != pref]
    for name in order:
        if os.path.exists(dict(BROWSER_APPS)[name]):
            return name
    return None


def resolve_driver_path():
    p = os.environ.get("YKT_DRIVER")
    if p and os.path.exists(p):
        return p
    p = os.path.join(ENGINE_DIR, "chromedriver")
    return p if os.path.exists(p) else None


def get_driver(cfg, headless=False):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    last_err = None
    for attempt in range(1, 4):
        ensure_browser_clean()
        try:
            return _launch_browser(cfg, headless)
        except Exception as e:
            last_err = e
            emit_log(f"浏览器启动失败 (第 {attempt}/3 次)")
            try:
                subprocess.run(["pkill", "-9", "-f", f"--user-data-dir={PROFILE_DIR}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

    drv = resolve_driver_path()
    service = Service(executable_path=drv) if drv else None
    if service:
        return driver_cls(options=opts, service=service)
    return driver_cls(options=opts)


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------

def _chat(api_base, api_key, model, messages, timeout=15, max_tokens=500, temperature=0.1):
    """OpenAI 兼容调用; deepseek 接口关闭深度思考以保证响应速度"""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if "deepseek" in api_base:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["max_tokens"] = min(max_tokens, 1024)  # 部分平台(如智谱)上限 1024
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


def call_solver(cfg, question_text, q_type, options=None, image_path=None):
    api_base = cfg.get("api_base", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    models_pool = list(cfg.get("models", []))
    enable_mm = cfg.get("enable_multimodal", True)
    mm_models = list(cfg.get("multimodal_models", []))

    if not api_key or api_key == "YOUR_API_KEY_HERE" or not api_base or not models_pool:
        emit_log("模型 API 未配置")
        if "单选" in q_type: return "C"
        elif "多选" in q_type: return "ABCD"
        return "已收到并作答"

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

    if "单选" in q_type: return "C"
    elif "多选" in q_type: return "ABCD"
    return "已收到并作答"


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

const unfin = Array.from(document.querySelectorAll('.timeline__item.J_slide, .timeline__item'))
    .find(el => el.innerText.includes('未完成'));
if (unfin && !unfin.className.includes('active')) {
    unfin.click();
}

const bodyText = document.body.innerText || '';
const isCompleted = bodyText.includes('已完成') && !bodyText.includes('未完成');
const hasTiming = Array.from(document.querySelectorAll('*')).some(el =>
    (el.className && typeof el.className === 'string' && el.className.includes('timing')) ||
    el.textContent.includes('倒计时')
);
const hasSubmit = Array.from(document.querySelectorAll('*')).some(el =>
    (el.className && typeof el.className === 'string' && el.className.includes('submit-btn')) ||
    el.textContent.includes('提交答案')
);
const centerCanvas = document.querySelector('.ppt__wrapper, .lesson__page, .presentation, .center-area');
const centerText = centerCanvas ? centerCanvas.innerText : '';

const hasZuoda = centerCanvas && Array.from(centerCanvas.querySelectorAll('*')).some(el =>
    el.children.length === 0 && el.textContent.trim() === '作答' && el.offsetWidth > 0
);

if ((hasTiming || hasSubmit || hasZuoda) && !isCompleted) {
    info.hasQuiz = true;
    info.probId = currSlide ? (currSlide.problemID || currSlide.sid || currSlide.slideID) : null;
    info.domText = (currSlide ? (currSlide.body || currSlide.title || '') : '') || centerText.slice(0, 300);

    const centerOpts = [];
    if (centerCanvas) {
        const pList = Array.from(centerCanvas.querySelectorAll('p, span, div, li'));
        for (const p of pList) {
            const t = p.textContent.trim();
            if (['A', 'B', 'C', 'D', 'E', 'F'].includes(t) && p.children.length === 0 && p.offsetWidth > 0) {
                centerOpts.push(t);
            }
        }
    }
    const uniqueOpts = Array.from(new Set(centerOpts));
    const hasChoiceOptions = uniqueOpts.length >= 2;

    let qType = '单选题';
    const probType = currSlide ? currSlide.problemType : null;
    if (hasChoiceOptions) {
        if (probType === 2 || centerText.includes('多选')) qType = '多选题';
        else qType = '单选题';
    } else {
        if (probType === 5 || centerText.includes('主观') || centerText.includes('简答') || (hasZuoda && !centerText.includes('填空'))) qType = '主观题';
        else if (probType === 3 || probType === 4 || centerText.includes('填空') || (hasZuoda && centerText.includes('填空'))) qType = '填空题';
        else if (probType === 2 || centerText.includes('多选')) qType = '多选题';
        else if (probType === 1 || centerText.includes('单选')) qType = '单选题';
        else qType = hasZuoda ? '主观题' : '单选题';
    }
    info.qType = qType;
    if (qType.includes('选')) info.options = uniqueOpts.length > 0 ? uniqueOpts : ['A','B','C','D'];
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
    // 大屏中央区域的原图候选: img 元素 src 与 CSS 背景图
    const center = document.querySelector('.ppt__wrapper, .lesson__page, .presentation, .center-area');
    const urls = [];
    if (center) {
        for (const img of center.querySelectorAll('img')) {
            const s = img.currentSrc || img.src;
            if (s && /^https?:/.test(s) && img.offsetWidth > 200) urls.push(s);
        }
        const els = [center].concat(Array.from(center.querySelectorAll('div')).slice(0, 300));
        for (const el of els) {
            try {
                const bg = getComputedStyle(el).backgroundImage;
                const m = bg && bg.match(/url\(["']?(.*?)["']?\)/);
                if (m && /^https?:/.test(m[1])) urls.push(m[1]);
            } catch(e) {}
        }
    }
    out.imgUrls = Array.from(new Set(urls));
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
        self.index = 0

    def bind_course(self, course_name):
        if self.course is None and course_name:
            self.course = sanitize_filename(course_name)
            self.dir = os.path.join(SLIDE_CACHE_ROOT, f"{self.date}-{self.course}")
            os.makedirs(self.dir, exist_ok=True)
            self.emit_session()

    def emit_session(self):
        emit({"event": "session", "dir": self.dir, "course": self.course,
              "date": self.date, "count": len(self.pages)})

    def save_slide(self, data_bytes, ext="png", page=None, pres_switch=False):
        """保存一张课件图片(优先原图字节), 返回文件名"""
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
            with open(os.path.join(self.dir, "slides.json"), "w", encoding="utf-8") as f:
                json.dump({"date": self.date, "course": self.course, "pages": self.pages},
                          f, ensure_ascii=False, indent=1)


def download_image(driver, url, referer):
    """用浏览器会话的 Cookie 下载原图, 返回 (bytes, ext) 或 (None, None)"""
    try:
        s = requests.Session()
        for c in driver.get_cookies():
            s.cookies.set(c.get("name"), c.get("value"), domain=c.get("domain"))
        r = s.get(url, headers={"Referer": referer, "User-Agent":
                  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                  timeout=15)
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
        for _ in range(200):
            time.sleep(1.5)
            try:
                st = driver.execute_script(LOGIN_STATE_JS)
            except WebDriverException:
                emit_log("浏览器已关闭，登录态已保存")
                ok = True
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
            with open(STATE_PATH) as f:
                state = json.load(f)
        except Exception:
            pass
    state["logged_in_as"] = name or state.get("logged_in_as")
    state["last_login_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 模式二: 监听 + 答题 + 课件扫描
# ---------------------------------------------------------------------------

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
        except Exception:
            pass
    t = threading.Thread(target=loop, daemon=True)
    t.start()


def run_listen():
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
    except Exception as e:
        emit({"event": "status", "listening": False})
        emit({"event": "error", "msg": str(e)})
        return

    # 课件扫描状态
    scan_on = {"on": False}
    session = {"s": None}   # 当前 SlideSession

    def set_scan(state):
        scan_on["on"] = state
        emit({"event": "scan_state", "on": state})
        emit_log("课件扫描已开启" if state else "课件扫描已关闭")

    stdin_command_loop({
        "scan_on": lambda c: set_scan(True),
        "scan_off": lambda c: set_scan(False),
    })

    emit({"event": "status", "listening": True})
    prevent_system_sleep()
    answered = set()

    def handle_slide():
        """课件翻页检测与抓取, 在监听线程内同步执行"""
        if not scan_on["on"]:
            return
        try:
            info = driver.execute_script(SLIDE_INFO_JS)
        except Exception:
            return
        if not info or not info.get("sid"):
            return
        sid = info["sid"]
        s = session["s"]
        if s and sid in s.captured_sids:
            return
        if s is None:
            return  # 还没进课堂, 无课程名
        pres_switch = (s.last_pres_id is not None and info.get("presId")
                       and info.get("presId") != s.last_pres_id)
        s.last_pres_id = info.get("presId") or s.last_pres_id
        s.captured_sids.add(sid)

        referer = driver.current_url
        data = ext = None
        for url in (info.get("imgUrls") or [])[:3]:
            data, ext = download_image(driver, url, referer)
            if data:
                emit_log(f"第 {s.index + 1} 张: 原图下载")
                break
        if not data:
            emit_log(f"第 {s.index + 1} 张: 未取到原图，使用页面截图")
            path, fname = s.save_screenshot_slide(driver, page=info.get("page"),
                                                  pres_switch=pres_switch)
        else:
            path, fname = s.save_slide(data, ext, page=info.get("page"),
                                       pres_switch=pres_switch)

        text, ok = ocr_image(cfg, path)
        if s.pages:
            s.pages[-1]["ocr"] = text
        s.flush_meta()
        head = text.replace("\n", " ")[:40] if text else ""
        emit_log(f"第 {s.index} 张已保存" + (f"，识别 {len(text)} 字" if ok else "，识别失败"))
        if pres_switch:
            emit_log("检测到课件切换")
        emit({"event": "slide", "index": s.index,
              "page": s.pages[-1]["page"] if s.pages else s.index,
              "file": fname, "ocr_head": head, "ocr_ok": ok})
        s.emit_session()

    try:
        driver.get(f"{base_url}/v2/web/index")
        time.sleep(2)
        emit_log("监听已启动")

        while True:
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
                        if session["s"] is None or session["s"].course != sanitize_filename(c_name):
                            session["s"] = SlideSession(cfg)
                            session["s"].bind_course(c_name)
                        driver.get(f"{base_url}/lesson/fullscreen/v3/{l_id}")
                        time.sleep(4)
                        continue
                    else:
                        time.sleep(3)
                        continue

                quiz_info = driver.execute_script(QUIZ_PROBE_JS)

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

                handle_slide()

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
        s = session.get("s")
        if s:
            s.flush_meta()
        emit({"event": "status", "listening": False})


def handle_quiz(driver, cfg, quiz_info, auto_submit, enable_mm):
    q_type = quiz_info.get("qType", "单选题")
    options = quiz_info.get("options", ["A", "B", "C", "D"])
    dom_text = (quiz_info.get("domText") or "").strip()
    t_stamp = ts()

    emit_log(f"[{t_stamp}] 检测到题目（{q_type}）")
    try:
        driver.save_screenshot(QUIZ_SHOT)
        shot = QUIZ_SHOT
    except Exception:
        shot = None

    question_content = dom_text
    if len(question_content) < 10 or "PPT" in question_content:
        if enable_mm:
            question_content = question_content or "题面见图片，请读取题目与选项作答"

    ans = call_solver(cfg, question_content, q_type, options, image_path=shot)

    submit_desc = "未自动提交"
    try:
        if "选" in q_type:
            letters = [c for c in ans.upper() if 'A' <= c <= 'Z']
            driver.execute_script("""
                const letters = arguments[0];
                const allEls = Array.from(document.querySelectorAll('p, span, div, li'));
                for (const ch of letters) {
                    let optEl = allEls.find(el => el.children.length === 0 && el.textContent.trim() === ch && el.offsetWidth > 0);
                    if (optEl) {
                        optEl.click();
                        if (optEl.parentElement) optEl.parentElement.click();
                    }
                }
            """, letters)
            time.sleep(1)

        if "填空" in q_type or "主观" in q_type:
            driver.execute_script("""
                const all = Array.from(document.querySelectorAll('*'));
                const zuoda = all.find(el => el.children.length === 0 && el.textContent.trim() === '作答' && el.offsetWidth > 0);
                if (zuoda) {
                    zuoda.click();
                    if (zuoda.parentElement) zuoda.parentElement.click();
                }
            """)
            time.sleep(1.5)
            detected = driver.execute_script("""
                const drawer = document.querySelector('[class*="drawer"], [class*="sheet"], [class*="sidebar"]');
                const root = drawer || document;
                return Array.from(root.querySelectorAll('textarea, input[type="text"], [contenteditable="true"]'))
                    .filter(el => el.offsetWidth > 0 || el.offsetHeight > 0).length;
            """) or 0
            if "主观" in q_type:
                ans_list = [ans]
            else:
                ans_list = split_blank_answers(ans, expected_count=detected)
            driver.execute_script("""
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
            """, ans_list)
            time.sleep(1)

        if auto_submit:
            if "填空" in q_type or "主观" in q_type:
                sub_res = driver.execute_script("""
                    const btnBoxes = Array.from(document.querySelectorAll('.btn-box, [class*="drawer"], .submission-btn'));
                    for (const box of btnBoxes) {
                        const btn = Array.from(box.querySelectorAll('button, span, div, a')).find(el =>
                            ['提交答案', '提交'].includes(el.textContent.trim()) && el.offsetWidth > 0);
                        if (btn) { btn.click(); if (btn.parentElement) btn.parentElement.click(); return {success:true,target:'drawer'}; }
                    }
                    const all = Array.from(document.querySelectorAll('*')).reverse();
                    const sub = all.find(el => el.children.length === 0 && ['提交答案','提交'].includes(el.textContent.trim()) && el.offsetWidth > 0);
                    if (sub) { sub.click(); if (sub.parentElement) sub.parentElement.click(); return {success:true,target:'reverse'}; }
                    return {success:false};
                """)
            else:
                sub_res = driver.execute_script("""
                    const allEls = Array.from(document.querySelectorAll('button, .submit-btn, div, span, a'));
                    const sub = allEls.find(b => {
                        const t = b.textContent.trim();
                        const cls = b.className || '';
                        return b.offsetWidth > 0 && (t === '提交答案' || t === '提交' ||
                            (typeof cls === 'string' && cls.includes('submit-btn')));
                    });
                    if (sub) { sub.click(); if (sub.parentElement) sub.parentElement.click(); return {success:true,target:'canvas'}; }
                    return {success:false};
                """)
            submit_desc = "已提交" if (sub_res and sub_res.get("success")) else "提交按钮未找到"
            emit_log("已提交" if submit_desc == "已提交" else f"提交失败: 未找到提交按钮")
        else:
            emit_log("自动提交已关闭，答案已填入")
            submit_desc = "未自动提交"
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
    """用文本模型把页面划分为章节。pages: [{index,page,ocr,pres_switch}]"""
    api_base = cfg.get("api_base", "").rstrip("/")
    api_key = cfg.get("api_key", "")
    model = (cfg.get("models") or [""])[0]
    if not (api_base and api_key and model):
        emit_log("模型 API 未配置，全部页面合并为一个文件")
        return None

    lines = []
    for p in pages:
        mark = "switch" if p.get("pres_switch") else "-"
        head = (p.get("ocr") or "").replace("\n", " ")[:120]
        lines.append(f"{p['index']}|{mark}|{head}")
    listing = "\n".join(lines)

    prompt = f"""你是课件整理助手。下面是一次课的课件页面清单，每行格式为「页序号|课件切换标记|该页文字开头」。

请把页面划分为章节并输出。判定规则：
1. 出现新的章号（如从「第4章」变为「第5章」）即开始新章节；
2. 某页开头出现明显的一行独立大字标题（与上一页内容主题明显不同）也视为新章节；
3. 标记为 switch 的行表示老师切换了课件文件，可作为参考；
4. 每章标题取该章起始页的标题文字，20 字以内，去掉页码序号；
5. 若整份课件没有明显章节划分，输出单一章节，标题用课件主题或「课件」。

严格输出 JSON（不要输出任何其他内容）：
{{"chapters":[{{"title":"第1章 函数与极限","pages":[1,2,3]}},{{"title":"...","pages":[4,5]}}]}}
pages 使用页序号，必须按顺序覆盖全部 {len(pages)} 页，不遗漏不重复。

清单：
{listing}"""

    try:
        out = _chat(api_base, api_key, model,
                    [{"role": "user", "content": prompt}],
                    timeout=30, max_tokens=1500, temperature=0.1)
        m = re.search(r'\{.*\}', out, re.S)
        data = json.loads(m.group(0))
        chapters = data.get("chapters") or []
        valid = []
        seen = set()
        for ch in chapters:
            pgs = [x for x in (ch.get("pages") or [])
                   if isinstance(x, int) and 1 <= x <= len(pages) and x not in seen]
            if pgs:
                for x in pgs:
                    seen.add(x)
                valid.append({"title": sanitize_filename(ch.get("title") or f"第{len(valid)+1}部分", 24),
                              "pages": sorted(pgs)})
        if not valid or len(seen) < len(pages) // 2:
            emit_log("章节划分结果不完整，按单一文件处理")
            return None
        # 未覆盖的页并入最后一章
        missing = [i for i in range(1, len(pages) + 1) if i not in seen]
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
    pages = [p for p in meta.get("pages", []) if os.path.exists(os.path.join(session_dir, p["file"]))]
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
        shutil.rmtree(session_dir, ignore_errors=True)
        emit_log(f"缓存图片已清理，文件保存在 {out_dir}")
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
    args = ap.parse_args()

    if args.mode == "scan":
        run_scan()
    elif args.mode == "listen":
        run_listen()
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
