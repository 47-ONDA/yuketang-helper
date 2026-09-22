# -*- coding: utf-8 -*-
"""雨课堂助手 - Windows GUI (tkinter)

与 engine 子进程通过 stdin/stdout JSON 行协议通信, 事件与 macOS 版完全一致:
status/log/login/question/session/slide/slide_ocr/slide_removed/scan_state/chapters/merge_done/error
内部事件: _exited(进程退出) / _cache_list(待导出会话查询结果)

源码运行:  python gui/gui.py          (相对仓库根)
打包运行:  ykt-helper-gui.exe         (同级目录需有 engine.exe)
"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

if sys.platform.startswith("win"):
    subprocess_flags = subprocess.CREATE_NO_WINDOW
else:
    subprocess_flags = 0

RUNTIME_DIR = os.path.join(os.path.expanduser("~"), ".yuketang-helper")
CONFIG_PATH = os.path.join(RUNTIME_DIR, "config.json")
STATE_PATH = os.path.join(RUNTIME_DIR, "state.json")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def engine_command(args):
    """返回启动引擎子进程的命令行 (打包后用同级 engine.exe, 源码运行用 python)"""
    if getattr(sys, "frozen", False):
        return [os.path.join(os.path.dirname(sys.executable), "engine.exe")] + args
    return [sys.executable, "-u", os.path.join(_ROOT, "engine", "engine.py")] + args


def load_config():
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    return cfg


def save_config(cfg):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class EngineProcess:
    """引擎子进程: 行协议解析 + 事件回调 + 退出通知"""

    def __init__(self, args, on_event, role=""):
        self.on_event = on_event
        self.role = role
        self.proc = subprocess.Popen(
            engine_command(args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess_flags)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 进程退出通知: 崩溃/被杀时界面也能恢复状态
        self._exit_watcher = threading.Thread(target=self._exit_loop, daemon=True)
        self._exit_watcher.start()

    def _read_loop(self):
        try:
            # readline 逐行读取; for-line 迭代有缓冲预读, 事件会攒批延迟到达
            while True:
                raw = self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("{"):
                    try:
                        self.on_event(json.loads(line))
                    except Exception:
                        continue
                else:
                    # 引擎的报错原文(traceback / chromedriver 错误)不是 JSON,
                    # 原样透传到日志页——否则启动失败时界面毫无线索
                    self.on_event({"event": "log", "msg": "引擎: " + line})
        except Exception:
            pass

    def _exit_loop(self):
        self.proc.wait()
        self.on_event({"event": "_exited", "role": self.role, "code": self.proc.returncode})

    def send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            pass

    def stop(self):
        self.send({"cmd": "stop"})

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    def alive(self):
        return self.proc.poll() is None


def _set_nested(cfg, dotted, value):
    """按 a.b 路径写入嵌套配置字段"""
    if "." in dotted:
        part, field = dotted.split(".", 1)
        sub = dict(cfg.get(part) or {})
        sub[field] = value
        cfg[part] = sub
    else:
        cfg[dotted] = value


def _get_nested(cfg, dotted, default=""):
    if "." in dotted:
        part, field = dotted.split(".", 1)
        return (cfg.get(part) or {}).get(field, default)
    return cfg.get(dotted, default)


class SettingsDialog(tk.Toplevel):
    """模型 / 视觉模型 / 课件识别接口 / 保存目录 / 提交行为设置"""

    # (标签, 配置键, 类型); ocr 字段用 part.field 点路径
    FIELDS = [
        ("模型接口地址 (OpenAI 兼容)", "api_base", "str"),
        ("模型 API Key", "api_key", "secret"),
        ("模型名 (多个用英文逗号分隔)", "models", "csv"),
        ("视觉模型名 (留空则使用模型名)", "multimodal_models", "csv"),
        ("课件识别首选 地址", "ocr_primary.api_base", "str"),
        ("课件识别首选 Key", "ocr_primary.api_key", "secret"),
        ("课件识别首选 模型", "ocr_primary.model", "str"),
        ("课件识别备用 地址", "ocr_backup.api_base", "str"),
        ("课件识别备用 Key", "ocr_backup.api_key", "secret"),
        ("课件识别备用 模型", "ocr_backup.model", "str"),
    ]

    def __init__(self, master, on_saved):
        super().__init__(master)
        self.title("设置")
        self.resizable(False, False)
        self.on_saved = on_saved
        cfg = load_config()

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        self.vars = {}
        row = 0
        for label, key, kind in self.FIELDS:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            val = _get_nested(cfg, key)
            if kind == "csv":
                val = ", ".join(val if isinstance(val, list) else [])
            var.set(str(val) if val is not None else "")
            ent = ttk.Entry(frm, textvariable=var, width=52,
                            show="*" if kind == "secret" else "")
            ent.grid(row=row, column=1, sticky="we", pady=3)
            self.vars[key] = (var, kind)
            row += 1

        self.slide_dir = tk.StringVar(value=cfg.get("slide_dir", "~/Documents/雨课堂课件"))
        ttk.Label(frm, text="课件保存目录").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.slide_dir, width=52).grid(row=row, column=1, sticky="we", pady=3)
        ttk.Button(frm, text="选择…", command=self.pick_dir).grid(row=row, column=2, padx=4)
        row += 1

        self.auto_submit = tk.BooleanVar(value=bool(cfg.get("auto_submit", True)))
        ttk.Checkbutton(frm, text="自动提交答案", variable=self.auto_submit).grid(
            row=row, column=1, sticky="w", pady=2)
        row += 1
        self.enable_mm = tk.BooleanVar(value=bool(cfg.get("enable_multimodal", True)))
        ttk.Checkbutton(frm, text="启用视觉作答 (答题时把题目截图发给模型)", variable=self.enable_mm).grid(
            row=row, column=1, sticky="w", pady=2)

        btns = ttk.Frame(frm)
        btns.grid(row=row + 1, column=0, columnspan=3, pady=(12, 0))
        ttk.Button(btns, text="保存", command=self.save).pack(side="left", padx=6)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="left")

    def pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="选择课件保存目录")
        if d:
            self.slide_dir.set(d)

    def save(self):
        cfg = load_config()
        for label, key, kind in self.FIELDS:
            var, _ = self.vars[key]
            val = var.get().strip()
            # 允许显式清空: 空值也写回, 否则旧 Key 永远清不掉
            if kind == "csv":
                _set_nested(cfg, key, [m.strip() for m in val.split(",") if m.strip()])
            else:
                _set_nested(cfg, key, val)
        cfg["slide_dir"] = os.path.expanduser(self.slide_dir.get().strip())
        cfg["auto_submit"] = bool(self.auto_submit.get())
        cfg["enable_multimodal"] = bool(self.enable_mm.get())
        save_config(cfg)
        self.on_saved()
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("雨课堂助手")
        self.geometry("860x600")
        self.minsize(720, 480)

        self.listener = None        # 监听引擎进程
        self.scan_proc = None       # 扫码登录进程
        self.merge_proc = None
        self.stopping = False       # 监听正在停止(等待退出期间禁用按钮)
        self.current_session = None
        self.pending_ocr = {}       # slide_ocr 早于 slide 到达时暂存 {file: (head, ok)}
        self.events = queue.Queue()

        self._build_ui()
        self.after(120, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self._show_login_state()

    # ---------- UI ----------
    def _build_ui(self):
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.login_label = ttk.Label(bar, text="未登录")
        self.login_label.pack(side="left")
        self.btn_login = ttk.Button(bar, text="扫码登录", command=self.start_login)
        self.btn_login.pack(side="left", padx=6)
        self.btn_listen = ttk.Button(bar, text="开始监听", command=self.toggle_listen)
        self.btn_listen.pack(side="left", padx=6)
        self.scan_on = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="扫描课件", variable=self.scan_on,
                        command=self.on_scan_toggle).pack(side="left", padx=6)
        self.btn_export = ttk.Button(bar, text="导出课件", command=self.export_slides)
        self.btn_export.pack(side="left", padx=6)
        ttk.Button(bar, text="设置", command=self.open_settings).pack(side="left", padx=6)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        frame_q = ttk.Frame(self.notebook)
        cols = ("time", "qtype", "answer", "submit")
        self.quiz_tree = ttk.Treeview(frame_q, columns=cols, show="headings", height=14)
        for c, w, t in [("time", 90, "时间"), ("qtype", 80, "题型"),
                        ("answer", 120, "答案"), ("submit", 420, "题目 / 提交状态")]:
            self.quiz_tree.heading(c, text=t)
            self.quiz_tree.column(c, width=w, anchor="w")
        self.quiz_tree.pack(fill="both", expand=True)
        self.notebook.add(frame_q, text="题目")

        frame_s = ttk.Frame(self.notebook)
        scols = ("idx", "page", "head")
        self.slide_tree = ttk.Treeview(frame_s, columns=scols, show="headings", height=14)
        for c, w, t in [("idx", 70, "序号"), ("page", 70, "页码"), ("head", 640, "识别内容")]:
            self.slide_tree.heading(c, text=t)
            self.slide_tree.column(c, width=w, anchor="w")
        self.slide_tree.pack(fill="both", expand=True)
        self.session_label = ttk.Label(frame_s, text="尚未进入课堂")
        self.session_label.pack(fill="x", pady=2)
        self.notebook.add(frame_s, text="课件")

        frame_l = ttk.Frame(self.notebook)
        self.log_text = tk.Text(frame_l, height=14, state="disabled", wrap="none")
        self.log_text.pack(fill="both", expand=True)
        self.notebook.add(frame_l, text="日志")

        self.status = ttk.Label(self, text="就绪", anchor="w", padding=(8, 2))
        self.status.pack(fill="x")

    def _log(self, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_events(self):
        # 单条事件异常不能中断循环, 否则后续日志/状态全部停更
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(ev)
            except Exception as e:
                try:
                    self._log(f"界面处理事件出错: {e}")
                except Exception:
                    pass
        self.after(120, self._drain_events)

    # ---------- 事件处理 ----------
    def _handle_event(self, ev):
        kind = ev.get("event")
        if kind == "log":
            self._log(str(ev.get("msg", "")))
        elif kind == "status":
            listening = bool(ev.get("listening"))
            self.stopping = False
            self.btn_listen.configure(text="停止监听" if listening else "开始监听",
                                      state="normal")
            self.status.configure(text="监听中…" if listening else "就绪")
        elif kind == "scan_state":
            self.scan_on.set(bool(ev.get("on")))
        elif kind == "login":
            if ev.get("ok"):
                self._show_login_state()
            self.btn_login.configure(state="normal", text="扫码登录")
        elif kind == "question":
            self.quiz_tree.insert("", 0, values=(
                ev.get("time", ""), ev.get("qtype", ""), ev.get("answer", ""),
                f"{ev.get('question', '')[:60]}  [{ev.get('submit', '')}]"))
        elif kind == "session":
            self.current_session = ev.get("dir")
            course = ev.get("course") or ""
            self.session_label.configure(
                text=f"{course}  ·  已保存 {ev.get('count', 0)} 张  ·  导出后自动清理缓存图片")
        elif kind == "slide":
            fname = str(ev.get("file", ""))
            self.slide_tree.insert("", "end", iid=fname, values=(
                ev.get("index", ""), ev.get("page", ""), "识别中…"))
            # OCR 结果先于 slide 到达时, 这里补挂
            if fname in self.pending_ocr:
                head, ok = self.pending_ocr.pop(fname)
                self.slide_tree.set(fname, "head", head or ("识别失败" if not ok else "（无文字）"))
        elif kind == "slide_ocr":
            fname = str(ev.get("file", ""))
            if self.slide_tree.exists(fname):
                head = str(ev.get("ocr_head", "")) or ("识别失败" if not ev.get("ocr_ok") else "（无文字）")
                self.slide_tree.set(fname, "head", head)
            else:
                # OCR 线程快于主循环 slide 事件: 暂存, 等 slide 到达再挂
                self.pending_ocr[fname] = (str(ev.get("ocr_head", "")), bool(ev.get("ocr_ok")))
        elif kind == "slide_removed":
            fname = str(ev.get("file", ""))
            if self.slide_tree.exists(fname):
                self.slide_tree.delete(fname)
            self.pending_ocr.pop(fname, None)
        elif kind == "chapters":
            for it in ev.get("items", []):
                self._log(f"章节「{it.get('title')}」已导出")
        elif kind == "merge_done":
            self.btn_export.configure(state="normal")
            if ev.get("ok"):
                messagebox.showinfo("导出完成", f"已保存到:\n{ev.get('out_dir')}", parent=self)
            else:
                messagebox.showwarning("导出失败", "详情见日志页", parent=self)
        elif kind == "error":
            self._log(f"错误: {ev.get('msg', '')}")
            self.status.configure(text="出错")
        elif kind == "_exited":
            self._on_proc_exit(ev)

    def _on_proc_exit(self, ev):
        role = ev.get("role")
        if role == "listen":
            self.stopping = False
            self.btn_listen.configure(text="开始监听", state="normal")
            self.status.configure(text="就绪")
            self._log(f"监听引擎已退出 (code {ev.get('code')})" if ev.get("code") else "监听已停止")
        elif role == "scan":
            self.btn_login.configure(state="normal", text="扫码登录")
        elif role == "merge":
            self.btn_export.configure(state="normal")

    def _show_login_state(self):
        name = ""
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, encoding="utf-8") as f:
                    name = (json.load(f) or {}).get("logged_in_as", "")
        except Exception:
            pass
        self.login_label.configure(text=f"登录: {name}" if name else "未登录")

    # ---------- 动作 ----------
    def start_login(self):
        if (self.scan_proc and self.scan_proc.alive()) or \
                (self.listener and self.listener.alive()):
            messagebox.showinfo("请先停止", "扫码登录与监听共用浏览器配置，请先停止监听再扫码", parent=self)
            return
        self.btn_login.configure(state="disabled", text="等待扫码…")
        self._log("请用微信扫一扫完成登录")
        try:
            self.scan_proc = EngineProcess(["scan"], self.events.put, role="scan")
        except Exception as e:
            self.btn_login.configure(state="normal", text="扫码登录")
            messagebox.showerror("启动失败", f"引擎进程无法启动:\n{e}", parent=self)

    def toggle_listen(self):
        if self.listener and self.listener.alive():
            if self.stopping:
                return
            self.stopping = True
            self.btn_listen.configure(state="disabled")
            self.status.configure(text="正在停止…")
            self.listener.stop()
            # 兜底: 8 秒未退出则强杀 (_exited 事件负责恢复界面)
            threading.Thread(target=self._force_stop_later, daemon=True).start()
            return
        if self.scan_proc and self.scan_proc.alive():
            messagebox.showinfo("请先完成扫码", "扫码登录与监听共用浏览器配置，请先等扫码结束", parent=self)
            return
        for iid in self.quiz_tree.get_children():
            self.quiz_tree.delete(iid)
        for iid in self.slide_tree.get_children():
            self.slide_tree.delete(iid)
        self.current_session = None
        args = ["listen"] + (["--scan"] if self.scan_on.get() else [])
        try:
            self.listener = EngineProcess(args, self.events.put, role="listen")
        except Exception as e:
            messagebox.showerror("启动失败", f"引擎进程无法启动:\n{e}", parent=self)
            return
        self._log("监听引擎已启动")

    def _force_stop_later(self):
        if self.listener:
            try:
                self.listener.proc.wait(timeout=8)
            except Exception:
                pass
            if self.listener and self.listener.alive():
                self.listener.kill()

    def on_scan_toggle(self):
        if self.listener and self.listener.alive():
            self.listener.send({"cmd": "scan_on" if self.scan_on.get() else "scan_off"})

    def export_slides(self):
        if self.merge_proc and self.merge_proc.alive():
            return
        session_dir = self.current_session
        if not session_dir:
            self.btn_export.configure(state="disabled")
            self._query_cached_sessions()
            return
        self.btn_export.configure(state="disabled")
        self._log(f"开始导出: {session_dir}")
        try:
            self.merge_proc = EngineProcess(["merge", "--dir", session_dir], self.events.put, role="merge")
        except Exception as e:
            self.btn_export.configure(state="normal")
            messagebox.showerror("启动失败", f"引擎进程无法启动:\n{e}", parent=self)

    def _query_cached_sessions(self):
        """后台查询待导出会话, 结果走事件队列; 失败与空缓存区分提示"""
        def worker():
            try:
                out = subprocess.run(engine_command(["cache-list"]), capture_output=True,
                                     timeout=20, creationflags=subprocess_flags)
                sessions = (json.loads(out.stdout.decode("utf-8")) or {}).get("sessions", [])
                self.events.put({"event": "_cache_list", "sessions": sessions, "error": None})
            except Exception as e:
                self.events.put({"event": "_cache_list", "sessions": [], "error": str(e)})
        self.status.configure(text="查询待导出课件…")
        threading.Thread(target=worker, daemon=True).start()

    def _pick_cached_session(self, sessions):
        chosen = {"dir": None}
        win = tk.Toplevel(self)
        win.title("选择要导出的会话")
        lst = tk.Listbox(win, width=70, height=8)
        lst.pack(padx=10, pady=10)
        for s in sessions:
            lst.insert("end", f"{s.get('date', '')}  {s.get('course', '')}  ({s['count']} 张)")
        def confirm():
            sel = lst.curselection()
            if sel:
                chosen["dir"] = sessions[sel[0]]["dir"]
            win.destroy()
        ttk.Button(win, text="导出所选", command=confirm).pack(pady=(0, 10))
        self.wait_window(win)
        return chosen["dir"]

    def open_settings(self):
        SettingsDialog(self, on_saved=lambda: self._log("设置已保存"))

    def _handle_cache_list(self, ev):
        self.btn_export.configure(state="normal")
        self.status.configure(text="就绪")
        if ev.get("error"):
            messagebox.showerror("查询待导出课件失败", f"请检查引擎是否完整:\n{ev['error']}", parent=self)
            return
        sessions = [s for s in ev.get("sessions", []) if s.get("count", 0) > 0]
        if not sessions:
            messagebox.showinfo("没有可导出的课件", "开启「扫描课件」后自动保存", parent=self)
            return
        session_dir = sessions[0]["dir"] if len(sessions) == 1 else self._pick_cached_session(sessions)
        if not session_dir:
            return
        self.btn_export.configure(state="disabled")
        self._log(f"开始导出: {session_dir}")
        try:
            self.merge_proc = EngineProcess(["merge", "--dir", session_dir], self.events.put, role="merge")
        except Exception as e:
            self.btn_export.configure(state="normal")
            messagebox.showerror("启动失败", f"引擎进程无法启动:\n{e}", parent=self)

    def on_close(self):
        if self.listener and self.listener.alive():
            if not messagebox.askyesno("退出", "监听进行中，确定退出？", parent=self):
                return
            self.listener.stop()
            try:
                self.listener.proc.wait(timeout=5)
            except Exception:
                pass
            if self.listener.alive():
                self.listener.kill()
        if self.merge_proc and self.merge_proc.alive():
            if not messagebox.askyesno("退出", "课件导出进行中，退出会中断导出。确定退出？", parent=self):
                return
            self.merge_proc.kill()
        if self.scan_proc and self.scan_proc.alive():
            self.scan_proc.kill()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
