import createClient from 'openapi-fetch';
import { canonicalApiBase, parsePairResponse, record, type Session } from '../pairing/validation';
import type { components, paths } from './schema';

export type DeviceInfo = components['schemas']['DeviceInfo'];
export type DevicePatch = components['schemas']['DevicePatch'];
export type Telemetry = components['schemas']['Signals'];
export type TelemetryResult = components['schemas']['TelemetryResponse'];
export type RequestBudget = { signal: AbortSignal; timeoutMs: number };

function telemetryResponse(value: unknown): value is TelemetryResult {
	if (!record(value)) return false;
	const fields = [
		'evaluation_id',
		'local_date',
		'gate',
		'score',
		'decision',
		'confirm_reason',
		'threshold',
		'consecutive_high',
		'rule_version',
		'breakdown',
		'penalties',
		'day_state',
		'next_tick_in_sec',
	];
	if (Object.keys(value).length !== fields.length || fields.some((key) => !(key in value)))
		return false;
	const integer = (n: unknown) => typeof n === 'number' && Number.isSafeInteger(n);
	const text = (s: unknown) => typeof s === 'string' && s.length > 0 && s.length <= 64;
	return (
		integer(value.evaluation_id) &&
		integer(value.score) &&
		integer(value.consecutive_high) &&
		integer(value.rule_version) &&
		text(value.local_date) &&
		text(value.gate) &&
		text(value.decision) &&
		text(value.day_state) &&
		(value.confirm_reason === null || text(value.confirm_reason)) &&
		(value.next_tick_in_sec === null || integer(value.next_tick_in_sec)) &&
		record(value.threshold) &&
		Object.values(value.threshold).every(integer) &&
		Array.isArray(value.breakdown) &&
		value.breakdown.every(record) &&
		Array.isArray(value.penalties) &&
		value.penalties.every(record)
	);
}
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
		public readonly code = '',
		public readonly serverTime?: string,
	) {
		super(message);
		this.name = 'ApiError';
	}
}

async function responseError(response: Response, signal: AbortSignal): Promise<ApiError> {
	const messages: Record<number, string> = {
		400: 'The code is invalid or already used. Generate a new code on the laptop.',
		401: 'Pairing is no longer authorized. Pair again.',
		403: 'The laptop revoked this pairing. Pair again.',
		409: 'A device is already paired. Run chronos device revoke on the laptop before retrying.',
		410: 'The code expired. Generate a new code on the laptop.',
		429: 'Too many attempts. Wait before trying again.',
	};
	const retry = Number(response.headers.get('retry-after'));
	let code = '';
	let serverTime: string | undefined;
	try {
		const bounded = await boundedResponse(response, signal);
		const problem: unknown = await bounded.json();
		if (record(problem)) {
			if (
				typeof problem.code === 'string' &&
				['clock_skew', 'validation', 'rate_limited', 'revoked', 'unauthorized'].includes(
					problem.code,
				)
			)
				code = problem.code;
			if (
				typeof problem.server_time === 'string' &&
				problem.server_time.length <= 40 &&
				/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(problem.server_time) &&
				Number.isFinite(Date.parse(problem.server_time))
			)
				serverTime = problem.server_time;
		}
	} catch {
		if (signal.aborted) throw new ApiError(0, 'The request deadline expired.');
	}
	return new ApiError(
		response.status,
		messages[response.status] ?? 'The laptop could not complete this request. Try again later.',
		response.status === 429
			? Number.isFinite(retry) && retry > 0
				? Math.min(3600, Math.ceil(retry))
				: 60
			: 0,
		code,
		serverTime,
	);
}

/** Abort the native operation and settle locally even if a faulty transport ignores its signal. */
async function abortable<T>(
	operation: Promise<T>,
	signal: AbortSignal,
	cancel?: () => void,
): Promise<T> {
	let aborted!: () => void;
	const failure = new Promise<never>((_, reject) => {
		aborted = () => {
			cancel?.();
			reject(new ApiError(0, 'The request deadline expired.'));
		};
		signal.addEventListener('abort', aborted, { once: true });
		if (signal.aborted) aborted();
	});
	try {
		return await Promise.race([operation, failure]);
	} finally {
		signal.removeEventListener('abort', aborted);
	}
}

