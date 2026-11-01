import { ProtectedScene } from '../src/session/provider';
import { SettingsScreen } from '../src/settings/screen';
export default function Settings() {
	return (
		<ProtectedScene>
			<SettingsScreen />
		</ProtectedScene>
	);
}
