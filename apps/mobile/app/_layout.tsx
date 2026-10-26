import { Stack } from 'expo-router';
import { SessionProvider } from '../src/session/provider';

export default function RootLayout() {
	return (
		<SessionProvider>
			<Stack
				screenOptions={{ headerStyle: { backgroundColor: '#faf9f6' }, headerTintColor: '#202722' }}
			>
				<Stack.Screen name="index" options={{ title: 'Chronos' }} />
				<Stack.Screen name="pairing" options={{ title: 'Pair your laptop' }} />
				<Stack.Screen name="certificate" options={{ title: 'Trust your laptop' }} />
			</Stack>
		</SessionProvider>
	);
}
