import { createApi } from '../src/api/client';
import { createDebugController, MAX_HISTORY } from '../src/debug/controller';
import type { DebugSnapshot } from '../src/debug/validation';
import type { SessionLease } from '../src/session/store';
import type { TelemetryController, TelemetryOutcome } from '../src/telemetry/controller';
import { debugFixture, scoreFixture, telemetryFixture, tickFixture } from '../test-fixtures/debug';

function fixture() {
	let generation = 0;
	let paired = true;
	let snapshot = debugFixture();
	let status = telemetryFixture();
	const listeners = new Set<() => void>();
	const telemetry: TelemetryController = {
		status: () => status,
		subscribe: (listener) => {
			listeners.add(listener);
			return () => {
				listeners.delete(listener);
			};
		},
		refresh: jest.fn(),
		flush: jest.fn(),
		sampleAndSend: jest.fn(),
	};
	const api = {
		...createApi('https://laptop.local/v1', async () => new Response('{}'), 'a'.repeat(43)),
		debugSnapshot: jest.fn(async (_date?: string, _budget?: { signal: AbortSignal }) =>
			structuredClone(snapshot),
		),
		debugScores: jest.fn(async (_cursor?: string) => scoreFixture()),
		debugTicks: jest.fn(async (_cursor?: string) => tickFixture()),
		actualWake: jest.fn(async (_date: string, hhmm: string) => {
			if (snapshot.day) snapshot.day.actual_wake_local = hhmm;
		}),
	};
	const lease = (): SessionLease | null => {
		if (!paired) return null;
		const ticket = generation;
		return {
			identity: `device:${ticket}`,
			current: () => paired && generation === ticket,
			run: (call) => call(api),
		};
	};
	const sendNow = jest.fn<Promise<TelemetryOutcome>, [AbortSignal]>(async () => 'sent');
	const controller = createDebugController({
		lease,
		telemetry,
		sendNow,
		now: () => Date.parse('2026-11-01T08:00:00Z'),
	});
	return {
		controller,
		api,
		telemetry,
		sendNow,
		snapshot: () => snapshot,
		replaceSnapshot: (next: DebugSnapshot) => {
			snapshot = next;
		},
		emit: (update: Partial<typeof status>) => {
			status = { ...status, ...update };
			for (const listener of listeners) listener();
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

test('focus fetch coalesces and queue changes do not refetch or sample', async () => {
	const f = fixture();
	f.controller.activate(true);
	const one = f.controller.refresh();
	const two = f.controller.refresh();
	expect(one).toBe(two);
	await one;
	expect(f.api.debugSnapshot).toHaveBeenCalledTimes(1);
	f.controller.activate(true);
	f.emit({ count: 2 });
	expect(f.controller.status().telemetry.count).toBe(2);
	expect(f.api.debugSnapshot).toHaveBeenCalledTimes(1);
	expect(f.sendNow).not.toHaveBeenCalled();
	f.controller.dispose();
});
test('blur aborts all active reads; late result cannot replace dated state', async () => {
	const f = fixture();
	let resolve!: (value: DebugSnapshot) => void;
	f.api.debugSnapshot.mockImplementationOnce(
		() =>
			new Promise((done) => {
				resolve = done;
			}),
	);
	f.controller.activate(true);
	const pending = f.controller.refresh();
	const signal = f.api.debugSnapshot.mock.calls[0][1]?.signal;
	f.controller.activate(false);
	expect(signal?.aborted).toBe(true);
	resolve(debugFixture());
	await pending;
	expect(f.controller.status().snapshot).toBeNull();
	await ready(f);
	expect(f.api.debugSnapshot).toHaveBeenCalledTimes(2);
	expect(f.controller.status().snapshot?.latest?.signals.seq).toBe(7);
	f.controller.dispose();
});
test('changed lease clears data/draft and ignores old reads and annotation completion', async () => {
	const f = fixture();
	await ready(f);
	f.controller.setDraft('07:05');
	let resolve!: () => void;
	f.api.actualWake.mockImplementationOnce(
		() =>
			new Promise((done) => {
				resolve = done;
			}),
	);
	const saving = f.controller.saveActualWake();
	f.repair();
	expect(f.controller.status().snapshot).toBeNull();
	expect(f.controller.status().draft).toBe('');
	resolve();
	await saving;
	expect(f.controller.status().message).toBe('');
	expect(f.api.debugSnapshot).toHaveBeenCalledTimes(1);
	f.controller.dispose();
});
test('send now uses shared operation once, then refreshes acknowledged values', async () => {
	const f = fixture();
	await ready(f);
	f.sendNow.mockImplementationOnce(async () => {
		const next = debugFixture();
		if (next.latest) next.latest.signals.seq = 8;
		f.replaceSnapshot(next);
		f.emit({ count: 0, lastSeq: 8 });
		return 'sent';
	});
	const one = f.controller.sendNow();
	const two = f.controller.sendNow();
	expect(one).toBe(two);
	await one;
	expect(f.sendNow).toHaveBeenCalledTimes(1);
	expect(f.controller.status().snapshot?.latest?.signals.seq).toBe(8);
	expect(f.controller.status().message).toContain('acknowledged');
	f.controller.dispose();
});
test('send now waits out an older in-flight fetch and performs a fresh post-send read', async () => {
	const f = fixture();
	let resolve!: (value: DebugSnapshot) => void;
	f.api.debugSnapshot.mockImplementationOnce(
		() =>
			new Promise((done) => {
				resolve = done;
			}),
	);
	f.controller.activate(true);
	const sending = f.controller.sendNow();
	const next = debugFixture();
	if (next.latest) next.latest.signals.seq = 8;
	f.replaceSnapshot(next);
	resolve(debugFixture());
	await sending;
	expect(f.api.debugSnapshot).toHaveBeenCalledTimes(2);
	expect(f.controller.status().snapshot?.latest?.signals.seq).toBe(8);
	f.controller.dispose();
});
test('offline manual attempt preserves dated server state and exposes queued status', async () => {
	const f = fixture();
	await ready(f);
	f.api.debugSnapshot.mockRejectedValueOnce(new Error('PRIVATE'));
	f.sendNow.mockImplementationOnce(async () => {
		f.emit({ count: 1 });
		return 'queued';
	});
	await f.controller.sendNow();
	expect(f.controller.status().snapshot?.latest?.signals.seq).toBe(7);
	expect(f.controller.status().telemetry.count).toBe(1);
	expect(f.controller.status().message).toContain('queued');
	expect(f.controller.status().error).not.toContain('PRIVATE');
	f.controller.dispose();
});
test('annotation validates exact HH:MM, saves explicit server day and verifies readback', async () => {
	const f = fixture();
	await ready(f);
	f.controller.setDraft('7:5');
	await f.controller.saveActualWake();
	expect(f.api.actualWake).not.toHaveBeenCalled();
	f.controller.setDraft('07:05');
	await f.controller.saveActualWake();
	expect(f.api.actualWake.mock.calls[0].slice(0, 2)).toEqual(['2026-10-31', '07:05']);
	expect(f.api.debugSnapshot.mock.calls[1][0]).toBe('2026-10-31');
	expect(f.controller.status().snapshot?.day?.actual_wake_local).toBe('07:05');
	expect(f.controller.status().message).toContain('verified');
	f.controller.dispose();
});
test('timeout after committed annotation succeeds only on readback; failed readback keeps draft', async () => {
	const f = fixture();
	await ready(f);
	f.api.actualWake.mockImplementationOnce(async (_day, hhmm) => {
		const day = f.snapshot().day;
		if (day) day.actual_wake_local = hhmm;
		throw new Error('lost response');
	});
	f.controller.setDraft('08:10');
	await f.controller.saveActualWake();
	expect(f.controller.status().message).toContain('Saved 08:10');
	f.api.actualWake.mockRejectedValueOnce(new Error('offline'));
	f.api.debugSnapshot.mockRejectedValueOnce(new Error('offline'));
	f.controller.setDraft('08:20');
	await f.controller.saveActualWake();
	expect(f.controller.status().error).toContain('not confirmed');
	expect(f.controller.status().draft).toBe('08:20');
	expect(f.controller.status().snapshot?.day?.actual_wake_local).toBe('08:10');
	f.controller.dispose();
});
test('paging keeps frozen intervals, rejects duplicate rows and bounds loaded history', async () => {
	const f = fixture();
	f.api.debugScores.mockImplementation(async (cursor) => {
		const page = scoreFixture();
		const offset = cursor ? Number(cursor) : 0;
		page.points = Array.from({ length: 40 }, (_, n) => ({
			...page.points[0],
			event_id: 1000 - offset - n,
		}));
		page.next_cursor = String(offset + 40);
		return page;
	});
	await ready(f);
	for (let i = 0; i < 12; i++) await f.controller.loadMore('scores');
	expect(f.controller.status().scores?.points).toHaveLength(MAX_HISTORY);
	expect(f.controller.status().scores?.next_cursor).not.toBeNull();
	expect(f.api.debugScores).toHaveBeenCalledTimes(10);
	f.controller.dispose();
});
test('wrong history interval and duplicate pages remain explicit errors', async () => {
	const f = fixture();
	f.api.debugScores.mockResolvedValueOnce({ ...scoreFixture(), next_cursor: 'more' });
	await ready(f);
	f.api.debugScores.mockResolvedValueOnce({ ...scoreFixture(), end: '2026-11-01T09:00:00Z' });
	await f.controller.loadMore('scores');
	expect(f.controller.status().error).toContain('new interval');
	expect(f.controller.status().scores?.points).toHaveLength(1);
	f.api.debugScores.mockResolvedValueOnce(scoreFixture());
	await f.controller.loadMore('scores');
	expect(f.controller.status().error).toContain('new interval');
	expect(f.controller.status().scores?.points).toHaveLength(1);
	f.controller.dispose();
});
test('unpaired or disposed controller never reads or sends', async () => {
	const f = fixture();
	f.forget();
	await ready(f);
	await f.controller.sendNow();
	expect(f.api.debugSnapshot).not.toHaveBeenCalled();
	f.controller.dispose();
	await f.controller.refresh();
	expect(f.sendNow).not.toHaveBeenCalled();
});
