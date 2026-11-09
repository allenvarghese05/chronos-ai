/**
 * Reel player (T5.3): full-screen video, a vertical scene pager (swipe to jump between
 * stories), a karaoke caption overlay that can be toggled, a source link per story, and
 * save / delete. Media and captions come from signed URLs on the paired laptop only.
 */

import { useEvent } from 'expo';
import { useRouter } from 'expo-router';
import { type TimeUpdateEventPayload, useVideoPlayer, VideoView } from 'expo-video';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
	ActivityIndicator,
	Alert,
	FlatList,
	Linking,
	Pressable,
	StyleSheet,
	Text,
	useWindowDimensions,
	View,
	type ViewToken,
} from 'react-native';
import { useSession } from '../session/provider';
import {
	type CaptionDoc,
	captionAt,
	isCaptionDoc,
	type ReelDetail,
	type SceneSummary,
	sameOrigin,
	sceneAt,
	seekTarget,
} from './model';

const START: TimeUpdateEventPayload = {
	currentTime: 0,
	currentLiveTimestamp: null,
	currentOffsetFromLive: null,
	bufferedPosition: 0,
};

export function ReelPlayer({ id }: { id: string }) {
	const { controller, session } = useSession();
	const router = useRouter();
	const [reel, setReel] = useState<ReelDetail | null>(null);
	const [captions, setCaptions] = useState<CaptionDoc | null>(null);
	const [showCaptions, setShowCaptions] = useState(true);
	const [error, setError] = useState('');

	useEffect(() => {
		let live = true;
		const lease = controller.lease();
		lease
			?.run((api) => api.getReel(id))
			.then(async (detail) => {
				if (!live) return;
				setReel(detail);
				const url = detail.captions_url;
				if (url && session && sameOrigin(url, session.apiBase)) {
					const response = await fetch(url, { credentials: 'omit', redirect: 'error' });
					const doc: unknown = response.ok ? await response.json() : null;
					if (live && isCaptionDoc(doc)) setCaptions(doc);
				}
			})
			.catch((e: unknown) => {
				if (live) setError(e instanceof Error ? e.message : 'Could not open this reel.');
			});
		return () => {
			live = false;
		};
	}, [controller, session, id]);

	if (error) return <Message text={error} />;
	if (!reel) return <Message text="Opening your reel…" spinner />;
	if (!reel.media_url || !session || !sameOrigin(reel.media_url, session.apiBase))
		return <Message text="This reel is still being prepared." />;
	return (
		<Playback
			reel={reel}
			mediaUrl={reel.media_url}
			captions={showCaptions ? captions : null}
			onToggleCaptions={() => setShowCaptions((v) => !v)}
			onSave={async () => {
				const updated = await controller.lease()?.run((api) => api.setSaved(id, !reel.saved));
				if (updated) setReel({ ...reel, saved: updated.saved, expires_at: updated.expires_at });
			}}
			onDelete={() =>
				Alert.alert('Delete this reel?', 'The video is removed from your laptop.', [
					{ text: 'Cancel', style: 'cancel' },
					{
						text: 'Delete',
						style: 'destructive',
						onPress: () =>
							void controller
								.lease()
								?.run((api) => api.deleteReel(id))
								.then(() => router.back()),
					},
				])
			}
		/>
	);
}

