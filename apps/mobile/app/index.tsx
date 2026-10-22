/**
 * Home screen — minimal sensor display for T2.1 Simulator verification.
 * Shows deviceState() as JSON and the querySteps result (or pedometer_unavailable
 * on the Simulator). T2.5 owns the real debug screen.
 */
import { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, Text } from 'react-native';

import ChronosSensors, {
	type DeviceState,
	type StepQueryResult,
} from '../modules/chronos-sensors/src';

export default function Index() {
	const [deviceState, setDeviceState] = useState<DeviceState | null>(null);
	const [stepsResult, setStepsResult] = useState<StepQueryResult | null>(null);
	const [stepsError, setStepsError] = useState<string | null>(null);

	useEffect(() => {
		// deviceState() is synchronous.
		setDeviceState(ChronosSensors.deviceState());

		// querySteps rejects with "pedometer_unavailable" on the Simulator.
		const now = Date.now();
		ChronosSensors.querySteps(now - 600_000, now)
			.then((r) => setStepsResult(r))
			.catch((e: unknown) => {
				const msg = e instanceof Error ? e.message : String(e);
				setStepsError(msg);
			});
	}, []);

	return (
		<ScrollView contentContainerStyle={styles.container}>
			<Text style={styles.heading}>Chronos Sensors</Text>

			<Text style={styles.label}>deviceState()</Text>
			<Text style={styles.code}>
				{deviceState ? JSON.stringify(deviceState, null, 2) : 'loading…'}
			</Text>

			<Text style={styles.label}>querySteps (last 10 min)</Text>
			{stepsResult && <Text style={styles.code}>{JSON.stringify(stepsResult, null, 2)}</Text>}
			{stepsError && <Text style={[styles.code, styles.error]}>{stepsError}</Text>}
		</ScrollView>
	);
}

const styles = StyleSheet.create({
	container: {
		padding: 20,
		paddingTop: 60,
	},
	heading: {
		fontSize: 20,
		fontWeight: 'bold',
		marginBottom: 16,
	},
	label: {
		fontSize: 14,
		fontWeight: '600',
		marginTop: 12,
		marginBottom: 4,
		color: '#555',
	},
	code: {
		fontFamily: 'Courier',
		fontSize: 12,
		backgroundColor: '#f5f5f5',
		padding: 8,
		borderRadius: 4,
	},
	error: {
		color: '#c00',
	},
});
