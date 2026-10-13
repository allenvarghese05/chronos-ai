/**
 * WordCaptions — STUB (T0.5).
 *
 * Renders a visible placeholder in the lower-third area. Full implementation
 * in T4.4: karaoke-style word highlighting, 3–5 words per line, active-word
 * accent colour, positioned above the bottom safe area.
 *
 * The stub renders a semi-transparent bar so smoke renders confirm the layer
 * is mounted even when captions are empty.
 */

import { AbsoluteFill } from 'remotion';
import {
	FONT_FAMILY,
	FONT_SIZE,
	SAFE_BOTTOM,
	SAFE_SIDE,
	THEMES,
	type Theme,
} from '../design/tokens';
import type { CaptionWord } from '../schema';

interface Props {
	/** Caption words for the current scene. May be empty. */
	words: CaptionWord[];
	theme: Theme;
}

/** Vertical height of the caption bar (placeholder). */
const CAPTION_BAR_HEIGHT = 120;

export function WordCaptions({ words, theme }: Props) {
	const palette = THEMES[theme];

	return (
		<AbsoluteFill style={{ pointerEvents: 'none' }}>
			{/* Caption bar — always rendered so smoke shows the layer exists. */}
			<div
				style={{
					position: 'absolute',
					bottom: SAFE_BOTTOM,
					left: SAFE_SIDE,
					right: SAFE_SIDE,
					height: CAPTION_BAR_HEIGHT,
					backgroundColor: palette.scrim,
					display: 'flex',
					alignItems: 'center',
					justifyContent: 'center',
					borderRadius: 12,
				}}
			>
				{words.length === 0 ? (
					<span
						style={{
							fontFamily: FONT_FAMILY.mono,
							fontSize: FONT_SIZE.kicker,
							color: palette.kicker,
							opacity: 0.5,
						}}
					>
						captions — T4.4
					</span>
				) : (
					<span
						style={{
							fontFamily: FONT_FAMILY.sans,
							fontSize: FONT_SIZE.caption,
							fontWeight: 500,
							color: palette.text,
							textAlign: 'center',
						}}
					>
						{words.map((w) => w.w).join(' ')}
					</span>
				)}
			</div>
		</AbsoluteFill>
	);
}
