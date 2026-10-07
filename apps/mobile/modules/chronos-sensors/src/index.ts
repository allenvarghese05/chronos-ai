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

function loadModule(): ChronosSensorsModuleType {
	try {
		return requireNativeModule<ChronosSensorsModuleType>('ChronosSensors');
	} catch {
		// Native module is not linked (Jest, Expo Go, CI without prebuild).
		return stub;
	}
}

const ChronosSensors: ChronosSensorsModuleType = loadModule();

export { ChronosSensors };
export default ChronosSensors;
