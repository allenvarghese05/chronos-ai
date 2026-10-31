import type { components } from '../api/schema';
import { record } from '../pairing/validation';

export type DebugSnapshot = components['schemas']['DebugSnapshot'];
export type ScorePage = components['schemas']['ScorePage'];
export type TickPage = components['schemas']['TickPage'];
export type ScorePoint = components['schemas']['ScorePoint'];
export type TickPoint = components['schemas']['TickPoint'];

const integer = (v: unknown, min = 0, max = Number.MAX_SAFE_INTEGER) =>
	typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max;
const text = (v: unknown, max = 128): v is string =>
	typeof v === 'string' && v.length > 0 && v.length <= max;
const choice = (v: unknown, values: readonly string[]) =>
	typeof v === 'string' && values.includes(v);
const keys = (v: Record<string, unknown>, required: string[], optional: string[] = []) =>
	required.every((key) => key in v) &&
	Object.keys(v).every((key) => [...required, ...optional].includes(key));
export const instant = (v: unknown): v is string =>
	typeof v === 'string' &&
	v.length <= 40 &&
	/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(v) &&
	Number.isFinite(Date.parse(v));
export const calendarDate = (v: unknown): v is string =>
	typeof v === 'string' &&
	/^\d{4}-\d{2}-\d{2}$/.test(v) &&
	Number.isFinite(Date.parse(`${v}T00:00:00Z`)) &&
	new Date(`${v}T00:00:00Z`).toISOString().slice(0, 10) === v;
export const wakeTime = (v: unknown): v is string =>
	typeof v === 'string' && /^([01]\d|2[0-3]):[0-5]\d$/.test(v);
const source = (v: unknown) => choice(v, ['silent_push', 'bg_refresh', 'foreground', 'manual']);
const decision = (v: unknown) =>
	choice(v, ['outside_window', 'low', 'high', 'confirm', 'ignored_dedupe', 'already_confirmed']);

function signals(v: unknown): boolean {
	if (
		!record(v) ||
		!keys(
			v,
			[
				'device_ts',
				'tz',
				'wake_source',
				'steps_10m',
				'steps_since_window',
				'walking_sec_10m',
				'walking_conf',
				'stationary_sec_10m',
				'is_unlocked',
				'battery_state',
				'app_state',
				'low_power_mode',
				'pedometer_auth',
				'seq',
				'app_version',
				'ios_version',
			],
			['battery_level', 'tick_seq'],
		)
	)
		return false;
	return (
		instant(v.device_ts) &&
		text(v.tz) &&
		source(v.wake_source) &&
		integer(v.steps_10m, 0, 5000) &&
		integer(v.steps_since_window, 0, 5000) &&
		integer(v.walking_sec_10m, 0, 600) &&
		integer(v.stationary_sec_10m, 0, 600) &&
		choice(v.walking_conf, ['low', 'medium', 'high']) &&
		typeof v.is_unlocked === 'boolean' &&
		choice(v.battery_state, ['unknown', 'unplugged', 'charging', 'full']) &&
		(v.battery_level === undefined ||
			v.battery_level === null ||
			(typeof v.battery_level === 'number' &&
				Number.isFinite(v.battery_level) &&
				v.battery_level >= 0 &&
				v.battery_level <= 1)) &&
		choice(v.app_state, ['active', 'inactive', 'background']) &&
		typeof v.low_power_mode === 'boolean' &&
		choice(v.pedometer_auth, ['authorized', 'denied', 'restricted', 'not_determined']) &&
		integer(v.seq) &&
		(v.tick_seq === undefined || v.tick_seq === null || integer(v.tick_seq)) &&
		text(v.app_version, 64) &&
		text(v.ios_version, 64)
	);
}

