import { Redirect } from 'expo-router';
import { createContext, type PropsWithChildren, useContext, useEffect } from 'react';
import { AppState, Text, View } from 'react-native';
import { useStore } from 'zustand';
import { ui } from '../ui';
import { sessionController } from './native';
import type { SessionController } from './store';

const Context = createContext<SessionController>(sessionController);
export function SessionProvider({
	children,
	controller = sessionController,
}: PropsWithChildren<{ controller?: SessionController }>) {
	useEffect(() => {
		void controller.hydrate();
		const listener = AppState.addEventListener('change', (state) => {
			if (state === 'active') void controller.validate();
		});
		return () => listener.remove();
	}, [controller]);
	return <Context.Provider value={controller}>{children}</Context.Provider>;
}
export function useSession() {
	const controller = useContext(Context);
	const state = useStore(controller.store);
	return { ...state, controller };
}
export function ProtectedScene({ children }: PropsWithChildren) {
	const { phase } = useSession();
	if (phase === 'hydrating')
		return (
			<View style={ui.page}>
				<Text>Reading secure pairing…</Text>
			</View>
		);
	if (phase !== 'authenticated') return <Redirect href="/pairing" />;
	return children;
}
