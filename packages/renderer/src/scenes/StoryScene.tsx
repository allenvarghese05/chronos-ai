/**
 * StoryScene (T4.4): media layer (Ken Burns image, muted b-roll, or animated gradient), a
 * scrim for contrast, kicker chip, fitted headline (≤ 3 lines), bullets staggering in, and a
 * source badge. Captions are drawn by MorningReel on top of every scene.
 */

import {
	AbsoluteFill,
	Img,
	OffthreadVideo,
	spring,
	staticFile,
	useCurrentFrame,
	useVideoConfig,
} from 'remotion';
import type { z } from 'zod';
import {
	FONT_FAMILY,
	FONT_SIZE,
	SAFE_BOTTOM,
	SAFE_SIDE,
	SAFE_TOP,
	SPRING_TEXT,
	THEMES,
	type Theme,
} from '../design/tokens';
import { fadeOpacity, headlineSize, kenBurnsScale } from '../lib/scene';
import type { StorySceneSchema } from '../schema';

type StorySceneProps = z.infer<typeof StorySceneSchema>;

interface Props {
	scene: StorySceneProps;
	theme: Theme;
}

function MediaLayer({ scene, theme }: Props) {
	const frame = useCurrentFrame();
	const { durationInFrames } = useVideoConfig();
	const palette = THEMES[theme];
	const media = scene.media;
	if (media?.type === 'image' && media.url) {
		return (
			<AbsoluteFill style={{ overflow: 'hidden' }}>
				<Img
					src={staticFile(media.url)}
					style={{
						width: '100%',
						height: '100%',
						objectFit: 'cover',
						transform: `scale(${kenBurnsScale(frame, durationInFrames)})`,
					}}
				/>
			</AbsoluteFill>
		);
	}
	if (media?.type === 'video' && media.url) {
		return (
			<AbsoluteFill>
				<OffthreadVideo
					src={staticFile(media.url)}
					muted
					style={{ width: '100%', height: '100%', objectFit: 'cover' }}
				/>
			</AbsoluteFill>
		);
	}
	const [a, b, c] = palette.background;
	const angle = 160 + (frame / Math.max(durationInFrames, 1)) * 40;
	return (
		<AbsoluteFill style={{ background: `linear-gradient(${angle}deg, ${a}, ${b} 55%, ${c})` }} />
	);
}

export function StoryScene({ scene, theme }: Props) {
	const frame = useCurrentFrame();
	const { fps, durationInFrames } = useVideoConfig();
	const palette = THEMES[theme];
	const title = scene.title ?? '';
	const bullets = scene.body ?? [];
	const enter = spring({ frame, fps, config: SPRING_TEXT });

	return (
		<AbsoluteFill style={{ opacity: fadeOpacity(frame, durationInFrames) }}>
			<MediaLayer scene={scene} theme={theme} />
			<AbsoluteFill
				style={{
					background: `linear-gradient(180deg, ${palette.scrim} 0%, rgba(0,0,0,0) 35%, ${palette.scrim} 100%)`,
				}}
			/>
			<div
				style={{
					position: 'absolute',
					top: SAFE_TOP,
					left: SAFE_SIDE,
					right: SAFE_SIDE,
					display: 'flex',
					flexDirection: 'column',
					gap: 24,
					transform: `translateY(${(1 - enter) * 40}px)`,
					opacity: enter,
				}}
			>
				{scene.kicker !== undefined && (
					<span
						style={{
							alignSelf: 'flex-start',
							padding: '8px 16px',
							borderRadius: 999,
							border: `2px solid ${palette.accent}`,
							backgroundColor: palette.surface,
							fontFamily: FONT_FAMILY.mono,
							fontSize: FONT_SIZE.kicker + 4,
							fontWeight: 500,
							letterSpacing: 2,
							color: palette.accent,
							textTransform: 'uppercase',
						}}
					>
						{scene.kicker}
					</span>
				)}
				<h2
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.sans,
						fontSize: headlineSize(title),
						fontWeight: 700,
						lineHeight: 1.12,
						color: palette.text,
						display: '-webkit-box',
						WebkitLineClamp: 3,
						WebkitBoxOrient: 'vertical',
						overflow: 'hidden',
						textShadow: '0 2px 12px rgba(0,0,0,0.35)',
					}}
				>
					{title}
				</h2>
				<ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'grid', gap: 12 }}>
					{bullets.map((bullet, i) => {
						const s = spring({ frame: frame - 10 - i * 8, fps, config: SPRING_TEXT });
						return (
							<li
								key={bullet}
								style={{
									fontFamily: FONT_FAMILY.sans,
									fontSize: FONT_SIZE.bullet,
									fontWeight: 500,
									color: palette.text,
									opacity: s,
									transform: `translateX(${(1 - s) * 24}px)`,
									textShadow: '0 1px 8px rgba(0,0,0,0.35)',
								}}
							>
								— {bullet}
							</li>
						);
					})}
				</ul>
			</div>
			{scene.source !== undefined && (
				<div
					style={{
						position: 'absolute',
						bottom: SAFE_BOTTOM + 170,
						left: SAFE_SIDE,
						fontFamily: FONT_FAMILY.mono,
						fontSize: FONT_SIZE.kicker,
						color: palette.kicker,
						letterSpacing: 1,
					}}
				>
					via {scene.source.name}
				</div>
			)}
		</AbsoluteFill>
	);
}
