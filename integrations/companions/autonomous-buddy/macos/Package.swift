// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AutonomousBuddy",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "AutonomousBuddy", targets: ["AutonomousBuddy"])
    ],
    dependencies: [
        // URLSessionWebSocketTask on Ventura misreads WS frames as HTTP; Starscream does its own framing.
        .package(url: "https://github.com/daltoniam/Starscream", from: "4.0.8")
    ],
    targets: [
        .executableTarget(
            name: "AutonomousBuddy",
            dependencies: ["Starscream"],
            path: "Sources/AutonomousBuddy"
        ),
        .testTarget(
            name: "AutonomousBuddyTests",
            dependencies: ["AutonomousBuddy"],
            path: "Tests/AutonomousBuddyTests"
        )
    ]
)
