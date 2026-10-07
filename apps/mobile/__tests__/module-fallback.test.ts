// Test the graceful fallback when the native module is not available.
// In Jest / Expo Go, requireNativeModule throws for unregistered modules;
// the loadModule() wrapper in src/index.ts catches this and returns the stub.
// No manual mock is required — jest-expo does not register ChronosSensors.
import ChronosSensors from '../modules/chronos-sensors/src';

describe('ChronosSensors fallback (native module not linked)', () => {
	it('falls back gracefully: hello() returns a string without throwing', () => {
		expect(() => ChronosSensors.hello()).not.toThrow();
		const result = ChronosSensors.hello();
		expect(typeof result).toBe('string');
	});

	it('fallback hello() returns the stub message', () => {
		// The stub (used when requireNativeModule throws) returns this value.
		expect(ChronosSensors.hello()).toBe('ChronosSensors unavailable');
	});
});
