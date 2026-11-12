// CGEvent helper for the "os" backend of game-restart.
// No third-party deps: build with `swiftc -O os-helper.swift -o os-helper`.
//
//   os-helper windows                 -> JSON [{owner,name,x,y,w,h}]
//   os-helper click X Y               -> left click at absolute screen coords
//   os-helper key Escape|Return|...   -> key press by name
//
// Requires Accessibility permission (System Settings > Privacy & Security >
// Accessibility) for the terminal/app that runs it. Without it, events are
// silently dropped by macOS.

import Foundation
import CoreGraphics

func post(_ e: CGEvent?) {
    e?.post(tap: .cghidEventTap)
}

func click(_ x: Double, _ y: Double) {
    let p = CGPoint(x: x, y: y)
    post(CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left))
    usleep(60_000)
    post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left))
    usleep(60_000)
    post(CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left))
}

let keyCodes: [String: CGKeyCode] = [
    "Escape": 53, "Return": 36, "Enter": 36, "Tab": 48, "Space": 49,
    "Delete": 51, "Up": 126, "Down": 125, "Left": 123, "Right": 124,
    "W": 13, "A": 0, "S": 1, "D": 2, "E": 14, "R": 15, "Q": 12, "F": 3,
    "Cmd": 55, "Shift": 56, "Ctrl": 59, "Opt": 58,
]

func key(_ name: String, _ down: Bool) {
    guard let code = keyCodes[name] else {
        FileHandle.standardError.write("unknown key: \(name)\n".data(using: .utf8)!)
        exit(2)
    }
    let e = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)
    post(e)
}

func windows() {
    let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
        print("[]"); return
    }
    var out: [[String: Any]] = []
    for w in list {
        let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
        out.append([
            "id": w[kCGWindowNumber as String] as? Int ?? 0,
            "owner": w[kCGWindowOwnerName as String] as? String ?? "",
            "name": w[kCGWindowName as String] as? String ?? "",
            "x": b["X"] as? Double ?? 0, "y": b["Y"] as? Double ?? 0,
            "w": b["Width"] as? Double ?? 0, "h": b["Height"] as? Double ?? 0,
        ])
    }
    let data = try! JSONSerialization.data(withJSONObject: out, options: [])
    print(String(data: data, encoding: .utf8)!)
}

func displays() {
    var count: UInt32 = 0
    CGGetActiveDisplayList(0, nil, &count)
    var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
    CGGetActiveDisplayList(count, &ids, &count)
    let out: [[String: Any]] = ids.prefix(Int(count)).map { id in
        let b = CGDisplayBounds(id)
        let mode = CGDisplayCopyDisplayMode(id)
        return ["id": id, "main": id == CGMainDisplayID(), "x": b.minX, "y": b.minY,
                "w": b.width, "h": b.height, "pixel_w": mode?.pixelWidth ?? 0,
                "pixel_h": mode?.pixelHeight ?? 0]
    }
    let data = try! JSONSerialization.data(withJSONObject: out, options: [])
    print(String(data: data, encoding: .utf8)!)
}

let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else {
    FileHandle.standardError.write("usage: os-helper windows|click X Y|key NAME\n".data(using: .utf8)!)
    exit(2)
}
switch cmd {
case "windows": windows()
case "displays": displays()
case "permissions": print("{\"screen_recording\":\(CGPreflightScreenCaptureAccess())}")
case "click":
    guard args.count >= 3, let x = Double(args[1]), let y = Double(args[2]) else { exit(2) }
    click(x, y)
case "key":
    guard args.count >= 2 else { exit(2) }
    key(args[1], true); usleep(50_000); key(args[1], false)
case "keydown":
    guard args.count >= 2 else { exit(2) }
    key(args[1], true)
case "keyup":
    guard args.count >= 2 else { exit(2) }
    key(args[1], false)
default:
    exit(2)
}
