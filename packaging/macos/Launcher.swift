import AppKit
import Foundation

func backendURL() -> URL {
    Bundle.main.resourceURL!.appendingPathComponent("backend/TimeLapseServer")
}

func runtimeEnvironment() -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    for key in Array(environment.keys) where key.hasPrefix("PYTHON") || key.hasPrefix("DYLD_") {
        environment.removeValue(forKey: key)
    }
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return environment
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var backend: Process?
    private var output = Data()
    private var errors = Data()
    private var workbenchURL: URL?
    private var statusItem: NSStatusItem?
    private var stopping = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let applicationMenu = NSMenu()
        applicationMenu.addItem(withTitle: "打开工作台", action: #selector(openWorkbench), keyEquivalent: "o").target = self
        applicationMenu.addItem(.separator())
        applicationMenu.addItem(withTitle: "退出 Time-lapse", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let menuBar = NSMenu()
        let root = NSMenuItem()
        root.submenu = applicationMenu
        menuBar.addItem(root)
        NSApp.mainMenu = menuBar
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem?.button?.image = NSImage(systemSymbolName: "camera.aperture", accessibilityDescription: "Time-lapse")
        statusItem?.button?.toolTip = "Time-lapse · 正在启动"
        statusItem?.menu = applicationMenu.copy() as? NSMenu

        let process = Process()
        process.executableURL = backendURL()
        process.arguments = ["--parent-pid", String(ProcessInfo.processInfo.processIdentifier)]
        process.currentDirectoryURL = Bundle.main.resourceURL
        process.environment = runtimeEnvironment()
        let stdout = Pipe(), stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async { self?.readOutput(data) }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            DispatchQueue.main.async {
                self?.errors.append(data)
                if let count = self?.errors.count, count > 16000 { self?.errors.removeFirst(count - 16000) }
            }
        }
        process.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.stopping { NSApp.reply(toApplicationShouldTerminate: true) }
                else {
                    self.backend = nil
                    self.showError("本地服务已停止（\(process.terminationStatus)）。请重新打开 App。")
                }
            }
        }
        backend = process
        do { try process.run() }
        catch { backend = nil; showError("无法启动内置服务：\(error.localizedDescription)") }
    }

    private func readOutput(_ data: Data) {
        output.append(data)
        while let end = output.firstIndex(of: 10) {
            let line = String(data: output[..<end], encoding: .utf8) ?? ""
            output.removeSubrange(...end)
            if line.hasPrefix("TIMELAPSE_READY "),
               let url = URL(string: String(line.dropFirst("TIMELAPSE_READY ".count))),
               url.scheme == "http", url.host == "127.0.0.1", url.port != nil,
               url.fragment?.hasPrefix("token=") == true {
                workbenchURL = url
                statusItem?.button?.toolTip = "Time-lapse · 点击打开或退出"
                if !stopping { openWorkbench() }
            }
        }
    }

    @objc func openWorkbench() {
        guard !stopping, let url = workbenchURL else { return }
        NSWorkspace.shared.open(url)
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openWorkbench()
        return false
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let process = backend, process.isRunning else { return .terminateNow }
        if !stopping {
            stopping = true
            statusItem?.button?.toolTip = "Time-lapse · 正在停止并保存进度"
            process.terminate()
        }
        return .terminateLater
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Time-lapse 未能运行"
        alert.informativeText = message + "\n\n" + (String(data: errors, encoding: .utf8) ?? "")
        alert.addButton(withTitle: "退出")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
        NSApp.terminate(nil)
    }
}

// The actual .app can validate itself offline without a GUI or external Python.
if CommandLine.arguments.contains("--self-test") {
    let process = Process()
    process.executableURL = backendURL()
    process.arguments = ["--self-test"]
    process.environment = runtimeEnvironment()
    process.standardOutput = FileHandle.standardOutput
    process.standardError = FileHandle.standardError
    do { try process.run(); process.waitUntilExit(); exit(process.terminationStatus) }
    catch { fputs("\(error)\n", stderr); exit(1) }
} else {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
}
