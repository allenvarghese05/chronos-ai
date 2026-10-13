/**
 * MorningReel — 1080×1920 @ 30 fps Remotion composition.
 *
 * Duration is derived from `props.audio.durationSec` via `calculateMetadata`
 * so the render length always matches the audio: no manual frame count needed.
 *
 * Scene routing:
 *   "cover"  → CoverScene  (working in T0.5)
 *   "story"  → StoryScene  (stub; implemented in T4.4)
 *   "outro"  → OutroScene  (stub; implemented in T4.4)
 *
 * Each scene is wrapped in a `<Sequence>` whose `from` and `durationInFrames`
 * are computed by `sceneFrames(scene.startSec, scene.endSec, fps)`.
 */

import { useMemo } from 'react';
import type { CalculateMetadataFunction } from 'remotion';
import { Audio, Sequence, staticFile, useVideoConfig } from 'remotion';
import { waitForFonts } from '../design/fonts';
import { COMPOSITION } from '../design/tokens';
import { sceneFrames } from '../lib/timing';
import { CoverScene } from '../scenes/CoverScene';
import { OutroScene } from '../scenes/OutroScene';
import { StoryScene } from '../scenes/StoryScene';
import type { ReelProps, SceneProps } from '../schema';

// ─── calculateMetadata ────────────────────────────────────────────────────────

/**
 * Derive `durationInFrames` from `props.audio.durationSec`.
 *
 * Formula: `ceil(durationSec * fps) + tailFrames`
 *
 * `tailFrames` (15 at 30 fps = 0.5 s) ensures the last audio sample is not
 * clipped by the renderer even with minor floating-point drift.
 */
export const morningReelMetadata: CalculateMetadataFunction<ReelProps> = async ({
	props,
	defaultProps: _defaultProps,
}) => {
	await waitForFonts();

	const fps = COMPOSITION.fps;
	const durationInFrames = Math.ceil(props.audio.durationSec * fps) + COMPOSITION.tailFrames;

	return {
		fps,
		width: COMPOSITION.width,
		height: COMPOSITION.height,
		durationInFrames,
		props,
	};
};

// ─── MorningReel component ────────────────────────────────────────────────────

export function MorningReel(props: ReelProps) {
	const { fps } = useVideoConfig();

	// Trigger font loading from component scope so loadFont's own delayRender
	// is registered in the React render pass (not only in calculateMetadata).
	// waitForFonts() is idempotent — returns the same Promise on subsequent calls.
	useMemo(() => {
		void waitForFonts();
	}, []);

	return (
		<>
			{/* Background audio track — spans the full composition. */}
			<Audio src={staticFile(props.audio.src)} />

			{/* Scene sequences */}
			{props.scenes.map((scene: SceneProps) => {
				const { startFrame, durationInFrames } = sceneFrames(scene.startSec, scene.endSec, fps);

				if (durationInFrames === 0) return null;

				return (
					<Sequence
						key={scene.index}
						from={startFrame}
						durationInFrames={durationInFrames}
						name={`${scene.type}-${scene.index}`}
					>
						<SceneRouter scene={scene} reelProps={props} />
					</Sequence>
				);
			})}
		</>
	);
}

// ─── Scene router ─────────────────────────────────────────────────────────────

function SceneRouter({ scene, reelProps }: { scene: SceneProps; reelProps: ReelProps }) {
	switch (scene.type) {
		case 'cover':
			return (
				<CoverScene
					scene={scene}
					theme={reelProps.theme}
					greeting={reelProps.greeting}
					localDate={reelProps.localDate}
				/>
			);
		case 'story':
			return <StoryScene scene={scene} theme={reelProps.theme} />;
		case 'outro':
			return (
				<OutroScene scene={scene} theme={reelProps.theme} attribution={reelProps.attribution} />
			);
	}
}
