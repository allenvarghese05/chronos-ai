import { ApiError, createApi } from '../src/api/client';
import type { SessionLease } from '../src/session/store';
import { createSettingsController } from '../src/settings/controller';
import type { PreferencePatch, WakeRules } from '../src/settings/validation';
import { preferencesFixture, wakeConfigFixture } from '../test-fixtures/settings';

function fixture() {
	let generation = 0,
		paired = true,
		wake = wakeConfigFixture(),
		preferences = preferencesFixture();
	const api = {
		...createApi('https://laptop.local/v1', async () => new Response('{}'), 'x'.repeat(43)),
		getWakeConfig: jest.fn(async (_budget?: { signal: AbortSignal }) => structuredClone(wake)),
		putWakeConfig: jest.fn(async (rules: WakeRules, _sha: string) => {
			wake = {
				...wake,
				rules: structuredClone(rules),
				version: wake.version + 1,
				sha256: 'c'.repeat(64),
			};
			return { version: wake.version, created: true };
		}),
		getPreferences: jest.fn(async () => structuredClone(preferences)),
		patchPreferences: jest.fn(async (patch: PreferencePatch) => {
			preferences = { ...preferences, ...patch };
			return structuredClone(preferences);
		}),
		testPush: jest.fn(async () => ({
			outcome: 'unavailable' as const,
			reason: 'not_registered' as const,
		})),
	};
	const lease = (): SessionLease | null => {
		if (!paired) return null;
		const ticket = generation;
		return {
			identity: `device${generation}`,
			current: () => paired && ticket === generation,
			run: (call) => call(api),
		};
	};
	const controller = createSettingsController({ lease });
	return {
		controller,
		api,
		wake: () => wake,
		preferences: () => preferences,
		changeServer: () => {
			wake = { ...wake, version: 4, sha256: 'd'.repeat(64) };
		},
		repair: () => {
			generation++;
		},
		forget: () => {
			paired = false;
			generation++;
		},
	};
}
async function ready(f: ReturnType<typeof fixture>) {
	f.controller.activate(true);
	await f.controller.refresh();
}
const confirm = JSON.stringify(['thresholds', 'confirm']);

test('focus reads coalesce and dirty refresh never replaces the baseline', async () => {
	const f = fixture();
	f.controller.activate(true);
	const one = f.controller.refresh(),
		two = f.controller.refresh();
	expect(one).toBe(two);
	await one;
	f.controller.activate(true);
	expect(f.api.getWakeConfig).toHaveBeenCalledTimes(1);
	f.controller.editField(confirm, '71');
	f.changeServer();
	await f.controller.refresh();
	expect(f.controller.status().fields[confirm]).toBe('71');
	expect(f.controller.status().wake?.sha256).toBe('a'.repeat(64));
	expect(f.controller.status().conflict).toBe(true);
	await f.controller.saveRules();
	expect(f.api.putWakeConfig).not.toHaveBeenCalled();
	await f.controller.reload();
	expect(f.controller.status().dirtyRules).toBe(false);
	expect(f.controller.status().conflict).toBe(false);
});

test('explicit rule save preserves whole document and verifies new version', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editField(confirm, '71');
	await f.controller.saveRules();
	expect(f.api.putWakeConfig).toHaveBeenCalledTimes(1);
	expect(f.api.putWakeConfig.mock.calls[0][1]).toBe('a'.repeat(64));
	const sent = f.api.putWakeConfig.mock.calls[0][0],
		original = wakeConfigFixture().rules;
	expect(sent.rules).toEqual(original.rules);
	expect(sent.penalties).toEqual(original.penalties);
	expect(sent.window).toEqual(original.window);
	expect(sent.thresholds.confirm).toBe(71);
	expect(f.controller.status().wake?.version).toBe(4);
	expect(f.controller.status().dirtyRules).toBe(false);
	expect(f.controller.status().message).toContain('verified');
});

test('invalid input and cross-field constraints never write', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editField(confirm, 'bad');
	await f.controller.saveRules();
	expect(f.controller.status().issues.length).toBeGreaterThan(0);
	expect(f.api.putWakeConfig).not.toHaveBeenCalled();
	f.controller.editField(confirm, '90');
	await f.controller.saveRules();
	expect(f.api.putWakeConfig).not.toHaveBeenCalled();
});

