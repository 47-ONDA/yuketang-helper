// 雨课堂助手 · macOS GUI (SwiftUI)
// 与 Python 引擎 (Resources/engine.py) 通过 JSON 行协议通信

import SwiftUI
import AppKit
import Foundation

// MARK: - 数据模型

struct QuizEvent: Identifiable, Hashable {
    let id = UUID()
    let time: String
    let qtype: String
    let question: String
    let answer: String
    let submit: String
}

struct SlideEvent: Identifiable, Hashable {
    let id = UUID()
    let index: Int
    let page: Int
    let file: String
    let head: String
    let ok: Bool
    var chapter: String = ""
}

struct CachedSession: Identifiable, Hashable {
    var id: String { dir }
    let dir: String
    let course: String?
    let date: String?
    var count: Int
    var name: String { "\(date ?? "")-\(course ?? "未命名")" }
}

struct OcrApiConfig: Codable, Hashable {
    var apiBase: String = ""
    var apiKey: String = ""
    var model: String = ""

    enum CodingKeys: String, CodingKey {
        case apiBase = "api_base"
        case apiKey = "api_key"
        case model
    }
}

struct AppConfig: Codable {
    var yuketangBaseUrl: String = "https://changjiang.yuketang.cn"
    var browser: String = "chrome"
    var apiBase: String = ""
    var apiKey: String = ""
    var models: [String] = []
    var enableMultimodal: Bool = true
    var multimodalModels: [String] = []
    var autoSubmit: Bool = true
    var listenInterval: Double = 1.0
    var ocrPrimary: OcrApiConfig = OcrApiConfig()
    var ocrBackup: OcrApiConfig = OcrApiConfig()
    var slideDir: String = "~/Documents/雨课堂课件"

    enum CodingKeys: String, CodingKey {
        case yuketangBaseUrl = "yuketang_base_url"
        case browser
        case apiBase = "api_base"
        case apiKey = "api_key"
        case models
        case enableMultimodal = "enable_multimodal"
        case multimodalModels = "multimodal_models"
        case autoSubmit = "auto_submit"
        case listenInterval = "listen_interval"
        case ocrPrimary = "ocr_primary"
        case ocrBackup = "ocr_backup"
        case slideDir = "slide_dir"
    }
}

// MARK: - 引擎管理

final class EngineManager: ObservableObject {
    @Published var logs: [String] = []
    @Published var questions: [QuizEvent] = []
    @Published var slides: [SlideEvent] = []
    @Published var cachedSessions: [CachedSession] = []
    @Published var isListening = false
    @Published var isScanning = false          // 引擎实际扫描状态
    @Published var scanToggle = false          // 开关目标状态
    @Published var isScanRunning = false       // 扫码登录进程
    @Published var isExporting = false
    @Published var loggedInName: String?
    @Published var lastLoginTime: String?
    @Published var envReady = false
    @Published var busyPreparing = false
    @Published var settings: AppConfig = AppConfig()
    @Published var currentSession: CachedSession?

    private var listenProc: Process?
    private var scanProc: Process?
    private var stdinWriter: FileHandle?
    private var envChecked = false

    var homeDir: String { NSHomeDirectory() }
    var runtimeDir: String { homeDir + "/.yuketang-helper" }
    var venvPython: String { runtimeDir + "/venv/bin/python" }
    var configPath: String { runtimeDir + "/config.json" }

    var enginePath: String? { Bundle.main.path(forResource: "engine", ofType: "py") }
    var driverPath: String? {
        let p = Bundle.main.resourceURL?.appendingPathComponent("chromedriver").path
        return (p != nil && FileManager.default.fileExists(atPath: p!)) ? p : nil
    }
    var requirementsPath: String? { Bundle.main.path(forResource: "requirements", ofType: "txt") }

    init() {
        loadSettings()
        loadState()
        refreshCacheList()
    }

    // MARK: 配置与状态

    func loadSettings() {
        guard let data = FileManager.default.contents(atPath: configPath) else { return }
        if let cfg = try? JSONDecoder().decode(AppConfig.self, from: data) {
            DispatchQueue.main.async { self.settings = cfg }
        }
    }