function Playback({
	reel,
	mediaUrl,
	captions,
	onToggleCaptions,
	onSave,
	onDelete,
}: {
	reel: ReelDetail;
	mediaUrl: string;
	captions: CaptionDoc | null;
	onToggleCaptions: () => void;
	onSave: () => Promise<void>;
	onDelete: () => void;
}) {
	const { height } = useWindowDimensions();
	const player = useVideoPlayer(mediaUrl, (p) => {
		p.timeUpdateEventInterval = 0.25;
		p.loop = false;
		p.play();
	});
	const time = useEvent(player, 'timeUpdate', START);
	const now = time?.currentTime ?? 0;
	const scenes = reel.scenes;
	const playing = sceneAt(scenes, now);
	const pager = useRef<FlatList<SceneSummary>>(null);
	const dragging = useRef(false);

	useEffect(() => {
		if (!dragging.current && scenes.length > 0)
			pager.current?.scrollToIndex({ index: playing, animated: true });
	}, [playing, scenes.length]);

	const onViewable = useRef(({ viewableItems }: { viewableItems: ViewToken[] }) => {
		const first = viewableItems[0];
		if (dragging.current && first?.index != null) {
			player.currentTime = seekTarget(scenes, first.index);
			dragging.current = false;
		}
	}).current;
	const caption = useMemo(() => captionAt(captions, now), [captions, now]);

	return (
		<View style={styles.root}>
			<VideoView player={player} style={StyleSheet.absoluteFill} contentFit="cover" />
			<FlatList
				ref={pager}
				data={scenes}
				keyExtractor={(s) => String(s.index)}
				pagingEnabled
				showsVerticalScrollIndicator={false}
				getItemLayout={(_, index) => ({ length: height, offset: height * index, index })}
				onScrollBeginDrag={() => {
					dragging.current = true;
				}}
				onViewableItemsChanged={onViewable}
				viewabilityConfig={{ itemVisiblePercentThreshold: 60 }}
				style={StyleSheet.absoluteFill}
				renderItem={({ item }) => (
					<View style={{ height, justifyContent: 'flex-end', padding: 24, paddingBottom: 140 }}>
						{item.url ? (
							<Pressable
								accessibilityRole="link"
								accessibilityLabel={`Read the story on ${item.source ?? 'the source site'}`}
								onPress={() => void Linking.openURL(item.url ?? '')}
								style={styles.sourceChip}
							>
								<Text style={styles.chipText}>Read on {item.source ?? 'source'} ↗</Text>
							</Pressable>
						) : null}
					</View>
				)}
			/>
			{caption ? (
				<View pointerEvents="none" style={styles.caption}>
					<Text style={styles.captionText}>
						{caption.words.map((w, i) => (
							<Text
								// biome-ignore lint/suspicious/noArrayIndexKey: words repeat within a line
								key={`${w.s}-${i}`}
								style={i === caption.active ? styles.activeWord : undefined}
							>
								{i > 0 ? ' ' : ''}
								{w.w}
							</Text>
						))}
					</Text>
				</View>
			) : null}
			<View style={styles.toolbar}>
				<ToolButton label="CC" onPress={onToggleCaptions} />
				<ToolButton label={reel.saved ? 'Saved ✓' : 'Save'} onPress={() => void onSave()} />
				<ToolButton label="Delete" onPress={onDelete} />
			</View>
		</View>
	);
}

function ToolButton({ label, onPress }: { label: string; onPress: () => void }) {
	return (
		<Pressable accessibilityRole="button" onPress={onPress} style={styles.tool}>
			<Text style={styles.chipText}>{label}</Text>
		</Pressable>
	);
}

function Message({ text, spinner }: { text: string; spinner?: boolean }) {
	return (
		<View style={[styles.root, styles.center]}>
			{spinner ? <ActivityIndicator color="#fff" /> : null}
			<Text style={styles.chipText}>{text}</Text>
		</View>
	);
}

const styles = StyleSheet.create({
	root: { flex: 1, backgroundColor: '#000' },
	center: { alignItems: 'center', justifyContent: 'center', gap: 12, padding: 24 },
	sourceChip: {
		alignSelf: 'flex-start',
		backgroundColor: 'rgba(0,0,0,0.6)',
		paddingHorizontal: 14,
		paddingVertical: 8,
		borderRadius: 999,
	},
	chipText: { color: '#fff', fontSize: 15, fontWeight: '600' },
	caption: {
		position: 'absolute',
		left: 20,
		right: 20,
		bottom: 200,
		backgroundColor: 'rgba(0,0,0,0.55)',
		borderRadius: 14,
		padding: 12,
	},
	captionText: { color: '#fff', fontSize: 20, fontWeight: '500', textAlign: 'center' },
	activeWord: { color: '#FFD700', fontWeight: '700' },
	toolbar: {
		position: 'absolute',
		top: 60,
		right: 16,
		gap: 10,
	},
	tool: {
		backgroundColor: 'rgba(0,0,0,0.6)',
		paddingHorizontal: 14,
		paddingVertical: 8,
		borderRadius: 999,
		alignItems: 'center',
	},
});
