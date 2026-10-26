/** Values crossing the QR, HTTP and Keychain trust boundaries. */
export type Session = { version: 1; apiBase: string; deviceId: string; token: string };

function fail(message: string): never {
	throw new Error(message);
}
export function record(value: unknown): value is Record<string, unknown> {
	return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Accept only a canonical HTTPS API root, with no authority/path ambiguity. */
export function canonicalApiBase(raw: string): string {
	if (
		raw.length > 512 ||
		/[\\\s]/.test(raw) ||
		Array.from(raw).some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127)
	)
		fail('Enter the HTTPS API URL printed by your laptop.');
	let url: URL;
	try {
		url = new URL(raw);
	} catch {
		return fail('Enter a valid HTTPS API URL.');
	}
	if (
		url.protocol !== 'https:' ||
		url.username ||
		url.password ||
		url.search ||
		url.hash ||
		!['/v1', '/v1/'].includes(url.pathname) ||
		!url.hostname ||
		['0.0.0.0', '[::]', '::'].includes(url.hostname) ||
		raw.includes('%')
	) {
		fail('Use the HTTPS URL ending in /v1, without credentials, query, or fragment.');
	}
	return `${url.origin}/v1`;
}

/** Match the server's actual alphabet, normalizing only spaces and dashes. */
export function normaliseCode(raw: string): string {
	if (raw.length > 32) fail('Enter the eight-character pairing code.');
	const code = raw.toUpperCase().replace(/[\s-]/g, '');
	if (!/^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$/.test(code))
		fail('Enter the eight-character pairing code printed by the laptop.');
	return code;
}

/** QR scanning only populates a form; callers must still confirm its destination. */
export function parsePairingQr(raw: string): { apiBase: string; code: string } {
	if (raw.length > 2048) fail('This is not a Chronos pairing QR.');
	let value: unknown;
	try {
		value = JSON.parse(raw);
	} catch {
		return fail('Scan the Chronos pairing QR, not the Expo development QR.');
	}
	if (
		!record(value) ||
		Object.keys(value).sort().join(',') !== 'api_base,code' ||
		typeof value.api_base !== 'string' ||
		typeof value.code !== 'string'
	)
		fail('This is not a Chronos pairing QR.');
	return { apiBase: canonicalApiBase(value.api_base), code: normaliseCode(value.code) };
}

/** Reject incomplete, old or unbound credentials before making any request. */
export function parseSession(value: unknown): Session {
	if (
		!record(value) ||
		value.version !== 1 ||
		typeof value.apiBase !== 'string' ||
		typeof value.deviceId !== 'string' ||
		!/^[0-9A-HJKMNP-TV-Z]{26}$/.test(value.deviceId) ||
		typeof value.token !== 'string' ||
		!/^[A-Za-z0-9_-]{43}$/.test(value.token) ||
		Object.keys(value).sort().join(',') !== 'apiBase,deviceId,token,version'
	)
		fail('Stored pairing is invalid. Forget it and pair again.');
	return {
		version: 1,
		apiBase: canonicalApiBase(value.apiBase),
		deviceId: value.deviceId,
		token: value.token,
	};
}

/** Bind the server's one-time credential to the host the owner explicitly approved. */
export function parsePairResponse(value: unknown, approved: string): Session {
	if (
		!record(value) ||
		typeof value.api_base !== 'string' ||
		canonicalApiBase(value.api_base) !== canonicalApiBase(approved) ||
		typeof value.server_time !== 'string' ||
		!Number.isFinite(Date.parse(value.server_time))
	)
		fail(
			'Pairing returned an invalid or different server. Revoke the device on the laptop before retrying.',
		);
	return parseSession({
		version: 1,
		apiBase: value.api_base,
		deviceId: value.device_id,
		token: value.token,
	});
}
