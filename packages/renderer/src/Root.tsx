/**
 * Root.tsx — registers all Remotion compositions.
 *
 * Additional compositions (e.g. individual-scene previews) can be added here
 * in future tasks. Keep this file lean: it only calls registerRoot and declares
 * the <Composition> entries. All logic lives in the composition files.
 */

import { Composition } from 'remotion';
import { MorningReel, morningReelMetadata } from './compositions/MorningReel';
import { COMPOSITION } from './design/tokens';
import { ReelPropsSchema } from './schema';

export function RemotionRoot() {
	return (
		<Composition
			id="MorningReel"
			component={MorningReel}
			// calculateMetadata derives durationInFrames from props.audio.durationSec.
			// The values below are fallbacks for when Remotion Studio opens the composition
			// without props (e.g. before you click "Calculate metadata").
			durationInFrames={COMPOSITION.fps * 3 + COMPOSITION.tailFrames} // 3 s default
			fps={COMPOSITION.fps}
			width={COMPOSITION.width}
			height={COMPOSITION.height}
			calculateMetadata={morningReelMetadata}
			schema={ReelPropsSchema}
			defaultProps={{
				localDate: '2026-10-06',
				greeting: 'Good morning',
				tone: 'calm' as const,
				audio: { src: 'audio/silence-3s.wav', durationSec: 3 },
				scenes: [{ index: 0, type: 'cover' as const, startSec: 0, endSec: 3 }],
				captions: [],
				theme: 'dawn' as const,
				attribution: [],
			}}
		/>
	);
}
