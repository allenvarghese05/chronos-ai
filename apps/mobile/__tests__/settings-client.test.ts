import { ApiError, createApi, type Transport } from '../src/api/client';
import { buildDraft, initialFields } from '../src/settings/draft';
import { fieldIssues, preferences, wakeConfig, wakeRules } from '../src/settings/validation';
import { preferencesFixture, wakeConfigFixture } from '../test-fixtures/settings';

const apiFor = (transport: Transport) =>
	createApi('https://laptop.local/v1', transport, 'x'.repeat(43));
const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

test('typed fixed settings routes send bearer and quoted precondition', async () => {
	const requests: Request[] = [];
	const api = apiFor(async (request, options) => {
		requests.push(request);
		expect(options.redirect).toBe('error');
		expect(options.credentials).toBe('omit');
		expect(request.headers.get('authorization')).toBe(`Bearer ${'x'.repeat(43)}`);
		const path = new URL(request.url).pathname;
		if (path === '/v1/config/wake')
			return request.method === 'PUT'
				? json({ version: 4, created: true }, 201)
				: json(wakeConfigFixture());
		if (path === '/v1/config') return json(preferencesFixture());
		return json({ outcome: 'unavailable', reason: 'not_registered' });
	});
	await api.getWakeConfig();
	await api.putWakeConfig(wakeConfigFixture().rules, 'a'.repeat(64));
	await api.getPreferences();
	await api.patchPreferences({ theme: 'ink' });
	await api.testPush();
	expect(requests.map((r) => [r.method, new URL(r.url).pathname])).toEqual([
		['GET', '/v1/config/wake'],
		['PUT', '/v1/config/wake'],
		['GET', '/v1/config'],
		['PATCH', '/v1/config'],
		['POST', '/v1/admin/push-test'],
	]);
	expect(requests[1].headers.get('if-match')).toBe(`"${'a'.repeat(64)}"`);
	expect(await requests[3].json()).toEqual({ theme: 'ink' });
});

test('rules errors recognized by type without code and prose never rendered', async () => {
	const api = apiFor(async () =>
		json(
			{
				type: 'urn:chronos:error:rules_invalid',
				detail: 'secret-remote-body',
				errors: [
					{ loc: ['rules', 0, 'points', 0, 1], msg: 'secret-remote-body', type: 'value_error' },
					{ loc: ['secret-remote-body'], msg: 'secret-remote-body' },
				],
			},
			422,
		),
	);
	try {
		await api.putWakeConfig(wakeConfigFixture().rules, 'a'.repeat(64));
		throw new Error('expected failure');
	} catch (error) {
		expect(error).toBeInstanceOf(ApiError);
		const e = error as ApiError;
		expect(e.code).toBe('rules_invalid');
		expect(e.issues[0].path).toBe('rules.0.points.0.1');
		expect(JSON.stringify(e)).not.toContain('secret-remote-body');
	}
});

test('conflict error is settings-specific, not pairing guidance', async () => {
	const api = apiFor(async () => json({ code: 'rules_conflict' }, 409));
	await expect(api.putWakeConfig(wakeConfigFixture().rules, 'a'.repeat(64))).rejects.toMatchObject({
		status: 409,
		code: 'rules_conflict',
		message: expect.stringContaining('Reload'),
	});
});

test('settings responses and patches are bounded and validated', async () => {
	const config = wakeConfigFixture();
	expect(wakeConfig(config)).toBe(true);
	expect(preferences(preferencesFixture())).toBe(true);
	const bad = structuredClone(config);
	bad.rules.rules[0].points[0][1] = '20';
	expect(wakeConfig(bad)).toBe(false);
	const huge = apiFor(async () => new Response('x'.repeat(33000)));
	await expect(huge.getWakeConfig()).rejects.toBeInstanceOf(ApiError);
	const invalid = apiFor(async () => json({ ...preferencesFixture(), secret: 'never' }));
	await expect(invalid.getPreferences()).rejects.toBeInstanceOf(ApiError);
	await expect(invalid.patchPreferences({ theme: 'invalid' as 'auto' })).rejects.toBeInstanceOf(
		ApiError,
	);
	expect(
		fieldIssues(Array.from({ length: 100 }, () => ({ loc: ['rules', 0], msg: 'untrusted' })))
			.length,
	).toBe(1);
});

test('settings fetch deadline settles even if native transport ignores cancellation', async () => {
	const api = apiFor(() => new Promise(() => {}));
	await expect(
		api.getPreferences({ signal: new AbortController().signal, timeoutMs: 5 }),
	).rejects.toMatchObject({ status: 0 });
});

test('threshold-only draft preserves custom rules, penalties, holidays, bool/enum/numeric types', () => {
	const baseline = wakeConfigFixture().rules,
		fields = initialFields(baseline);
	fields[JSON.stringify(['thresholds', 'confirm'])] = '71';
	const draft = buildDraft(baseline, fields);
	expect(draft.issues).toEqual([]);
	expect(draft.rules.thresholds.confirm).toBe(71);
	const expected = structuredClone(baseline);
	expected.thresholds.confirm = 71;
	expect(draft.rules).toEqual(expected);
	expect(draft.rules.rules[0].points[0][0]).toBe(35.5);
	expect(draft.rules.rules[1].points[0][0]).toBe(true);
	expect(baseline.thresholds.confirm).toBe(70);
	fields[JSON.stringify(['thresholds', 'confirm'])] = '71.5';
	expect(buildDraft(baseline, fields).issues.length).toBeGreaterThan(0);
});

test('raw award validation leaves numeric threshold support intact', () => {
	const rules = wakeConfigFixture().rules;
	expect(wakeRules(rules)).toBe(true);
	const invalid = structuredClone(rules);
	invalid.rules[0].points[0][1] = true;
	expect(wakeRules(invalid)).toBe(false);
});
