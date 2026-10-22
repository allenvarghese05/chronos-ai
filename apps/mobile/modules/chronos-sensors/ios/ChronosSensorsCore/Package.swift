// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "ChronosSensorsCore",
    platforms: [.iOS(.v17), .macOS(.v13)],
    products: [
        .library(name: "ChronosSensorsCore", targets: ["ChronosSensorsCore"]),
    ],
    targets: [
        .target(name: "ChronosSensorsCore"),
        .testTarget(
            name: "ChronosSensorsCoreTests",
            dependencies: ["ChronosSensorsCore"]
        ),
    ]
)
