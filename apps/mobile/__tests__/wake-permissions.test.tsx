import { act, type ReactElement } from 'react';
import { AppState, Linking } from 'react-native';
import ChronosSensors from '../modules/chronos-sensors/src';
import { WakePermissions } from '../src/wake/permissions';

const mockPush = jest.fn(async () => {});
jest.mock('expo-device', () => ({ isDevice: false }));
jest.mock('../src/session/native', () => ({ notificationProjectId: () => null }));
jest.mock('../src/session/provider', () => ({
	useSession: () => ({ controller: { registerPush: mockPush } }),
}));
jest.mock('../src/wake/bootstrap', () => ({
	subscribeWakeSetup: () => () => {},
	wakeSetupStatus: () => ({ registrationStatus: 'Simulator', scheduleStatus: 'Opportunistic' }),
}));
jest.mock('../modules/chronos-sensors/src', () => ({
	__esModule: true,
	default: {
		pedometerAuthorization: jest.fn(async () => 'denied'),
		backgroundRefreshStatus: () => 'restricted',
		deviceState: () => ({ lowPowerMode: true }),
		requestMotionPermission: jest.fn(async () => {}),
	},
}));
type Tree = {
	toJSON(): unknown;
	root: { findByProps(props: { title: string }): { props: { onPress(): Promise<void> | void } } };
	unmount(): void;
};
const renderer = require('react-test-renderer') as { create(element: ReactElement): Tree };
beforeEach(() => {
	jest.clearAllMocks();
	Object.defineProperty(AppState, 'currentState', {
		configurable: true,
		writable: true,
		value: 'active',
	});
});
test('reads distinct actual statuses and shows force-quit caveat without requesting permissions', async () => {
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(<WakePermissions />);
	});
	const text = JSON.stringify(tree.toJSON());
	for (const expected of [
		'denied',
		'restricted',
		'on',
		'force-quit',
		'unlock it once',
		'not guaranteed',
		'Simulator',
	])
		expect(text).toContain(expected);
	expect(ChronosSensors.requestMotionPermission).not.toHaveBeenCalled();
	expect(mockPush).not.toHaveBeenCalled();
	await act(async () => {
		tree.root.findByProps({ title: 'Allow Motion access' }).props.onPress();
	});
	expect(ChronosSensors.requestMotionPermission).toHaveBeenCalledTimes(1);
	expect(ChronosSensors.pedometerAuthorization).toHaveBeenCalledTimes(2);
	await act(async () => tree.unmount());
});
test('explicit notification action registers; inactive action cannot prompt; Settings opens native settings', async () => {
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(<WakePermissions />);
	});
	await act(async () => {
		tree.root.findByProps({ title: 'Set up notification alerts' }).props.onPress();
	});
	expect(mockPush).toHaveBeenCalledTimes(1);
	Object.defineProperty(AppState, 'currentState', { value: 'background' });
	await act(async () => {
		tree.root.findByProps({ title: 'Allow Motion access' }).props.onPress();
	});
	expect(ChronosSensors.requestMotionPermission).not.toHaveBeenCalled();
	const open = jest.spyOn(Linking, 'openSettings').mockResolvedValue();
	await act(async () => {
		tree.root.findByProps({ title: 'Open iOS Settings' }).props.onPress();
	});
	expect(open).toHaveBeenCalledTimes(1);
	open.mockRestore();
	await act(async () => tree.unmount());
});
