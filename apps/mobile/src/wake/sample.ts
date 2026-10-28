import type { ChronosSensorsModuleType } from '../../modules/chronos-sensors/src';
import {
	type SampleInput,
	type WakeSource,
	type WindowContext,
	windowStart,
} from './buildTelemetry';

export type Sample = { input: SampleInput; diagnostics: string[] };
export type Sensors = Pick<
	ChronosSensorsModuleType,
	'deviceState' | 'pedometerAuthorization' | 'querySteps' | 'queryActivity'
>;

/** Settled native values after the deadline are discarded; no callback can mutate the snapshot. */
async function bounded<T>(
	operation: () => Promise<T>,
	ms: number,
	fallback: T,
	failed: () => void,
): Promise<T> {
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		return await Promise.race([
			Promise.resolve().then(operation),
			new Promise<T>((resolve) => {
				timer = setTimeout(
					() => {
						failed();
						resolve(fallback);
					},
					Math.max(0, ms),
				);
			}),
		]);
	} catch {
		failed();
		return fallback;
	} finally {
		clearTimeout(timer);
	}
}

/** Read actual permission before motion queries; never request permission while sampling. */
export async function sample(
	sensors: Sensors,
	source: WakeSource,
	now: () => number,
	tz: string,
	appVersion: string,
	context?: WindowContext,
	tickSeq?: number | null,
): Promise<Sample> {
	const instant = now();
	const started = performance.now();
	const remaining = () => Math.max(0, 5000 - (performance.now() - started));
	const state = sensors.deviceState();
	if (state.iosVersion === '0.0') throw new Error('Native sensors unavailable');
	const diagnostics: string[] = [];
	const auth = await bounded(
		() => sensors.pedometerAuthorization(),
		remaining(),
		'not_determined',
		() => diagnostics.push('Motion authorization unavailable.'),
	);
	const start = windowStart(context, instant, tz);
	if (start === null) diagnostics.push('Window unavailable; window steps are zero.');
	const emptyActivity = {
		walkingSec: 0,
		runningSec: 0,
		stationarySec: 0,
		walkingConf: 'low' as const,
	};
	let steps = 0;
	let windowSteps = 0;
	let activity: SampleInput['activity'] = emptyActivity;
	if (auth === 'authorized' && remaining() > 0) {
		const values = await Promise.all([
			bounded(
				() => sensors.querySteps(instant - 600_000, instant),
				remaining(),
				{ steps: 0, distanceM: 0 },
				() => diagnostics.push('Ten-minute steps unavailable.'),
			),
			start === null
				? Promise.resolve({ steps: 0 })
				: bounded(
						() => sensors.querySteps(start, instant),
						remaining(),
						{ steps: 0, distanceM: 0 },
						() => diagnostics.push('Window steps unavailable.'),
					),
			bounded(
				() => sensors.queryActivity(instant - 600_000, instant),
				remaining(),
				{ ...emptyActivity, automotiveSec: 0, cyclingSec: 0, unknownSec: 0 },
				() => diagnostics.push('Motion activity unavailable.'),
			),
		]);
		steps = values[0].steps;
		windowSteps = values[1].steps;
		activity = values[2];
		if (
			[steps, windowSteps].some(
				(n) => !Number.isFinite(n) || !Number.isInteger(n) || n < 0 || n > 5000,
			)
		)
			diagnostics.push('Invalid or out-of-range step readings were normalized.');
		if (
			[activity.walkingSec, activity.runningSec, activity.stationarySec].some(
				(n) => !Number.isFinite(n) || n < 0 || n > 600,
			)
		)
			diagnostics.push('Invalid or out-of-range activity readings were normalized.');
	} else diagnostics.push('Motion queries skipped without authorization.');
	return {
		input: { instant, tz, source, state, auth, steps, windowSteps, activity, appVersion, tickSeq },
		diagnostics,
	};
}
