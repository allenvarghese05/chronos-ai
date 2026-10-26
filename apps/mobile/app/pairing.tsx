import { CameraView, useCameraPermissions } from 'expo-camera';
import * as Device from 'expo-device';
import { Link, Redirect } from 'expo-router';
import { useRef, useState } from 'react';
import { Button, ScrollView, Text, TextInput, View } from 'react-native';
import { canonicalApiBase, normaliseCode, parsePairingQr } from '../src/pairing/validation';
import { useSession } from '../src/session/provider';
import { ui } from '../src/ui';

export default function Pairing() {
	const state = useSession();
	const [apiBase, setApiBase] = useState('');
	const [code, setCode] = useState('');
	const [confirmation, setConfirmation] = useState<{ apiBase: string; code: string } | null>(null);
	const [scanner, setScanner] = useState(false);
	const [message, setMessage] = useState('');
	const [permission, requestPermission] = useCameraPermissions();
	const scanConsumed = useRef(false);
	const submitLock = useRef(false);
	if (state.phase === 'authenticated') return <Redirect href="/" />;
	if (state.phase === 'hydrating')
		return (
			<View style={ui.page}>
				<Text>Reading secure pairing…</Text>
			</View>
		);
	if (state.phase === 'storage-error')
		return (
			<ScrollView contentContainerStyle={ui.page}>
				<Text style={ui.title}>Keychain unavailable</Text>
				<Text style={ui.notice}>{state.message}</Text>
				<Button
					title="Retry after unlocking"
					onPress={() => void state.controller.retryHydration()}
				/>
				<Button title="Forget stored pairing" onPress={() => void state.controller.forget()} />
			</ScrollView>
		);
	async function openScanner() {
		setMessage('');
		if (!Device.isDevice) {
			setMessage('Camera scanning needs a physical iPhone. Enter the laptop URL and code below.');
			return;
		}
		try {
			const granted = permission?.granted || (await requestPermission()).granted;
			if (!granted) {
				setMessage('Camera access is not allowed. You can still enter the code manually.');
				return;
			}
			scanConsumed.current = false;
			setScanner(true);
		} catch {
			setMessage('Camera unavailable. Enter the code manually.');
		}
	}
	function scanned(raw: string) {
		if (scanConsumed.current || state.busy) return;
		try {
			const parsed = parsePairingQr(raw);
			scanConsumed.current = true;
			setScanner(false);
			setApiBase(parsed.apiBase);
			setCode(parsed.code);
			setConfirmation(parsed);
			setMessage('');
		} catch {
			setMessage('This is not a valid Chronos pairing QR. Use the QR from chronos pair-code.');
		}
	}
	function review() {
		try {
			setConfirmation({ apiBase: canonicalApiBase(apiBase.trim()), code: normaliseCode(code) });
			setMessage('');
		} catch (error) {
			setMessage(error instanceof Error ? error.message : 'Check the laptop URL and code.');
		}
	}
	async function submit() {
		if (!confirmation || submitLock.current) return;
		submitLock.current = true;
		const saved = await state.controller.pair(confirmation.apiBase, confirmation.code);
		submitLock.current = false;
		if (saved) {
			setCode('');
			setConfirmation(null);
		}
	}
	return (
		<ScrollView keyboardShouldPersistTaps="handled" contentContainerStyle={ui.page}>
			<Text style={ui.title}>Your morning starts here.</Text>
			<Text style={ui.text}>
				Connect to your laptop on the same Wi-Fi. On the laptop, run chronos pair-code to get a
				short-lived code.
			</Text>
			<Link href="/certificate" style={ui.host}>
				First, trust your laptop’s HTTPS certificate →
			</Link>
			<Text style={ui.label}>Laptop API URL</Text>
			<TextInput
				accessibilityLabel="Laptop API URL"
				style={ui.input}
				value={apiBase}
				onChangeText={(value) => {
					setApiBase(value);
					setConfirmation(null);
				}}
				placeholder="https://your-laptop.local:8443/v1"
				autoCapitalize="none"
				autoCorrect={false}
				keyboardType="url"
				maxLength={512}
				editable={!state.busy}
			/>
			<Text style={ui.label}>Pairing code</Text>
			<TextInput
				accessibilityLabel="Pairing code"
				style={ui.input}
				value={code}
				onChangeText={(value) => {
					setCode(value);
					setConfirmation(null);
				}}
				placeholder="ABCD-EFGH"
				autoCapitalize="characters"
				autoCorrect={false}
				maxLength={32}
				editable={!state.busy}
			/>
			<Button title="Scan laptop QR" onPress={() => void openScanner()} disabled={state.busy} />
			{scanner && (
				<View>
					<CameraView
						style={ui.camera}
						facing="back"
						barcodeScannerSettings={{ barcodeTypes: ['qr'] }}
						onBarcodeScanned={({ data }) => scanned(data)}
					/>
					<Button title="Cancel scan" onPress={() => setScanner(false)} />
				</View>
			)}
			{confirmation ? (
				<View style={{ gap: 12 }}>
					<Text style={ui.label}>Send this code only to:</Text>
					<Text style={ui.host}>{confirmation.apiBase}</Text>
					<Button
						title={state.busy ? 'Saving secure pairing…' : 'Confirm and pair'}
						onPress={() => void submit()}
						disabled={state.busy}
					/>
					<Button
						title="Edit details"
						onPress={() => setConfirmation(null)}
						disabled={state.busy}
					/>
				</View>
			) : (
				<Button title="Review pairing details" onPress={review} disabled={state.busy} />
			)}
			{!!(message || state.message) && (
				<Text accessibilityRole="alert" style={ui.notice}>
					{message || state.message}
				</Text>
			)}
			{state.retryAt > Date.now() && (
				<Text style={ui.notice}>
					Please wait until {new Date(state.retryAt).toLocaleTimeString()} before trying again.
				</Text>
			)}
			<Text style={ui.text}>
				Notification setup comes after pairing and is optional. Camera frames and pairing codes are
				never saved. Force-quitting the app may prevent future background wakes.
			</Text>
		</ScrollView>
	);
}
