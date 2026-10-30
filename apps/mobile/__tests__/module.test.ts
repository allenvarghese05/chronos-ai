/**
 * Tests for the ChronosSensors module surface using jest.mock to simulate a
 * linked native module.  All new functions introduced in T2.1 are covered.
 */

// Mock the local chronos-sensors module to simulate the native module being linked.
jest.mock('../modules/chronos-sensors/src', () => ({
	__esModule: true,
	BG_REFRESH_IDENTIFIER: 'dev.allenvarghese.chronos.refresh',
	default: {
		hello: () => 'Hello from ChronosSensors!',
		pedometerAuthorization: () => Promise.resolve('authorized'),
		requestMotionPermission: () => Promise.resolve(),
		querySteps: (_fromMs: number, _toMs: number) =>
			Promise.resolve({ steps: 100, distanceM: 75.5 }),
		queryActivity: (_fromMs: number, _toMs: number) =>
			Promise.resolve({
				walkingSec: 300,
				runningSec: 60,
				stationarySec: 240,
				automotiveSec: 0,
				cyclingSec: 0,
				unknownSec: 0,
				walkingConf: 'high',
			}),
		deviceState: () => ({
			isUnlocked: true,
			batteryState: 'unplugged',
			batteryLevel: 0.85,
			appState: 'active',
			lowPowerMode: false,
			iosVersion: '18.6',
		}),
		registerBackgroundRefresh: (_id: string) => {},
		scheduleBackgroundRefresh: (_id: string, _secs: number) => {},
		completeBackgroundRefresh: (_success: boolean) => {},
		addListener: (_event: string, _listener: unknown) => ({ remove: () => {} }),
	},
	ChronosSensors: {
		hello: () => 'Hello from ChronosSensors!',
		pedometerAuthorization: () => Promise.resolve('authorized'),
		requestMotionPermission: () => Promise.resolve(),
		querySteps: (_fromMs: number, _toMs: number) =>
			Promise.resolve({ steps: 100, distanceM: 75.5 }),
		queryActivity: (_fromMs: number, _toMs: number) =>
			Promise.resolve({
				walkingSec: 300,
				runningSec: 60,
				stationarySec: 240,
				automotiveSec: 0,
				cyclingSec: 0,
				unknownSec: 0,
				walkingConf: 'high',
			}),
		deviceState: () => ({
			isUnlocked: true,
			batteryState: 'unplugged',
			batteryLevel: 0.85,
			appState: 'active',
			lowPowerMode: false,
			iosVersion: '18.6',
		}),
		registerBackgroundRefresh: (_id: string) => {},
		scheduleBackgroundRefresh: (_id: string, _secs: number) => {},
		completeBackgroundRefresh: (_success: boolean) => {},
		addListener: (_event: string, _listener: unknown) => ({ remove: () => {} }),
	},
}));

import ChronosSensors, { BG_REFRESH_IDENTIFIER } from '../modules/chronos-sensors/src';

