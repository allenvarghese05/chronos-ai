import Ajv2020 from 'ajv/dist/2020';
import addFormats from 'ajv-formats';
import { buildTelemetry, type SampleInput, windowStart } from '../src/wake/buildTelemetry';
import type { Sensors } from '../src/wake/sample';
import { sample } from '../src/wake/sample';

const openapi = require('../../../services/api/openapi.json');
const ajv = addFormats(new Ajv2020({ strict: false }));
ajv.addSchema(openapi, 'published');
const validate = ajv.compile({ $ref: 'published#/components/schemas/Signals' });
const instant = Date.parse('2026-10-28T12:00:00Z');
const state = {
	isUnlocked: true,
	batteryState: 'unknown' as const,
	batteryLevel: null,
	appState: 'active' as const,
	lowPowerMode: false,
	iosVersion: '18.6',
};
const input = (): SampleInput => ({
	instant,
	tz: 'America/New_York',
	source: 'manual',
	state,
	auth: 'authorized',
	steps: 9000.5,
	windowSteps: -1,
	activity: { walkingSec: 300, runningSec: 60, stationarySec: 4000, walkingConf: 'high' },
	appVersion: '0.0.1',
	tickSeq: 0,
});
test('pure output matches actual published JSONSchema2020 and excludes native extras', () => {
	const body = buildTelemetry(input(), 7);
	expect(validate(body)).toBe(true);
	expect(body).toMatchObject({
		seq: 7,
		steps_10m: 5000,
		steps_since_window: 0,
		walking_sec_10m: 360,
		stationary_sec_10m: 600,
		battery_level: null,
		tick_seq: 0,
	});
	expect(validate({ ...body, distanceM: 3 })).toBe(false);
	expect(validate({ ...body, tick_seq: -1 })).toBe(false);
});
test('invalid numbers are normalized, battery unknown remains null and native stub rejected', () => {
	const value = input();
	value.steps = Number.NaN;
	value.state = { ...state, batteryLevel: -1 };
	expect(buildTelemetry(value, 1)).toMatchObject({ steps_10m: 0, battery_level: null });
	expect(() => buildTelemetry({ ...value, state: { ...state, iosVersion: '0.0' } }, 1)).toThrow();
	expect(() => buildTelemetry(value, Number.MAX_SAFE_INTEGER + 1)).toThrow();
});
test('window context never guesses rules across DST, travel or stale dates', () => {
	const context = {
		windowStart: '2026-10-28T06:00:00-04:00',
		tz: 'America/New_York',
		localDate: '2026-10-28',
	};
	expect(windowStart(context, instant, context.tz)).toBe(Date.parse(context.windowStart));
	expect(windowStart(context, instant, 'Europe/London')).toBeNull();
	expect(
		windowStart({ ...context, windowStart: '2026-10-28T09:00:00-04:00' }, instant, context.tz),
	).toBeNull();
	expect(
		windowStart({ ...context, windowStart: '2026-10-28T06:00:00' }, instant, context.tz),
	).toBeNull();
	expect(windowStart({ ...context, localDate: '2026-10-27' }, instant, context.tz)).toBeNull();
	const fold = {
		windowStart: '2026-11-01T01:00:00-04:00',
		tz: context.tz,
		localDate: '2026-11-01',
	};
	expect(windowStart(fold, Date.parse('2026-11-01T01:30:00-05:00'), fold.tz)).toBe(
		Date.parse(fold.windowStart),
	);
});
const sensors = (auth = 'authorized'): Sensors => ({
	deviceState: () => state,
	pedometerAuthorization: jest.fn(async () => auth as SampleInput['auth']),
	querySteps: jest.fn(async () => ({ steps: 80, distanceM: 5 })),
	queryActivity: jest.fn(async () => ({
		walkingSec: 20,
		runningSec: 3,
		stationarySec: 5,
		automotiveSec: 0,
		cyclingSec: 0,
		unknownSec: 0,
		walkingConf: 'high' as const,
	})),
});
test.each(['denied', 'restricted', 'not_determined'])(
	'actual %s permission never triggers motion query or prompt',
	async (auth) => {
		const native = sensors(auth);
		const result = await sample(native, 'manual', () => instant, 'Etc/UTC', '0.0.1');
		expect(result.input.auth).toBe(auth);
		expect(native.querySteps).not.toHaveBeenCalled();
		expect(native.queryActivity).not.toHaveBeenCalled();
		expect(result.input.windowSteps).toBe(0);
	},
);
test('hanging sensor calls finalize at deadline and late values cannot rewrite snapshot', async () => {
	jest.useFakeTimers();
	try {
		const native = sensors();
		let resolve!: (v: { steps: number; distanceM: number }) => void;
		native.querySteps = jest.fn(
			() =>
				new Promise((yes) => {
					resolve = yes;
				}),
		);
		const pending = sample(native, 'manual', () => instant, 'Etc/UTC', '0.0.1');
		await jest.advanceTimersByTimeAsync(5000);
		const result = await pending;
		expect(result.input.steps).toBe(0);
		resolve({ steps: 333, distanceM: 3 });
		await Promise.resolve();
		expect(result.input.steps).toBe(0);
		expect(result.diagnostics).toContain('Ten-minute steps unavailable.');
	} finally {
		jest.useRealTimers();
	}
});
