/**
 * StoryScene — STUB (T0.5).
 *
 * Renders a visible placeholder so smoke renders of multi-scene fixtures show
 * something at each story slot. Full implementation in T4.4: kicker chip,
 * headline (fitted type, max 3 lines), bullets stagger-in, media layer (Ken Burns
 * image, b-roll video, or gradient), source badge.
 */

import { AbsoluteFill } from 'remotion';
import type { z } from 'zod';
import {
	FONT_FAMILY,
	FONT_SIZE,
	SAFE_BOTTOM,
	SAFE_SIDE,
	SAFE_TOP,
	THEMES,
	type Theme,
} from '../design/tokens.js';
import type { StorySceneSchema } from '../schema.js';

type StorySceneProps = z.infer<typeof StorySceneSchema>;

interface Props {
	scene: StorySceneProps;
	theme: Theme;
}

export function StoryScene({ scene, theme }: Props) {
	const palette = THEMES[theme];

	return (
		<AbsoluteFill style={{ backgroundColor: palette.background[1] }}>
			<div
				style={{
					position: 'absolute',
					top: SAFE_TOP,
					bottom: SAFE_BOTTOM,
					left: SAFE_SIDE,
					right: SAFE_SIDE,
					display: 'flex',
					flexDirection: 'column',
					justifyContent: 'center',
					alignItems: 'flex-start',
					gap: 16,
				}}
			>
				<p
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.mono,
						fontSize: FONT_SIZE.kicker,
						fontWeight: 500,
						color: palette.accent,
						textTransform: 'uppercase',
						letterSpacing: 2,
					}}
				>
					Story {scene.index + 1} — coming in T4.4
				</p>
				{scene.title !== undefined && (
					<h2
						style={{
							margin: 0,
							fontFamily: FONT_FAMILY.sans,
							fontSize: FONT_SIZE.headline,
							fontWeight: 700,
							color: palette.text,
							lineHeight: 1.2,
						}}
					>
						{scene.title}
					</h2>
				)}
			</div>
		</AbsoluteFill>
	);
}
