import type { components } from '../api/schema';
import { instant } from '../debug/validation';
import { record } from '../pairing/validation';

export type WakeConfig = components['schemas']['ConfigWakeResponse'];
export type WakeRules = components['schemas']['WakeRules'];
export type RuleSaved = components['schemas']['ConfigWakeSaved'];
export type Preferences = components['schemas']['ReelPreferences'];
export type PreferenceValues = Pick<Preferences, 'theme' | 'render_mode' | 'pexels_enabled'>;
export type PreferencePatch = Partial<PreferenceValues>;
export type PushResult = components['schemas']['PushTestResult'];
export type FieldIssue = { path: string; message: string };
export const sha = (v: unknown): v is string => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const integer = (v: unknown, min = 0, max = Number.MAX_SAFE_INTEGER): v is number =>
	typeof v === 'number' && Number.isSafeInteger(v) && v >= min && v <= max;
const text = (v: unknown, max = 128): v is string => typeof v === 'string' && v.length <= max;
const keys = (v: Record<string, unknown>, expected: string[], optional: string[] = []) =>
	expected.every((k) => k in v) &&
	Object.keys(v).every((k) => [...expected, ...optional].includes(k));
const choice = (v: unknown, values: readonly string[]) =>
	typeof v === 'string' && values.includes(v);
