import { ApiError } from '../api/client';
import type { SessionController } from '../session/store';
import {
	buildTelemetry,
	type Telemetry,
	type TelemetryResult,
	type WakeSource,
	type WindowContext,
} from '../wake/buildTelemetry';
import type { Sample } from '../wake/sample';
import type { Outbox } from './outbox';

export type TelemetryStatus = {
	busy: boolean;
	count: number;
	lastSeq: number | null;
	lastResult: TelemetryResult | null;
	message: string;
	diagnostics: string[];
	retryAt: number;
};
export type TelemetryDeps = {
	session: Pick<SessionController, 'lease'>;
	outbox: Outbox;
	sample(source: WakeSource, context?: WindowContext): Promise<Sample>;
	now(): number;
	elapsed?: () => number;
};

/** One sample/flush owner; persistent immutable rows always precede network transmission. */
export function createTelemetryController(deps: TelemetryDeps) {
	const elapsed = deps.elapsed ?? (() => performance.now());
	let status: TelemetryStatus = {
		busy: false,
		count: 0,
		lastSeq: null,
		lastResult: null,
		message: 'No telemetry sent yet.',
		diagnostics: [],
		retryAt: 0,
	};
	const listeners = new Set<() => void>();
	let sampling: Promise<void> | undefined;
	let flushing: Promise<void> | undefined;
	const publish = (update: Partial<TelemetryStatus>) => {
		status = { ...status, ...update };
		for (const listener of listeners) listener();
	};
	const note = (message: string) =>
		publish({ message, diagnostics: [...status.diagnostics.slice(-7), message] });
	function flush(): Promise<void> {
		if (flushing) return flushing;
		const lease = deps.session.lease();
		if (!lease) return Promise.resolve();
		const controller = new AbortController();
		const deadline = elapsed() + 8000;
		const timer = setTimeout(() => controller.abort(), 8000);
		publish({ busy: true });
		const flight = (async () => {
			try {
				const pruned = deps.outbox.prune(deps.now());
				if (pruned) note('Expired or future-dated local snapshots dropped; check the phone clock.');
				while (lease.current() && !controller.signal.aborted && elapsed() < deadline) {
					const row = deps.outbox.head(lease.identity);
					if (!row) {
						publish({ count: 0, retryAt: 0 });
						break;
					}
					publish({ count: deps.outbox.count(lease.identity), retryAt: row.retry_ms });
					if (row.retry_ms > deps.now()) {
						publish({ message: 'Retry is scheduled; queued snapshots are preserved.' });
						break;
					}
					let body: Telemetry;
					try {
						body = JSON.parse(row.body) as Telemetry;
						if (
							!body ||
							body.seq !== row.seq ||
							typeof body.device_ts !== 'string' ||
							!Number.isFinite(Date.parse(body.device_ts))
						)
							throw new Error('corrupt');
					} catch {
						deps.outbox.ack(lease.identity, row.seq);
						note('An invalid queued snapshot was dropped.');
						continue;
					}
					const attempt = Math.min(100, row.attempts + 1);
					try {
						// Reserve retry metadata before I/O, so a timeout/crash never needs a late write.
						deps.outbox.defer(
							lease.identity,
							row.seq,
							deps.now() + Math.min(60_000, 1000 * 2 ** Math.min(attempt, 6)),
							attempt,
						);
						const result = await lease.run((api) =>
							api.telemetry(body, {
								signal: controller.signal,
								timeoutMs: Math.max(0, deadline - elapsed()),
							}),
						);
						if (!lease.current() || controller.signal.aborted || elapsed() >= deadline) return;
						deps.outbox.ack(lease.identity, row.seq);
						publish({
							lastSeq: row.seq,
							lastResult: result,
							message: 'Telemetry acknowledged by the laptop.',
							count: deps.outbox.count(lease.identity),
							retryAt: 0,
						});
					} catch (error) {
						if (error instanceof ApiError && [401, 403].includes(error.status)) {
							deps.outbox.retire(lease.identity);
							return;
						}
						if (!lease.current() || controller.signal.aborted || elapsed() >= deadline) return;
						if (
							error instanceof ApiError &&
							error.status >= 400 &&
							error.status < 500 &&
							![408, 429].includes(error.status)
						) {
							deps.outbox.ack(lease.identity, row.seq);
							note(
								error.code === 'clock_skew'
									? 'Laptop rejected the original sample time; snapshot dropped. Check the phone clock.'
									: 'Laptop rejected a queued snapshot; it was dropped.',
							);
							continue;
						}
						const delay = Math.max(
							Math.min(60_000, 1000 * 2 ** Math.min(attempt, 6)),
							error instanceof ApiError ? error.retryAfterSec * 1000 : 0,
						);
						const retryAt = deps.now() + delay;
						deps.outbox.defer(lease.identity, row.seq, retryAt, attempt);
						publish({ message: 'Laptop unavailable; snapshot remains queued for retry.', retryAt });
						break;
					}
				}
			} catch {
				if (lease.current())
					publish({
						message: 'Telemetry storage unavailable. Unlock the phone or free space, then retry.',
					});
			} finally {
				clearTimeout(timer);
				controller.abort();
				if (lease.current()) {
					try {
						publish({ busy: false, count: deps.outbox.count(lease.identity) });
					} catch {
						publish({
							busy: false,
							message: 'Telemetry storage unavailable. Retry after unlocking or freeing space.',
						});
					}
				}
			}
		})();
		flushing = flight.finally(() => {
			flushing = undefined;
		});
		return flushing;
	}
	function sampleAndSend(source: WakeSource, context?: WindowContext): Promise<void> {
		if (sampling) return sampling;
		const lease = deps.session.lease();
		if (!lease) return Promise.resolve();
		publish({ busy: true });
		const flight = (async () => {
			try {
				const sampled = await deps.sample(source, context);
				if (!lease.current()) return;
				const row = deps.outbox.enqueue(lease.identity, deps.now(), (seq) =>
					buildTelemetry(sampled.input, seq),
				);
				publish({
					lastSeq: row.seq,
					count: deps.outbox.count(lease.identity),
					diagnostics: sampled.diagnostics.slice(0, 8),
					message: 'Snapshot saved locally.',
				});
				await flush();
			} catch {
				if (lease.current())
					publish({
						message:
							'Telemetry could not be sampled or saved. Check native availability and free space.',
					});
			} finally {
				if (lease.current()) publish({ busy: false });
			}
		})();
		sampling = flight.finally(() => {
			sampling = undefined;
		});
		return sampling;
	}
	return {
		flush,
		sampleAndSend,
		status: () => status,
		subscribe(listener: () => void) {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
		refresh() {
			const lease = deps.session.lease();
			if (lease) {
				try {
					publish({ count: deps.outbox.count(lease.identity) });
				} catch {
					publish({
						message: 'Telemetry storage unavailable. Retry after unlocking or freeing space.',
					});
				}
			}
		},
	};
}
export type TelemetryController = ReturnType<typeof createTelemetryController>;
