import type { SessionController } from '../session/store';
import type { TelemetryController, TelemetryOutcome } from '../telemetry/controller';
import { type WakeSource, type WindowContext, windowStart } from './buildTelemetry';
import type { Tick } from './tick';

export type WakeDeps = {
	session: Pick<SessionController, 'restore' | 'lease'>;
	telemetry(): Pick<TelemetryController, 'sampleAndSend'>;
	metadata(): { tz: string };
	now(): number;
	elapsed(): number;
	schedule(): void;
};

/** App-wide lifecycle owner; no React mount, permission prompt, payload URL or guessed window. */
export function createWakeRuntime(deps: WakeDeps) {
	let running = false;
	let active = false;
	let foregroundIdentity: string | null = null;
	let cached: { identity: string; context: WindowContext } | undefined;
	const ticks = new Set<string>();
	async function run(source: WakeSource, tick?: Tick): Promise<TelemetryOutcome> {
		if (running) return 'skipped';
		running = true;
		const abort = new AbortController();
		const deadline = deps.elapsed() + 18_000;
		const timer = setTimeout(() => abort.abort(), 18_000);
		let tickKey: string | undefined;
		let onAbort: (() => void) | undefined;
		try {
			const operation = (async (): Promise<TelemetryOutcome> => {
				await deps.session.restore();
				if (abort.signal.aborted || deps.elapsed() >= deadline) return 'failed';
				const lease = deps.session.lease();
				if (!lease) return 'skipped';
				const { tz } = deps.metadata();
				if (
					cached &&
					(cached.identity !== lease.identity ||
						windowStart(cached.context, deps.now(), tz) === null)
				)
					cached = undefined;
				if (tick) {
					const context = { windowStart: tick.windowStart, localDate: tick.localDate, tz };
					if (windowStart(context, deps.now(), tz) === null) return 'skipped';
					tickKey = `${lease.identity}|${tick.localDate}|${tick.tickSeq}`;
					if (ticks.has(tickKey)) return 'skipped';
					cached = { identity: lease.identity, context };
				}
				const telemetry = deps.telemetry();
				if (!lease.current() || abort.signal.aborted || deps.elapsed() >= deadline) return 'failed';
				const result = await telemetry.sampleAndSend(source, cached?.context, {
					signal: abort.signal,
					deadline,
					tickSeq: tick?.tickSeq,
				});
				if (tickKey && (result === 'sent' || result === 'queued')) {
					ticks.add(tickKey);
					if (ticks.size > 64) ticks.delete(ticks.values().next().value as string);
				}
				return result;
			})();
			return await Promise.race([
				operation,
				new Promise<TelemetryOutcome>((resolve) => {
					onAbort = () => resolve('failed');
					abort.signal.addEventListener('abort', onAbort, { once: true });
				}),
			]);
		} catch {
			return 'failed';
		} finally {
			clearTimeout(timer);
			if (onAbort) abort.signal.removeEventListener('abort', onAbort);
			abort.abort();
			running = false;
			try {
				deps.schedule();
			} catch {
				/* Native scheduling status is surfaced separately. */
			}
		}
	}
	return {
		run,
		foreground(isActive: boolean) {
			const identity = deps.session.lease()?.identity ?? null;
			const shouldSample = isActive && identity && (!active || foregroundIdentity !== identity);
			active = isActive;
			foregroundIdentity = identity;
			if (shouldSample) void run('foreground');
		},
	};
}