function evaluation(v: unknown): boolean {
	if (
		!record(v) ||
		!keys(
			v,
			[
				'gate',
				'score',
				'decision',
				'consecutive_high',
				'thresholds',
				'rule_version',
				'breakdown',
				'penalties',
				'derived',
			],
			['confirm_reason', 'notes'],
		)
	)
		return false;
	if (
		!choice(v.gate, ['open', 'closed']) ||
		!integer(v.score, 0, 100) ||
		!decision(v.decision) ||
		!integer(v.consecutive_high) ||
		!integer(v.rule_version, 1)
	)
		return false;
	if (
		v.confirm_reason !== undefined &&
		v.confirm_reason !== null &&
		!choice(v.confirm_reason, ['strong', 'consecutive', 'both'])
	)
		return false;
	if (
		!record(v.thresholds) ||
		!keys(v.thresholds, ['confirm', 'strong', 'confirm_max_gap_min']) ||
		!Object.values(v.thresholds).every((n) => integer(n, 0, 1000))
	)
		return false;
	if (
		!record(v.derived) ||
		!keys(v.derived, [
			'charger_disconnected',
			'consecutive_unlocked_ticks',
			'minutes_since_window_start',
			'ticks_received_today',
			'pedometer_unavailable',
		]) ||
		typeof v.derived.charger_disconnected !== 'boolean' ||
		typeof v.derived.pedometer_unavailable !== 'boolean' ||
		!integer(v.derived.consecutive_unlocked_ticks) ||
		!integer(v.derived.minutes_since_window_start) ||
		!integer(v.derived.ticks_received_today)
	)
		return false;
	const note = (n: unknown) => n === undefined || n === null || text(n, 512);
	if (
		!Array.isArray(v.breakdown) ||
		v.breakdown.length > 64 ||
		!v.breakdown.every(
			(row: unknown) =>
				record(row) &&
				keys(row, ['rule_id', 'input', 'points', 'cap'], ['note']) &&
				text(row.rule_id) &&
				integer(row.points, 0, 10000) &&
				integer(row.cap, 0, 10000) &&
				note(row.note) &&
				(typeof row.input === 'boolean' ||
					text(row.input) ||
					(typeof row.input === 'number' && Number.isFinite(row.input))),
		)
	)
		return false;
	if (
		!Array.isArray(v.penalties) ||
		v.penalties.length > 64 ||
		!v.penalties.every(
			(row: unknown) =>
				record(row) &&
				keys(row, ['rule_id', 'points'], ['note']) &&
				text(row.rule_id) &&
				integer(row.points, -10000, 0) &&
				note(row.note),
		)
	)
		return false;
	return (
		v.notes === undefined ||
		(Array.isArray(v.notes) && v.notes.length <= 32 && v.notes.every((n: unknown) => text(n, 512)))
	);
}

/** Remote debug data is validated before display; unknown keys never become a record dump. */
export function debugSnapshot(v: unknown): v is DebugSnapshot {
	if (
		!record(v) ||
		!keys(v, ['server_time', 'server_version', 'active_rule_version', 'day', 'latest']) ||
		!instant(v.server_time) ||
		!text(v.server_version, 64) ||
		!integer(v.active_rule_version, 1)
	)
		return false;
	if (
		v.day !== null &&
		(!record(v.day) ||
			!keys(v.day, ['local_date', 'tz', 'state', 'actual_wake_local']) ||
			!calendarDate(v.day.local_date) ||
			!text(v.day.tz) ||
			!choice(v.day.state, ['idle', 'monitoring', 'confirmed', 'fallback', 'delivered']) ||
			!(v.day.actual_wake_local === null || wakeTime(v.day.actual_wake_local)))
	)
		return false;
	return (
		v.latest === null ||
		(record(v.latest) &&
			keys(v.latest, [
				'event_id',
				'evaluation_id',
				'received_at',
				'evaluated_at',
				'signals',
				'evaluation',
			]) &&
			integer(v.latest.event_id, 1) &&
			integer(v.latest.evaluation_id, 1) &&
			instant(v.latest.received_at) &&
			instant(v.latest.evaluated_at) &&
			signals(v.latest.signals) &&
			evaluation(v.latest.evaluation))
	);
}

function page(
	v: unknown,
): v is Record<string, unknown> & { start: string; end: string; points: unknown[] } {
	return (
		record(v) &&
		keys(v, ['start', 'end', 'next_cursor', 'points']) &&
		instant(v.start) &&
		instant(v.end) &&
		Date.parse(v.end) - Date.parse(v.start) === 86_400_000 &&
		(v.next_cursor === null || (text(v.next_cursor, 512) && /^[\w-]+$/.test(v.next_cursor))) &&
		Array.isArray(v.points) &&
		v.points.length <= 40
	);
}
function chronological(v: { start: string; end: string; points: unknown[] }): boolean {
	let previous = Date.parse(v.end);
	for (const row of v.points) {
		if (!record(row) || !instant(row.at)) return false;
		const at = Date.parse(row.at);
		if (at < Date.parse(v.start) || at > previous) return false;
		previous = at;
	}
	return true;
}
export function scorePage(v: unknown): v is ScorePage {
	return (
		page(v) &&
		chronological(v) &&
		v.points.every(
			(row: unknown) =>
				record(row) &&
				keys(row, [
					'event_id',
					'evaluation_id',
					'at',
					'seq',
					'score',
					'decision',
					'wake_source',
					'local_date',
				]) &&
				integer(row.event_id, 1) &&
				integer(row.evaluation_id, 1) &&
				integer(row.seq) &&
				integer(row.score, 0, 100) &&
				decision(row.decision) &&
				source(row.wake_source) &&
				calendarDate(row.local_date),
		)
	);
}
export function tickPage(v: unknown): v is TickPage {
	return (
		page(v) &&
		chronological(v) &&
		v.points.every(
			(row: unknown) =>
				record(row) &&
				keys(row, ['attempt_id', 'at', 'local_date', 'tick_seq', 'status']) &&
				integer(row.attempt_id, 1) &&
				(row.tick_seq === null || integer(row.tick_seq)) &&
				calendarDate(row.local_date) &&
				choice(row.status, ['received', 'awaiting', 'overdue', 'send_failed']),
		)
	);
}
