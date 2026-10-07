import { requireNativeModule } from 'expo-modules-core';

/**
 * Public interface of the ChronosSensors native module.
 * In T0.4 only hello() exists; T2.1 adds the full sensor API
 * (querySteps, queryActivity, deviceState, backgroundRefresh events).
 */
export type ChronosSensorsModuleType = {
	/** Returns a greeting string to prove the native bridge is working. */
	hello(): string;
};

/** Fallback used in Jest / Expo Go where the native module is not linked. */
const stub: ChronosSensorsModuleType = {
	hello: () => 'ChronosSensors unavailable',
};

// NOTE: the match string below is copied verbatim from
// expo-modules-core 57 src/requireNativeModule.ts.
// It MUST be re-verified against the installed expo-modules-core on every
// Expo SDK bump; if the message changes the stub fallback will silently stop
// working and errors will propagate instead.
/**
 * Returns true when the error indicates the native module is simply not
 * registered (Jest, Expo Go, CI without prebuild). Any other error
 * (e.g. the module was found but threw during initialisation) should
 * propagate so it isn't silently swallowed.
 *
 * expo-modules-core 57 throws exactly:
 *   `Cannot find native module '<name>'`
 * See: node_modules/expo-modules-core/src/requireNativeModule.ts
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