    func saveSettings(_ cfg: AppConfig) {
        settings = cfg
        try? FileManager.default.createDirectory(atPath: runtimeDir, withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let data = try? enc.encode(cfg) {
            try? data.write(to: URL(fileURLWithPath: configPath))
            log("设置已保存，下次启动监听后生效")
        }
    }

    func loadState() {
        let path = runtimeDir + "/state.json"
        guard let data = FileManager.default.contents(atPath: path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        let name = obj["logged_in_as"] as? String
        let t = obj["last_login_time"] as? String
        DispatchQueue.main.async {
            self.loggedInName = name
            self.lastLoginTime = t
        }
    }

    // MARK: 环境准备

    func ensureEnvironment(_ done: @escaping (Bool) -> Void) {
        if envChecked { done(envReady); return }
        busyPreparing = true
        log("检查运行环境")

        let script = """
        set -e
        PY="\(venvPython)"
        REQ="\(requirementsPath ?? "")"
        if [ ! -x "$PY" ] || ! "$PY" -c "import selenium, requests, img2pdf" >/dev/null 2>&1; then
            echo "首次运行，正在准备环境（约半分钟）"
            /usr/bin/python3 -m venv "\(runtimeDir)/venv"
            "$PY" -m pip install -q --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple || true
            "$PY" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r "$REQ"
        fi
        "$PY" -c "import selenium, requests, img2pdf" >/dev/null 2>&1 && echo ENV_OK || echo ENV_FAIL
        """

        runShell(script) { [weak self] output in
            guard let self = self else { return }
            let ok = output.contains("ENV_OK")
            DispatchQueue.main.async {
                self.envReady = ok
                self.envChecked = true
                self.busyPreparing = false
                self.log(ok ? "运行环境就绪" : "环境准备失败（需要 Python 3 与网络）")
                done(ok)
            }
        }
    }

    private func runShell(_ script: String, _ done: @escaping (String) -> Void) {
        DispatchQueue.global().async {
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: "/bin/bash")
            proc.arguments = ["-c", script]
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            try? proc.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            let out = String(data: data, encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                for line in out.split(separator: "\n") where !line.isEmpty {
                    self.log(String(line))
                }
            }
            done(out)
        }
    }

    // MARK: 引擎进程

    func baseProcess(_ arguments: [String]) -> Process? {
        guard let engine = enginePath else {
            log("引擎文件缺失")
            return nil
        }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: venvPython)
        proc.arguments = ["-u", engine] + arguments
        var env = ProcessInfo.processInfo.environment
        env["PYTHONWARNINGS"] = "ignore"
        env["PYTHONUNBUFFERED"] = "1"
        if let drv = driverPath { env["YKT_DRIVER"] = drv }
        proc.environment = env
        return proc
    }

