import { ApiError, createApi, type Transport } from '../src/api/client';
import { debugFixture, scoreFixture, tickFixture } from '../test-fixtures/debug';

const base = 'https://laptop.local:8443/v1';
const token = 'a'.repeat(43);
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
test('fixed debug and annotation paths attach bearer and preserve exact server day', async () => {
	const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>(async (request) => {
		const path = new URL(request.url).pathname;
		return json(
			path.endsWith('/scores')
				? scoreFixture()
				: path.endsWith('/ticks')
					? tickFixture()
					: debugFixture(),
		);
	});
	const api = createApi(base, transport, token);
	await api.debugSnapshot('2026-10-31');
	await api.debugScores();
	await api.debugTicks();
	await api.actualWake('2026-10-31', '07:05');
	expect(
		transport.mock.calls.map(([r]) => [
			r.method,
			new URL(r.url).pathname,
			r.headers.get('authorization'),
		]),
	).toEqual([
		['GET', '/v1/debug/wake', `Bearer ${token}`],
		['GET', '/v1/debug/scores', `Bearer ${token}`],
		['GET', '/v1/debug/ticks', `Bearer ${token}`],
		['PUT', '/v1/day/2026-10-31/actual-wake', `Bearer ${token}`],
	]);
	expect(new URL(transport.mock.calls[0][0].url).searchParams.get('local_date')).toBe('2026-10-31');
	expect(JSON.parse(await transport.mock.calls[3][0].text())).toEqual({
		actual_wake_local: '07:05',
	});
});
test('invalid dates, cursors and annotation drafts never reach transport', async () => {
	const transport = jest.fn(async () => json({}));
	const api = createApi(base, transport, token);
	await expect(api.debugSnapshot('2026-02-30')).rejects.toThrow('valid server day');
	await expect(api.debugScores('../secret')).rejects.toThrow('restart history');
	await expect(api.actualWake('2026-10-31', '7:5')).rejects.toThrow('HH:MM');
	await expect(api.actualWake('2026-10-31/../../device/me', '07:05')).rejects.toThrow('HH:MM');
	expect(transport).not.toHaveBeenCalled();
});
test.each(['signals', 'evaluation', 'extra', 'page', 'range'])(
	'rejects malformed bounded remote %s',
	async (kind) => {
		const data = debugFixture();
		if (kind === 'signals' && data.latest) data.latest.signals.steps_10m = 5001;
		if (kind === 'evaluation' && data.latest) data.latest.evaluation.score = 101;
		const raw = kind === 'extra' ? { ...data, token: 'PRIVATE' } : data;
		const page = scoreFixture();
		if (kind === 'page') page.points = Array.from({ length: 41 }, () => page.points[0]);
		if (kind === 'range') page.start = page.end;
		const api = createApi(
			base,
			async () => json(kind === 'page' || kind === 'range' ? page : raw),
			token,
		);
		await expect(
			kind === 'page' || kind === 'range' ? api.debugScores() : api.debugSnapshot(),
		).rejects.toThrow('invalid');
	},
);
test('malformed JSON, oversized responses and redirects remain sanitized', async () => {
	const api = createApi(base, async () => new Response('PRIVATE invalid JSON'), token);
	await expect(api.debugSnapshot()).rejects.toThrow('invalid diagnostics');
	await expect(api.debugSnapshot()).rejects.not.toThrow('PRIVATE');
	await expect(
		createApi(
			base,
			async () => new Response('x', { headers: { 'content-length': '32769' } }),
			token,
		).debugScores(),
	).rejects.toThrow('too large');
	await expect(
		createApi(base, async () => new Response(null, { status: 302 }), token).debugTicks(),
	).rejects.toThrow('redirected');
});
test.each([401, 403])(
	'debug auth %s rejects immediately without reading hanging body',
	async (status) => {
		const read = jest.fn();
		const api = createApi(
			base,
			async () =>
				({
					status,
					ok: false,
					headers: new Headers(),
					body: { cancel: async () => {}, getReader: () => ({ read }) },
				}) as unknown as Response,
			token,
		);
		await expect(api.debugSnapshot()).rejects.toBeInstanceOf(ApiError);
		expect(read).not.toHaveBeenCalled();
	},
);
test('debug response reads respect external cancellation', async () => {
	const aborted = new AbortController();
	const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>(
		() => new Promise(() => {}),
	);
	const pending = createApi(base, transport, token).debugSnapshot(undefined, {
		signal: aborted.signal,
		timeoutMs: 5000,
	});
	aborted.abort();
	await expect(pending).rejects.toThrow('deadline');
	expect(transport.mock.calls[0][1].signal.aborted).toBe(true);
});
