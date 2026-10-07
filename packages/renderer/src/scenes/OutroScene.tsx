/**
 * OutroScene — STUB (T0.5).
 *
 * Renders a visible placeholder. Full implementation in T4.4: "Saved to your
 * feed · tap to read sources" heading, list of source names, attribution lines.
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
} from '../design/tokens';
import type { OutroSceneSchema } from '../schema';

type OutroSceneProps = z.infer<typeof OutroSceneSchema>;

interface Props {
	scene: OutroSceneProps;
	theme: Theme;
	attribution: string[];
}

export function OutroScene({ theme, attribution }: Props) {
	const palette = THEMES[theme];

	return (
		<AbsoluteFill style={{ backgroundColor: palette.background[3] }}>
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
				<h2
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.sans,
						fontSize: FONT_SIZE.headline,
						fontWeight: 700,
						color: palette.text,
					}}
				>
					Saved to your feed
				</h2>
				<p
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.sans,
						fontSize: FONT_SIZE.bullet,
						fontWeight: 500,
						color: palette.kicker,
					}}
				>
					Tap to read sources
				</p>
				{attribution.length > 0 && (
					<ul
						style={{
							margin: 0,
							padding: 0,
							listStyle: 'none',
							display: 'flex',
							flexDirection: 'column',
							gap: 8,
						}}
					>
						{attribution.map((line) => (
							<li
								key={line}
								style={{
									fontFamily: FONT_FAMILY.mono,
									fontSize: FONT_SIZE.kicker,
									color: palette.kicker,
								}}
							>
								{line}
							</li>
						))}
					</ul>
				)}
			</div>
		</AbsoluteFill>
	);
}
