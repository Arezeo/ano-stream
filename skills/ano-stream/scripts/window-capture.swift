// Single-window capture for ano-stream on macOS (ScreenCaptureKit).
//
//   window-capture --match TEXT --width W --height H [--fps 60] [--frames N]
//   window-capture --window-id ID --width W --height H [--fps 60] [--frames N]
//   window-capture --check
//
// Writes raw BGRA frames (W*H*4 bytes each, no header) to stdout at a constant
// rate, repeating the last frame while the window is unchanged. Only the chosen
// window's pixels are captured, so overlapping windows, notifications and the
// menu bar never appear. Needs Screen Recording permission for the responsible
// process (the Terminal/app that launched ano-stream).
//
// --match picks the largest normal window whose "<app> <title>" contains TEXT
// (case-insensitive) and re-resolves it whenever the window is closed or
// recreated. If no matching window is capturable for 60 s the tool exits, so a
// dead window ends the broadcast instead of freezing on a stale frame.
// --window-id pins one window id for its whole life (no re-resolve).
//
// Exit codes: 0 done, 2 usage, 3 no permission, 4 window not found,
// 5 capture could not start, 6 output closed.

import Foundation
import AppKit
import ScreenCaptureKit
import CoreMedia
import CoreVideo

func log(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

func fail(_ code: Int32, _ message: String) -> Never {
    log(message)
    exit(code)
}

var windowID: UInt32 = 0
var match = ""
var width = 0
var height = 0
var fps = 60
var maxFrames = -1
var checkOnly = false

var it = CommandLine.arguments.dropFirst().makeIterator()
while let a = it.next() {
    switch a {
    case "--window-id": windowID = UInt32(it.next() ?? "") ?? 0
    case "--match": match = (it.next() ?? "").lowercased()
    case "--width": width = Int(it.next() ?? "") ?? 0
    case "--height": height = Int(it.next() ?? "") ?? 0
    case "--fps": fps = Int(it.next() ?? "") ?? 0
    case "--frames": maxFrames = Int(it.next() ?? "") ?? -1
    case "--check": checkOnly = true
    default: fail(2, "usage: window-capture (--match TEXT | --window-id ID) --width W --height H [--fps N] [--frames N] | --check")
    }
}

if !CGPreflightScreenCaptureAccess() {
    if checkOnly { print("{\"screen_recording\":false}"); exit(3) }
    fail(3, "Screen Recording permission is not granted for the process that launched this tool")
}
if checkOnly { print("{\"screen_recording\":true}"); exit(0) }
guard windowID > 0 || !match.isEmpty, width >= 16, height >= 16, width % 2 == 0, height % 2 == 0, (1...60).contains(fps) else {
    fail(2, "need --match or --window-id, even --width/--height >= 16, and --fps 1..60")
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    let lock = NSLock()
    var latest: CVPixelBuffer?
    var lastFrameAt = Date()
    var warnedIdle = false
    var sent = 0
    var streamStopped = false
    var packed = [UInt8](repeating: 0, count: width * height * 4)
    let writeQueue = DispatchQueue(label: "ano.window-capture.write")
    var timer: DispatchSourceTimer?

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sb.isValid,
              let attachments = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
              let raw = attachments.first?[.status] as? Int,
              SCFrameStatus(rawValue: raw) == .complete,
              let pixels = CMSampleBufferGetImageBuffer(sb) else { return }
        seed(pixels)
    }

    func secondsSinceFrame() -> Double {
        lock.lock(); defer { lock.unlock() }
        return Date().timeIntervalSince(lastFrameAt)
    }

    func seed(_ pixels: CVPixelBuffer) {
        lock.lock(); latest = pixels; lastFrameAt = Date(); warnedIdle = false; lock.unlock()
    }

    func setStopped(_ value: Bool) {
        lock.lock(); streamStopped = value; lock.unlock()
    }

    func isStopped() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return streamStopped
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("capture stream stopped: \(error.localizedDescription)")
        setStopped(true)
    }

    func emit() {
        lock.lock()
        let pixels = latest
        let idle = Date().timeIntervalSince(lastFrameAt)
        if idle > 5 && !warnedIdle {
            warnedIdle = true
            log("no new frame for 5s (window minimized, hidden or on another Space?); repeating last frame")
        }
        lock.unlock()
        guard let pixels else { return }
        CVPixelBufferLockBaseAddress(pixels, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixels, .readOnly) }
        guard CVPixelBufferGetWidth(pixels) == width, CVPixelBufferGetHeight(pixels) == height,
              let base = CVPixelBufferGetBaseAddress(pixels) else { return }
        let stride = CVPixelBufferGetBytesPerRow(pixels)
        let rowBytes = width * 4
        packed.withUnsafeMutableBytes { dst in
            for row in 0..<height {
                memcpy(dst.baseAddress! + row * rowBytes, base + row * stride, rowBytes)
            }
        }
        let wrote = packed.withUnsafeBytes { fwrite($0.baseAddress, 1, $0.count, stdout) }
        if wrote != packed.count || fflush(stdout) != 0 { exit(6) }
        sent += 1
        if maxFrames > 0 && sent >= maxFrames { exit(0) }
    }

    func startTimer() {
        let t = DispatchSource.makeTimerSource(queue: writeQueue)
        t.schedule(deadline: .now(), repeating: .nanoseconds(1_000_000_000 / fps), leeway: .microseconds(500))
        t.setEventHandler { [unowned self] in self.emit() }
        timer = t
        t.resume()
    }
}

