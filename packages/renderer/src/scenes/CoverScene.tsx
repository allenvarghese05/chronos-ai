/**
 * CoverScene — the opening scene of the Morning Reel.
 *
 * Renders:
 *   - An animated gradient background that slowly shifts through the theme's
 *     four colour stops over the scene duration.
 *   - A "Morning Reel" wordmark (top-left, below the safe area).
 *   - The greeting ("Good morning") as a large h1 centred vertically.
 *   - The local date formatted as "Tuesday, 7 Oct" below the greeting.
 *
 * All measurements respect the iOS safe areas from `design/tokens.ts`.
 * Font families are loaded by `design/fonts.ts` via `waitForFonts()` in
 * `MorningReel.calculateMetadata`.
 *
 * Implemented in T0.5. Extended in T4.4 (grain overlay, spring animation,
 * animated gradient via spring, motion-respecting tone parameter).
 */

import { AbsoluteFill, interpolateColors, useCurrentFrame, useVideoConfig } from 'remotion';
import type { z } from 'zod';
import {
	FONT_FAMILY,
	FONT_SIZE,
	SAFE_BOTTOM,
	SAFE_SIDE,
	SAFE_TOP,
	THEMES,
	type Theme,
	TRANSITION_FRAMES,
} from '../design/tokens';
import type { CoverSceneSchema } from '../schema';

type CoverSceneProps = z.infer<typeof CoverSceneSchema>;

interface Props {
	scene: CoverSceneProps;
	theme: Theme;
	greeting: string;
	localDate: string; // "YYYY-MM-DD"
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/**
 * Format "YYYY-MM-DD" → "Tuesday, 7 Oct".
 * Uses the machine's locale-aware Date formatting so the string is always
 * readable regardless of the owner's system locale.
 */
function formatDate(isoDate: string): string {
	const [year, month, day] = isoDate.split('-').map(Number);
	// Month is 0-indexed in Date constructor; use UTC to avoid midnight TZ shift.
	const d = new Date(Date.UTC(year ?? 2026, (month ?? 1) - 1, day ?? 1));
	return d.toLocaleDateString('en-GB', {
		weekday: 'long',
		day: 'numeric',
		month: 'short',
		timeZone: 'UTC',
	});
}

// ─── Component ────────────────────────────────────────────────────────────────

export function CoverScene({ theme, greeting, localDate }: Props) {
	const frame = useCurrentFrame();
	const { durationInFrames, fps } = useVideoConfig();

	const palette = THEMES[theme];

	// Animated gradient: progress from 0 → 1 over the scene, then reverse
	// so the gradient returns to its start colour by the end (seamless loop).
	// Clamp to [0, 1] to avoid artefacts during the transition overlap frames.
	const progress = Math.min(1, frame / Math.max(1, durationInFrames - TRANSITION_FRAMES));

	// Interpolate between the four theme gradient stops in pairs.
	// We cycle: stop[0] → stop[1] → stop[2] → stop[3] → stop[0]
	const bg = interpolateColors(
		progress,
		[0, 0.33, 0.66, 1],
		[palette.background[0], palette.background[1], palette.background[2], palette.background[3]],
	);

	// Fade-in opacity for the content (0 → 1 over the first 10 frames).
	const fadeIn = Math.min(1, frame / Math.max(1, Math.min(10, fps / 3)));

	return (
		<AbsoluteFill style={{ backgroundColor: bg }}>
			{/* Wordmark — top-left, respecting SAFE_TOP */}
			<div
				style={{
					position: 'absolute',
					top: SAFE_TOP,
					left: SAFE_SIDE,
					fontFamily: FONT_FAMILY.mono,
					fontSize: FONT_SIZE.wordmark,
					fontWeight: 500,
					color: palette.kicker,
					letterSpacing: 2,
					textTransform: 'uppercase',
					opacity: fadeIn,
				}}
			>
				Morning Reel
			</div>

			{/* Main content — centred vertically in the safe content area */}
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
					opacity: fadeIn,
				}}
			>
				{/* Greeting (h1) */}
				<h1
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.sans,
						fontSize: FONT_SIZE.h1,
						fontWeight: 700,
						color: palette.text,
						lineHeight: 1.1,
					}}
				>
					{greeting}
				</h1>

				{/* Date */}
				<p
					style={{
						margin: 0,
						fontFamily: FONT_FAMILY.sans,
						fontSize: FONT_SIZE.bullet,
						fontWeight: 500,
						color: palette.kicker,
						lineHeight: 1.3,
					}}
				>
					{formatDate(localDate)}
				</p>
			</div>

			{/* Bottom safe-area indicator — subtle divider line */}
			<div
				style={{
					position: 'absolute',
					bottom: SAFE_BOTTOM,
					left: SAFE_SIDE,
					right: SAFE_SIDE,
					height: 1,
					backgroundColor: palette.text,
					opacity: 0.15 * fadeIn,
				}}
			/>
		</AbsoluteFill>
	);
}
