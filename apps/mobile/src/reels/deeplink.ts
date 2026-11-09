/**
 * Notification taps and deep links into the player (T5.4).
 *
 * - Warm start: a tapped `reel_ready` notification opens `/reel/<id>`.
 * - Cold start: the response that launched the app is read once on mount.
 * - `chronos://reel/<id>` links are routed by expo-router itself (scheme `chronos`).
 * - The badge is cleared whenever the app becomes active.
 *
 * `expo-notifications` is loaded lazily, as elsewhere in the app (T2.2).
 */

import { useRouter } from 'expo-router';
import { useEffect } from 'react';
import { AppState } from 'react-native';
import { reelIdFrom } from './model';

type Notifications = typeof import('expo-notifications');

function load(): Notifications | null {
	try {
		return require('expo-notifications') as Notifications;
	} catch {
		return null;
	}
}

/** Pure routing decision for a notification payload (exported for tests). */
export function routeForNotification(data: unknown): string | null {
	const id = reelIdFrom(data);
	if (id) return `/reel/${id}`;
	if (
		typeof data === 'object' &&
		data !== null &&
		(data as { type?: unknown }).type === 'text_fallback'
	)
		return '/';
	return null;
}

export function useReelDeepLinks(): void {
	const router = useRouter();
	useEffect(() => {
		const notifications = load();
		if (!notifications) return;
		let handled = false;
		const open = (data: unknown) => {
			const route = routeForNotification(data);
			if (route) router.push(route as never);
		};
		void notifications
			.getLastNotificationResponseAsync()
			.then((response) => {
				if (!handled && response) {
					handled = true;
					open(response.notification.request.content.data);
				}
			})
			.catch(() => undefined);
		const subscription = notifications.addNotificationResponseReceivedListener((response) => {
			handled = true;
			open(response.notification.request.content.data);
		});
		const clear = () => void notifications.setBadgeCountAsync(0).catch(() => undefined);
		clear();
		const appState = AppState.addEventListener('change', (state) => {
			if (state === 'active') clear();
		});
		return () => {
			subscription.remove();
			appState.remove();
		};
	}, [router]);
}
