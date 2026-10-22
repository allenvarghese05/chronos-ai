import Foundation

// MARK: - Inputs

/// Raw inputs from UIDevice / UIApplication / ProcessInfo.
/// All fields are primitives so the struct is testable without UIKit.
public struct DeviceStateInputs: Sendable {
    /// UIApplication.shared.isProtectedDataAvailable (false when locked)
    public let isProtectedDataAvailable: Bool
    /// UIDevice.current.batteryState.rawValue:
    ///   0 = unknown, 1 = unplugged, 2 = charging, 3 = full
    public let batteryStateRaw: Int
    /// UIDevice.current.batteryLevel: [0, 1] or -1 when unavailable
    public let batteryLevel: Float
    /// UIApplication.shared.applicationState.rawValue:
    ///   0 = active, 1 = inactive, 2 = background
    public let appStateRaw: Int
    /// ProcessInfo.processInfo.isLowPowerModeEnabled
    public let isLowPowerModeEnabled: Bool
    /// UIDevice.current.systemVersion
    public let iosVersion: String

    public init(
        isProtectedDataAvailable: Bool,
        batteryStateRaw: Int,
        batteryLevel: Float,
        appStateRaw: Int,
        isLowPowerModeEnabled: Bool,
        iosVersion: String
    ) {
        self.isProtectedDataAvailable = isProtectedDataAvailable
        self.batteryStateRaw = batteryStateRaw
        self.batteryLevel = batteryLevel
        self.appStateRaw = appStateRaw
        self.isLowPowerModeEnabled = isLowPowerModeEnabled
        self.iosVersion = iosVersion
    }
}

// MARK: - Pure mapping function

/// Map UIDevice/UIApplication/ProcessInfo primitive values to a JSON-serialisable dictionary.
///
/// - batteryState: 0→"unknown", 1→"unplugged", 2→"charging", 3→"full"
/// - batteryLevel: negative → nil (omitted in JSON); otherwise rounded to 4 decimal places
/// - appState:     0→"active", 1→"inactive", otherwise→"background"
/// - isUnlocked:   isProtectedDataAvailable (false when screen is locked)
public func mapDeviceState(_ inputs: DeviceStateInputs) -> [String: Any] {
    let batteryState: String
    switch inputs.batteryStateRaw {
    case 1:  batteryState = "unplugged"
    case 2:  batteryState = "charging"
    case 3:  batteryState = "full"
    default: batteryState = "unknown"
    }

    let appState: String
    switch inputs.appStateRaw {
    case 0:  appState = "active"
    case 1:  appState = "inactive"
    default: appState = "background"
    }

    var result: [String: Any] = [
        "isUnlocked":   inputs.isProtectedDataAvailable,
        "batteryState": batteryState,
        "appState":     appState,
        "lowPowerMode": inputs.isLowPowerModeEnabled,
        "iosVersion":   inputs.iosVersion,
    ]

    // batteryLevel < 0 means unavailable — omit from the dict so JSON serialisation
    // produces null when the caller sets the key to NSNull, or omits it entirely.
    if inputs.batteryLevel >= 0 {
        result["batteryLevel"] = Double(inputs.batteryLevel)
    } else {
        result["batteryLevel"] = NSNull()
    }

    return result
}
