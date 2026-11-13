// Single-window capture for ano-stream on macOS (ScreenCaptureKit).
//
//   window-capture --match TEXT --width W --height H [--fps 60] [--frames N]
//   window-capture --window-id ID --width W --height H [--fps 60] [--frames N]
//   window-capture --pick --width W --height H [--fps 60] [--guard TEXT]
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
// --pick shows Apple's system sharing picker (the same one Meet and Zoom use): click any
// window or screen. Nothing is sent until you choose, and you can change the shared
// window later from the sharing control in the menu bar. Cancelling or stopping sharing
// ends the capture.
//
// --audio-fd N also captures the shared app's audio and writes it to file descriptor N as
// raw interleaved float32 little-endian, 48 kHz stereo, at a constant rate (silence when
// there is none). ScreenCaptureKit exposes audio per application, not per tab or window, so
// every tab of the shared browser that is playing sound is included; mute the others.
//
// --guard TEXT (with --pick) keeps the shared window honest: whenever its title does not
// contain TEXT (for example a different tab was opened in it) the picture goes black and the
// audio goes silent until the title matches again. The check runs four times a second, so a
// tab switch can show for a fraction of a second before the black frames start.
//
// --window-id pins one window id for its whole life (no re-resolve). With both
// --window-id and --match, the window must have that id AND a matching title.
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
var pick = false
var audioFd: Int32 = -1
var guardText = ""

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
    case "--pick": pick = true
    case "--audio-fd": audioFd = Int32(it.next() ?? "") ?? -1
    case "--guard": guardText = (it.next() ?? "").lowercased()
    default: fail(2, "usage: window-capture (--match TEXT | --window-id ID) --width W --height H [--fps N] [--frames N] | --check")
    }
}

