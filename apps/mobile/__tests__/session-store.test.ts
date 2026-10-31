import { type Api, ApiError } from '../src/api/client';
import type { Session } from '../src/pairing/validation';
import { createSessionStore, type SecureStorage } from '../src/session/store';

const session: Session = {
	version: 1,
	apiBase: 'https://laptop.local:8443/v1',
	deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
	token: 'a'.repeat(43),
};
const metadata = () => ({
	name: 'Simulator',
	model: 'iOS Simulator',
	ios_version: '18.6',
	app_version: '0.0.1',
	tz: 'Etc/UTC',
});
function deferred<T>() {
	let resolve!: (value: T) => void;
	let reject!: (reason: unknown) => void;
	const promise = new Promise<T>((yes, no) => {
		resolve = yes;
		reject = no;
	});
	return { promise, resolve, reject };
}
function fixture(initial: string | null = null) {
	let saved = initial;
	const storage: SecureStorage = {
		get: jest.fn(async () => saved),
		set: jest.fn(async (value) => {
			saved = value;
		}),
		remove: jest.fn(async () => {
			saved = null;
		}),
	};
	const api: Api = {
		debugSnapshot: jest.fn(),
		debugScores: jest.fn(),
		debugTicks: jest.fn(),
		actualWake: jest.fn(),
		telemetry: jest.fn(),
		pair: jest.fn(async () => session),
		getDevice: jest.fn(async () => session.deviceId),
		patchDevice: jest.fn(async () => session.deviceId),
		revoke: jest.fn(async () => {}),
	};
	const deps = {
		storage,
		api: jest.fn(() => api),
		metadata,
		push: jest.fn(async () => ({ token: 'ExpoPushToken[fake]' })),
		now: () => 1_000,
	};
	return { deps, api, storage, saved: () => saved, controller: createSessionStore(deps) };
}
test('persists before authenticating and restores without reentry', async () => {
	const f = fixture();
	await f.controller.hydrate();
	expect(await f.controller.pair(session.apiBase, '7K2M9QXD')).toBe(true);
	const restarted = createSessionStore(f.deps);
	await restarted.hydrate();
	expect(restarted.store.getState().session).toEqual(session);
	expect(f.api.getDevice).toHaveBeenCalled();
});
test('storage write failure never claims pairing persisted', async () => {
	const f = fixture();
	await f.controller.hydrate();
	f.storage.set = jest.fn(async () => {
		throw new Error('SECRET');
	});
	expect(await f.controller.pair(session.apiBase, '7K2M9QXD')).toBe(false);
	expect(f.controller.store.getState().phase).toBe('unpaired');
	expect(f.controller.store.getState().session).toBeNull();
	expect(f.controller.store.getState().message).toContain('Revoke');
	expect(f.controller.store.getState().message).not.toContain('SECRET');
});
test.each([401, 403])(
	'startup status %s invalidates secure storage exactly once',
	async (status) => {
		const f = fixture(JSON.stringify(session));
		f.api.getDevice = jest.fn(async () => {
			throw new ApiError(status, 'Pair again');
		});
		await f.controller.hydrate();
		expect(f.storage.remove).toHaveBeenCalledTimes(1);
		expect(f.controller.store.getState().phase).toBe('unpaired');
	},
);
test('offline GET preserves persisted session and retry succeeds', async () => {
	const f = fixture(JSON.stringify(session));
	f.api.getDevice = jest
		.fn()
		.mockRejectedValueOnce(new Error('network'))
		.mockResolvedValue(session.deviceId);
	await f.controller.hydrate();
	expect(f.controller.store.getState().phase).toBe('authenticated');
	expect(f.storage.remove).not.toHaveBeenCalled();
	await f.controller.validate();
	expect(f.controller.store.getState().message).toBe('');
});
test('bad envelope and locked Keychain are storage errors, not absence', async () => {
	for (const raw of ['bad json', JSON.stringify({ token: session.token })]) {
		const f = fixture(raw);
		await f.controller.hydrate();
		expect(f.controller.store.getState().phase).toBe('storage-error');
		expect(f.deps.api).not.toHaveBeenCalled();
	}
	const f = fixture();
	f.storage.get = async () => {
		throw new Error('locked');
	};
	await f.controller.hydrate();
	expect(f.controller.store.getState().phase).toBe('storage-error');
});
test('failed deletion blocks pairing and successful retry clears identity', async () => {
	const f = fixture(JSON.stringify(session));
	await f.controller.hydrate();
	f.storage.remove = jest
		.fn()
		.mockRejectedValueOnce(new Error('locked'))
		.mockResolvedValue(undefined);
	await f.controller.forget();
	expect(f.controller.store.getState().phase).toBe('storage-error');
	await f.controller.forget();
	expect(f.controller.store.getState().phase).toBe('unpaired');
});
test('stale GET cannot clear newly paired identity and validations deduplicate', async () => {
	const f = fixture(JSON.stringify(session));
	await f.controller.hydrate();
	const old = deferred<string>();
	f.api.getDevice = jest.fn(() => old.promise);
	const check1 = f.controller.validate();
	const check2 = f.controller.validate();
	expect(check1).toBe(check2);
	await f.controller.forget();
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	old.reject(new ApiError(403, 'revoked'));
	await check1;
	expect(f.controller.store.getState().phase).toBe('authenticated');
	expect(f.storage.remove).toHaveBeenCalledTimes(1);
});
test('forgotten in-flight pair cannot restore or persist identity', async () => {
	const f = fixture();
	await f.controller.hydrate();
	const pending = deferred<Session>();
	f.api.pair = jest.fn(() => pending.promise);
	const pairing = f.controller.pair(session.apiBase, '7K2M9QXD');
	expect(await f.controller.pair(session.apiBase, '7K2M9QXD')).toBe(false);
	await f.controller.forget();
	pending.resolve(session);
	expect(await pairing).toBe(false);
	expect(f.storage.set).not.toHaveBeenCalled();
});
test('late hydration cannot overwrite explicit forgetting', async () => {
	const f = fixture();
	const pending = deferred<string | null>();
	f.storage.get = () => pending.promise;
	const hydration = f.controller.hydrate();
	await Promise.resolve();
	const forgetting = f.controller.forget();
	pending.resolve(JSON.stringify(session));
	await Promise.all([hydration, forgetting]);
	expect(f.controller.store.getState().phase).toBe('unpaired');
	expect(f.controller.store.getState().session).toBeNull();
});
test('fixed clock enforces RetryAfter without consuming another code', async () => {
	const f = fixture();
	await f.controller.hydrate();
	f.api.pair = jest.fn(async () => {
		throw new ApiError(429, 'Wait', 12);
	});
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	expect(f.controller.store.getState().retryAt).toBe(13_000);
	expect(f.api.pair).toHaveBeenCalledTimes(1);
});
test('push unavailable and rotation are separate from valid pairing', async () => {
	const f = fixture(JSON.stringify(session));
	await f.controller.hydrate();
	const controller = createSessionStore({
		...f.deps,
		push: async () => ({ unavailable: 'Simulator' }),
	});
	await controller.hydrate();
	await controller.registerPush();
	expect(controller.store.getState().phase).toBe('authenticated');
	expect(f.api.patchDevice).not.toHaveBeenCalled();
	await f.controller.registerPush();
	expect(f.api.patchDevice).toHaveBeenCalledWith({
		expo_push_token: 'ExpoPushToken[fake]',
		tz: 'Etc/UTC',
		app_version: '0.0.1',
		ios_version: '18.6',
	});
	f.deps.push.mockResolvedValue({ token: 'ExpoPushToken[rotated]' });
	await f.controller.registerPush();
	expect(f.api.patchDevice).toHaveBeenLastCalledWith(
		expect.objectContaining({ expo_push_token: 'ExpoPushToken[rotated]' }),
	);
});
test('unconfirmed server revoke keeps session; successful revoke clears it', async () => {
	const f = fixture(JSON.stringify(session));
	await f.controller.hydrate();
	f.api.revoke = jest.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined);
	await f.controller.revoke();
	expect(f.controller.store.getState().phase).toBe('authenticated');
	await f.controller.revoke();
	expect(f.controller.store.getState().phase).toBe('unpaired');
});

