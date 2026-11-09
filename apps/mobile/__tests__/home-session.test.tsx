import { act, type ReactElement } from 'react';
import Index from '../app';
import ChronosSensors from '../modules/chronos-sensors/src';
import { SessionProvider } from '../src/session/provider';
import { createSessionStore } from '../src/session/store';

jest.mock('../src/session/native', () => ({ sessionController: undefined }));
jest.mock('../src/telemetry/controls', () => ({ TelemetryControls: () => null }));
jest.mock('../src/wake/permissions', () => ({ WakePermissions: () => null }));
jest.mock('../src/reels/feed', () => ({ ReelFeed: () => null }));
jest.mock('expo-router', () => ({ Redirect: 'Redirect', Link: 'Link' }));
jest.mock('../modules/chronos-sensors/src', () => ({
	__esModule: true,
	default: {
		pedometerAuthorization: jest.fn(async () => 'authorized'),
		deviceState: jest.fn(() => ({
			isUnlocked: true,
			batteryState: 'unknown',
			batteryLevel: null,
			appState: 'active',
			lowPowerMode: false,
			iosVersion: '18.6',
			tz: 'Etc/UTC',
		})),
		querySteps: jest.fn(async () => {
			throw new Error('pedometer_unavailable');
		}),
	},
}));
type Tree = { toJSON(): unknown; unmount(): void };
const renderer = require('react-test-renderer') as { create(element: ReactElement): Tree };
const session = {
	version: 1 as const,
	apiBase: 'https://laptop.local:8443/v1',
	deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
	token: 'a'.repeat(43),
};
function controller() {
	return createSessionStore({
		storage: {
			get: () => new Promise<string | null>(() => {}),
			set: async () => {},
			remove: async () => {},
		},
		api: () => {
			throw new Error('No API needed for render guard');
		},
		metadata: () => ({
			name: 'Simulator',
			model: 'Simulator',
			ios_version: '18.6',
			app_version: '0.0.1',
			tz: 'Etc/UTC',
		}),
		push: async () => ({ unavailable: 'Simulator' }),
		now: () => 0,
	});
}
beforeEach(() => jest.clearAllMocks());
test('hydrating and unpaired home never read sensors', async () => {
	const state = controller();
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<SessionProvider controller={state}>
				<Index />
			</SessionProvider>,
		);
	});
	expect(ChronosSensors.deviceState).not.toHaveBeenCalled();
	await act(async () => {
		state.store.setState({ phase: 'unpaired' });
	});
	expect(ChronosSensors.querySteps).not.toHaveBeenCalled();
	await act(async () => tree.unmount());
});
test('authenticated home preserves device state and Simulator step error', async () => {
	const state = controller();
	state.store.setState({ phase: 'authenticated', session });
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<SessionProvider controller={state}>
				<Index />
			</SessionProvider>,
		);
	});
	expect(ChronosSensors.deviceState).toHaveBeenCalledTimes(1);
	expect(ChronosSensors.querySteps).toHaveBeenCalledTimes(1);
	expect(JSON.stringify(tree.toJSON())).toContain('pedometer_unavailable');
	expect(JSON.stringify(tree.toJSON())).toContain('deviceState()');
	await act(async () => tree.unmount());
});
