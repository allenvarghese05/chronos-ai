import { ApiError } from '../api/client';
import type { SessionLease } from '../session/store';
import { buildDraft, initialFields, ruleFields, sameContent } from './draft';
import type { FieldIssue, Preferences, PreferenceValues, WakeConfig } from './validation';

export type SettingsState = {
	wake: WakeConfig | null;
	preferences: Preferences | null;
	fields: Record<string, string>;
	preferenceDraft: PreferenceValues | null;
	dirtyRules: boolean;
	dirtyPreferences: boolean;
	busy: boolean;
	saving: boolean;
	testing: boolean;
	conflict: boolean;
	issues: readonly FieldIssue[];
	error: string;
	message: string;
};
export type SettingsDeps = { lease(): SessionLease | null };
/** Focus-owned reads and explicit writes. No sensor polling, mutation queue or automatic push retry. */
export function createSettingsController(deps: SettingsDeps) {
	const empty = (): SettingsState => ({
		wake: null,
		preferences: null,
		fields: {},
		preferenceDraft: null,
		dirtyRules: false,
		dirtyPreferences: false,
		busy: false,
		saving: false,
		testing: false,
		conflict: false,
		issues: [],
		error: '',
		message: '',
	});
	let state = empty(),
		active = false,
		disposed = false,
		epoch = 0;
	let owner: SessionLease | null = null;
	let readFlight: Promise<void> | undefined,
		writeFlight: Promise<void> | undefined,
		pushFlight: Promise<void> | undefined;
	const requests = new Set<AbortController>(),
		listeners = new Set<() => void>();
	const publish = (update: Partial<SettingsState>) => {
		state = { ...state, ...update };
		for (const listener of listeners) listener();
	};
	function cancel() {
		epoch++;
		for (const request of requests) request.abort();
		requests.clear();
		readFlight = undefined;
		writeFlight = undefined;
		pushFlight = undefined;
	}
	function scope() {
		const lease = deps.lease();
		if (!owner?.current() || owner.identity !== lease?.identity) {
			cancel();
			state = empty();
			owner = lease;
		}
		return lease;
	}
	function request() {
		const lease = scope();
		if (!lease || !active || disposed) return null;
		const controller = new AbortController(),
			ticket = epoch;
		requests.add(controller);
		return {
			lease,
			controller,
			valid: () =>
				active && !disposed && epoch === ticket && lease.current() && !controller.signal.aborted,
			budget: () => ({ signal: controller.signal, timeoutMs: 10_000 }),
		};
	}
	const values = (p: Preferences): PreferenceValues => ({
		theme: p.theme,
		render_mode: p.render_mode,
		pexels_enabled: p.pexels_enabled,
	});
	function refresh(discard = false): Promise<void> {
		scope();
		if (writeFlight) return writeFlight;
		if (readFlight) return readFlight;
		const run = request();
		if (!run) return Promise.resolve();
		publish({ busy: true, error: '' });
		const flight = (async () => {
			try {
				const [wake, preferences] = await Promise.all([
					run.lease.run((api) => api.getWakeConfig(run.budget())),
					run.lease.run((api) => api.getPreferences(run.budget())),
				]);
				if (!run.valid()) return;
				const retainRules = state.dirtyRules && !discard,
					retainPreferences = state.dirtyPreferences && !discard;
				publish({
					wake: retainRules ? state.wake : wake,
					fields: retainRules ? state.fields : initialFields(wake.rules),
					preferences: retainPreferences ? state.preferences : preferences,
					preferenceDraft: retainPreferences ? state.preferenceDraft : values(preferences),
					dirtyRules: retainRules,
					dirtyPreferences: retainPreferences,
					conflict: retainRules && (state.conflict || state.wake?.sha256 !== wake.sha256),
					issues: retainRules ? state.issues : [],
					message: retainRules
						? 'Unsaved rules preserved. Reload active rules explicitly before reapplying after a conflict.'
						: state.message,
				});
			} catch {
				if (run.valid())
					publish({
						error: 'Settings unavailable. Your draft is preserved; check the laptop and refresh.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ busy: false });
				run.controller.abort();
			}
		})();
		const tracked = flight.finally(() => {
			if (readFlight === tracked) readFlight = undefined;
		});
		readFlight = tracked;
		return tracked;
	}
	function saveRules(): Promise<void> {
		scope();
		if (writeFlight) return writeFlight;
		if (pushFlight) return pushFlight;
		const baseline = state.wake;
		if (!baseline) return Promise.resolve();
		if (state.conflict) {
			publish({ error: 'Rules changed. Reload active rules, then explicitly reapply your edits.' });
			return Promise.resolve();
		}
		const draft = buildDraft(baseline.rules, state.fields);
		if (draft.issues.length) {
			publish({ issues: draft.issues, error: 'Check the highlighted fields.' });
			return Promise.resolve();
		}
		const run = request();
		if (!run) return Promise.resolve();
		publish({ saving: true, error: '', issues: [], message: '' });
		const flight = (async () => {
			try {
				if (readFlight) await readFlight;
				if (!run.valid()) return;
				let failure: unknown;
				try {
					if (!sameContent(draft.rules, baseline.rules)) {
						await run.lease.run((api) =>
							api.putWakeConfig(draft.rules, baseline.sha256, run.budget()),
						);
					}
				} catch (error) {
					failure = error;
				}
				if (!run.valid()) return;
				if (
					failure instanceof ApiError &&
					(failure.status === 409 || failure.status === 422 || failure.status === 400)
				) {
					publish({
						conflict: failure.status === 409,
						issues: failure.issues,
						error: failure.message,
					});
					return;
				}
				const actual = await run.lease.run((api) => api.getWakeConfig(run.budget()));
				if (!run.valid()) return;
				if (!sameContent(actual.rules, draft.rules)) throw new Error('Write not confirmed');
				publish({
					wake: actual,
					fields: initialFields(actual.rules),
					dirtyRules: false,
					conflict: false,
					message: `Wake rules saved as active version ${actual.version}; verified with the laptop.`,
				});
			} catch {
				if (run.valid())
					publish({
						error:
							'Rule save is not confirmed. Your draft is preserved; refresh to check the laptop.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ saving: false });
				run.controller.abort();
			}
		})();
		const tracked = flight.finally(() => {
			if (writeFlight === tracked) writeFlight = undefined;
		});
		writeFlight = tracked;
		return tracked;
	}
	function savePreferences(): Promise<void> {
		scope();
		if (writeFlight) return writeFlight;
		if (pushFlight) return pushFlight;
		const draft = state.preferenceDraft,
			baseline = state.preferences;
		if (!draft || !baseline) return Promise.resolve();
		const run = request();
		if (!run) return Promise.resolve();
		publish({ saving: true, error: '', message: '' });
		const patch: Partial<PreferenceValues> = {};
		if (draft.theme !== baseline.theme) patch.theme = draft.theme;
		if (draft.render_mode !== baseline.render_mode) patch.render_mode = draft.render_mode;
		if (draft.pexels_enabled !== baseline.pexels_enabled)
			patch.pexels_enabled = draft.pexels_enabled;
		const flight = (async () => {
			try {
				if (readFlight) await readFlight;
				if (!run.valid()) return;
				try {
					if (Object.keys(patch).length)
						await run.lease.run((api) => api.patchPreferences(patch, run.budget()));
				} catch {
					/* A lost response can follow a committed write. */
				}
				if (!run.valid()) return;
				const actual = await run.lease.run((api) => api.getPreferences(run.budget()));
				if (!run.valid()) return;
				if (
					!Object.entries(patch).every(
						([key, value]) => actual[key as keyof PreferenceValues] === value,
					)
				)
					throw new Error('Write not confirmed');
				publish({
					preferences: actual,
					preferenceDraft: values(actual),
					dirtyPreferences: false,
					message: 'Reel preferences saved and verified. Rendering integration is future work.',
				});
			} catch {
				if (run.valid())
					publish({
						error:
							'Preference save is not confirmed. Your draft is preserved; refresh to check the laptop.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ saving: false });
				run.controller.abort();
			}
		})();
		const tracked = flight.finally(() => {
			if (writeFlight === tracked) writeFlight = undefined;
		});
		writeFlight = tracked;
		return tracked;
	}
	function testPush(): Promise<void> {
		scope();
		if (pushFlight) return pushFlight;
		if (writeFlight) return writeFlight;
		const run = request();
		if (!run) return Promise.resolve();
		publish({ testing: true, error: '', message: '' });
		const flight = (async () => {
			try {
				const result = await run.lease.run((api) => api.testPush(run.budget()));
				if (!run.valid()) return;
				publish({
					message:
						result.outcome === 'accepted'
							? 'Expo accepted the test. Delivery and display are not confirmed.'
							: result.outcome === 'unavailable'
								? 'Test push unavailable: no registered/configured physical device.'
								: result.outcome === 'failed'
									? 'The test was rejected. Check notification registration on a supported physical build.'
									: 'Test outcome unknown. It may have reached Expo; no automatic retry was made.',
				});
			} catch (error) {
				if (run.valid())
					publish({
						error:
							error instanceof ApiError && error.status === 429
								? 'Test push is rate limited. Wait at least one minute before another explicit attempt.'
								: 'Test outcome unknown. Do not retry automatically; it may have reached Expo.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ testing: false });
				run.controller.abort();
			}
		})();
		const tracked = flight.finally(() => {
			if (pushFlight === tracked) pushFlight = undefined;
		});
		pushFlight = tracked;
		return tracked;
	}
	return {
		status: () => {
			scope();
			return state;
		},
		subscribe(listener: () => void) {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
		activate(next: boolean) {
			const previous = owner;
			scope();
			if (active === next && previous?.current()) return;
			active = next;
			if (next) void refresh();
			else {
				cancel();
				publish({ busy: false, saving: false, testing: false });
			}
		},
		refresh: () => refresh(false),
		reload: () => refresh(true),
		saveRules,
		savePreferences,
		testPush,
		editField(key: string, value: string) {
			scope();
			if (state.saving || !state.wake || !ruleFields(state.wake.rules).some((f) => f.key === key))
				return;
			const fields = { ...state.fields, [key]: value.slice(0, 32) };
			publish({
				fields,
				dirtyRules: !sameContent(fields, initialFields(state.wake.rules)),
				issues: [],
				message: '',
			});
		},
		editPreference<K extends keyof PreferenceValues>(key: K, value: PreferenceValues[K]) {
			scope();
			if (state.saving || !state.preferenceDraft || !state.preferences) return;
			const preferenceDraft = { ...state.preferenceDraft, [key]: value };
			publish({
				preferenceDraft,
				dirtyPreferences: !sameContent(preferenceDraft, values(state.preferences)),
				message: '',
			});
		},
		dispose() {
			active = false;
			disposed = true;
			cancel();
			listeners.clear();
		},
	};
}
export type SettingsController = ReturnType<typeof createSettingsController>;
