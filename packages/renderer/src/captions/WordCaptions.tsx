/**
 * WordCaptions (T4.4): karaoke captions in the lower third. Words are grouped into lines of
 * 3–5 (`layoutCaptions`); the line being spoken is shown and its active word is emphasised by
 * weight and colour, not scale (docs/07 §6). Word times are absolute seconds in the reel.
 */

import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import {
	FONT_FAMILY,
	FONT_SIZE,
	SAFE_BOTTOM,
	SAFE_SIDE,
	THEMES,
	type Theme,
} from '../design/tokens';
import { layoutCaptions } from '../lib/captions';
import { activeLineIndex, isWordActive } from '../lib/scene';
import type { CaptionWord } from '../schema';

interface Props {
	/** Caption words for the current scene. May be empty. */
	words: CaptionWord[];
	theme: Theme;
	/** Absolute start of the enclosing scene, in seconds (Sequence frames are relative). */
	sceneStartSec: number;
}

export function WordCaptions({ words, theme, sceneStartSec }: Props) {
	const frame = useCurrentFrame();
	const { fps } = useVideoConfig();
	const palette = THEMES[theme];
	if (words.length === 0) return null;
	const now = sceneStartSec + frame / fps;
	const lines = layoutCaptions(words);
	const line = lines[activeLineIndex(lines, now)];
	if (line === undefined) return null;

	return (
		<AbsoluteFill style={{ pointerEvents: 'none' }}>
			<div
				style={{
					position: 'absolute',
					bottom: SAFE_BOTTOM,
					left: SAFE_SIDE,
					right: SAFE_SIDE,
					padding: '18px 28px',
					borderRadius: 20,
					backgroundColor: palette.scrim,
					textAlign: 'center',
					fontFamily: FONT_FAMILY.sans,
					fontSize: FONT_SIZE.caption,
					lineHeight: 1.25,
				}}
			>
				{line.words.map((w, i) => (
					<span
						// biome-ignore lint/suspicious/noArrayIndexKey: words can repeat within a line
						key={`${w.s}-${i}`}
						style={{
							fontWeight: isWordActive(w, now) ? 700 : 500,
							color: isWordActive(w, now) ? palette.accent : palette.text,
						}}
					>
						{i > 0 ? ' ' : ''}
						{w.w}
					</span>
				))}
			</div>
		</AbsoluteFill>
	);
}