    private func attachOutput(_ proc: Process, mode: String) {
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            guard let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async {
                for line in text.split(separator: "\n") where !line.isEmpty {
                    self?.handleEngineLine(String(line), mode: mode)
                }
            }
        }
    }

    private func handleEngineLine(_ line: String, mode: String) {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = obj["event"] as? String else {
            if !line.contains("Warning") { log(line) }
            return
        }
        switch event {
        case "log":
            log(obj["msg"] as? String ?? "")
        case "status":
            if mode == "listen", let on = obj["listening"] as? Bool, !on {
                isListening = false
                isScanning = false
                scanToggle = false
            }
        case "login":
            if mode == "scan" {
                isScanRunning = false
                if (obj["ok"] as? Bool) == true {
                    loggedInName = (obj["name"] as? String) ?? ""
                    loadState()
                }
            }
        case "question":
            let q = QuizEvent(
                time: obj["time"] as? String ?? "",
                qtype: obj["qtype"] as? String ?? "",
                question: obj["question"] as? String ?? "",
                answer: obj["answer"] as? String ?? "",
                submit: obj["submit"] as? String ?? "")
            questions.insert(q, at: 0)
        case "session":
            currentSession = CachedSession(
                dir: obj["dir"] as? String ?? "",
                course: obj["course"] as? String,
                date: obj["date"] as? String,
                count: obj["count"] as? Int ?? 0)
        case "slide":
            let s = SlideEvent(
                index: obj["index"] as? Int ?? 0,
                page: obj["page"] as? Int ?? 0,
                file: obj["file"] as? String ?? "",
                head: obj["ocr_head"] as? String ?? "",
                ok: (obj["ocr_ok"] as? Bool) ?? false)
            slides.append(s)
            if var cs = currentSession { cs.count += 1; currentSession = cs }
        case "scan_state":
            if let on = obj["on"] as? Bool {
                isScanning = on
                scanToggle = on
            }
        case "chapters":
            if let items = obj["items"] as? [[String: Any]] {
                for item in items {
                    let title = item["title"] as? String ?? ""
                    let pages = item["pages"] as? [Int] ?? []
                    for p in pages {
                        if let idx = slides.firstIndex(where: { $0.index == p }) {
                            slides[idx].chapter = title
                        }
                    }
                }
            }
        case "merge_done":
            isExporting = false
            if (obj["ok"] as? Bool) == true {
                currentSession = nil
                slides.removeAll()
                refreshCacheList()
            }
        case "error":
            log("错误: " + (obj["msg"] as? String ?? ""))
            if mode == "listen" { isListening = false }
        default:
            break
        }
    }

    // MARK: 动作

    func startScan() {
        guard !isScanRunning, !isListening else { return }
        ensureEnvironment { [weak self] ok in
            guard let self = self, ok else { return }
            DispatchQueue.main.async {
                guard let proc = self.baseProcess(["scan"]) else { return }
                self.attachOutput(proc, mode: "scan")
                self.scanProc = proc
                self.isScanRunning = true
                proc.terminationHandler = { [weak self] _ in
                    DispatchQueue.main.async { self?.isScanRunning = false }
                }
                do { try proc.run() } catch {
                    self.log("启动扫码失败: \(error.localizedDescription)")
                    self.isScanRunning = false
                }
            }
        }
    }

    func startListen() {
        guard !isListening, !isScanRunning else { return }
        ensureEnvironment { [weak self] ok in
            guard let self = self, ok else { return }
            DispatchQueue.main.async {
                guard let proc = self.baseProcess(["listen"]) else { return }
                let inPipe = Pipe()
                proc.standardInput = inPipe
                self.stdinWriter = inPipe.fileHandleForWriting
                self.attachOutput(proc, mode: "listen")
                self.listenProc = proc
                self.isListening = true
                self.log("正在启动监听")
                proc.terminationHandler = { [weak self] _ in
                    DispatchQueue.main.async {
                        self?.isListening = false
                        self?.isScanning = false
                        try? self?.stdinWriter?.close()
                        self?.stdinWriter = nil
                    }
                }
                do { try proc.run() } catch {
                    self.log("启动引擎失败: \(error.localizedDescription)")
                    self.isListening = false
                }
            }
        }
    }

    func stopListen() {
        guard let proc = listenProc, proc.isRunning else { return }
        log("正在停止监听")
        proc.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 6) { [weak proc] in
            if let p = proc, p.isRunning {
                kill(p.processIdentifier, SIGKILL)
            }
        }
    }

    private func sendCommand(_ cmd: String) {
        guard let fh = stdinWriter, let data = "{\"cmd\":\"\(cmd)\"}\n".data(using: .utf8) else { return }
        try? fh.write(contentsOf: data)
    }

    func toggleSlideScan() {
        guard isListening else { return }
        scanToggle.toggle()
        sendCommand(scanToggle ? "scan_on" : "scan_off")
    }

    func exportSlides(sessionDir: String?) {
        let dir = sessionDir ?? currentSession?.dir
        guard let dir = dir, !dir.isEmpty else {
            log("没有可导出的课件")
            return
        }
        guard !isExporting else { return }
        ensureEnvironment { [weak self] ok in
            guard let self = self, ok else { return }
            DispatchQueue.main.async {
                guard let proc = self.baseProcess(["merge", "--dir", dir]) else { return }
                self.attachOutput(proc, mode: "merge")
                self.isExporting = true
                self.log("正在导出课件")
                proc.terminationHandler = { [weak self] _ in
                    DispatchQueue.main.async {
                        self?.isExporting = false
                        self?.refreshCacheList()
                    }
                }
                do { try proc.run() } catch {
                    self.log("导出失败: \(error.localizedDescription)")
                    self.isExporting = false
                }
            }
        }
    }

    func refreshCacheList() {
        DispatchQueue.global().async { [weak self] in
            guard let self = self,
                  let proc = self.baseProcess(["cache-list"]) else { return }
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = Pipe()
            try? proc.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            guard let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n").reversed() {
                guard let d = String(line).data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                      let arr = obj["sessions"] as? [[String: Any]] else { continue }
                let sessions = arr.map {
                    CachedSession(dir: $0["dir"] as? String ?? "",
                                  course: $0["course"] as? String,
                                  date: $0["date"] as? String,
                                  count: $0["count"] as? Int ?? 0)
                }
                DispatchQueue.main.async { self.cachedSessions = sessions }
                break
            }
        }
    }

    func deleteSession(_ dir: String) {
        try? FileManager.default.removeItem(atPath: dir)
        refreshCacheList()
    }

    func log(_ msg: String) {
        let stamp = DateFormatter.timeStamp.string(from: Date())
        logs.append("[\(stamp)] \(msg)")
        if logs.count > 600 { logs.removeFirst(logs.count - 600) }
    }
}

