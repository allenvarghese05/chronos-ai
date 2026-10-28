import { createStore } from 'zustand/vanilla';
import { type Api, ApiError, type DeviceInfo } from '../api/client';
import { canonicalApiBase, normaliseCode, parseSession, type Session } from '../pairing/validation';

export type SecureStorage = {
	get(): Promise<string | null>;
	set(value: string): Promise<void>;
	remove(): Promise<void>;
};
export type PushResult = { token: string } | { unavailable: string };
export type SessionLease = {
	identity: string;
	current(): boolean;
	run<T>(operation: (api: Api) => Promise<T>): Promise<T>;
};
export type SessionDeps = {
	storage: SecureStorage;
	api(session: Pick<Session, 'apiBase'> & Partial<Pick<Session, 'token'>>): Api;
	metadata(): DeviceInfo;
	push(): Promise<PushResult>;
	now(): number;
};
export type SessionState = {
	phase: 'hydrating' | 'unpaired' | 'authenticated' | 'storage-error';
	session: Session | null;
	busy: boolean;
	checking: boolean;
	message: string;
	retryAt: number;
	pushStatus: string;
};

/** In-memory Zustand state; secrets persist only through the injected secure adapter. */
export function createSessionStore(deps: SessionDeps) {
	const store = createStore<SessionState>(() => ({
		phase: 'hydrating',
		session: null,
		busy: false,
		checking: false,
		message: '',
		retryAt: 0,
		pushStatus: 'Notifications not configured.',
	}));
	let generation = 0;
	let storageQueue: Promise<unknown> = Promise.resolve();
	let hydration: Promise<void> | undefined;
	let validation: { generation: number; promise: Promise<void> } | undefined;
	let pushFlight: Promise<void> | undefined;
	function storage<T>(operation: () => Promise<T>): Promise<T> {
		const next = storageQueue.then(operation, operation);
		storageQueue = next.catch(() => {});
		return next;
	}
	const current = (ticket: number) => generation === ticket;
	async function clear(message: string, expected = generation) {
		if (!current(expected)) return;
		const ticket = ++generation;
		store.setState({
			session: null,
			phase: 'hydrating',
			busy: true,
			checking: false,
			message,
			pushStatus: 'Notifications not configured.',
		});
		try {
			await storage(() => deps.storage.remove());
			if (current(ticket)) store.setState({ phase: 'unpaired', busy: false });
		} catch {
			if (current(ticket))
				store.setState({
					phase: 'storage-error',
					busy: false,
					message:
						'Could not clear the Keychain session. Retry forgetting it before pairing again.',
				});
		}
	}
	async function authenticated<T>(
		operation: (api: Api) => Promise<T>,
		ticket: number,
		session: Session,
		waitForStorage = true,
	): Promise<T> {
		try {
			return await operation(deps.api(session));
		} catch (error) {
			if (error instanceof ApiError && [401, 403].includes(error.status)) {
				const clearing = clear(error.message, ticket);
				if (waitForStorage) await clearing;
			}
			throw error;
		}
	}
	function validate(): Promise<void> {
		const session = store.getState().session;
		const ticket = generation;
		if (!session) return Promise.resolve();
		if (validation?.generation === ticket) return validation.promise;
		store.setState({ checking: true });
		const promise = (async () => {
			try {
				const id = await authenticated((api) => api.getDevice(), ticket, session);
				if (id !== session.deviceId) throw new Error('Device identity mismatch');
				if (current(ticket)) store.setState({ message: '' });
			} catch {
				if (current(ticket))
					store.setState({
						message:
							'Pairing is saved, but the laptop could not be verified. Check Wi-Fi and certificate trust, then retry.',
					});
			} finally {
				if (current(ticket)) store.setState({ checking: false });
				if (validation?.generation === ticket) validation = undefined;
			}
		})();
		validation = { generation: ticket, promise };
		return promise;
	}
	function hydrate(): Promise<void> {
		if (hydration) return hydration;
		const ticket = generation;
		hydration = (async () => {
			try {
				const raw = await storage(() => deps.storage.get());
				const session = raw === null ? null : parseSession(JSON.parse(raw));
				if (!current(ticket)) return;
				store.setState({ session, phase: session ? 'authenticated' : 'unpaired' });
				if (session) await validate();
			} catch {
				if (current(ticket))
					store.setState({
						phase: 'storage-error',
						message:
							'Could not read a valid Keychain session. Unlock the phone and retry, or explicitly forget the stored pairing.',
					});
			}
		})();
		return hydration;
	}
	async function pair(apiBase: string, code: string): Promise<boolean> {
		const state = store.getState();
		if (state.busy || state.phase !== 'unpaired' || deps.now() < state.retryAt) return false;
		let base: string;
		let normalized: string;
		try {
			base = canonicalApiBase(apiBase);
			normalized = normaliseCode(code);
		} catch {
			store.setState({ message: 'Check the HTTPS /v1 API URL and eight-character code.' });
			return false;
		}
		const ticket = ++generation;
		store.setState({ busy: true, message: '' });
		let accepted = false;
		try {
			const session = await deps.api({ apiBase: base }).pair(normalized, deps.metadata());
			accepted = true;
			if (!current(ticket)) return false;
			await storage(async () => {
				if (current(ticket)) await deps.storage.set(JSON.stringify(session));
			});
			if (!current(ticket)) return false;
			store.setState({ session, phase: 'authenticated', busy: false, retryAt: 0, message: '' });
			return true;
		} catch (error) {
			if (current(ticket))
				store.setState({
					busy: false,
					message: accepted
						? 'The laptop accepted pairing, but Keychain could not save it. Revoke the device on the laptop before pairing again.'
						: error instanceof ApiError
							? error.message
							: 'Could not pair securely. Check the laptop URL and certificate trust.',
					retryAt:
						error instanceof ApiError && error.retryAfterSec
							? deps.now() + error.retryAfterSec * 1000
							: 0,
				});
			return false;
		}
	}
	async function revoke() {
		const session = store.getState().session;
		if (!session || store.getState().busy) return;
		const ticket = generation;
		store.setState({ busy: true });
		try {
			await authenticated((api) => api.revoke(), ticket, session);
			if (current(ticket)) await clear('The laptop confirmed revocation.', ticket);
		} catch {
			if (current(ticket))
				store.setState({
					busy: false,
					message:
						'The laptop did not confirm revocation. Pairing is still saved. Retry, or explicitly forget only this phone.',
				});
		}
	}
	function registerPush(): Promise<void> {
		if (pushFlight) return pushFlight;
		const session = store.getState().session;
		const ticket = generation;
		if (!session) return Promise.resolve();
		store.setState({ pushStatus: 'Checking notification setup…' });
		const flight = (async () => {
			try {
				const result = await deps.push();
				if (!current(ticket)) return;
				if ('unavailable' in result) {
					store.setState({ pushStatus: result.unavailable });
					return;
				}
				const info = deps.metadata();
				const id = await authenticated(
					(api) =>
						api.patchDevice({
							expo_push_token: result.token,
							tz: info.tz,
							app_version: info.app_version,
							ios_version: info.ios_version,
						}),
					ticket,
					session,
				);
				if (id !== session.deviceId) throw new Error('Device identity mismatch');
				if (current(ticket))
					store.setState({ pushStatus: 'Notifications registered with the laptop.' });
			} catch {
				if (current(ticket))
					store.setState({
						pushStatus: 'Notifications unavailable. Pairing is saved; retry setup separately.',
					});
			}
		})();
		pushFlight = flight.finally(() => {
			pushFlight = undefined;
		});
		return pushFlight;
	}
	function retryHydration() {
		hydration = undefined;
		return hydrate();
	}
	function lease(): SessionLease | null {
		const state = store.getState();
		const session = state.session;
		if (state.phase !== 'authenticated' || !session) return null;
		const ticket = generation;
		const matches = () => current(ticket) && store.getState().phase === 'authenticated';
		return {
			identity: `${session.apiBase}|${session.deviceId}`,
			current: matches,
			async run(operation) {
				if (!matches()) throw new Error('Pairing changed during telemetry.');
				// Invalidation is synchronous; its serialized native Keychain cleanup may not be cancellable.
				const result = await authenticated(operation, ticket, session, false);
				if (!matches()) throw new Error('Pairing changed during telemetry.');
				return result;
			},
		};
	}
	return {
		lease,
		store,
		hydrate,
		retryHydration,
		pair,
		validate,
		forget: () =>
			clear('Forgot this phone only. Revoke the old device on the laptop before pairing again.'),
		revoke,
		registerPush,
	};
}
export type SessionController = ReturnType<typeof createSessionStore>;
