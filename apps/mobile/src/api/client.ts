import createClient from 'openapi-fetch';
import { canonicalApiBase, parsePairResponse, record, type Session } from '../pairing/validation';
import type { components, paths } from './schema';

export type DeviceInfo = components['schemas']['DeviceInfo'];
export type DevicePatch = components['schemas']['DevicePatch'];
export type Transport = (
	request: Request,
	options: { redirect: 'error'; credentials: 'omit'; signal: AbortSignal },
) => Promise<Response>;

/** Only fixed, sanitized diagnostics cross into UI; never surface remote bodies. */
export class ApiError extends Error {
	constructor(
		public readonly status: number,
		message: string,
		public readonly retryAfterSec = 0,
	) {
		super(message);
		this.name = 'ApiError';
	}
}

function responseError(response: Response): ApiError {
	const messages: Record<number, string> = {
		400: 'The code is invalid or already used. Generate a new code on the laptop.',
		401: 'Pairing is no longer authorized. Pair again.',
		403: 'The laptop revoked this pairing. Pair again.',
		409: 'A device is already paired. Run chronos device revoke on the laptop before retrying.',
		410: 'The code expired. Generate a new code on the laptop.',
		429: 'Too many attempts. Wait before trying again.',
	};
	const retry = Number(response.headers.get('retry-after'));
	return new ApiError(
		response.status,
		messages[response.status] ?? 'The laptop could not complete this request. Try again later.',
		response.status === 429
			? Number.isFinite(retry) && retry > 0
				? Math.min(3600, Math.ceil(retry))
				: 60
			: 0,
	);
}

/** Bound the body before generated-client parsing, including chunked responses. */
async function boundedResponse(response: Response): Promise<Response> {
	const limit = 32 * 1024;
	if (Number(response.headers.get('content-length')) > limit)
		throw new ApiError(0, 'The laptop response was too large.');
	if (!response.body) return response;
	const reader = response.body.getReader();
	const chunks: Uint8Array[] = [];
	let size = 0;
	try {
		while (true) {
			const { done, value } = await reader.read();
			if (done) break;
			size += value.byteLength;
			if (size > limit) {
				await reader.cancel();
				throw new ApiError(0, 'The laptop response was too large.');
			}
			chunks.push(value);
		}
	} finally {
		reader.releaseLock();
	}
	const bytes = new Uint8Array(size);
	let offset = 0;
	for (const chunk of chunks) {
		bytes.set(chunk, offset);
		offset += chunk.length;
	}
	return new Response(size ? bytes : null, {
		status: response.status,
		statusText: response.statusText,
		headers: response.headers,
	});
}

/** Generated full paths are joined to origin exactly once. Transport must refuse redirects. */
export function createApi(apiBase: string, transport: Transport, token?: string) {
	const origin = new URL(canonicalApiBase(apiBase)).origin;
	const client = createClient<paths>({
		baseUrl: origin,
		fetch: async (request: Request) => {
			if (
				new URL(request.url).origin !== origin ||
				!['/v1/pair', '/v1/device/me'].includes(new URL(request.url).pathname)
			)
				throw new ApiError(0, 'The API destination is not approved.');
			const controller = new AbortController();
			const timeout = setTimeout(() => controller.abort(), 10_000);
			try {
				const headers = new Headers(request.headers);
				headers.delete('authorization');
				if (request.url.endsWith('/v1/device/me')) {
					if (!token) throw new ApiError(401, 'Pair this phone before continuing.');
					headers.set('authorization', `Bearer ${token}`);
				}
				const response = await transport(
					new Request(request, { headers, signal: controller.signal }),
					{ redirect: 'error', credentials: 'omit', signal: controller.signal },
				);
				if (response.redirected || (response.status >= 300 && response.status < 400))
					throw new ApiError(0, 'The laptop redirected the request. Check its HTTPS API URL.');
				if (!response.ok) throw responseError(response);
				return await boundedResponse(response);
			} catch (error) {
				if (error instanceof ApiError) throw error;
				throw new ApiError(
					0,
					'Cannot reach the laptop securely. Check Wi-Fi, server availability, and certificate trust.',
				);
			} finally {
				clearTimeout(timeout);
			}
		},
	});
	async function deviceResult(call: () => Promise<{ data?: unknown }>) {
		const { data } = await call();
		if (!record(data) || typeof data.device_id !== 'string' || data.status !== 'active')
			throw new ApiError(0, 'The laptop returned an invalid device record.');
		return data.device_id;
	}
	return {
		async pair(code: string, device: DeviceInfo): Promise<Session> {
			try {
				const { data } = await client.POST('/v1/pair', { body: { code, device } });
				return parsePairResponse(data, apiBase);
			} catch (error) {
				if (error instanceof ApiError) throw error;
				throw new ApiError(
					0,
					'Pairing response was invalid. Revoke the device on the laptop before retrying.',
				);
			}
		},
		getDevice: () => deviceResult(() => client.GET('/v1/device/me')),
		patchDevice: (body: DevicePatch) => deviceResult(() => client.PATCH('/v1/device/me', { body })),
		async revoke() {
			await client.DELETE('/v1/device/me');
		},
	};
}
export type Api = ReturnType<typeof createApi>;
