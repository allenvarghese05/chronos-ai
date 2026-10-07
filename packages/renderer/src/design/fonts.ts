/**
 * Self-hosted font loading for the Morning Reel.
 *
 * Fonts are committed under public/fonts/ (OFL licensed, downloaded from
 * google/fonts GitHub at the pinned commit SHA below). Both are variable fonts
 * so a single file covers all needed weights without network calls at render time.
 *
 * Sources (pinned git commit):
 *   Inter Tight  — google/fonts @ 0b58fb370093f9a9f4ff785d94405710b79de67c
 *     ofl/intertight/InterTight[wght].ttf
 *   JetBrains Mono — google/fonts @ 6e4b84c976cadb3c49a40fd9a1c203e4f7fcf2da
 *     ofl/jetbrainsmono/JetBrainsMono[wght].ttf
 *
 * Usage:
 *   Call `await waitForFonts()` in your composition's `calculateMetadata` or
 *   at the top of `Root.tsx` (outside any component) so fonts are ready before
 *   the first frame is rendered.
 */

import { loadFont } from '@remotion/fonts';
import { staticFile } from 'remotion';

/** Promise-based guard — resolved once both font faces have been injected. */
let fontsReady: Promise<void> | null = null;

/**
 * Loads Inter Tight (weights 100–900) and JetBrains Mono (weights 100–800)
 * from the self-hosted variable TTF files in public/fonts/.
 *
 * Idempotent — multiple calls return the same Promise.
 */
export function waitForFonts(): Promise<void> {
	if (fontsReady) return fontsReady;

	fontsReady = Promise.all([
		// Inter Tight — used for headings (700) and body (500)
		loadFont({
			family: 'Inter Tight',
			url: staticFile('fonts/InterTight-variable.ttf'), // staticFile: relative to public/
			weight: '100 900',
			style: 'normal',
			format: 'truetype',
		}),
		// JetBrains Mono — used for the kicker chip (500)
		loadFont({
			family: 'JetBrains Mono',
			url: staticFile('fonts/JetBrainsMono-variable.ttf'), // staticFile: relative to public/
			weight: '100 800',
			style: 'normal',
			format: 'truetype',
		}),
	]).then(() => undefined);

	return fontsReady;
}
