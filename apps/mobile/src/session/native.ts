import { fetch as expoFetch } from 'expo/fetch';
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';
import { createApi, type DeviceInfo } from '../api/client';
import { createSessionStore, type PushResult, type SecureStorage } from './store';

const SESSION_KEY = 'chronos.session.v1';
const options = { keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY };
/** One device-bound Keychain record; no secret duplication in general-purpose storage. */
export const secureStorage: SecureStorage = {
	get: () => SecureStore.getItemAsync(SESSION_KEY, options),
	set: (value) => SecureStore.setItemAsync(SESSION_KEY, value, options),
	remove: () => SecureStore.deleteItemAsync(SESSION_KEY, options),
};
const bounded = (value: string | null | undefined, max: number, fallback: string) =>
	(value?.trim() || fallback).slice(0, max);
export function deviceMetadata(): DeviceInfo {
	const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
	if (!tz || tz.length > 64) throw new Error('Device timezone unavailable');
	return {
		name: Device.isDevice ? 'Chronos iPhone' : 'Chronos Simulator',
		model: bounded(Device.modelName, 80, Device.isDevice ? 'iPhone' : 'iOS Simulator'),
		ios_version: bounded(Device.osVersion ?? String(Platform.Version), 20, 'unknown'),
		app_version: bounded(Constants.expoConfig?.version, 20, '0.0.1'),
		tz,
	};
}
/** Explicit, nonblocking registration; never invent a Simulator token or create Expo resources. */
export async function requestPushToken(): Promise<PushResult> {
	if (!Device.isDevice)
		return {
			unavailable:
				'Push registration needs a signed physical iPhone build (T2.4). Pairing works in Simulator.',
		};
	const projectId = Constants.easConfig?.projectId ?? Constants.expoConfig?.extra?.eas?.projectId;
	if (
		typeof projectId !== 'string' ||
		!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(projectId)
	)
		return {
			unavailable:
				'Set EXPO_PROJECT_ID for your existing Expo project and rebuild to enable notifications.',
		};
	// Importing notifications starts its native Keychain auto-registration side effect.
	// Load it only after explicit registration passes device/config prerequisites.
	const Notifications: typeof import('expo-notifications') = require('expo-notifications');
	let permission = await Notifications.getPermissionsAsync();
	if (permission.status !== 'granted') permission = await Notifications.requestPermissionsAsync();
	if (permission.status !== 'granted')
		return {
			unavailable:
				'Notifications are not allowed. Pairing is saved; enable them in iOS Settings to retry.',
		};
	const result = await Notifications.getExpoPushTokenAsync({ projectId });
	if (!/^(Expo|Exponent)PushToken\[[A-Za-z0-9_-]+\]$/.test(result.data) || result.data.length > 256)
		throw new Error('Invalid push registration');
	return { token: result.data };
}
export const sessionController = createSessionStore({
	storage: secureStorage,
	api: (session) =>
		createApi(session.apiBase, (request, options) => expoFetch(request, options), session.token),
	metadata: deviceMetadata,
	push: requestPushToken,
	now: Date.now,
});
