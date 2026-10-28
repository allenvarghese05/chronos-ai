import { createApi, type Transport } from '../src/api/client';
import { createSessionStore } from '../src/session/store';
import { createTelemetryController } from '../src/telemetry/controller';
import type { Outbox, Row } from '../src/telemetry/outbox';
import { RETENTION_MS } from '../src/telemetry/outbox';
import { buildTelemetry } from '../src/wake/buildTelemetry';
import type { Sample } from '../src/wake/sample';

const base = 'https://laptop.local:8443/v1';
const pairing = {
	version: 1 as const,
	apiBase: base,
	deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
	token: 'a'.repeat(43),
};
const response = (status = 200, code = '') =>
	new Response(
		JSON.stringify(
			status === 200
				? {
						evaluation_id: 7,
						local_date: '2026-10-28',
						gate: 'closed',
						score: 0,
						decision: 'wait',
						confirm_reason: null,
						threshold: {},
						consecutive_high: 0,
						rule_version: 1,
						breakdown: [],
						penalties: [],
						day_state: 'pending',
						next_tick_in_sec: null,
					}
				: { code, detail: 'PRIVATE' },
		),
		{ status, headers: { 'content-type': 'application/json', 'retry-after': '600' } },
	);
const captured = Date.parse('2026-10-28T12:00:00Z');
const sampled = (): Sample => ({
	input: {
		instant: captured,
		tz: 'Etc/UTC',
		source: 'manual',
		state: {
			isUnlocked: true,
			batteryState: 'unknown',
			batteryLevel: null,
			appState: 'active',
			lowPowerMode: false,
			iosVersion: '18.6',
		},
		auth: 'not_determined',
		steps: 0,
		windowSteps: 0,
		activity: { walkingSec: 0, runningSec: 0, stationarySec: 0, walkingConf: 'low' },
		appVersion: '0.0.1',
	},
	diagnostics: ['Window unavailable.'],
});
function fixture() {
	let now = captured;
	let rows: Row[] = [];
	const next = new Map<string, number>();
	const outbox: Outbox = {
		enqueue(identity, created_ms, build) {
			const seq = next.get(identity) ?? 1;
			next.set(identity, seq + 1);
			const row = {
				identity,
				seq,
				body: JSON.stringify(build(seq)),
				created_ms,
				retry_ms: 0,
				attempts: 0,
			};
			rows.push(row);
			return row;
		},
		head: (identity) =>
			rows.filter((r) => r.identity === identity).sort((a, b) => a.seq - b.seq)[0] ?? null,
		ack(identity, seq) {
			rows = rows.filter((r) => r.identity !== identity || r.seq !== seq);
		},
		count: (identity) => rows.filter((r) => r.identity === identity).length,
		defer(identity, seq, retry_ms, attempts) {
			const row = rows.find((r) => r.identity === identity && r.seq === seq);
			if (row) Object.assign(row, { retry_ms, attempts });
		},
		retire(identity) {
			rows = rows.filter((r) => r.identity !== identity);
		},
		prune(at) {
			const count = rows.length;
			rows = rows.filter((r) => r.created_ms >= at - RETENTION_MS && r.created_ms <= at);
			return count - rows.length;
		},
	};
	const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>(async () => response());
	const remove = jest.fn(async () => {});
	const session = createSessionStore({
		storage: { get: async () => null, set: async () => {}, remove },
		api: (s) => createApi(s.apiBase, transport, s.token),
		metadata: () => ({
			name: 'test',
			model: 'Simulator',
			ios_version: '18.6',
			app_version: '0.0.1',
			tz: 'Etc/UTC',
		}),
		push: async () => ({ unavailable: 'Simulator' }),
		now: () => now,
	});
	session.store.setState({ phase: 'authenticated', session: pairing });
	const sample = jest.fn(async () => sampled());
	const telemetry = createTelemetryController({ session, outbox, sample, now: () => now });
	return {
		session,
		outbox,
		sample,
		telemetry,
		transport,
		remove,
		rows: () => rows,
		advance: (ms: number) => {
			now += ms;
		},
	};
}
function queue(f: ReturnType<typeof fixture>, count = 2) {
	const identity = f.session.lease()?.identity as string;
	for (let n = 0; n < count; n++)
		f.outbox.enqueue(identity, captured, (seq) => buildTelemetry(sampled().input, seq));
	return identity;
}
test('transient first row prevents overtaking, retries preserve seq/time/body then FIFO ACK', async () => {
	const f = fixture();
	queue(f);
	const originals = f.rows().map((r) => r.body);
	f.transport.mockRejectedValueOnce(new Error('offline'));
	await f.telemetry.flush();
	expect(f.transport).toHaveBeenCalledTimes(1);
	expect(f.rows()).toHaveLength(2);
	expect(f.rows()[0].retry_ms).toBeGreaterThan(captured);
	await f.telemetry.flush();
	expect(f.transport).toHaveBeenCalledTimes(1);
	f.advance(3000);
	await f.telemetry.flush();
	const bodies = await Promise.all(f.transport.mock.calls.map(([r]) => r.text()));
	expect(bodies).toEqual([originals[0], originals[0], originals[1]]);
	expect(f.rows()).toHaveLength(0);
});
test('terminal clock skew/corrupt snapshot drops continue FIFO and remain observable', async () => {
	const f = fixture();
	queue(f, 3);
	f.rows()[0].body = 'corrupt';
	f.transport.mockResolvedValueOnce(response(422, 'clock_skew'));
	await f.telemetry.flush();
	expect(f.rows()).toHaveLength(0);
	expect(f.transport).toHaveBeenCalledTimes(2);
	expect(f.telemetry.status().diagnostics.join(' ')).toMatch(/invalid.*original sample time/i);
});
test('429 preserves FIFO and honors long Retry-After without sleeping in wake budget', async () => {
	const f = fixture();
	queue(f);
	f.transport.mockResolvedValueOnce(response(429, 'rate_limited'));
	await f.telemetry.flush();
	expect(f.rows()[0].retry_ms).toBe(captured + 600000);
	expect(f.rows()).toHaveLength(2);
	expect(f.telemetry.status().busy).toBe(false);
});
test.each([401, 403])(
	'status%s centrally invalidates matching session and retires its partition',
	async (status) => {
		const f = fixture();
		queue(f);
		f.transport.mockResolvedValueOnce(response(status, 'revoked'));
		await f.telemetry.flush();
		expect(f.remove).toHaveBeenCalledTimes(1);
		expect(f.session.store.getState().phase).toBe('unpaired');
		expect(f.rows()).toHaveLength(0);
	},
);
test('native sample completion after forget cannot enqueue under a new identity', async () => {
	const f = fixture();
	let resolve!: (s: Sample) => void;
	f.sample.mockImplementationOnce(
		() =>
			new Promise((yes) => {
				resolve = yes;
			}),
	);
	const pending = f.telemetry.sampleAndSend('manual');
	await f.session.forget();
	f.session.store.setState({
		phase: 'authenticated',
		session: { ...pairing, deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAW' },
	});
	resolve(sampled());
	await pending;
	expect(f.rows()).toHaveLength(0);
	expect(f.transport).not.toHaveBeenCalled();
});
test('one flush owner and stale response never ACK or overwrite new session status', async () => {
	const f = fixture();
	queue(f);
	let resolve!: (r: Response) => void;
	f.transport.mockImplementationOnce(
		() =>
			new Promise((yes) => {
				resolve = yes;
			}),
	);
	const one = f.telemetry.flush();
	const two = f.telemetry.flush();
	expect(one).toBe(two);
	await f.session.forget();
	f.session.store.setState({
		phase: 'authenticated',
		session: { ...pairing, deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAW' },
	});
	resolve(response());
	await one;
	expect(f.rows()).toHaveLength(2);
	expect(f.telemetry.status().lastResult).toBeNull();
});
test('real client aborts ignoring fetch at8s; late resolution leaves immutable row queued', async () => {
	jest.useFakeTimers();
	try {
		const f = fixture();
		queue(f, 1);
		let resolve!: (r: Response) => void;
		let signal!: AbortSignal;
		f.transport.mockImplementationOnce((_, options) => {
			signal = options.signal;
			return new Promise((yes) => {
				resolve = yes;
			});
		});
		const pending = f.telemetry.flush();
		await jest.advanceTimersByTimeAsync(8000);
		await pending;
		expect(signal.aborted).toBe(true);
		expect(f.rows()).toHaveLength(1);
		const before = JSON.stringify(f.rows());
		resolve(response());
		await Promise.resolve();
		expect(JSON.stringify(f.rows())).toBe(before);
	} finally {
		jest.useRealTimers();
	}
});
test('concurrent sample requests coalesce and enqueue before first network attempt', async () => {
	const f = fixture();
	f.transport.mockImplementationOnce(async () => {
		expect(f.rows()).toHaveLength(1);
		return response();
	});
	const one = f.telemetry.sampleAndSend('manual');
	const two = f.telemetry.sampleAndSend('manual');
	expect(one).toBe(two);
	await one;
	expect(f.sample).toHaveBeenCalledTimes(1);
	expect(f.rows()).toHaveLength(0);
});