if !CGPreflightScreenCaptureAccess() {
    if checkOnly { print("{\"screen_recording\":false}"); exit(3) }
    fail(3, "Screen Recording permission is not granted for the process that launched this tool")
}
if checkOnly { print("{\"screen_recording\":true}"); exit(0) }
guard windowID > 0 || !match.isEmpty || pick, width >= 16, height >= 16, width % 2 == 0, height % 2 == 0, (1...60).contains(fps) else {
    fail(2, "need --match, --window-id or --pick, even --width/--height >= 16, and --fps 1..60")
}
if !guardText.isEmpty && !pick { fail(2, "--guard needs --pick (--match already requires the title text)") }

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    let lock = NSLock()
    var latest: CVPixelBuffer?
    var lastFrameAt = Date()
    var warnedIdle = false
    var sent = 0
    var streamStopped = false
    var blank = false
    var zeros = [UInt8](repeating: 0, count: width * height * 4)
    var packed = [UInt8](repeating: 0, count: width * height * 4)
    let writeQueue = DispatchQueue(label: "ano.window-capture.write")
    var timer: DispatchSourceTimer?

    // Audio: interleaved stereo float32 at 48 kHz, queued from ScreenCaptureKit and paid out at
    // exactly wall-clock rate so the encoder always sees a continuous track.
    let audioRate = 48000
    var audioQueue = [Float]()
    var audioFile: UnsafeMutablePointer<FILE>?
    var audioStartedAt: Date?
    var audioWritten = 0
    let audioWriteQueue = DispatchQueue(label: "ano.window-capture.audio-write")
    var audioTimer: DispatchSourceTimer?

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        if type == .audio { takeAudio(sb); return }
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
        lock.lock()
        // While blanked, nothing from the (wrong) window may become the next frame sent.
        if !blank { latest = pixels }
        lastFrameAt = Date(); warnedIdle = false
        lock.unlock()
    }

    func isBlank() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return blank
    }

    func takeAudio(_ sb: CMSampleBuffer) {
        guard audioFd >= 0, sb.isValid, CMSampleBufferGetNumSamples(sb) > 0,
              let format = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(format)?.pointee,
              asbd.mFormatID == kAudioFormatLinearPCM,
              asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0, asbd.mBitsPerChannel == 32 else { return }
        let frames = CMSampleBufferGetNumSamples(sb)
        let channels = max(Int(asbd.mChannelsPerFrame), 1)
        let planar = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
        var samples = [Float](repeating: 0, count: frames * 2)
        do {
            try sb.withAudioBufferList { list, _ in
                for (index, buffer) in list.enumerated() {
                    guard let data = buffer.mData?.assumingMemoryBound(to: Float.self) else { continue }
                    if planar {
                        let available = min(frames, Int(buffer.mDataByteSize) / 4)
                        for i in 0..<available {
                            if list.count == 1 { samples[i * 2] = data[i]; samples[i * 2 + 1] = data[i] }
                            else if index < 2 { samples[i * 2 + index] = data[i] }
                        }
                    } else {
                        let available = min(frames, Int(buffer.mDataByteSize) / (4 * channels))
                        for i in 0..<available {
                            samples[i * 2] = data[i * channels]
                            samples[i * 2 + 1] = data[i * channels + (channels > 1 ? 1 : 0)]
                        }
                    }
                }
            }
        } catch { return }
        lock.lock()
        if !blank { audioQueue.append(contentsOf: samples) }
        // Never let the queue fall more than a second behind real time.
        if audioQueue.count > audioRate * 2 { audioQueue.removeFirst(audioQueue.count - audioRate * 2 / 5) }
        lock.unlock()
    }

    /// Start the audio clock when capture first begins, so audio and video start together.
    func startAudioClock() {
        lock.lock(); defer { lock.unlock() }
        if audioStartedAt == nil { audioStartedAt = Date() }
    }

    func emitAudio() {
        guard let file = audioFile else { return }
        lock.lock()
        guard let start = audioStartedAt else { lock.unlock(); return }
        let due = Int(Date().timeIntervalSince(start) * Double(audioRate)) - audioWritten
        if due <= 0 { lock.unlock(); return }
        var out = [Float](repeating: 0, count: due * 2)
        if blank {
            audioQueue.removeAll()
        } else {
            let n = min(out.count, audioQueue.count)
            for i in 0..<n { out[i] = audioQueue[i] }
            audioQueue.removeFirst(n)
        }
        audioWritten += due
        lock.unlock()
        let wrote = out.withUnsafeBytes { fwrite($0.baseAddress, 1, $0.count, file) }
        if wrote != out.count * 4 || fflush(file) != 0 { exit(6) }
    }

    func startAudioTimer() {
        guard audioFd >= 0 else { return }
        audioFile = fdopen(audioFd, "w")
        guard audioFile != nil else { fail(2, "cannot write audio to file descriptor \(audioFd)") }
        setvbuf(audioFile, nil, _IOFBF, 1 << 16)
        let t = DispatchSource.makeTimerSource(queue: audioWriteQueue)
        t.schedule(deadline: .now(), repeating: .milliseconds(20), leeway: .milliseconds(2))
        t.setEventHandler { [unowned self] in self.emitAudio() }
        audioTimer = t
        t.resume()
    }

    func setStopped(_ value: Bool) {
        lock.lock(); streamStopped = value; lock.unlock()
    }

    func isStopped() -> Bool {
        lock.lock(); defer { lock.unlock() }
        return streamStopped
    }

    /// While no matching window is capturable, send black frames and drop the last real frame
    /// so nothing stale (or from a window that stopped matching) is ever broadcast.
    func setBlank(_ value: Bool) {
        lock.lock(); blank = value; if value { latest = nil; audioQueue.removeAll() }; lock.unlock()
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("capture stream stopped: \(error.localizedDescription)")
        setStopped(true)
    }

    func emit() {
        lock.lock()
        let pixels = latest
        let showBlank = blank
        let idle = Date().timeIntervalSince(lastFrameAt)
        if idle > 5 && !warnedIdle {
            warnedIdle = true
            log("no new frame for 5s (window minimized, hidden or on another Space?); repeating last frame")
        }
        lock.unlock()
        if showBlank {
            let wroteBlank = zeros.withUnsafeBytes { fwrite($0.baseAddress, 1, $0.count, stdout) }
            if wroteBlank != zeros.count || fflush(stdout) != 0 { exit(6) }
            return
        }
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
    if windowID > 0 {
        // A pinned id must also still match the title text, if one was given.
        return content.windows.first { w in
            w.windowID == windowID
                && (match.isEmpty || "\(w.owningApplication?.applicationName ?? "") \(w.title ?? "")".lowercased().contains(match))
        }
    }
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
    let window: SCWindow?
    let stream: SCStream
    var filter: SCContentFilter
    let config: SCStreamConfiguration
}

func begin(_ window: SCWindow) async throws -> Session {
    try await begin(filter: SCContentFilter(desktopIndependentWindow: window), window: window, label: describe(window))
}

func begin(filter: SCContentFilter, window: SCWindow?, label: String) async throws -> Session {
    let config = SCStreamConfiguration()
    config.width = width
    config.height = height
    config.pixelFormat = kCVPixelFormatType_32BGRA
    config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
    config.showsCursor = false
    config.scalesToFit = true
    config.queueDepth = 5
    config.capturesAudio = audioFd >= 0
    config.sampleRate = capture.audioRate
    config.channelCount = 2
    config.excludesCurrentProcessAudio = true
    let stream = SCStream(filter: filter, configuration: config, delegate: capture)
    try stream.addStreamOutput(capture, type: .screen, sampleHandlerQueue: DispatchQueue(label: "ano.window-capture.frames"))
    if audioFd >= 0 {
        try stream.addStreamOutput(capture, type: .audio, sampleHandlerQueue: DispatchQueue(label: "ano.window-capture.audio"))
    }
    try await stream.startCapture()
    capture.startAudioClock()
    capture.setStopped(false)
    capture.setBlank(false)
    log("capturing \(label) at \(width)x\(height)@\(fps)")
    return Session(window: window, stream: stream, filter: filter, config: config)
}

