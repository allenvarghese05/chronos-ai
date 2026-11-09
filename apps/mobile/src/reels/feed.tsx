/**
 * Reel feed (T5.3): today's reel and saved reels, newest first. Tapping opens the player.
 */

import { useFocusEffect, useRouter } from 'expo-router';
import { useCallback, useState } from 'react';
import { Image, Pressable, StyleSheet, Text, View } from 'react-native';
import { useSession } from '../session/provider';
import { ui } from '../ui';
import type { ReelSummary } from './model';

function label(reel: ReelSummary): string {
	if (reel.status !== 'ready') return `Preparing (${reel.status})`;
	const seconds = reel.duration_sec ? `${Math.round(reel.duration_sec)} s` : '';
	return [seconds, reel.saved ? 'Saved' : 'Today'].filter(Boolean).join(' · ');
}

export function ReelFeed() {
	const { controller } = useSession();
	const router = useRouter();
	const [reels, setReels] = useState<ReelSummary[] | null>(null);
	const [error, setError] = useState('');

	useFocusEffect(
		useCallback(() => {
			let live = true;
			const lease = controller.lease();
			if (!lease) return;
			lease
				.run((api) => api.listReels())
				.then((list) => {
					if (live) {
						setReels(list);
						setError('');
					}
				})
				.catch((e: unknown) => {
					if (live) setError(e instanceof Error ? e.message : 'Could not load reels.');
				});
			return () => {
				live = false;
			};
		}, [controller]),
	);

	return (
		<View style={styles.section} accessibilityLabel="Morning reels">
			<Text style={ui.subtitle}>Morning reels</Text>
			{error ? <Text style={ui.notice}>{error}</Text> : null}
			{reels === null && !error ? <Text style={ui.text}>Loading…</Text> : null}
			{reels?.length === 0 ? (
				<Text style={ui.text}>
					No reel yet today. It is prepared before your wake window and arrives once you are up.
				</Text>
			) : null}
			{reels?.map((reel) => (
				<Pressable
					key={reel.public_id}
					accessibilityRole="button"
					accessibilityLabel={`Open reel for ${reel.local_date}`}
					disabled={reel.status !== 'ready'}
					onPress={() => router.push(`/reel/${reel.public_id}`)}
					style={styles.card}
				>
					{reel.thumbnail_url ? (
						<Image source={{ uri: reel.thumbnail_url }} style={styles.thumb} />
					) : (
						<View style={[styles.thumb, styles.placeholder]} />
					)}
					<View style={styles.meta}>
						<Text style={ui.label}>{reel.local_date}</Text>
						<Text style={ui.text}>{label(reel)}</Text>
					</View>
				</Pressable>
			))}
		</View>
	);
}

const styles = StyleSheet.create({
	section: { gap: 12 },
	card: {
		flexDirection: 'row',
		gap: 14,
		alignItems: 'center',
		backgroundColor: '#fff',
		borderRadius: 12,
		padding: 10,
		borderWidth: 1,
		borderColor: '#e0e4df',
	},
	thumb: { width: 64, height: 114, borderRadius: 8, backgroundColor: '#e0e4df' },
	placeholder: { opacity: 0.6 },
	meta: { flex: 1, gap: 4 },
});
