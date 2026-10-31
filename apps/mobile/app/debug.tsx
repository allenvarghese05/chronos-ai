import { DebugScreen } from '../src/debug/screen';
import { ProtectedScene } from '../src/session/provider';

export default function Debug() {
	return (
		<ProtectedScene>
			<DebugScreen />
		</ProtectedScene>
	);
}
