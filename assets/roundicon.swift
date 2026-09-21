// 圆角化图标源: 用法 roundicon <输入图> <输出png> <边长>
import AppKit

let args = CommandLine.arguments
guard args.count >= 4 else { exit(2) }
let input = args[1], output = args[2]
let size = CGFloat(Int(args[3]) ?? 1024)

guard let img = NSImage(contentsOfFile: input),
      let outRep = NSBitmapImageRep(bitmapDataPlanes: nil,
                                    pixelsWide: Int(size), pixelsHigh: Int(size),
                                    bitsPerSample: 8, samplesPerPixel: 4,
                                    hasAlpha: true, isPlanar: false,
                                    colorSpaceName: .deviceRGB,
                                    bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: outRep)
// macOS 图标规范: 图形本体约占画布 80%, 四周留透明边, 否则视觉上比系统图标大
let margin = size * 0.1
let body = size * 0.8
let rect = NSRect(x: margin, y: margin, width: body, height: body)
let radius = body * 0.225
NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius).addClip()
img.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1.0)
NSGraphicsContext.restoreGraphicsState()

guard let png = outRep.representation(using: .png, properties: [:]) else { exit(1) }
try! png.write(to: URL(fileURLWithPath: output))