/** Bound the body before generated-client parsing, including chunked responses. */
async function boundedResponse(response: Response, signal: AbortSignal): Promise<Response> {
	const limit = 32 * 1024;
	if (Number(response.headers.get('content-length')) > limit)
		throw new ApiError(0, 'The laptop response was too large.');
	if (!response.body) return response;
	const reader = response.body.getReader();
	const chunks: Uint8Array[] = [];
	let size = 0;
	try {
		while (true) {
			const { done, value } = await abortable(reader.read(), signal, () => {
				void reader.cancel().catch(() => {});
			});
			if (done) break;
			size += value.byteLength;
			if (size > limit) {
				void reader.cancel().catch(() => {});
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
	function scopedClient(budget?: RequestBudget) {
		return createClient<paths>({
			baseUrl: origin,
			fetch: async (request: Request) => {
				if (
					new URL(request.url).origin !== origin ||
					!['/v1/pair', '/v1/device/me', '/v1/telemetry'].includes(new URL(request.url).pathname)
				)
					throw new ApiError(0, 'The API destination is not approved.');
				const controller = new AbortController();
				const abort = () => controller.abort();
				budget?.signal.addEventListener('abort', abort, { once: true });
				if (budget?.signal.aborted) controller.abort();
				const duration = budget ? Math.min(10_000, Math.max(0, budget.timeoutMs)) : 10_000;
				const timeout = setTimeout(abort, Number.isFinite(duration) ? duration : 0);
				try {
					if (controller.signal.aborted || duration <= 0)
						throw new ApiError(0, 'The request deadline expired.');
					const headers = new Headers(request.headers);
					headers.delete('authorization');
					if (request.url.endsWith('/v1/device/me') || request.url.endsWith('/v1/telemetry')) {
						if (!token) throw new ApiError(401, 'Pair this phone before continuing.');
						headers.set('authorization', `Bearer ${token}`);
					}
					const response = await abortable(
						transport(new Request(request, { headers, signal: controller.signal }), {
							redirect: 'error',
							credentials: 'omit',
							signal: controller.signal,
						}),
						controller.signal,
					);
					if (response.redirected || (response.status >= 300 && response.status < 400))
						throw new ApiError(0, 'The laptop redirected the request. Check its HTTPS API URL.');
					if (!response.ok) throw await responseError(response, controller.signal);
					return await boundedResponse(response, controller.signal);
				} catch (error) {
					if (error instanceof ApiError) throw error;
					throw new ApiError(
						0,
						'Cannot reach the laptop securely. Check Wi-Fi, server availability, and certificate trust.',
					);
				} finally {
					clearTimeout(timeout);
					controller.abort();
					budget?.signal.removeEventListener('abort', abort);
				}
			},
		});
	}
	const client = scopedClient();
	async function deviceResult(call: () => Promise<{ data?: unknown }>) {
		try {
			const { data } = await call();
			if (!record(data) || typeof data.device_id !== 'string' || data.status !== 'active')
				throw new ApiError(0, 'The laptop returned an invalid device record.');
			return data.device_id;
		} catch (error) {
			if (error instanceof ApiError) throw error;
			throw new ApiError(0, 'The laptop returned an invalid device response.');
		}
	}
	return {
		async telemetry(body: Telemetry, budget: RequestBudget): Promise<TelemetryResult> {
			try {
				const { data } = await scopedClient(budget).POST('/v1/telemetry', { body });
				if (!telemetryResponse(data))
					throw new ApiError(0, 'The laptop returned an invalid telemetry response.');
				return data;
			} catch (error) {
				if (error instanceof ApiError) throw error;
				throw new ApiError(0, 'The laptop returned an invalid telemetry response.');
			}
		},
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
			try {
				await client.DELETE('/v1/device/me');
			} catch (error) {
				if (error instanceof ApiError) throw error;
				throw new ApiError(0, 'The laptop did not return a valid revocation response.');
			}
		},
	};
}
export type Api = ReturnType<typeof createApi>;
