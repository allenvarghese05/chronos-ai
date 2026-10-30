import type {
	ActivitySummary,
	DeviceState,
	PedometerAuthStatus,
} from '../../modules/chronos-sensors/src';
import type { components } from '../api/schema';

export type Telemetry = components['schemas']['Signals'];
export type TelemetryResult = components['schemas']['TelemetryResponse'];
export type WakeSource = Telemetry['wake_source'];
export type WindowContext = { windowStart: string; tz: string; localDate: string };
export type SampleInput = {
	instant: number;
	tz: string;
	source: WakeSource;
	state: DeviceState;
	auth: PedometerAuthStatus;
	steps: number;
	windowSteps: number;
	activity: Pick<ActivitySummary, 'walkingSec' | 'runningSec' | 'stationarySec' | 'walkingConf'>;
	appVersion: string;
	tickSeq?: number | null;
};
export const integer = (value: number, max: number) =>
	Number.isFinite(value) ? Math.min(max, Math.max(0, Math.floor(value))) : 0;

/** Captured phone timezone, never a server-local clock or guessed wake-rule window. */
export function windowStart(
	context: WindowContext | undefined,
	now: number,
	tz: string,
): number | null {
	if (!context || context.tz !== tz || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(context.windowStart))
		return null;
	const parts = new Intl.DateTimeFormat('en', {
		timeZone: tz,
		year: 'numeric',
		month: '2-digit',
		day: '2-digit',
	}).formatToParts(now);
	const part = (kind: string) => parts.find((p) => p.type === kind)?.value;
	if (context.localDate !== `${part('year')}-${part('month')}-${part('day')}`) return null;
	const start = Date.parse(context.windowStart);
	return Number.isFinite(start) && start <= now && now - start <= 86_400_000 ? start : null;
}

/** Pure whitelist mapping. Sequence is allocated by the same transaction that persists this body. */
export function buildTelemetry(input: SampleInput, seq: number): Telemetry {
	if (!Number.isSafeInteger(seq) || seq < 0) throw new Error('Telemetry sequence unavailable');
	if (input.tickSeq != null && (!Number.isSafeInteger(input.tickSeq) || input.tickSeq < 0))
		throw new Error('Server tick sequence is invalid');
	if (!input.state.iosVersion || input.state.iosVersion === '0.0')
		throw new Error('Native sensors unavailable');
	new Intl.DateTimeFormat('en', { timeZone: input.tz }).format(input.instant);
	const level = input.state.batteryLevel;
	return {
		device_ts: new Date(input.instant).toISOString(),
		tz: input.tz,
		wake_source: input.source,
		seq,
		tick_seq: input.tickSeq ?? null,
		steps_10m: integer(input.steps, 5000),
		steps_since_window: integer(input.windowSteps, 5000),
		walking_sec_10m: integer(input.activity.walkingSec + input.activity.runningSec, 600),
		walking_conf: input.activity.walkingConf,
		stationary_sec_10m: integer(input.activity.stationarySec, 600),
		is_unlocked: input.state.isUnlocked,
		battery_state: input.state.batteryState,
		battery_level:
			level === null || !Number.isFinite(level) || level < 0 ? null : Math.min(1, level),
		app_state: input.state.appState,
		low_power_mode: input.state.lowPowerMode,
		pedometer_auth: input.auth,
		app_version: input.appVersion.slice(0, 20),
		ios_version: input.state.iosVersion.slice(0, 20),
	};
}
