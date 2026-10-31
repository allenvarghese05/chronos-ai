import { type EventSubscription, requireNativeModule } from 'expo-modules-core';

// ─── Types ───────────────────────────────────────────────────────────────────

export type PedometerAuthStatus = 'authorized' | 'denied' | 'restricted' | 'not_determined';

export type StepQueryResult = {
	steps: number;
	distanceM: number;
};

export type ActivitySummary = {
	walkingSec: number;
	runningSec: number;
	stationarySec: number;
	automotiveSec: number;
	cyclingSec: number;
	unknownSec: number;
	walkingConf: 'low' | 'medium' | 'high';
};

export type DeviceState = {
	/** Protected-data availability proxy, not an exact screen-unlock event. */
	isUnlocked: boolean;
	batteryState: 'unplugged' | 'charging' | 'full' | 'unknown';
	/** Battery level [0, 1] or null when unavailable */
	batteryLevel: number | null;
	appState: 'active' | 'inactive' | 'background';
	lowPowerMode: boolean;
	iosVersion: string;
};

export type BGRefreshEvent = { id: string; endedExecutionId?: string };
export type BGRefreshExecution = { id: string; executionId: string };
export type RefreshStatus = 'available' | 'denied' | 'restricted' | 'unknown';

// ─── Module interface ─────────────────────────────────────────────────────────

export type ChronosSensorsModuleType = {
	/** Returns a greeting string to prove the native bridge is working. */
	hello(): string;

	/** Returns the current CoreMotion pedometer authorization status. */
	pedometerAuthorization(): Promise<PedometerAuthStatus>;

	/**
	 * Triggers a short pedometer query to prompt iOS to show the
	 * NSMotionUsageDescription permission dialog (if not yet determined).
	 * Resolves when the query completes or the permission is already known.
	 */
	requestMotionPermission(): Promise<void>;

	/**
	 * Query step count and distance between two timestamps (ms since epoch).
	 * Rejects with code "pedometer_unavailable" when the device does not
	 * support step counting (e.g. iOS Simulator).
	 */
	querySteps(fromMs: number, toMs: number): Promise<StepQueryResult>;

	/**
	 * Query per-activity-type durations between two timestamps (ms since epoch).
	 * Duration is computed from CoreMotion's change-point format.
	 */
	queryActivity(fromMs: number, toMs: number): Promise<ActivitySummary>;

	/**
	 * Return current device state synchronously.
	 * Must be called on the main thread; the native implementation ensures this.
	 */
	deviceState(): DeviceState;

	/**
	 * Log that the JS layer has registered for background refresh.
	 * The native BGTaskScheduler is registered at app launch by the
	 * AppDelegateSubscriber; this call is informational only.
	 */
	registerBackgroundRefresh(identifier: string): void;

	/**
	 * Schedule the next BGAppRefreshTask to run no earlier than
	 * `earliestInSec` seconds from now.
	 */
	scheduleBackgroundRefresh(identifier: string, earliestInSec: number): boolean;
	/** Actual UIKit setting; this is not a requestable permission. */
	backgroundRefreshStatus(): RefreshStatus;
	/** Atomically claim a pending execution after the event listener is installed. */
	takePendingBackgroundRefresh(): BGRefreshExecution | null;

	/**
	 * Signal completion for the claimed execution ID; stale IDs cannot complete a newer task.
	 * Must be called within ~25 s of receiving the event; the native module
	 * auto-completes with success=false after that timeout.
	 */
	completeBackgroundRefresh(executionId: string, success: boolean): void;

	/**
	 * Subscribe to background refresh events.
	 * The event payload is `{ id: string }` where `id` is the BG task identifier.
	 */
	addListener(
		eventType: 'backgroundRefresh',
		listener: (event: BGRefreshEvent) => void,
	): EventSubscription;
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

/** Fallback used in Jest / Expo Go where the native module is not linked. */
const stub: ChronosSensorsModuleType = {
	hello: () => 'ChronosSensors unavailable',
	pedometerAuthorization: () => Promise.resolve('not_determined'),
	requestMotionPermission: () => Promise.resolve(),
	querySteps: () => Promise.resolve({ steps: 0, distanceM: 0 }),
	queryActivity: () =>
		Promise.resolve({
			walkingSec: 0,
			runningSec: 0,
			stationarySec: 0,
			automotiveSec: 0,
			cyclingSec: 0,
			unknownSec: 0,
			walkingConf: 'low',
		}),
	deviceState: () => ({
		isUnlocked: true,
		batteryState: 'unknown',
		batteryLevel: null,
		appState: 'active',
		lowPowerMode: false,
		iosVersion: '0.0',
	}),
	registerBackgroundRefresh: () => {},
	scheduleBackgroundRefresh: () => false,
	backgroundRefreshStatus: () => 'unknown',
	takePendingBackgroundRefresh: () => null,
	completeBackgroundRefresh: () => {},
	addListener: () => ({ remove: () => {} }),
};

// NOTE: The match string below is copied verbatim from
// expo-modules-core 57 src/requireNativeModule.ts.
// It MUST be re-verified against the installed expo-modules-core on every
// Expo SDK bump; if the message changes the stub fallback will silently stop
// working and errors will propagate instead.
//
// Verified against expo-modules-core 57.0.x (installed in node_modules):
//   `Cannot find native module '<name>'`
// See: node_modules/expo-modules-core/src/requireNativeModule.ts
/**
 * Returns true when the error indicates the native module is simply not
 * registered (Jest, Expo Go, CI without prebuild). Any other error
 * (e.g. the module was found but threw during initialisation) should
 * propagate so it isn't silently swallowed.
 */
function isModuleNotFoundError(err: unknown): boolean {
	return err instanceof Error && err.message.includes("Cannot find native module 'ChronosSensors'");
}

function loadModule(): ChronosSensorsModuleType {
	try {
		return requireNativeModule<ChronosSensorsModuleType>('ChronosSensors');
	} catch (err) {
		if (isModuleNotFoundError(err)) {
			// Native module is not linked (Jest, Expo Go, CI without prebuild).
			if (__DEV__) {
				console.warn(
					'[ChronosSensors] Native module not linked — using stub fallback. ' +
						'Run `expo run:ios` with a dev-client build to enable the real module.',
				);
			}
			return stub;
		}
		// Module was found but initialisation threw — do not swallow.
		throw err;
	}
}

const ChronosSensors: ChronosSensorsModuleType = loadModule();

export { ChronosSensors };
export default ChronosSensors;

// ─── Convenience re-export ────────────────────────────────────────────────────

/** The BGTaskScheduler identifier used by this module (matches app.config.ts). */
export const BG_REFRESH_IDENTIFIER = 'dev.allenvarghese.chronos.refresh';

/**
 * Subscribe to background refresh events.
 * Thin wrapper around `ChronosSensors.addListener` for ergonomic use.
 */
export function addBackgroundRefreshListener(
	listener: (event: BGRefreshEvent) => void,
): EventSubscription {
	return ChronosSensors.addListener('backgroundRefresh', listener);
}
