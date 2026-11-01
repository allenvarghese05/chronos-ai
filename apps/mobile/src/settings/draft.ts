import type { FieldIssue, WakeRules } from './validation';
import { wakeRules } from './validation';

export type RuleField = {
	key: string;
	label: string;
	value: string;
	kind: 'integer' | 'time';
	path: (string | number)[];
};
/** Only explicit leaf controls are editable; descriptors are rebuilt from the complete baseline. */
export function ruleFields(rules: WakeRules): RuleField[] {
	const fields: RuleField[] = [];
	const add = (
		path: (string | number)[],
		label: string,
		value: unknown,
		kind: RuleField['kind'] = 'integer',
	) => {
		fields.push({ key: JSON.stringify(path), path, label, value: String(value), kind });
	};
	for (const day of ['weekday', 'weekend'] as const) {
		for (const part of ['start', 'end'] as const)
			add(['window', day, part], `${day} window ${part} (HH:MM)`, rules.window[day][part], 'time');
	}
	add(['thresholds', 'confirm'], 'Confirm threshold', rules.thresholds.confirm);
	add(['thresholds', 'strong'], 'Strong threshold', rules.thresholds.strong);
	rules.rules.forEach((rule, index) => {
		rule.points.forEach((row, rowIndex) => {
			add(
				['rules', index, 'points', rowIndex, 1],
				`${rule.id}: award at ${JSON.stringify(row[0])}`,
				row[1],
			);
		});
		add(['rules', index, 'cap'], `${rule.id}: cap`, rule.cap);
		if (rule.bonus_consecutive)
			add(
				['rules', index, 'bonus_consecutive', 'points'],
				`${rule.id}: consecutive bonus`,
				rule.bonus_consecutive.points,
			);
	});
	for (const [id, award] of Object.entries(rules.reweight_without_pedometer ?? {}))
		add(['reweight_without_pedometer', id], `${id}: without-pedometer weight`, award);
	return fields;
}
export const initialFields = (rules: WakeRules): Record<string, string> =>
	Object.fromEntries(ruleFields(rules).map((field) => [field.key, field.value]));
/** Clone the full document and apply changed known leaves only; never rebuild defaults. */
export function buildDraft(
	baseline: WakeRules,
	fields: Record<string, string>,
): { rules: WakeRules; issues: FieldIssue[] } {
	const rules: WakeRules = JSON.parse(JSON.stringify(baseline));
	const issues: FieldIssue[] = [];
	for (const field of ruleFields(baseline)) {
		const value = fields[field.key] ?? field.value;
		if (value === field.value) continue;
		let next: string | number;
		if (field.kind === 'time') {
			if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) {
				issues.push({ path: field.label, message: 'Enter exact 24-hour HH:MM.' });
				continue;
			}
			next = value;
		} else {
			if (!/^(0|[1-9]\d*)$/.test(value) || !Number.isSafeInteger(Number(value))) {
				issues.push({ path: field.label, message: 'Enter a nonnegative whole number.' });
				continue;
			}
			next = Number(value);
		}
		let target: unknown = rules;
		for (const part of field.path.slice(0, -1))
			target = (target as Record<string | number, unknown>)[part];
		(target as Record<string | number, unknown>)[field.path[field.path.length - 1]] = next;
	}
	if (!issues.length && !wakeRules(rules))
		issues.push({
			path: 'Wake rules',
			message: 'Check thresholds, award caps and related rule limits.',
		});
	return { rules, issues };
}
/** Stable structural equality ignores object key order, while retaining array order/types. */
export function sameContent(a: unknown, b: unknown): boolean {
	const canonical = (value: unknown): unknown => {
		if (Array.isArray(value)) return value.map(canonical);
		if (value !== null && typeof value === 'object')
			return Object.fromEntries(
				Object.entries(value)
					.sort(([a], [b]) => a.localeCompare(b))
					.map(([key, item]) => [key, canonical(item)]),
			);
		return value;
	};
	return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));
}
