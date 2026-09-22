# -*- coding: utf-8 -*-
"""雨课堂助手 - Windows GUI (tkinter)

与 engine 子进程通过 stdin/stdout JSON 行协议通信, 事件与 macOS 版完全一致:
status/log/login/question/session/slide/slide_ocr/slide_removed/scan_state/chapters/merge_done/error

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
    """引擎子进程: 行协议解析 + 事件回调"""

    def __init__(self, args, on_event):
        self.on_event = on_event
        self.proc = subprocess.Popen(
            engine_command(args),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess_flags)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            # readline 逐行读取; for-line 迭代有缓冲预读, 事件会攒批延迟到达
            while True:
                raw = self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    # 引擎的报错原文(traceback / chromedriver 错误)不是 JSON,
                    # 原样透传到日志页——否则启动失败时界面毫无线索
                    if line:
                        self.on_event({"event": "log", "msg": "引擎: " + line})
                    continue
                try:
                    self.on_event(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass

    def send(self, obj):
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except Exception:
            pass

    def stop(self):
        try:
            self.send({"cmd": "stop"})
        except Exception:
            pass

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass

    def alive(self):
        return self.proc.poll() is None


class SettingsDialog(tk.Toplevel):
    """模型 API / 课件识别 / 保存目录设置; 只写用户填过的字段, 不动其他配置"""

    FIELDS = [
        ("模型接口地址 (OpenAI 兼容)", "api_base"),
        ("模型 API Key", "api_key"),
        ("模型名 (多个用英文逗号分隔)", "models_csv"),
        ("课件识别首选 Key", "ocr_primary_key"),
        ("课件识别备用 Key", "ocr_backup_key"),
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
        for label, key in self.FIELDS:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar()
            val = cfg.get(key.replace("_csv", ""), "")
            if key == "models_csv":
                val = ", ".join(val if isinstance(val, list) else [])
            elif key.startswith("ocr_"):
                part, field = key.rsplit("_", 1)
                sub = cfg.get(part) or {}
                val = sub.get({"key": "api_key"}.get(field, field), "")
            var.set(str(val) if val is not None else "")
            ent = ttk.Entry(frm, textvariable=var, width=52,
                            show="*" if "Key" in label else "")
            ent.grid(row=row, column=1, sticky="we", pady=3)
            self.vars[key] = var
            row += 1

        self.slide_dir = tk.StringVar(value=cfg.get("slide_dir", "~/Documents/雨课堂课件"))
        ttk.Label(frm, text="课件保存目录").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.slide_dir, width=52).grid(row=row, column=1, sticky="we", pady=3)
        ttk.Button(frm, text="选择…", command=self.pick_dir).grid(row=row, column=2, padx=4)
        row += 1

        self.auto_submit = tk.BooleanVar(value=bool(cfg.get("auto_submit", True)))
        ttk.Checkbutton(frm, text="自动提交答案", variable=self.auto_submit).grid(
            row=row, column=1, sticky="w", pady=3)

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
        for label, key in self.FIELDS:
            val = self.vars[key].get().strip()
            if key == "models_csv":
                cfg["models"] = [m.strip() for m in val.split(",") if m.strip()]
            elif key.startswith("ocr_"):
                part, field = key.rsplit("_", 1)
                sub = dict(cfg.get(part) or {})
                sub[{"key": "api_key"}.get(field, field)] = val
                cfg[part] = sub
            elif val:
                cfg[key] = val
        cfg["slide_dir"] = os.path.expanduser(self.slide_dir.get().strip())
        cfg["auto_submit"] = bool(self.auto_submit.get())
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
        self.current_session = None
        self.quiz_rows = []
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
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    # ---------- 事件处理 ----------
    def _handle_event(self, ev):
        kind = ev.get("event")
        if kind == "log":
            self._log(str(ev.get("msg", "")))
        elif kind == "status":
            listening = bool(ev.get("listening"))
            self.btn_listen.configure(text="停止监听" if listening else "开始监听")
            self.status.configure(text="监听中…" if listening else "就绪")
        elif kind == "scan_state":
            self.scan_on.set(bool(ev.get("on")))
        elif kind == "login":
            if ev.get("ok"):
                self._show_login_state()
            self.btn_login.configure(state="normal")
        elif kind == "question":
            self.quiz_rows.insert(0, ev)
            self.quiz_tree.insert("", 0, values=(
                ev.get("time", ""), ev.get("qtype", ""), ev.get("answer", ""),
                f"{ev.get('question', '')[:60]}  [{ev.get('submit', '')}]"))
        elif kind == "session":
            self.current_session = ev.get("dir")
            course = ev.get("course") or ""
            self.session_label.configure(
                text=f"{course}  ·  已保存 {ev.get('count', 0)} 张  ·  导出后自动清理缓存图片")
        elif kind == "slide":
            self.slide_tree.insert("", "end", iid=str(ev.get("file")), values=(
                ev.get("index", ""), ev.get("page", ""), "识别中…"))
        elif kind == "slide_ocr":
            item = self.slide_tree.exists(str(ev.get("file"))) and str(ev.get("file"))
            if item:
                head = str(ev.get("ocr_head", "")) or ("识别失败" if not ev.get("ocr_ok") else "（无文字）")
                self.slide_tree.set(item, "head", head)
        elif kind == "slide_removed":
            item = self.slide_tree.exists(str(ev.get("file"))) and str(ev.get("file"))
            if item:
                self.slide_tree.delete(item)
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
        if self.scan_proc and self.scan_proc.alive():
            return
        self.btn_login.configure(state="disabled", text="等待扫码…")
        self._log("请用微信扫一扫完成登录")

        def on_event(ev):
            if ev.get("event") == "log":
                self.events.put(ev)
            elif ev.get("event") == "login":
                self.events.put(ev)

        def watch():
            proc = EngineProcess(["scan"], on_event)
            self.scan_proc = proc
            proc.proc.wait()
            self.events.put({"event": "login", "ok": False})
            self.btn_login.configure(state="normal", text="扫码登录")

        threading.Thread(target=watch, daemon=True).start()

    def toggle_listen(self):
        if self.listener and self.listener.alive():
            self.status.configure(text="正在停止…")
            self.listener.stop()
            return
        self.quiz_rows.clear()
        for iid in self.quiz_tree.get_children():
            self.quiz_tree.delete(iid)
        for iid in self.slide_tree.get_children():
            self.slide_tree.delete(iid)
        self.current_session = None
        args = ["listen"] + (["--scan"] if self.scan_on.get() else [])
        self.listener = EngineProcess(args, self.events.put)
        self._log("监听引擎已启动")

    def on_scan_toggle(self):
        if self.listener and self.listener.alive():
            self.listener.send({"cmd": "scan_on" if self.scan_on.get() else "scan_off"})

    def export_slides(self):
        if self.merge_proc and self.merge_proc.alive():
            return
        session_dir = self.current_session
        if not session_dir:
            session_dir = self._pick_cached_session()
        if not session_dir:
            messagebox.showinfo("没有可导出的课件", "开启「扫描课件」后自动保存，或下次启动时补导出", parent=self)
            return
        self.btn_export.configure(state="disabled")
        self._log(f"开始导出: {session_dir}")
        self.merge_proc = EngineProcess(["merge", "--dir", session_dir], self.events.put)

    def _pick_cached_session(self):
        """应用重启后恢复待导出会话: 列出缓存里还有图片的会话让用户选"""
        try:
            out = subprocess.run(engine_command(["cache-list"]), capture_output=True,
                                 timeout=20, creationflags=subprocess_flags)
            sessions = (json.loads(out.stdout.decode("utf-8")) or {}).get("sessions", [])
        except Exception:
            return None
        sessions = [s for s in sessions if s.get("count", 0) > 0]
        if not sessions:
            return None
        if len(sessions) == 1:
            return sessions[0]["dir"]
        win = tk.Toplevel(self)
        win.title("选择要导出的会话")
        chosen = {"dir": None}
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

    def on_close(self):
        if self.listener and self.listener.alive():
            if not messagebox.askyesno("退出", "监听进行中，确定退出？", parent=self):
                return
            self.listener.stop()
        if self.scan_proc and self.scan_proc.alive():
            self.scan_proc.kill()
        if self.merge_proc and self.merge_proc.alive():
            self.merge_proc.kill()
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
