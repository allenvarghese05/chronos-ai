import { Link } from 'expo-router';
import { useEffect, useState } from 'react';
import { Button, ScrollView, Text } from 'react-native';
import ChronosSensors, {
	type DeviceState,
	type StepQueryResult,
} from '../modules/chronos-sensors/src';
import { ProtectedScene, useSession } from '../src/session/provider';
import { TelemetryControls } from '../src/telemetry/controls';
import { ui } from '../src/ui';
import { WakePermissions } from '../src/wake/permissions';

export default function Index() {
	return (
		<ProtectedScene>
			<PairedHome />
		</ProtectedScene>
	);
}

/** Mount sensor reads only inside the hydrated, authenticated boundary. */
function PairedHome() {
	const state = useSession();
	const [deviceState, setDeviceState] = useState<DeviceState | null>(null);
	const [stepsResult, setStepsResult] = useState<StepQueryResult | null>(null);
	const [stepsError, setStepsError] = useState<string | null>(null);
	useEffect(() => {
		setDeviceState(ChronosSensors.deviceState());
		const now = Date.now();
		let mounted = true;
		ChronosSensors.pedometerAuthorization()
			.then((auth) => {
				if (auth !== 'authorized')
					throw new Error(`Motion queries skipped: ${auth}. Use Allow Motion access below.`);
				return ChronosSensors.querySteps(now - 600_000, now);
			})
			.then((result) => {
				if (mounted) setStepsResult(result);
			})
			.catch((error: unknown) => {
				if (mounted) setStepsError(error instanceof Error ? error.message : String(error));
			});
		return () => {
			mounted = false;
		};
	}, []);
	return (
		<ScrollView contentContainerStyle={ui.page}>
			<Text style={ui.title}>Connected to your laptop.</Text>
			<Text style={ui.host}>{state.session?.apiBase}</Text>
			<Link href="/debug" style={ui.host}>
				Open wake diagnostics
			</Link>
			<TelemetryControls />
			<WakePermissions />
			<Text style={ui.subtitle}>Chronos Sensors</Text>
			<Text style={ui.label}>deviceState()</Text>
			<Text style={ui.text}>{deviceState ? JSON.stringify(deviceState, null, 2) : 'loading…'}</Text>
			<Text style={ui.label}>querySteps (last 10 min)</Text>
			{stepsResult && <Text style={ui.text}>{JSON.stringify(stepsResult, null, 2)}</Text>}
			{stepsError && <Text style={ui.notice}>{stepsError}</Text>}
			{!!state.message && <Text style={ui.notice}>{state.message}</Text>}
			<Button
				title={state.checking ? 'Checking laptop…' : 'Check connection'}
				disabled={state.checking || state.busy}
				onPress={() => void state.controller.validate()}
			/>
			<Text style={ui.subtitle}>Pairing controls</Text>
			<Text style={ui.text}>
				Revoke asks the laptop to disable this credential. Forget removes it only from this phone;
				the laptop’s active device remains until you revoke it there.
			</Text>
			<Button
				title="Revoke on laptop and forget"
				disabled={state.busy}
				onPress={() => void state.controller.revoke()}
			/>
			<Button
				title="Forget only this phone"
				disabled={state.busy}
				onPress={() => void state.controller.forget()}
			/>
		</ScrollView>
	);
}
