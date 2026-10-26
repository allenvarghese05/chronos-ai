import { Button, ScrollView, Text } from 'react-native';
import ChronosSensors from '../modules/chronos-sensors/src';
import { ProtectedScene, useSession } from '../src/session/provider';
import { ui } from '../src/ui';

export default function Index() {
	const state = useSession();
	return (
		<ProtectedScene>
			<ScrollView contentContainerStyle={ui.page}>
				<Text style={ui.title}>Connected to your laptop.</Text>
				<Text style={ui.host}>{state.session?.apiBase}</Text>
				<Text style={ui.text}>{ChronosSensors.hello()}</Text>
				{!!state.message && <Text style={ui.notice}>{state.message}</Text>}
				<Button
					title={state.checking ? 'Checking laptop…' : 'Check connection'}
					disabled={state.checking || state.busy}
					onPress={() => void state.controller.validate()}
				/>
				<Text style={ui.subtitle}>Notifications</Text>
				<Text style={ui.text}>{state.pushStatus}</Text>
				<Button
					title="Set up or retry notifications"
					onPress={() => void state.controller.registerPush()}
					disabled={state.busy}
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
		</ProtectedScene>
	);
}