test('concurrent GET and PATCH auth failures clear the same generation once', async () => {
	const f = fixture(JSON.stringify(session));
	await f.controller.hydrate();
	const get = deferred<string>();
	const patch = deferred<string>();
	f.api.getDevice = () => get.promise;
	f.api.patchDevice = () => patch.promise;
	const validation = f.controller.validate();
	const pushing = f.controller.registerPush();
	await Promise.resolve();
	await Promise.resolve();
	get.reject(new ApiError(401, 'Pair again'));
	patch.reject(new ApiError(403, 'Revoked'));
	await Promise.all([validation, pushing]);
	expect(f.storage.remove).toHaveBeenCalledTimes(1);
	expect(f.controller.store.getState().phase).toBe('unpaired');
});

test('telemetry lease expires across forget and repair even for the same device identity', async () => {
	const f = fixture();
	await f.controller.hydrate();
	expect(f.controller.lease()).toBeNull();
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	const old = f.controller.lease();
	expect(old?.identity).toBe(`${session.apiBase}|${session.deviceId}`);
	const pending = deferred<string>();
	f.api.getDevice = jest.fn(() => pending.promise);
	const flight = old?.run((api) => api.getDevice());
	await f.controller.forget();
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	pending.reject(new ApiError(403, 'revoked'));
	await expect(flight).rejects.toBeInstanceOf(ApiError);
	expect(old?.current()).toBe(false);
	expect(f.controller.store.getState().phase).toBe('authenticated');
	expect(f.saved()).toBe(JSON.stringify(session));
});
test.each([401, 403])(
	'matching lease status%s uses central Keychain invalidation',
	async (status) => {
		const f = fixture();
		await f.controller.hydrate();
		await f.controller.pair(session.apiBase, '7K2M9QXD');
		const lease = f.controller.lease();
		f.api.getDevice = jest.fn(async () => {
			throw new ApiError(status, 'Pair again.');
		});
		await expect(lease?.run((api) => api.getDevice())).rejects.toBeInstanceOf(ApiError);
		expect(lease?.current()).toBe(false);
		expect(f.controller.store.getState().session).toBeNull();
		await new Promise((resolve) => setTimeout(resolve, 0));
		expect(f.controller.store.getState().phase).toBe('unpaired');
		expect(f.storage.remove).toHaveBeenCalledTimes(1);
	},
);

