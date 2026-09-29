#!/usr/bin/env swift
//
// gen-app-icon.swift — placeholder 1024×1024 master PNG for `iconutil -c icns`.
// Run: `swift macos/scripts/gen-app-icon.swift <output.png>`

import AppKit
import CoreGraphics
import Foundation

let outputPath = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "dist/AppIcon-master.png"

let SIZE: CGFloat = 1024
let CORNER: CGFloat = 220   // matches macOS Big Sur+ app icon corner radius

let canvas = NSImage(size: NSSize(width: SIZE, height: SIZE))
canvas.lockFocus()

let bgRect = NSRect(x: 0, y: 0, width: SIZE, height: SIZE)
let bgPath = NSBezierPath(roundedRect: bgRect, xRadius: CORNER, yRadius: CORNER)
bgPath.addClip()

let gradient = NSGradient(starting: NSColor(red: 0.99, green: 0.86, blue: 0.45, alpha: 1),
                          ending:   NSColor(red: 0.95, green: 0.62, blue: 0.20, alpha: 1))!
gradient.draw(in: bgRect, angle: -90)

let config = NSImage.SymbolConfiguration(pointSize: 640, weight: .medium, scale: .large)
guard let symbolBase = NSImage(systemSymbolName: "lightbulb.fill",
                               accessibilityDescription: "Autonomous Buddy")?
    .withSymbolConfiguration(config) else {
    fputs("Could not load SF Symbol 'lightbulb.fill' — macOS 11+ required.\n", stderr)
    exit(1)
}

// Tint the symbol white by drawing into a fresh image and source-atop filling.
let symbolSize = symbolBase.size
let tinted = NSImage(size: symbolSize, flipped: false) { rect in
    symbolBase.draw(in: rect)
    NSColor.white.setFill()
    rect.fill(using: .sourceAtop)
    return true
}

let sx = (SIZE - symbolSize.width) / 2
let sy = (SIZE - symbolSize.height) / 2 - 20
tinted.draw(at: NSPoint(x: sx, y: sy),
            from: .zero,
            operation: .sourceOver,
            fraction: 1.0)

canvas.unlockFocus()

guard let tiff = canvas.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fputs("Failed to encode PNG.\n", stderr)
    exit(1)
}

let outURL = URL(fileURLWithPath: outputPath)
do {
    try FileManager.default.createDirectory(at: outURL.deletingLastPathComponent(),
                                            withIntermediateDirectories: true)
    try png.write(to: outURL)
    print("Wrote \(outputPath)")
} catch {
    fputs("Write failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
