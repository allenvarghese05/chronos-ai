import { useEffect, useState } from 'react';
import { Button, Text, View } from 'react-native';
import { useSession } from '../session/provider';
import { ui } from '../ui';
import type { TelemetryController, TelemetryStatus } from './controller';
import { nativeTelemetry } from './native';

/** Minimal manual acceptance controls; T2.4 owns wake wiring and T2.5 the debug screen. */
export function TelemetryControls() {
	const { controller: session } = useSession();
	const [controller, setController] = useState<TelemetryController>();
	const [status, setStatus] = useState<TelemetryStatus>();
	const [error, setError] = useState('');
	useEffect(() => {
		try {
			const telemetry = nativeTelemetry(session);
			setController(telemetry);
			telemetry.refresh();
			setStatus(telemetry.status());
			return telemetry.subscribe(() => setStatus(telemetry.status()));
		} catch {
			setError('Telemetry database unavailable. Unlock the phone or free space, then reopen.');
		}
	}, [session]);
	return (
		<View>
			<Text style={ui.subtitle}>Telemetry</Text>
			{!!error && <Text style={ui.notice}>{error}</Text>}
			<Text style={ui.text}>
				Queued: {status?.count ?? 0}; last sequence: {status?.lastSeq ?? 'none'}
			</Text>
			{!!status && <Text style={ui.notice}>{status.message}</Text>}
			{status?.diagnostics.map((item) => (
				<Text key={item} style={ui.notice}>
					{item}
				</Text>
			))}
			{status?.lastResult && (
				<Text style={ui.text}>
					Last evaluation: {status.lastResult.evaluation_id}; score: {status.lastResult.score}
				</Text>
			)}
			<Button
				title="Sample and send telemetry"
				disabled={!controller || status?.busy}
				onPress={() => void controller?.sampleAndSend('manual')}
			/>
			<Button
				title="Retry queued telemetry"
				disabled={!controller || status?.busy}
				onPress={() => void controller?.flush()}
			/>
		</View>
	);
}