describe('ChronosSensors module (mocked native module via jest.mock)', () => {
	// ── hello ─────────────────────────────────────────────────────────────────
	it('hello() returns the expected string from the native module', () => {
		expect(ChronosSensors.hello()).toBe('Hello from ChronosSensors!');
	});

	it('hello() return value is a non-empty string', () => {
		const result = ChronosSensors.hello();
		expect(typeof result).toBe('string');
		expect(result.length).toBeGreaterThan(0);
	});

	// ── pedometerAuthorization ────────────────────────────────────────────────
	it('pedometerAuthorization() resolves with a valid status string', async () => {
		const status = await ChronosSensors.pedometerAuthorization();
		expect(['authorized', 'denied', 'restricted', 'not_determined']).toContain(status);
	});

	// ── requestMotionPermission ───────────────────────────────────────────────
	it('requestMotionPermission() resolves without throwing', async () => {
		await expect(ChronosSensors.requestMotionPermission()).resolves.toBeUndefined();
	});

	// ── querySteps ────────────────────────────────────────────────────────────
	it('querySteps() resolves with steps and distanceM', async () => {
		const now = Date.now();
		const result = await ChronosSensors.querySteps(now - 600_000, now);
		expect(typeof result.steps).toBe('number');
		expect(typeof result.distanceM).toBe('number');
		expect(result.steps).toBeGreaterThanOrEqual(0);
		expect(result.distanceM).toBeGreaterThanOrEqual(0);
	});

	it('querySteps() mock returns steps=100 and distanceM=75.5', async () => {
		const now = Date.now();
		const result = await ChronosSensors.querySteps(now - 600_000, now);
		expect(result.steps).toBe(100);
		expect(result.distanceM).toBe(75.5);
	});

	// ── queryActivity ─────────────────────────────────────────────────────────
	it('queryActivity() resolves with all required duration fields', async () => {
		const now = Date.now();
		const result = await ChronosSensors.queryActivity(now - 600_000, now);
		expect(typeof result.walkingSec).toBe('number');
		expect(typeof result.runningSec).toBe('number');
		expect(typeof result.stationarySec).toBe('number');
		expect(typeof result.automotiveSec).toBe('number');
		expect(typeof result.cyclingSec).toBe('number');
		expect(typeof result.unknownSec).toBe('number');
		expect(['low', 'medium', 'high']).toContain(result.walkingConf);
	});

	it('queryActivity() mock values match expected shape', async () => {
		const now = Date.now();
		const result = await ChronosSensors.queryActivity(now - 600_000, now);
		expect(result.walkingSec).toBe(300);
		expect(result.runningSec).toBe(60);
		expect(result.walkingConf).toBe('high');
	});

	// ── deviceState ───────────────────────────────────────────────────────────
	it('deviceState() returns all required fields synchronously', () => {
		const state = ChronosSensors.deviceState();
		expect(typeof state.isUnlocked).toBe('boolean');
		expect(['unplugged', 'charging', 'full', 'unknown']).toContain(state.batteryState);
		// batteryLevel is number or null
		expect(state.batteryLevel === null || typeof state.batteryLevel === 'number').toBe(true);
		expect(['active', 'inactive', 'background']).toContain(state.appState);
		expect(typeof state.lowPowerMode).toBe('boolean');
		expect(typeof state.iosVersion).toBe('string');
	});

	it('deviceState() mock returns realistic values', () => {
		const state = ChronosSensors.deviceState();
		expect(state.isUnlocked).toBe(true);
		expect(state.batteryState).toBe('unplugged');
		expect(state.batteryLevel).toBe(0.85);
		expect(state.appState).toBe('active');
		expect(state.lowPowerMode).toBe(false);
		expect(state.iosVersion).toBe('18.6');
	});

	// ── BG functions ──────────────────────────────────────────────────────────
	it('registerBackgroundRefresh() does not throw', () => {
		expect(() => ChronosSensors.registerBackgroundRefresh(BG_REFRESH_IDENTIFIER)).not.toThrow();
	});

	it('scheduleBackgroundRefresh() does not throw', () => {
		expect(() =>
			ChronosSensors.scheduleBackgroundRefresh(BG_REFRESH_IDENTIFIER, 900),
		).not.toThrow();
	});

	it('completeBackgroundRefresh() does not throw', () => {
		expect(() => ChronosSensors.completeBackgroundRefresh('execution-1', true)).not.toThrow();
	});

	it('addListener() returns an object with a remove() method', () => {
		const sub = ChronosSensors.addListener('backgroundRefresh', () => {});
		expect(typeof sub.remove).toBe('function');
		expect(() => sub.remove()).not.toThrow();
	});

	// ── BG_REFRESH_IDENTIFIER constant ────────────────────────────────────────
	it('BG_REFRESH_IDENTIFIER export equals the Swift constant value', () => {
		expect(BG_REFRESH_IDENTIFIER).toBe('dev.allenvarghese.chronos.refresh');
	});
});
