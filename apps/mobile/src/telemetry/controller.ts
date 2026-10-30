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
export type TelemetryOutcome = 'sent' | 'queued' | 'skipped' | 'failed';
export type WakeAttempt = { signal?: AbortSignal; deadline?: number; tickSeq?: number };
export type TelemetryDeps = {
	session: Pick<SessionController, 'lease'>;
	outbox: Outbox;
	sample(source: WakeSource, context?: WindowContext, tickSeq?: number): Promise<Sample>;
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
	let sampling: Promise<TelemetryOutcome> | undefined;
	let flushing: Promise<TelemetryOutcome> | undefined;
	let lastAck: { identity: string; seq: number } | undefined;
	const publish = (update: Partial<TelemetryStatus>) => {
		status = { ...status, ...update };
		for (const listener of listeners) listener();
	};
	const note = (message: string) =>
		publish({ message, diagnostics: [...status.diagnostics.slice(-7), message] });
	function flush(attempt: WakeAttempt = {}): Promise<TelemetryOutcome> {
		if (flushing) return attempt.signal ? Promise.resolve('queued') : flushing;
		const lease = deps.session.lease();
		if (!lease) return Promise.resolve('skipped');
		const controller = new AbortController();
		const deadline = Math.min(elapsed() + 8000, attempt.deadline ?? Infinity);
		if (attempt.signal?.aborted || deadline <= elapsed()) return Promise.resolve('failed');
		const abort = () => controller.abort();
		attempt.signal?.addEventListener('abort', abort, { once: true });
		const timer = setTimeout(abort, Math.max(0, deadline - elapsed()));
		let outcome: TelemetryOutcome = 'skipped';
		publish({ busy: true });
		const flight = (async (): Promise<TelemetryOutcome> => {
			try {
				const pruned = deps.outbox.prune(deps.now());
				if (pruned) note('Expired or future-dated local snapshots dropped; check the phone clock.');
				while (lease.current() && !controller.signal.aborted && elapsed() < deadline) {
					const row = deps.outbox.head(lease.identity);
					if (!row) {
						publish({ count: 0, retryAt: 0 });
						break;
					}
					outcome = 'queued';
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
						if (!lease.current() || controller.signal.aborted || elapsed() >= deadline)
							return 'failed';
						deps.outbox.ack(lease.identity, row.seq);
						lastAck = { identity: lease.identity, seq: row.seq };
						outcome = 'sent';
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
							return 'failed';
						}
						if (!lease.current() || controller.signal.aborted || elapsed() >= deadline)
							return 'failed';
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
				outcome = 'failed';
				if (lease.current())
					publish({
						message: 'Telemetry storage unavailable. Unlock the phone or free space, then retry.',
					});
			} finally {
				clearTimeout(timer);
				attempt.signal?.removeEventListener('abort', abort);
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
			return outcome;
		})();
		flushing = flight.finally(() => {
			flushing = undefined;
		});
		return flushing;
	}
	function sampleAndSend(
		source: WakeSource,
		context?: WindowContext,
		attempt: WakeAttempt = {},
	): Promise<TelemetryOutcome> {
		if (sampling) return Promise.resolve('skipped');
		const lease = deps.session.lease();
		if (!lease) return Promise.resolve('skipped');
		const valid = () =>
			lease.current() && !attempt.signal?.aborted && elapsed() < (attempt.deadline ?? Infinity);
		if (!valid()) return Promise.resolve('failed');
		publish({ busy: true });
		const flight = (async (): Promise<TelemetryOutcome> => {
			let abort: (() => void) | undefined;
			try {
				const native = deps.sample(source, context, attempt.tickSeq);
				const sampled = attempt.signal
					? await Promise.race([
							native,
							new Promise<never>((_, reject) => {
								abort = () => reject(new Error('Wake expired'));
								attempt.signal?.addEventListener('abort', abort, { once: true });
								if (attempt.signal?.aborted) abort();
							}),
						])
					: await native;
				if (!valid()) return 'failed';
				const row = deps.outbox.enqueue(lease.identity, deps.now(), (seq) =>
					buildTelemetry(sampled.input, seq),
				);
				publish({
					lastSeq: row.seq,
					count: deps.outbox.count(lease.identity),
					diagnostics: sampled.diagnostics.slice(0, 8),
					message: 'Snapshot saved locally.',
				});
				const result = await flush(attempt);
				if (!valid() || result === 'failed') return 'failed';
				return lastAck?.identity === lease.identity && lastAck.seq === row.seq ? 'sent' : 'queued';
			} catch {
				if (lease.current())
					publish({
						message:
							'Telemetry could not be sampled or saved. Check native availability and free space.',
					});
				return 'failed';
			} finally {
				if (abort) attempt.signal?.removeEventListener('abort', abort);
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
