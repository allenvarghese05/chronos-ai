// Test the graceful fallback when the native module is not available, and that
// unrelated errors from requireNativeModule are NOT swallowed.
//
// In Jest / Expo Go, requireNativeModule throws:
//   "Cannot find native module 'ChronosSensors'"
// when the module is not registered. The loadModule() wrapper in src/index.ts
// falls back to the stub only for that specific message; any other error is
// rethrown so it isn't silently hidden.

// Isolate each test by resetting modules so the mock takes effect per-test.
// Silence the __DEV__ console.warn emitted by the stub fallback path so Jest
// output stays clean. The not-found test asserts the warn was actually fired.
let warnSpy: jest.SpyInstance;

beforeEach(() => {
	jest.resetModules();
	warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
});

describe('ChronosSensors fallback (native module not linked)', () => {
	it('falls back gracefully when requireNativeModule throws the not-found error', () => {
		// The real jest-expo environment does not register ChronosSensors, so
		// requireNativeModule throws the "Cannot find native module" message.
		// This test imports the module directly (no jest.mock) to exercise the
		// real loadModule() path.
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		const { default: ChronosSensors } = require('../modules/chronos-sensors/src') as {
			default: { hello: () => string };
		};
		expect(() => ChronosSensors.hello()).not.toThrow();
		expect(typeof ChronosSensors.hello()).toBe('string');
		// The stub fallback must have emitted the dev warning exactly once.
		expect(warnSpy).toHaveBeenCalledTimes(1);
		expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('ChronosSensors'));
	});

	it('fallback hello() returns the stub message when module is not linked', () => {
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		const { default: ChronosSensors } = require('../modules/chronos-sensors/src') as {
			default: { hello: () => string };
		};
		expect(ChronosSensors.hello()).toBe('ChronosSensors unavailable');
	});

	it('rethrows when requireNativeModule throws an unrelated error', () => {
		// Simulate a module that IS found but throws during initialisation.
		jest.mock('expo-modules-core', () => ({
			requireNativeModule: () => {
				throw new Error('NativeModule crashed during init');
			},
		}));
		expect(() => {
			// eslint-disable-next-line @typescript-eslint/no-require-imports
			require('../modules/chronos-sensors/src');
		}).toThrow('NativeModule crashed during init');
	});
});
