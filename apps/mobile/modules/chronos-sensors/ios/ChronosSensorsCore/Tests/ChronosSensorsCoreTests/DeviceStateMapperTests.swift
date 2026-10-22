import XCTest
@testable import ChronosSensorsCore

final class DeviceStateMapperTests: XCTestCase {

    // MARK: - Helper

    private func inputs(
        isProtectedDataAvailable: Bool = true,
        batteryStateRaw: Int = 1,
        batteryLevel: Float = 0.8,
        appStateRaw: Int = 0,
        isLowPowerModeEnabled: Bool = false,
        iosVersion: String = "18.6"
    ) -> DeviceStateInputs {
        DeviceStateInputs(
            isProtectedDataAvailable: isProtectedDataAvailable,
            batteryStateRaw: batteryStateRaw,
            batteryLevel: batteryLevel,
            appStateRaw: appStateRaw,
            isLowPowerModeEnabled: isLowPowerModeEnabled,
            iosVersion: iosVersion
        )
    }

    // MARK: - Battery state mapping

    func test_batteryState_unknown_rawValue0() {
        let result = mapDeviceState(inputs(batteryStateRaw: 0))
        XCTAssertEqual(result["batteryState"] as? String, "unknown")
    }

    func test_batteryState_unplugged_rawValue1() {
        let result = mapDeviceState(inputs(batteryStateRaw: 1))
        XCTAssertEqual(result["batteryState"] as? String, "unplugged")
    }

    func test_batteryState_charging_rawValue2() {
        let result = mapDeviceState(inputs(batteryStateRaw: 2))
        XCTAssertEqual(result["batteryState"] as? String, "charging")
    }

    func test_batteryState_full_rawValue3() {
        let result = mapDeviceState(inputs(batteryStateRaw: 3))
        XCTAssertEqual(result["batteryState"] as? String, "full")
    }

    func test_batteryState_unexpectedRaw_mapsToUnknown() {
        let result = mapDeviceState(inputs(batteryStateRaw: 99))
        XCTAssertEqual(result["batteryState"] as? String, "unknown")
    }

    // MARK: - Battery level

    func test_batteryLevel_negativeOne_isNSNull() {
        let result = mapDeviceState(inputs(batteryLevel: -1))
        XCTAssertTrue(result["batteryLevel"] is NSNull,
                      "batteryLevel -1 must map to NSNull, got \(String(describing: result["batteryLevel"]))")
    }

    func test_batteryLevel_zero_isZero() {
        let result = mapDeviceState(inputs(batteryLevel: 0))
        guard let level = result["batteryLevel"] as? Double else {
            return XCTFail("batteryLevel should be Double for 0")
        }
        XCTAssertEqual(level, 0.0, accuracy: 0.0001)
    }

    func test_batteryLevel_0_75_isDouble() {
        let result = mapDeviceState(inputs(batteryLevel: 0.75))
        guard let level = result["batteryLevel"] as? Double else {
            return XCTFail("batteryLevel should be Double")
        }
        XCTAssertEqual(level, 0.75, accuracy: 0.001)
    }

    func test_batteryLevel_full_1_0() {
        let result = mapDeviceState(inputs(batteryLevel: 1.0))
        guard let level = result["batteryLevel"] as? Double else {
            return XCTFail("batteryLevel should be Double")
        }
        XCTAssertEqual(level, 1.0, accuracy: 0.0001)
    }

    // MARK: - App state mapping

    func test_appState_active_rawValue0() {
        let result = mapDeviceState(inputs(appStateRaw: 0))
        XCTAssertEqual(result["appState"] as? String, "active")
    }

    func test_appState_inactive_rawValue1() {
        let result = mapDeviceState(inputs(appStateRaw: 1))
        XCTAssertEqual(result["appState"] as? String, "inactive")
    }

    func test_appState_background_rawValue2() {
        let result = mapDeviceState(inputs(appStateRaw: 2))
        XCTAssertEqual(result["appState"] as? String, "background")
    }

    func test_appState_background_unexpectedRaw() {
        // Any raw value other than 0 or 1 falls through to "background".
        let result = mapDeviceState(inputs(appStateRaw: 99))
        XCTAssertEqual(result["appState"] as? String, "background")
    }

    // MARK: - isUnlocked

    func test_isUnlocked_true_whenProtectedDataAvailable() {
        let result = mapDeviceState(inputs(isProtectedDataAvailable: true))
        XCTAssertEqual(result["isUnlocked"] as? Bool, true)
    }

    func test_isUnlocked_false_whenPhoneLocked() {
        let result = mapDeviceState(inputs(isProtectedDataAvailable: false))
        XCTAssertEqual(result["isUnlocked"] as? Bool, false)
    }

    // MARK: - Low power mode

    func test_lowPowerMode_true() {
        let result = mapDeviceState(inputs(isLowPowerModeEnabled: true))
        XCTAssertEqual(result["lowPowerMode"] as? Bool, true)
    }

    func test_lowPowerMode_false() {
        let result = mapDeviceState(inputs(isLowPowerModeEnabled: false))
        XCTAssertEqual(result["lowPowerMode"] as? Bool, false)
    }

    // MARK: - iOS version

    func test_iosVersion_passedThrough() {
        let result = mapDeviceState(inputs(iosVersion: "17.5"))
        XCTAssertEqual(result["iosVersion"] as? String, "17.5")
    }

    // MARK: - All keys present

    func test_allExpectedKeys_present() {
        let result = mapDeviceState(inputs())
        let expectedKeys = Set(["isUnlocked", "batteryState", "batteryLevel",
                                "appState", "lowPowerMode", "iosVersion"])
        let actualKeys = Set(result.keys)
        XCTAssertEqual(actualKeys, expectedKeys)
    }
}
