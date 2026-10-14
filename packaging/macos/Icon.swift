import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
for (size, suffix) in [(16,"16x16"),(32,"16x16@2x"),(32,"32x32"),(64,"32x32@2x"),(128,"128x128"),(256,"128x128@2x"),(256,"256x256"),(512,"256x256@2x"),(512,"512x512"),(1024,"512x512@2x")] {
    let image = NSImage(size: NSSize(width: size, height: size))
    image.lockFocus()
    let s = CGFloat(size)
    let rect = NSRect(x: s * 0.05, y: s * 0.05, width: s * 0.90, height: s * 0.90)
    let tile = NSBezierPath(roundedRect: rect, xRadius: s * 0.21, yRadius: s * 0.21)
    NSGradient(starting: NSColor(srgbRed: 0.07, green: 0.25, blue: 0.22, alpha: 1),
               ending: NSColor(srgbRed: 0.02, green: 0.10, blue: 0.13, alpha: 1))!.draw(in: tile, angle: -60)
    let symbol = NSImage(systemSymbolName: "camera.aperture", accessibilityDescription: nil)!
    let config = NSImage.SymbolConfiguration(paletteColors: [NSColor(srgbRed: 0.69, green: 0.92, blue: 0.66, alpha: 1)])
    symbol.withSymbolConfiguration(config)!.draw(in: NSRect(x: s * 0.21, y: s * 0.21, width: s * 0.58, height: s * 0.58))
    image.unlockFocus()
    let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
    try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent("icon_\(suffix).png"))
}