const time = (v: unknown) =>
	typeof v === 'string' && /^([01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?$/.test(v);
function strings(v: unknown, max = 128): boolean {
	return Array.isArray(v) && v.length <= max && v.every((item) => text(item));
}
function stringMap(v: unknown): boolean {
	return (
		record(v) &&
		Object.keys(v).length <= 32 &&
		Object.entries(v).every(([key, value]) => text(key) && text(value))
	);
}
/** Validate the complete fetched document; unsupported/oversized documents fail without replacement. */
export function wakeRules(v: unknown): v is WakeRules {
	if (
		!record(v) ||
		!keys(
			v,
			['version', 'window', 'thresholds', 'rules'],
			['penalties', 'reweight_without_pedometer'],
		)
	)
		return false;
	const w = v.window,
		t = v.thresholds;
	if (
		!integer(v.version) ||
		!record(w) ||
		!keys(
			w,
			['weekday', 'weekend'],
			['day_boundary', 'holidays_as_weekend', 'tick_interval_min'],
		) ||
		!stringMap(w.weekday) ||
		!stringMap(w.weekend) ||
		!record(w.weekday) ||
		!record(w.weekend) ||
		!time(w.weekday.start) ||
		!time(w.weekday.end) ||
		!time(w.weekend.start) ||
		!time(w.weekend.end) ||
		(w.day_boundary !== undefined && !time(w.day_boundary)) ||
		(w.tick_interval_min !== undefined && !integer(w.tick_interval_min, 2, 30)) ||
		(w.holidays_as_weekend !== undefined && !strings(w.holidays_as_weekend)) ||
		!record(t) ||
		!keys(t, ['confirm', 'strong', 'dedupe_min'], ['confirm_max_gap_min']) ||
		!integer(t.confirm, 1, 100) ||
		!integer(t.strong, t.confirm, 100) ||
		!integer(t.dedupe_min, 1, 60) ||
		(t.confirm_max_gap_min !== undefined && !integer(t.confirm_max_gap_min, 2, 60))
	)
		return false;
	if (!Array.isArray(v.rules) || v.rules.length < 1 || v.rules.length > 128) return false;
	const ids = new Set<string>();
	for (const r of v.rules) {
		if (
			!record(r) ||
			!keys(r, ['id', 'signal', 'points', 'cap'], ['requires', 'bonus_consecutive']) ||
			!text(r.id, 64) ||
			!r.id ||
			ids.has(r.id) ||
			!text(r.signal, 64) ||
			!r.signal ||
			!integer(r.cap) ||
			!Array.isArray(r.points) ||
			r.points.length > 64
		)
			return false;
		ids.add(r.id);
		for (const row of r.points) {
			if (
				!Array.isArray(row) ||
				row.length !== 2 ||
				!integer(row[1], 0, r.cap) ||
				!(
					typeof row[0] === 'boolean' ||
					text(row[0]) ||
					(typeof row[0] === 'number' && Number.isFinite(row[0]))
				)
			)
				return false;
		}
		if (
			r.requires !== undefined &&
			r.requires !== null &&
			(!record(r.requires) ||
				Object.keys(r.requires).length > 32 ||
				!Object.entries(r.requires).every(([k, x]) => text(k) && strings(x, 32)))
		)
			return false;
		if (
			r.bonus_consecutive !== undefined &&
			r.bonus_consecutive !== null &&
			(!record(r.bonus_consecutive) ||
				!keys(r.bonus_consecutive, ['ticks', 'points']) ||
				!integer(r.bonus_consecutive.ticks, 2) ||
				!integer(r.bonus_consecutive.points))
		)
			return false;
	}
	if (
		v.penalties !== undefined &&
		(!Array.isArray(v.penalties) ||
			v.penalties.length > 128 ||
			!v.penalties.every(
				(p) =>
					record(p) &&
					keys(p, ['id', 'when', 'points']) &&
					text(p.id, 64) &&
					integer(p.points, Number.MIN_SAFE_INTEGER, 0) &&
					record(p.when) &&
					Object.keys(p.when).length <= 32 &&
					Object.entries(p.when).every(([k, x]) => text(k) && (text(x) || strings(x, 32))),
			))
	)
		return false;
	if (
		v.reweight_without_pedometer !== undefined &&
		(!record(v.reweight_without_pedometer) ||
			Object.keys(v.reweight_without_pedometer).length > 128 ||
			!Object.entries(v.reweight_without_pedometer).every(
				([k, x]) => ids.has(k) && integer(x, 0, 100),
			))
	)
		return false;
	return true;
}
export function wakeConfig(v: unknown): v is WakeConfig {
	return (
		record(v) &&
		keys(v, [
			'version',
			'source',
			'rules',
			'sha256',
			'yaml_sha256',
			'yaml_stale',
			'created_at',
			'created_by',
		]) &&
		integer(v.version, 1) &&
		choice(v.source, ['api', 'yaml']) &&
		wakeRules(v.rules) &&
		sha(v.sha256) &&
		(v.yaml_sha256 === '' || sha(v.yaml_sha256)) &&
		typeof v.yaml_stale === 'boolean' &&
		instant(v.created_at) &&
		text(v.created_by)
	);
}
export function savedRules(v: unknown): v is RuleSaved {
	return (
		record(v) &&
		keys(v, ['version', 'created']) &&
		integer(v.version, 1) &&
		typeof v.created === 'boolean'
	);
}
export function preferences(v: unknown): v is Preferences {
	return (
		record(v) &&
		keys(v, [
			'theme',
			'render_mode',
			'pexels_enabled',
			'pexels_effective',
			'pexels_key_configured',
			'origins',
			'rendering_available',
		]) &&
		choice(v.theme, ['auto', 'dawn', 'mono', 'ink']) &&
		choice(v.render_mode, ['eager', 'on_trigger']) &&
		typeof v.pexels_enabled === 'boolean' &&
		typeof v.pexels_key_configured === 'boolean' &&
		typeof v.pexels_effective === 'boolean' &&
		v.pexels_effective === (v.pexels_enabled && v.pexels_key_configured) &&
		v.rendering_available === false &&
		record(v.origins) &&
		keys(v.origins, ['theme', 'render_mode', 'pexels_enabled']) &&
		Object.values(v.origins).every((x) => choice(x, ['env', 'db', 'default']))
	);
}
export function preferencePatch(v: unknown): v is PreferencePatch {
	return (
		record(v) &&
		Object.keys(v).every((k) => ['theme', 'render_mode', 'pexels_enabled'].includes(k)) &&
		(v.theme === undefined || choice(v.theme, ['auto', 'dawn', 'mono', 'ink'])) &&
		(v.render_mode === undefined || choice(v.render_mode, ['eager', 'on_trigger'])) &&
		(v.pexels_enabled === undefined || typeof v.pexels_enabled === 'boolean')
	);
}
export function pushResult(v: unknown): v is PushResult {
	if (!record(v) || !keys(v, ['outcome', 'reason'])) return false;
	return (
		(v.outcome === 'accepted' && v.reason === 'ticket_accepted') ||
		(v.outcome === 'failed' && v.reason === 'provider_rejected') ||
		(v.outcome === 'unavailable' &&
			['not_registered', 'not_configured'].includes(String(v.reason))) ||
		(v.outcome === 'unknown' && v.reason === 'uncertain')
	);
}
/** Ignore all provider/error prose; project only bounded known field paths to fixed guidance. */
export function fieldIssues(v: unknown): FieldIssue[] {
	if (!Array.isArray(v)) return [];
	const allowed = new Set([
		'body',
		'window',
		'weekday',
		'weekend',
		'start',
		'end',
		'thresholds',
		'confirm',
		'strong',
		'rules',
		'points',
		'cap',
		'bonus_consecutive',
		'ticks',
		'reweight_without_pedometer',
	]);
	const found: FieldIssue[] = [];
	for (const issue of v.slice(0, 20)) {
		if (!record(issue) || !Array.isArray(issue.loc) || issue.loc.length > 8) continue;
		const path: string[] = [];
		for (const item of issue.loc) {
			if (typeof item === 'string' && allowed.has(item)) path.push(item);
			else if (integer(item, 0, 127)) path.push(String(item));
			else {
				path.length = 0;
				break;
			}
		}
		const name = path.length ? path.join('.') : 'Wake rules';
		if (!found.some((x) => x.path === name))
			found.push({ path: name, message: 'Check this field and its related rule limits.' });
	}
	return found;
}
