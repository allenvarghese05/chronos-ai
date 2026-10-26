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
