import * as Device from 'expo-device';
import { useEffect, useState } from 'react';
import { AppState, Button, Linking, Text, View } from 'react-native';
import ChronosSensors from '../../modules/chronos-sensors/src';
import { notificationProjectId } from '../session/native';
import { useSession } from '../session/provider';
import { ui } from '../ui';
import { subscribeWakeSetup, wakeSetupStatus } from './bootstrap';

/** Foreground explanations/actions; background work never imports this screen or prompts. */
export function WakePermissions() {
	const { controller, pushStatus } = useSession();
	const [motion, setMotion] = useState('unknown');
	const [refresh, setRefresh] = useState('unknown');
	const [notifications, setNotifications] = useState('Not checked.');
	const [lowPower, setLowPower] = useState(false);
	const [message, setMessage] = useState('');
	const [busy, setBusy] = useState(false);
	const [setup, setSetup] = useState(wakeSetupStatus);
	useEffect(() => {
		let mounted = true;
		async function read() {
			if (AppState.currentState !== 'active') return;
			try {
				const auth = await ChronosSensors.pedometerAuthorization();
				const refreshStatus = ChronosSensors.backgroundRefreshStatus();
				const state = ChronosSensors.deviceState();
				let notificationStatus = !Device.isDevice
					? 'Unavailable in this Simulator setup.'
					: !notificationProjectId()
						? 'Existing Expo project and signed APNs build required.'
						: 'Unavailable.';
				if (Device.isDevice && notificationProjectId()) {
					const Notifications: typeof import('expo-notifications') = require('expo-notifications');
					notificationStatus = (await Notifications.getPermissionsAsync()).status;
				}
				if (mounted) {
					setMotion(auth);
					setRefresh(refreshStatus);
					setLowPower(state.lowPowerMode);
					setNotifications(notificationStatus);
					setSetup(wakeSetupStatus());
				}
			} catch {
				if (mounted) setMessage('Permission status unavailable. Reopen after unlocking the phone.');
			}
		}
		void read();
		const stopSetup = subscribeWakeSetup(() => {
			if (mounted) setSetup(wakeSetupStatus());
		});
		const subscription = AppState.addEventListener('change', (state) => {
			if (state === 'active') void read();
		});
		return () => {
			mounted = false;
			stopSetup();
			subscription.remove();
		};
	}, []);
	async function request(action: () => Promise<void>) {
		if (busy || AppState.currentState !== 'active') return;
		setBusy(true);
		setMessage('');
		try {
			await action();
			setMotion(await ChronosSensors.pedometerAuthorization());
			setRefresh(ChronosSensors.backgroundRefreshStatus());
			setLowPower(ChronosSensors.deviceState().lowPowerMode);
			if (Device.isDevice && notificationProjectId()) {
				const Notifications: typeof import('expo-notifications') = require('expo-notifications');
				setNotifications((await Notifications.getPermissionsAsync()).status);
			}
			setSetup(wakeSetupStatus());
			setMessage(
				'Permission action finished. Denied or restricted permissions may need iOS Settings.',
			);
		} catch {
			setMessage('Permission action unavailable. Check iOS Settings and build configuration.');
		} finally {
			setBusy(false);
		}
	}
	return (
		<View>
			<Text style={ui.subtitle}>Wake permissions and background status</Text>
			<Text style={ui.text}>
				Motion: {motion}. Background App Refresh: {refresh}. Low Power Mode:{' '}
				{lowPower ? 'on' : 'off'}.
			</Text>
			<Text style={ui.text}>Notification alerts: {notifications}</Text>
			<Text style={ui.text}>{pushStatus}</Text>
			<Text style={ui.text}>
				Motion history helps the laptop evaluate wake signals. Notification alerts announce a ready
				reel. Background App Refresh is an iOS setting, not a permission prompt.
			</Text>
			<Text style={ui.notice}>
				If you force-quit Chronos, silent wakes may stop until you reopen it. After restarting the
				phone, unlock it once so the secure pairing can be read. iOS may delay or suppress
				background work, especially with Low Power Mode or Background App Refresh disabled.
				Five-minute execution is not guaranteed.
			</Text>
			<Text style={ui.text}>{setup.registrationStatus}</Text>
			<Text style={ui.text}>{setup.scheduleStatus}</Text>
			{!!message && <Text style={ui.notice}>{message}</Text>}
			<Button
				title="Allow Motion access"
				disabled={busy}
				onPress={() => void request(() => ChronosSensors.requestMotionPermission())}
			/>
			<Button
				title="Set up notification alerts"
				disabled={busy}
				onPress={() => void request(() => controller.registerPush())}
			/>
			<Button
				title="Open iOS Settings"
				onPress={() => {
					void Linking.openSettings().catch(() => setMessage('Could not open iOS Settings.'));
				}}
			/>
		</View>
	);
}