// ScreenCaptureKit asserts (CGS_REQUIRE_INIT) in a bare command-line tool unless the
// window server connection is initialised first.
_ = NSApplication.shared
setvbuf(stdout, nil, _IOFBF, 1 << 20)
signal(SIGPIPE, SIG_IGN)

func screenshot(_ filter: SCContentFilter, _ config: SCStreamConfiguration) async -> CVPixelBuffer? {
    guard let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config) else { return nil }
    var buffer: CVPixelBuffer?
    let attrs = [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary
    guard CVPixelBufferCreate(nil, width, height, kCVPixelFormatType_32BGRA, attrs, &buffer) == kCVReturnSuccess,
          let buffer else { return nil }
    CVPixelBufferLockBaseAddress(buffer, [])
    defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
    guard let context = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: width, height: height,
                                  bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
                                  space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
    else { return nil }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return buffer
}

func describe(_ w: SCWindow) -> String {
    "\(w.owningApplication?.applicationName ?? "?") id \(w.windowID) \"\(w.title ?? "")\""
}

/// The window to capture right now, or nil. Matching windows are normal (layer 0) and
/// large enough to be a real browser window; the largest wins.
func resolveWindow() async -> SCWindow? {
    guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false) else { return nil }
    if windowID > 0 { return content.windows.first { $0.windowID == windowID } }
    let found = content.windows.filter { w in
        w.windowLayer == 0 && w.frame.width > 320 && w.frame.height > 200
            && "\(w.owningApplication?.applicationName ?? "") \(w.title ?? "")".lowercased().contains(match)
    }
    // Prefer windows that are visible right now, then the biggest.
    return found.max { a, b in
        if a.isOnScreen != b.isOnScreen { return !a.isOnScreen }
        return a.frame.width * a.frame.height < b.frame.width * b.frame.height
    }
}

let capture = Capture()

struct Session {
    let window: SCWindow
    let stream: SCStream
    let filter: SCContentFilter
    let config: SCStreamConfiguration
}

func begin(_ window: SCWindow) async throws -> Session {
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let config = SCStreamConfiguration()
    config.width = width
    config.height = height
    config.pixelFormat = kCVPixelFormatType_32BGRA
    config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
    config.showsCursor = false
    config.scalesToFit = true
    config.queueDepth = 5
    config.capturesAudio = false
    let stream = SCStream(filter: filter, configuration: config, delegate: capture)
    try stream.addStreamOutput(capture, type: .screen, sampleHandlerQueue: DispatchQueue(label: "ano.window-capture.frames"))
    try await stream.startCapture()
    capture.setStopped(false)
    log("capturing \(describe(window)) at \(width)x\(height)@\(fps)")
    return Session(window: window, stream: stream, filter: filter, config: config)
}

Task {
    var session: Session?
    var lostSince: Date?
    capture.startTimer()
    while true {
        let stopped = capture.isStopped()
        let quiet = capture.secondsSinceFrame() > 3
        if session == nil || stopped || quiet {
            let target = await resolveWindow()
            if let target, session == nil || stopped || target.windowID != session!.window.windowID {
                if let old = session { try? await old.stream.stopCapture() }
                session = nil
                do { session = try await begin(target) }
                catch { log("could not start capture of \(describe(target)): \(error.localizedDescription)") }
            }
            if target == nil || session == nil {
                if lostSince == nil {
                    lostSince = Date()
                    log("no capturable window matches \(windowID > 0 ? "id \(windowID)" : "\"\(match)\""); waiting up to 60s")
                }
                if Date().timeIntervalSince(lostSince!) > 60 {
                    fail(4, "window not found for 60s; ending capture so the broadcast does not freeze on a stale frame")
                }
            } else {
                lostSince = nil
            }
        }
        // ScreenCaptureKit sends frames only when the window changes, so a static page can
        // produce none at all. Refresh from a screenshot while the stream is quiet.
        if let s = session, capture.secondsSinceFrame() > 0.9, let shot = await screenshot(s.filter, s.config) {
            capture.seed(shot)
            lostSince = nil
        }
        try? await Task.sleep(nanoseconds: 1_000_000_000)
    }
}
dispatchMain()
