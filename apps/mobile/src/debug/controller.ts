import type { SessionLease } from '../session/store';
import type {
	TelemetryController,
	TelemetryOutcome,
	TelemetryStatus,
} from '../telemetry/controller';
import type { DebugSnapshot, ScorePage, TickPage } from './validation';
import { wakeTime } from './validation';

export type DebugState = {
	snapshot: DebugSnapshot | null;
	scores: ScorePage | null;
	ticks: TickPage | null;
	telemetry: TelemetryStatus;
	busy: boolean;
	sending: boolean;
	saving: boolean;
	message: string;
	error: string;
	draft: string;
	loadedAt: number | null;
};
export type DebugDeps = {
	lease(): SessionLease | null;
	telemetry: TelemetryController;
	sendNow(signal: AbortSignal): Promise<TelemetryOutcome>;
	now(): number;
};
export const MAX_HISTORY = 400;

/** Focus-owned diagnostics: no sensor polling, credential cache, or second telemetry runtime. */
export function createDebugController(deps: DebugDeps) {
	const empty = (): DebugState => ({
		snapshot: null,
		scores: null,
		ticks: null,
		telemetry: deps.telemetry.status(),
		busy: false,
		sending: false,
		saving: false,
		message: '',
		error: '',
		draft: '',
		loadedAt: null,
	});
	let state = empty();
	let owner: SessionLease | null = null;
	let active = false;
	let disposed = false;
	let epoch = 0;
	let readFlight: Promise<void> | undefined;
	let sendFlight: Promise<void> | undefined;
	let saveFlight: Promise<void> | undefined;
	const requests = new Set<AbortController>();
	const listeners = new Set<() => void>();
	const publish = (update: Partial<DebugState>) => {
		state = { ...state, ...update };
		for (const listener of listeners) listener();
	};
	function cancel() {
		epoch++;
		for (const request of requests) request.abort();
		requests.clear();
		readFlight = undefined;
		sendFlight = undefined;
		saveFlight = undefined;
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
		if (!active || disposed || !lease) return null;
		const controller = new AbortController();
		requests.add(controller);
		const ticket = epoch;
		return {
			lease,
			controller,
			valid: () =>
				active && !disposed && epoch === ticket && lease.current() && !controller.signal.aborted,
			budget: () => ({ signal: controller.signal, timeoutMs: 10_000 }),
		};
	}
	const unsubscribe = deps.telemetry.subscribe(() => {
		scope();
		if (active && owner?.current()) publish({ telemetry: deps.telemetry.status() });
	});
	function refresh(): Promise<void> {
		scope();
		if (saveFlight) return saveFlight;
		if (readFlight) return readFlight;
		const run = request();
		if (!run) return Promise.resolve();
		publish({ busy: true, error: '' });
		const flight = (async () => {
			try {
				const [snapshot, scores, ticks] = await Promise.all([
					run.lease.run((api) => api.debugSnapshot(undefined, run.budget())),
					run.lease.run((api) => api.debugScores(undefined, run.budget())),
					run.lease.run((api) => api.debugTicks(undefined, run.budget())),
				]);
				if (!run.valid()) return;
				const sameDay =
					state.snapshot?.day?.local_date === snapshot.day?.local_date &&
					state.snapshot?.day?.tz === snapshot.day?.tz;
				deps.telemetry.refresh();
				publish({
					snapshot,
					scores,
					ticks,
					telemetry: deps.telemetry.status(),
					loadedAt: deps.now(),
					draft: sameDay ? state.draft : (snapshot.day?.actual_wake_local ?? ''),
					error: '',
				});
			} catch {
				if (run.valid())
					publish({
						error:
							'Diagnostics unavailable. Stored values remain dated; check the laptop connection and refresh.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ busy: false });
				run.controller.abort();
			}
		})();
		readFlight = flight.finally(() => {
			if (readFlight === tracked) readFlight = undefined;
		});
		const tracked = readFlight;
		return readFlight;
	}
	function loadMore(kind: 'scores' | 'ticks'): Promise<void> {
		scope();
		if (saveFlight) return saveFlight;
		if (readFlight) return readFlight;
		const current = state[kind];
		const cursor = current?.next_cursor;
		const run = request();
		if (!run || !current || !cursor || current.points.length >= MAX_HISTORY)
			return Promise.resolve();
		publish({ busy: true, error: '' });
		const flight = (async () => {
			try {
				if (kind === 'scores') {
					const page = await run.lease.run((api) => api.debugScores(cursor, run.budget()));
					if (!run.valid()) return;
					const previous = state.scores;
					if (!previous || page.end !== previous.end || page.start !== previous.start)
						throw new Error('History interval changed');
					const ids = new Set(previous.points.map((point) => point.event_id));
					if (page.points.some((point) => ids.has(point.event_id)))
						throw new Error('History repeated');
					publish({
						scores: { ...page, points: [...previous.points, ...page.points].slice(0, MAX_HISTORY) },
					});
				} else {
					const page = await run.lease.run((api) => api.debugTicks(cursor, run.budget()));
					if (!run.valid()) return;
					const previous = state.ticks;
					if (!previous || page.end !== previous.end || page.start !== previous.start)
						throw new Error('History interval changed');
					const ids = new Set(previous.points.map((point) => point.attempt_id));
					if (page.points.some((point) => ids.has(point.attempt_id)))
						throw new Error('History repeated');
					publish({
						ticks: { ...page, points: [...previous.points, ...page.points].slice(0, MAX_HISTORY) },
					});
				}
			} catch {
				if (run.valid())
					publish({ error: 'More history could not be loaded. Refresh to start a new interval.' });
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ busy: false });
				run.controller.abort();
			}
		})();
		readFlight = flight.finally(() => {
			if (readFlight === tracked) readFlight = undefined;
		});
		const tracked = readFlight;
		return readFlight;
	}
	function sendNow(): Promise<void> {
		scope();
		if (sendFlight) return sendFlight;
		const run = request();
		if (!run) return Promise.resolve();
		publish({ sending: true, message: '', error: '' });
		const flight = (async () => {
			try {
				const outcome = await deps.sendNow(run.controller.signal);
				if (!run.valid()) return;
				publish({
					telemetry: deps.telemetry.status(),
					message:
						outcome === 'sent'
							? 'Manual sample acknowledged.'
							: outcome === 'queued'
								? 'Manual sample queued; server data has not caught up.'
								: outcome === 'skipped'
									? 'Another wake is active; no duplicate manual sample was created.'
									: 'Manual sample was not acknowledged. Check queue and status.',
				});
				// A pre-send fetch may contain the previous evaluation. Finish it, then
				// fetch again after the sample rather than treating it as post-send evidence.
				if (saveFlight) await saveFlight;
				if (readFlight) await readFlight;
				if (run.valid()) await refresh();
			} catch {
				if (run.valid())
					publish({
						error: 'Manual sampling could not complete. Existing queued snapshots are preserved.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ sending: false });
				run.controller.abort();
			}
		})();
		sendFlight = flight.finally(() => {
			if (sendFlight === tracked) sendFlight = undefined;
		});
		const tracked = sendFlight;
		return sendFlight;
	}
	function saveActualWake(): Promise<void> {
		scope();
		if (saveFlight) return saveFlight;
		const day = state.snapshot?.day;
		const draft = state.draft;
		if (!day || !wakeTime(draft)) {
			publish({ message: 'Enter exact 24-hour HH:MM for the displayed server day.' });
			return Promise.resolve();
		}
		const run = request();
		if (!run) return Promise.resolve();
		publish({ saving: true, message: '', error: '' });
		const flight = (async () => {
			try {
				if (readFlight) await readFlight;
				if (!run.valid()) return;
				try {
					await run.lease.run((api) => api.actualWake(day.local_date, draft, run.budget()));
				} catch {
					/* A timed-out PUT may have committed. Only readback confirms it. */
				}
				if (!run.valid()) return;
				const readback = await run.lease.run((api) =>
					api.debugSnapshot(day.local_date, run.budget()),
				);
				if (!run.valid()) return;
				if (
					readback.day?.local_date !== day.local_date ||
					readback.day.tz !== day.tz ||
					readback.day.actual_wake_local !== draft
				)
					throw new Error('Not confirmed');
				publish({
					snapshot: readback,
					loadedAt: deps.now(),
					message: `Saved ${draft} for ${day.local_date} (${day.tz}); verified with the laptop.`,
				});
			} catch {
				if (run.valid())
					publish({
						error:
							'Wake time save is not confirmed. Your draft is preserved; refresh or retry to check the laptop.',
					});
			} finally {
				requests.delete(run.controller);
				if (run.valid()) publish({ saving: false });
				run.controller.abort();
			}
		})();
		saveFlight = flight.finally(() => {
			if (saveFlight === tracked) saveFlight = undefined;
		});
		const tracked = saveFlight;
		return saveFlight;
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
			if (next) {
				publish({ telemetry: deps.telemetry.status() });
				void refresh();
			} else {
				cancel();
				publish({ busy: false, sending: false, saving: false });
			}
		},
		refresh,
		loadMore,
		sendNow,
		saveActualWake,
		setDraft(value: string) {
			scope();
			publish({ draft: value.slice(0, 5), message: '' });
		},
		dispose() {
			active = false;
			disposed = true;
			cancel();
			unsubscribe();
			listeners.clear();
		},
	};
}
export type DebugController = ReturnType<typeof createDebugController>;