test('structured server validation and conflict preserve dirty draft', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editField(confirm, '71');
	f.api.putWakeConfig.mockRejectedValueOnce(
		new ApiError(422, 'Check highlighted fields', 0, 'rules_invalid', undefined, [
			{ path: 'rules.0', message: 'Check limits' },
		]),
	);
	await f.controller.saveRules();
	expect(f.controller.status().issues[0].path).toBe('rules.0');
	expect(f.controller.status().dirtyRules).toBe(true);
	f.api.putWakeConfig.mockRejectedValueOnce(new ApiError(409, 'Reload rules', 0, 'rules_conflict'));
	await f.controller.saveRules();
	expect(f.controller.status().conflict).toBe(true);
	expect(f.controller.status().fields[confirm]).toBe('71');
});

test('rule timeout after commit is confirmed only by content readback', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editField(confirm, '71');
	const save = f.api.putWakeConfig.getMockImplementation();
	f.api.putWakeConfig.mockImplementationOnce(async (rules, sha) => {
		await save?.(rules, sha);
		throw new ApiError(0, 'lost response');
	});
	await f.controller.saveRules();
	expect(f.controller.status().dirtyRules).toBe(false);
	expect(f.controller.status().message).toContain('verified');
	f.controller.editField(confirm, '72');
	f.api.putWakeConfig.mockRejectedValueOnce(new ApiError(0, 'offline'));
	await f.controller.saveRules();
	expect(f.controller.status().dirtyRules).toBe(true);
	expect(f.controller.status().fields[confirm]).toBe('72');
	expect(f.controller.status().error).toContain('not confirmed');
});

test('preference patch sends changed fields only and reads back without claiming rendering', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editPreference('theme', 'ink');
	await f.controller.savePreferences();
	expect(f.api.patchPreferences.mock.calls[0][0]).toEqual({ theme: 'ink' });
	expect(f.controller.status().dirtyPreferences).toBe(false);
	expect(f.controller.status().message).toContain('future work');
});

test('lost preference response still confirms committed value without automatic resend', async () => {
	const f = fixture();
	await ready(f);
	f.controller.editPreference('render_mode', 'on_trigger');
	const save = f.api.patchPreferences.getMockImplementation();
	f.api.patchPreferences.mockImplementationOnce(async (patch) => {
		await save?.(patch);
		throw new ApiError(0, 'lost response');
	});
	await f.controller.savePreferences();
	expect(f.api.patchPreferences).toHaveBeenCalledTimes(1);
	expect(f.controller.status().dirtyPreferences).toBe(false);
});

test('blur aborts a read and late response cannot publish; repair clears drafts', async () => {
	const f = fixture();
	await ready(f);
	let resolve!: (v: ReturnType<typeof wakeConfigFixture>) => void;
	let signal: AbortSignal | undefined;
	f.api.getWakeConfig.mockImplementationOnce((budget) => {
		signal = budget?.signal;
		return new Promise((done) => {
			resolve = done;
		});
	});
	const pending = f.controller.refresh();
	f.controller.activate(false);
	expect(signal?.aborted).toBe(true);
	resolve({ ...wakeConfigFixture(), version: 99 });
	await pending;
	expect(f.controller.status().wake?.version).toBe(3);
	f.controller.editField(confirm, '71');
	f.repair();
	expect(f.controller.status().wake).toBe(null);
	expect(f.controller.status().fields).toEqual({});
	f.forget();
	f.controller.activate(true);
	await f.controller.refresh();
	expect(f.api.getWakeConfig).toHaveBeenCalledTimes(2);
});

test('overlapping test taps coalesce; unknown never triggers an automatic retry', async () => {
	const f = fixture();
	await ready(f);
	let resolve!: (v: { outcome: 'unavailable'; reason: 'not_registered' }) => void;
	f.api.testPush.mockImplementationOnce(
		() =>
			new Promise((done) => {
				resolve = done;
			}),
	);
	const one = f.controller.testPush(),
		two = f.controller.testPush();
	expect(one).toBe(two);
	resolve({ outcome: 'unavailable', reason: 'not_registered' });
	await one;
	expect(f.api.testPush).toHaveBeenCalledTimes(1);
	expect(f.controller.status().message).toContain('unavailable');
	f.api.testPush.mockRejectedValueOnce(new ApiError(0, 'timeout'));
	await f.controller.testPush();
	expect(f.api.testPush).toHaveBeenCalledTimes(2);
	expect(f.controller.status().error).toContain('unknown');
});

test('unchanged rules save is read-only and never creates a version', async () => {
	const f = fixture();
	await ready(f);
	await f.controller.saveRules();
	expect(f.api.putWakeConfig).not.toHaveBeenCalled();
	expect(f.controller.status().wake?.version).toBe(3);
	expect(f.controller.status().message).toContain('verified');
});