final class PickerObserver: NSObject, SCContentSharingPickerObserver {
    var session: Session?

    func describeFilter(_ filter: SCContentFilter) -> String {
        let windows = filter.includedWindows.map(describe)
        if !windows.isEmpty { return windows.joined(separator: ", ") }
        return filter.includedDisplays.map { "display \($0.displayID)" }.joined(separator: ", ")
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        fail(4, stream == nil ? "nothing was chosen in the sharing picker" : "sharing was stopped")
    }

    func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter, for stream: SCStream?) {
        Task {
            do {
                if let s = session {
                    try await s.stream.updateContentFilter(filter)
                    session?.filter = filter
                    log("now sharing \(describeFilter(filter))")
                } else {
                    session = try await begin(filter: filter, window: nil, label: describeFilter(filter))
                }
            } catch {
                fail(5, "could not start capture of the chosen content: \(error.localizedDescription)")
            }
        }
    }

    func contentSharingPickerStartDidFailWithError(_ error: Error) {
        fail(5, "sharing picker failed: \(error.localizedDescription)")
    }
}

if pick {
    let observer = PickerObserver()
    NSApplication.shared.setActivationPolicy(.accessory)
    capture.startTimer()   // no frames are sent until something is chosen
    capture.startAudioTimer()
    Task { @MainActor in
        var cfg = SCContentSharingPickerConfiguration()
        cfg.allowedPickerModes = [.singleWindow, .singleDisplay]
        cfg.allowsChangingSelectedContent = true
        let picker = SCContentSharingPicker.shared
        picker.add(observer)
        picker.defaultConfiguration = cfg
        picker.isActive = true
        picker.present()
        log("waiting for you to choose a window or screen in the macOS sharing picker")
        var guardFailing = false
        while true {
            if capture.isStopped() { fail(5, "capture stopped (sharing ended)") }
            if !guardText.isEmpty, let s = observer.session {
                // The shared window must still show the expected page (its title carries the
                // active tab); otherwise send black and silence instead of another tab.
                let ids = s.filter.includedWindows.map { $0.windowID }
                if ids.isEmpty { fail(2, "--guard needs a shared window, not a screen") }
                let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                if let content {
                    let ok = content.windows.contains { w in
                        ids.contains(w.windowID) && (w.title ?? "").lowercased().contains(guardText)
                    }
                    if !ok && !guardFailing { log("shared window no longer shows \"\(guardText)\" (another tab?); sending black and silence") }
                    if ok && guardFailing { log("shared window shows \"\(guardText)\" again; resuming") }
                    guardFailing = !ok
                    capture.setBlank(!ok)
                }
            }
            if let s = observer.session, !capture.isBlank(), capture.secondsSinceFrame() > 0.9, let shot = await screenshot(s.filter, s.config) {
                capture.seed(shot)
            }
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
    }
    NSApplication.shared.run()
}

Task {
    var session: Session?
    var lostSince: Date?
    capture.startTimer()
    capture.startAudioTimer()
    while true {
        // Every second, confirm the captured window still matches. A window whose title no
        // longer matches (for example its tab was switched to another page) is dropped at once.
        let target = await resolveWindow()
        if target == nil {
            if let old = session { try? await old.stream.stopCapture(); session = nil }
            capture.setBlank(true)
        } else if let target, session == nil || capture.isStopped() || target.windowID != session!.window?.windowID {
            if let old = session { try? await old.stream.stopCapture() }
            session = nil
            do { session = try await begin(target) }
            catch {
                log("could not start capture of \(describe(target)): \(error.localizedDescription)")
                capture.setBlank(true)
            }
        }
        if session == nil {
            if lostSince == nil {
                lostSince = Date()
                log("no capturable window matches \(windowID > 0 ? "id \(windowID)" : "\"\(match)\""); sending black, waiting up to 60s")
            }
            if Date().timeIntervalSince(lostSince!) > 60 {
                fail(4, "window not found for 60s; ending capture")
            }
        } else {
            lostSince = nil
        }
        // ScreenCaptureKit sends frames only when the window changes, so a static page can
        // produce none at all. Refresh from a screenshot while the stream is quiet.
        if let s = session, capture.secondsSinceFrame() > 0.9, let shot = await screenshot(s.filter, s.config) {
            capture.seed(shot)
        }
        try? await Task.sleep(nanoseconds: 1_000_000_000)
    }
}
dispatchMain()