extension DateFormatter {
    static let timeStamp: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f
    }()
}

// MARK: - 退出时处理未导出课件

class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var engine: EngineManager?

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let engine = AppDelegate.engine else { return .terminateNow }

        // 停掉引擎
        engine.stopListen()

        // 同步检查缓存
        let sessions = Self.syncCacheList(engine: engine)
        guard !sessions.isEmpty else { return .terminateNow }

        let alert = NSAlert()
        alert.messageText = "有 \(sessions.count) 次课的课件未导出"
        alert.informativeText = sessions.map { "\($0.name)（\($0.count) 张）" }.joined(separator: "\n")
            + "\n\n导出为 PDF 后退出，还是直接删除？"
        alert.addButton(withTitle: "导出并退出")
        alert.addButton(withTitle: "直接退出（删除图片）")
        alert.addButton(withTitle: "取消")
        switch alert.runModal() {
        case .alertFirstButtonReturn:
            for s in sessions {
                Self.syncMerge(engine: engine, dir: s.dir)
            }
            return .terminateNow
        case .alertSecondButtonReturn:
            for s in sessions {
                try? FileManager.default.removeItem(atPath: s.dir)
            }
            return .terminateNow
        default:
            return .terminateCancel
        }
    }

    private static func syncCacheList(engine: EngineManager) -> [CachedSession] {
        guard let proc = engine.baseProcess(["cache-list"]) else { return [] }
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = Pipe()
        try? proc.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        for line in text.split(separator: "\n").reversed() {
            if let d = String(line).data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               let arr = obj["sessions"] as? [[String: Any]] {
                return arr.map {
                    CachedSession(dir: $0["dir"] as? String ?? "",
                                  course: $0["course"] as? String,
                                  date: $0["date"] as? String,
                                  count: $0["count"] as? Int ?? 0)
                }
            }
        }
        return []
    }

    private static func syncMerge(engine: EngineManager, dir: String) {
        guard let proc = engine.baseProcess(["merge", "--dir", dir]) else { return }
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        try? proc.run()
        proc.waitUntilExit()   // 引擎内部有超时兜底
    }
}

// MARK: - 主界面

enum MainTab: String, CaseIterable, Identifiable {
    case questions = "题目"
    case slides = "课件"
    var id: String { rawValue }
}

struct ContentView: View {
    @StateObject private var engine = EngineManager()
    @State private var showSettings = false
    @State private var tab: MainTab = .questions

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            Picker("", selection: $tab) {
                ForEach(MainTab.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            tabContent
            Divider()
            logPanel
        }
        .frame(minWidth: 880, minHeight: 640)
        .sheet(isPresented: $showSettings) { SettingsSheet(engine: engine) }
        .onAppear { AppDelegate.engine = engine }
    }

