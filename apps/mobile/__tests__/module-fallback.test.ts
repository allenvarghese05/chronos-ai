/**
 * Tests the graceful fallback when the native module is not available, and that
 * unrelated errors from requireNativeModule are NOT swallowed.
 *
 * In Jest / Expo Go, requireNativeModule throws:
 *   "Cannot find native module 'ChronosSensors'"
 * when the module is not registered. The loadModule() wrapper in src/index.ts
 * falls back to the stub only for that specific message; any other error is
 * rethrown so it isn't silently hidden.
 */

// Isolate each test by resetting modules so the mock takes effect per-test.
// Silence the __DEV__ console.warn emitted by the stub fallback path so Jest
// output stays clean.
let warnSpy: jest.SpyInstance;

beforeEach(() => {
	jest.resetModules();
	warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
	warnSpy.mockRestore();
	// Clear any jest.mock() calls registered inside a test (e.g., the "rethrows" test).
	jest.unmock('expo-modules-core');
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

	// ── Not-found string verification ─────────────────────────────────────────
	// This test greps the installed expo-modules-core source to confirm the exact
	// not-found error message string that isModuleNotFoundError() matches against.
	// If the message ever changes (e.g. on an SDK bump), this test will fail,
	// prompting a re-verification and update of the match string.
	it('not-found match string matches installed expo-modules-core 57', () => {
		const fs = require('node:fs') as typeof import('fs');
		const path = require('node:path') as typeof import('path');

		// Verified path for expo-modules-core 57.x
		const srcFile = path.join(
			__dirname,
			'..',
			'node_modules',
			'expo-modules-core',
			'src',
			'requireNativeModule.ts',
		);

		expect(fs.existsSync(srcFile)).toBe(true);

		const source = fs.readFileSync(srcFile, 'utf-8');
		// The exact template literal from expo-modules-core:
		// throw new Error(`Cannot find native module '${moduleName}'`);
		const expectedFragment = "Cannot find native module '";
		expect(source).toContain(expectedFragment);

		// Also verify the module's isModuleNotFoundError check uses the same string.
		const moduleIndexFile = path.join(
			__dirname,
			'..',
			'modules',
			'chronos-sensors',
			'src',
			'index.ts',
		);
		const moduleSource = fs.readFileSync(moduleIndexFile, 'utf-8');
		expect(moduleSource).toContain(expectedFragment);
	});

	// ── Stub completeness ─────────────────────────────────────────────────────
	it('fallback stub querySteps resolves with { steps: 0, distanceM: 0 }', async () => {
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		const { default: ChronosSensors } = require('../modules/chronos-sensors/src') as {
			default: {
				querySteps: (from: number, to: number) => Promise<{ steps: number; distanceM: number }>;
			};
		};
		const result = await ChronosSensors.querySteps(0, 1);
		expect(result.steps).toBe(0);
		expect(result.distanceM).toBe(0);
	});

	it('fallback stub deviceState returns expected shape', () => {
		// eslint-disable-next-line @typescript-eslint/no-require-imports
		const { default: ChronosSensors } = require('../modules/chronos-sensors/src') as {
			default: { deviceState: () => Record<string, unknown> };
		};
		const state = ChronosSensors.deviceState();
		expect(typeof state.isUnlocked).toBe('boolean');
		expect(typeof state.batteryState).toBe('string');
		expect(typeof state.lowPowerMode).toBe('boolean');
	});
});
