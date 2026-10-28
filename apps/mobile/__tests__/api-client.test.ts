import { ApiError, createApi, type Transport } from '../src/api/client';

const base = 'https://laptop.local:8443/v1';
const id = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
const token = 'a'.repeat(43);
const info = {
	name: 'Simulator',
	model: 'iOS Simulator',
	ios_version: '18.6',
	app_version: '0.0.1',
	tz: 'Etc/UTC',
};
const response = (data: unknown, status = 200, headers = {}) =>
	new Response(status === 204 ? null : JSON.stringify(data), {
		status,
		headers: { 'content-type': 'application/json', ...headers },
	});
test('typed pair uses exactly /v1 once with no bearer and explicit native redirect refusal', async () => {
	const transport = jest
		.fn<ReturnType<Transport>, Parameters<Transport>>()
		.mockResolvedValue(
			response({ api_base: base, device_id: id, token, server_time: '2026-10-26T12:00:00Z' }, 201),
		);
	await createApi(base, transport, token).pair('7K2M9QXD', info);
	const [request, options] = transport.mock.calls[0];
	expect(request.url).toBe(`${base}/pair`);
	expect(request.headers.get('authorization')).toBeNull();
	expect(options.redirect).toBe('error');
	expect(options.credentials).toBe('omit');
	expect(JSON.parse(await request.text())).toEqual({ code: '7K2M9QXD', device: info });
});
test('GET PATCH DELETE use only approved bearer endpoint', async () => {
	const transport = jest
		.fn<ReturnType<Transport>, Parameters<Transport>>()
		.mockImplementation(async () => response({ device_id: id, status: 'active' }));
	const api = createApi(base, transport, token);
	expect(await api.getDevice()).toBe(id);
	await api.patchDevice({ tz: 'Etc/UTC' });
	transport.mockResolvedValueOnce(response(null, 204));
	await api.revoke();
	expect(
		transport.mock.calls.map(([r]) => [r.method, r.url, r.headers.get('authorization')]),
	).toEqual(
		['GET', 'PATCH', 'DELETE'].map((method) => [method, `${base}/device/me`, `Bearer ${token}`]),
	);
});
test.each([400, 401, 403, 409, 410, 429, 500])(
	'sanitizes status %s and bounded retry header',
	async (status) => {
		const api = createApi(
			base,
			async () => response({ detail: 'SECRET remote error' }, status, { 'retry-after': '12' }),
			token,
		);
		try {
			await api.getDevice();
			throw new Error('expected failure');
		} catch (error) {
			expect(error).toBeInstanceOf(ApiError);
			expect(String(error)).not.toContain('SECRET');
			expect((error as ApiError).status).toBe(status);
			if (status === 429) expect((error as ApiError).retryAfterSec).toBe(12);
		}
	},
);
test('rejects redirect and oversized bodies before parsing', async () => {
	await expect(createApi(base, async () => response({}, 302), token).getDevice()).rejects.toThrow(
		'redirected',
	);
	await expect(
		createApi(
			base,
			async () => response({}, 200, { 'content-length': '999999' }),
			token,
		).getDevice(),
	).rejects.toThrow('too large');
});
test('malformed or host-substituted pairing never saves a credential', async () => {
	await expect(
		createApi(base, async () => new Response('<html>secret</html>', { status: 201 })).pair(
			'7K2M9QXD',
			info,
		),
	).rejects.toThrow('invalid');
	await expect(
		createApi(base, async () =>
			response(
				{ api_base: 'https://other/v1', token, device_id: id, server_time: '2026-10-26T12:00:00Z' },
				201,
			),
		).pair('7K2M9QXD', info),
	).rejects.toThrow('invalid');
});
test('aborts stalled transport within10seconds without leaking error', async () => {
	jest.useFakeTimers();
	const result = createApi(
		base,
		(_request, options) =>
			new Promise((_resolve, reject) =>
				options.signal.addEventListener('abort', () => reject(new Error('SECRET'))),
			),
		token,
	).getDevice();
	jest.advanceTimersByTime(10_001);
	await expect(result).rejects.toThrow('Cannot reach');
	jest.useRealTimers();
});