    private var header: some View {
        HStack(spacing: 12) {
            HStack(spacing: 8) {
                Circle().fill(statusColor).frame(width: 10, height: 10)
                VStack(alignment: .leading, spacing: 1) {
                    Text(statusText).font(.system(size: 13, weight: .medium))
                    if let t = engine.lastLoginTime {
                        Text("上次登录 \(t)").font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 8).fill(statusColor.opacity(0.12)))

            Spacer()

            Button {
                engine.startScan()
            } label: {
                Label(engine.isScanRunning ? "扫码中" : "扫码登录", systemImage: "qrcode")
            }
            .disabled(engine.isScanRunning || engine.isListening || engine.busyPreparing)
            .buttonStyle(.borderedProminent)

            Button {
                if engine.isListening { engine.stopListen() } else { engine.startListen() }
            } label: {
                Label(engine.isListening ? "停止监听" : "开始监听",
                      systemImage: engine.isListening ? "stop.circle.fill" : "play.circle.fill")
            }
            .tint(engine.isListening ? .red : .accentColor)
            .buttonStyle(.borderedProminent)
            .disabled(engine.isScanRunning || engine.busyPreparing)

            Button {
                engine.toggleSlideScan()
            } label: {
                Label(engine.scanToggle ? "扫描开" : "扫描课件",
                      systemImage: engine.scanToggle ? "doc.badge.ellipsis" : "doc.text.viewfinder")
            }
            .tint(engine.scanToggle ? Color.orange : .accentColor)
            .buttonStyle(.borderedProminent)
            .disabled(!engine.isListening || engine.busyPreparing)
            .help("监听中开启后，课件翻页自动保存并识别")

            Button {
                engine.exportSlides(sessionDir: nil)
            } label: {
                Label(engine.isExporting ? "导出中" : "导出课件", systemImage: "square.and.arrow.down")
            }
            .buttonStyle(.borderedProminent)
            .tint(.green)
            .disabled(engine.isExporting || engine.busyPreparing ||
                      (engine.currentSession == nil && engine.cachedSessions.isEmpty))
            .help("把已扫描的课件按章节合并为 PDF 和文本")

            Button {
                showSettings = true
            } label: {
                Label("设置", systemImage: "gearshape")
            }
            .buttonStyle(.bordered)
            .disabled(engine.busyPreparing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var statusColor: Color {
        if engine.isScanRunning { return .orange }
        if engine.loggedInName != nil { return .green }
        return .secondary.opacity(0.7)
    }

    private var statusText: String {
        if engine.isScanRunning { return "扫码登录中" }
        if let n = engine.loggedInName, !n.isEmpty { return "已登录：\(n)" }
        if engine.loggedInName != nil { return "已登录" }
        return "未登录"
    }

    @ViewBuilder
    private var tabContent: some View {
        switch tab {
        case .questions: questionList
        case .slides: slidePanel
        }
    }

    private var questionList: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("题目记录").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                Spacer()
                if !engine.questions.isEmpty {
                    Button("清空") { engine.questions.removeAll() }
                        .buttonStyle(.borderless).font(.system(size: 11))
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 6)

            if engine.questions.isEmpty {
                Spacer()
                VStack(spacing: 8) {
                    Image(systemName: "doc.text.magnifyingglass")
                        .font(.system(size: 34)).foregroundStyle(.tertiary)
                    Text("监听中检测到的题目与回答会显示在这里")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                Spacer()
            } else {
                Table(engine.questions) {
                    TableColumn("时间") { Text($0.time) }.width(70)
                    TableColumn("题型") { Text($0.qtype) }.width(60)
                    TableColumn("题目") { q in Text(q.question).lineLimit(2).help(q.question) }
                    TableColumn("回答") { q in
                        Text(q.answer).fontWeight(.medium).foregroundStyle(.blue).help(q.answer)
                    }
                    TableColumn("提交") { q in
                        Text(q.submit).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var slidePanel: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !engine.cachedSessions.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("未导出的课件").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                    ForEach(engine.cachedSessions) { s in
                        HStack {
                            Text("\(s.name)（\(s.count) 张）").font(.system(size: 12))
                            Spacer()
                            Button("导出") { engine.exportSlides(sessionDir: s.dir) }
                                .buttonStyle(.bordered).controlSize(.small)
                                .disabled(engine.isExporting)
                            Button("删除") { engine.deleteSession(s.dir) }
                                .buttonStyle(.bordered).controlSize(.small)
                        }
                        .padding(.vertical, 2)
                    }
                }
                .padding(.horizontal, 14)
                .padding(.top, 6)
                Divider()
            }

            HStack {
                Text("本次扫描").font(.system(size: 12, weight: .semibold)).foregroundStyle(.secondary)
                if let cs = engine.currentSession {
                    Text("\(cs.name) · \(cs.count) 张").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                Spacer()
                if !engine.slides.isEmpty {
                    Button("清空列表") { engine.slides.removeAll() }
                        .buttonStyle(.borderless).font(.system(size: 11))
                }
            }
            .padding(.horizontal, 14)

            if engine.slides.isEmpty {
                Spacer()
                VStack(spacing: 8) {
                    Image(systemName: "doc.text.viewfinder")
                        .font(.system(size: 34)).foregroundStyle(.tertiary)
                    Text("开启「扫描课件」后，翻到新页面会自动保存并识别")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                Spacer()
            } else {
                Table(engine.slides) {
                    TableColumn("序号") { Text("\($0.index)") }.width(45)
                    TableColumn("页码") { Text("\($0.page)") }.width(45)
                    TableColumn("内容摘要") { s in
                        HStack(spacing: 4) {
                            if !s.ok { Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange) }
                            Text(s.head.isEmpty ? "（无文字）" : s.head).lineLimit(1)
                        }.help(s.head)
                    }
                    TableColumn("章节") { s in Text(s.chapter).foregroundStyle(.secondary) }
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var logPanel: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("日志").font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary).padding(.horizontal, 14)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(engine.logs.enumerated()), id: \.offset) { idx, line in
                            Text(line)
                                .font(.system(size: 10.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .id(idx)
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.bottom, 8)
                }
                .frame(height: 130)
                .onChange(of: engine.logs.count) { cnt in
                    if cnt > 0 { proxy.scrollTo(cnt - 1, anchor: .bottom) }
                }
            }
        }
        .padding(.top, 6)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

// MARK: - 设置

struct SettingsSheet: View {
    @ObservedObject var engine: EngineManager
    @State private var cfg = AppConfig()
    @State private var saved = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("设置").font(.system(size: 15, weight: .bold))
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.defaultAction)
            }
            .padding(14)

            Form {
                Section("平台") {
                    row("雨课堂地址", text: $cfg.yuketangBaseUrl)
                }
                Section("模型 API") {
                    row("API Base", text: $cfg.apiBase)
                    SecureRow("API Key", text: $cfg.apiKey)
                    row("文本模型（逗号分隔）", text: modelsBinding)
                    row("视觉模型（逗号分隔）", text: mmBinding)
                    Toggle("题目截图交视觉模型识别", isOn: $cfg.enableMultimodal)
                    Toggle("自动提交答案", isOn: $cfg.autoSubmit)
                }
                Section("课件识别 · 首选") {
                    row("API Base", text: $cfg.ocrPrimary.apiBase)
                    SecureRow("API Key", text: $cfg.ocrPrimary.apiKey)
                    row("模型", text: $cfg.ocrPrimary.model)
                }
                Section("课件识别 · 备用") {
                    row("API Base", text: $cfg.ocrBackup.apiBase)
                    SecureRow("API Key", text: $cfg.ocrBackup.apiKey)
                    row("模型", text: $cfg.ocrBackup.model)
                }
                Section("课件导出") {
                    LabeledContent("保存目录") {
                        HStack(spacing: 8) {
                            Text(cfg.slideDir)
                                .lineLimit(1).truncationMode(.middle)
                                .foregroundStyle(.secondary)
                                .help(cfg.slideDir)
                            Button("选择…") {
                                let panel = NSOpenPanel()
                                panel.canChooseDirectories = true
                                panel.canChooseFiles = false
                                panel.canCreateDirectories = true
                                panel.message = "选择课件导出目录"
                                if panel.runModal() == .OK, let url = panel.url {
                                    cfg.slideDir = url.path
                                }
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)

            HStack {
                if saved {
                    Label("已保存", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green).font(.system(size: 12))
                }
                Spacer()
                Button("保存") {
                    engine.saveSettings(cfg)
                    saved = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { saved = false }
                }
                .keyboardShortcut("s")
                .buttonStyle(.borderedProminent)
            }
            .padding(14)
        }
        .frame(width: 580, height: 620)
        .onAppear { cfg = engine.settings }
    }

    private func row(_ label: String, text: Binding<String>) -> some View {
        LabeledContent(label) {
            TextField("", text: text).textFieldStyle(.roundedBorder)
        }
    }

    private func SecureRow(_ label: String, text: Binding<String>) -> some View {
        LabeledContent(label) {
            SecureField("", text: text).textFieldStyle(.roundedBorder)
        }
    }

    private var modelsBinding: Binding<String> {
        Binding(get: { cfg.models.joined(separator: ",") },
                set: { cfg.models = $0.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } })
    }
    private var mmBinding: Binding<String> {
        Binding(get: { cfg.multimodalModels.joined(separator: ",") },
                set: { cfg.multimodalModels = $0.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty } })
    }
}

// MARK: - App

@main
struct YktHelperApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var engine = EngineManager()

    var body: some Scene {
        WindowGroup("雨课堂助手") {
            ContentView()
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
