import { Stack } from 'expo-router';
import { SessionProvider } from '../src/session/provider';

export default function RootLayout() {
	return (
		<SessionProvider>
			<Stack
				screenOptions={{ headerStyle: { backgroundColor: '#faf9f6' }, headerTintColor: '#202722' }}
			>
				<Stack.Screen name="index" options={{ title: 'Chronos' }} />
				<Stack.Screen name="debug" options={{ title: 'Wake diagnostics' }} />
				<Stack.Screen name="settings" options={{ title: 'Settings' }} />
				<Stack.Screen name="pairing" options={{ title: 'Pair your laptop' }} />
				<Stack.Screen name="certificate" options={{ title: 'Trust your laptop' }} />
				<Stack.Screen
					name="reel/[id]"
					options={{ title: 'Morning reel', headerTransparent: true, headerTintColor: '#fff' }}
				/>
			</Stack>
		</SessionProvider>
	);
}