test('chunked response is cancelled at the size bound without parsing', async () => {
	const reader = {
		read: jest.fn(async () => ({ done: false, value: new Uint8Array(32769) })),
		cancel: jest.fn(async () => {}),
		releaseLock: jest.fn(),
	};
	const reply = {
		status: 200,
		ok: true,
		headers: new Headers(),
		body: { getReader: () => reader },
	} as unknown as Response;
	await expect(createApi(base, async () => reply, token).getDevice()).rejects.toThrow('too large');
	expect(reader.cancel).toHaveBeenCalledTimes(1);
	expect(reader.releaseLock).toHaveBeenCalledTimes(1);
});

test('authenticated malformed JSON errors never expose remote body', async () => {
	const api = createApi(
		base,
		async () =>
			new Response('SECRET', { status: 200, headers: { 'content-type': 'application/json' } }),
		token,
	);
	await expect(api.getDevice()).rejects.toThrow('invalid device response');
	await expect(api.revoke()).rejects.toThrow('valid revocation response');
});

test('telemetry uses approved authenticated endpoint and preserves serialized sequence', async () => {
	const result = {
		evaluation_id: 7,
		local_date: '2026-10-28',
		score: 0,
		breakdown: [],
		penalties: [],
	};
	const transport = jest.fn<ReturnType<Transport>, Parameters<Transport>>(async () =>
		response(result),
	);
	const api = createApi(base, transport, token);
	const body = { seq: 7, device_ts: '2026-10-28T12:00:00Z' } as Parameters<typeof api.telemetry>[0];
	await api.telemetry(body, { signal: new AbortController().signal, timeoutMs: 1000 });
	const [request, options] = transport.mock.calls[0];
	expect(request.url).toBe(`${base}/telemetry`);
	expect(request.headers.get('authorization')).toBe(`Bearer ${token}`);
	expect(await request.json()).toEqual(body);
	expect(options.redirect).toBe('error');
});
test('telemetry deadline aborts an ignoring transport with no late success', async () => {
	jest.useFakeTimers();
	try {
		let signal!: AbortSignal;
		const transport: Transport = async (_, options) => {
			signal = options.signal;
			return new Promise(() => {});
		};
		const api = createApi(base, transport, token);
		const pending = api.telemetry({} as Parameters<typeof api.telemetry>[0], {
			signal: new AbortController().signal,
			timeoutMs: 50,
		});
		const rejected = expect(pending).rejects.toMatchObject({ status: 0 });
		await jest.advanceTimersByTimeAsync(50);
		await rejected;
		expect(signal.aborted).toBe(true);
	} finally {
		jest.useRealTimers();
	}
});
test('external cancellation interrupts hanging response reads', async () => {
	const reader = {
		read: jest.fn(() => new Promise(() => {})),
		cancel: jest.fn(async () => {}),
		releaseLock: jest.fn(),
	};
	const reply = {
		status: 200,
		ok: true,
		headers: new Headers(),
		body: { getReader: () => reader },
	} as unknown as Response;
	const api = createApi(base, async () => reply, token);
	const controller = new AbortController();
	const pending = api.telemetry({} as Parameters<typeof api.telemetry>[0], {
		signal: controller.signal,
		timeoutMs: 1000,
	});
	await new Promise((resolve) => setTimeout(resolve, 5));
	controller.abort();
	await expect(pending).rejects.toMatchObject({ status: 0 });
	expect(reader.read).toHaveBeenCalled();
	expect(reader.cancel).toHaveBeenCalledTimes(1);
});
test('bounded problem metadata is allowlisted and remote detail never surfaces', async () => {
	const api = createApi(
		base,
		async () =>
			response(
				{ code: 'clock_skew', server_time: '2026-10-28T12:00:00Z', detail: 'PRIVATE remote data' },
				422,
			),
		token,
	);
	try {
		await api.telemetry({} as Parameters<typeof api.telemetry>[0], {
			signal: new AbortController().signal,
			timeoutMs: 1000,
		});
		throw new Error('expected failure');
	} catch (error) {
		expect(error).toMatchObject({
			status: 422,
			code: 'clock_skew',
			serverTime: '2026-10-28T12:00:00Z',
		});
		expect(String(error)).not.toContain('PRIVATE');
	}
});
