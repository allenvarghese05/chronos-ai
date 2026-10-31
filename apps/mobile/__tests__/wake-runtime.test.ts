import { createWakeRuntime } from '../src/wake/runtime';

const now = Date.parse('2026-10-30T10:30:00Z');
const tick = { tickSeq: 3, localDate: '2026-10-30', windowStart: '2026-10-30T06:00:00-04:00' };
function fixture() {
	let identity = 'origin|device';
	const sampleAndSend = jest.fn(async () => 'sent' as const);
	const restore = jest.fn(async () => {});
	const schedule = jest.fn();
	const runtime = createWakeRuntime({
		session: {
			restore,
			lease: () =>
				identity
					? {
							identity,
							current: () => true,
							run: async () => {
								throw new Error('unused');
							},
						}
					: null,
		},
		telemetry: () => ({ sampleAndSend }),
		metadata: () => ({ tz: 'America/New_York' }),
		now: () => now,
		elapsed: () => performance.now(),
		schedule,
	});
	return {
		runtime,
		sampleAndSend,
		restore,
		schedule,
		identity: (value: string) => {
			identity = value;
		},
	};
}
test('headless run restores locally, preserves source/tick and only caches acknowledged/queued ticks', async () => {
	const f = fixture();
	expect(await f.runtime.run('silent_push', tick)).toBe('sent');
	expect(f.restore).toHaveBeenCalledTimes(1);
	expect(f.sampleAndSend).toHaveBeenCalledWith(
		'silent_push',
		{ localDate: tick.localDate, windowStart: tick.windowStart, tz: 'America/New_York' },
		expect.objectContaining({
			tickSeq: 3,
			deadline: expect.any(Number),
			signal: expect.any(Object),
		}),
	);
	expect(await f.runtime.run('silent_push', tick)).toBe('skipped');
	expect(f.sampleAndSend).toHaveBeenCalledTimes(1);
});
test('foreground samples once per actual transition/new identity, not repeated active notifications', async () => {
	const f = fixture();
	f.runtime.foreground(true);
	f.runtime.foreground(true);
	await new Promise(setImmediate);
	expect(f.sampleAndSend).toHaveBeenCalledTimes(1);
	f.runtime.foreground(false);
	f.runtime.foreground(false);
	f.runtime.foreground(true);
	await new Promise(setImmediate);
	expect(f.sampleAndSend).toHaveBeenCalledTimes(2);
	f.identity('origin|replacement');
	f.runtime.foreground(true);
	await new Promise(setImmediate);
	expect(f.sampleAndSend).toHaveBeenCalledTimes(3);
});
test('stalled secure restore expires by total deadline and late completion starts no sample', async () => {
	jest.useFakeTimers();
	try {
		const f = fixture();
		let resolve!: () => void;
		f.restore.mockImplementationOnce(
			() =>
				new Promise<void>((yes) => {
					resolve = yes;
				}),
		);
		const pending = f.runtime.run('bg_refresh');
		await jest.advanceTimersByTimeAsync(18_000);
		await expect(pending).resolves.toBe('failed');
		resolve();
		await Promise.resolve();
		await Promise.resolve();
		expect(f.sampleAndSend).not.toHaveBeenCalled();
		expect(f.schedule).toHaveBeenCalledTimes(1);
		expect(jest.getTimerCount()).toBe(0);
	} finally {
		jest.useRealTimers();
	}
});
test('overlapping source skips; unpaired and stale/future tick never sample', async () => {
	const f = fixture();
	const pending = f.runtime.run('manual');
	await expect(f.runtime.run('bg_refresh')).resolves.toBe('skipped');
	await pending;
	f.sampleAndSend.mockClear();
	await expect(f.runtime.run('silent_push', { ...tick, localDate: '2026-10-29' })).resolves.toBe(
		'skipped',
	);
	await expect(
		f.runtime.run('silent_push', { ...tick, windowStart: '2026-10-30T12:00:00Z' }),
	).resolves.toBe('skipped');
	f.identity('');
	await expect(f.runtime.run('foreground')).resolves.toBe('skipped');
	expect(f.sampleAndSend).not.toHaveBeenCalled();
});
test('cached window cannot cross a pairing identity', async () => {
	const f = fixture();
	await f.runtime.run('silent_push', tick);
	f.identity('other|device');
	await f.runtime.run('foreground');
	expect(f.sampleAndSend).toHaveBeenLastCalledWith('foreground', undefined, expect.any(Object));
});

test('native expiration aborts the bounded attempt before its total deadline', async () => {
	const f = fixture();
	let signal!: AbortSignal;
	f.sampleAndSend.mockImplementationOnce(
		(_source?: unknown, _context?: unknown, attempt?: { signal: AbortSignal }) => {
			signal = attempt?.signal as AbortSignal;
			return new Promise(() => {});
		},
	);
	const native = new AbortController();
	const pending = f.runtime.run('bg_refresh', undefined, native.signal);
	await Promise.resolve();
	await Promise.resolve();
	native.abort();
	await expect(pending).resolves.toBe('failed');
	expect(signal.aborted).toBe(true);
	await expect(f.runtime.run('bg_refresh', undefined, native.signal)).resolves.toBe('failed');
});

test('tick window written in a different device timezone after travel is rejected', async () => {
	const f = fixture();
	await expect(
		f.runtime.run('silent_push', { ...tick, windowStart: '2026-10-30T06:00:00+00:00' }),
	).resolves.toBe('skipped');
	expect(f.sampleAndSend).not.toHaveBeenCalled();
});