test('telemetry auth invalidates immediately without waiting on native Keychain cleanup', async () => {
	const f = fixture();
	await f.controller.hydrate();
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	const lease = f.controller.lease();
	f.storage.remove = jest.fn(() => new Promise(() => {}));
	f.api.getDevice = jest.fn(async () => {
		throw new ApiError(403, 'revoked');
	});
	await expect(lease?.run((api) => api.getDevice())).rejects.toBeInstanceOf(ApiError);
	expect(f.controller.store.getState().session).toBeNull();
	expect(lease?.current()).toBe(false);
	expect(f.controller.store.getState().phase).toBe('hydrating');
});

test('telemetry invalidation still exposes a later native Keychain clear failure', async () => {
	const f = fixture();
	await f.controller.hydrate();
	await f.controller.pair(session.apiBase, '7K2M9QXD');
	f.storage.remove = jest.fn(async () => {
		throw new Error('PRIVATE native data');
	});
	f.api.getDevice = jest.fn(async () => {
		throw new ApiError(401, 'unauthorized');
	});
	await expect(f.controller.lease()?.run((api) => api.getDevice())).rejects.toBeInstanceOf(
		ApiError,
	);
	await new Promise((resolve) => setTimeout(resolve, 0));
	expect(f.controller.store.getState().phase).toBe('storage-error');
	expect(f.controller.store.getState().message).toContain('Could not clear');
	expect(f.controller.store.getState().message).not.toContain('PRIVATE');
});

test('headless local restore shares Keychain read and never validates over network', async () => {
	const f = fixture(JSON.stringify(session));
	await Promise.all([f.controller.restore(), f.controller.restore()]);
	expect(f.storage.get).toHaveBeenCalledTimes(1);
	expect(f.api.getDevice).not.toHaveBeenCalled();
	expect(f.controller.lease()?.identity).toContain(session.deviceId);
	await f.controller.hydrate();
	expect(f.api.getDevice).toHaveBeenCalledTimes(1);
});
