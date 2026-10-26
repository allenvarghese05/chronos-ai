jest.mock('expo-device', () => ({
	__esModule: true,
	isDevice: false,
	osVersion: '18.6',
	modelName: 'Simulator',
}));
jest.mock('expo-constants', () => ({
	__esModule: true,
	default: { expoConfig: { version: '0.0.1' } },
}));
jest.mock('expo-notifications', () => ({
	getPermissionsAsync: jest.fn(),
	requestPermissionsAsync: jest.fn(),
	getExpoPushTokenAsync: jest.fn(),
}));
jest.mock('expo-secure-store', () => ({
	AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY: 'approved-accessibility',
	getItemAsync: jest.fn(),
	setItemAsync: jest.fn(),
	deleteItemAsync: jest.fn(),
}));
jest.mock('expo/fetch', () => ({ fetch: jest.fn() }));

import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import * as SecureStore from 'expo-secure-store';
import { deviceMetadata, requestPushToken, secureStorage } from '../src/session/native';

beforeEach(() => {
	jest.clearAllMocks();
	Object.assign(Device, { isDevice: false });
	Object.assign(Constants, { easConfig: undefined });
});
test('all secure operations use approved device-bound Keychain policy', async () => {
	await secureStorage.get();
	await secureStorage.set('fake-envelope');
	await secureStorage.remove();
	expect(SecureStore.setItemAsync).toHaveBeenCalledWith('chronos.session.v1', 'fake-envelope', {
		keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
	});
	expect(SecureStore.getItemAsync).toHaveBeenCalledWith('chronos.session.v1', {
		keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
	});
});
test('Simulator metadata is bounded without owner name/location', () => {
	expect(deviceMetadata()).toEqual(
		expect.objectContaining({ name: 'Chronos Simulator', tz: expect.any(String) }),
	);
});
test('Simulator never requests or fabricates push registration', async () => {
	expect(await requestPushToken()).toHaveProperty('unavailable');
	expect(Notifications.getExpoPushTokenAsync).not.toHaveBeenCalled();
});
test('missing projectId remains nonblocking before permission request', async () => {
	Object.assign(Device, { isDevice: true });
	expect(await requestPushToken()).toHaveProperty('unavailable');
	expect(Notifications.requestPermissionsAsync).not.toHaveBeenCalled();
});
test('denial and valid physical registration are explicit outcomes', async () => {
	Object.assign(Device, { isDevice: true });
	Object.assign(Constants, { easConfig: { projectId: '11111111-1111-4111-8111-111111111111' } });
	jest
		.mocked(Notifications.getPermissionsAsync)
		.mockResolvedValue({ status: 'denied' } as Notifications.NotificationPermissionsStatus);
	jest
		.mocked(Notifications.requestPermissionsAsync)
		.mockResolvedValue({ status: 'denied' } as Notifications.NotificationPermissionsStatus);
	expect(await requestPushToken()).toHaveProperty('unavailable');
	jest
		.mocked(Notifications.getPermissionsAsync)
		.mockResolvedValue({ status: 'granted' } as Notifications.NotificationPermissionsStatus);
	jest
		.mocked(Notifications.getExpoPushTokenAsync)
		.mockResolvedValue({ type: 'expo', data: 'ExpoPushToken[fake]' });
	expect(await requestPushToken()).toEqual({ token: 'ExpoPushToken[fake]' });
});
