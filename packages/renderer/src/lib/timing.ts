/**
 * sceneFrames — convert a scene's [startSec, endSec] to Remotion's
 * [startFrame, durationInFrames] pair.
 *
 * ## Rounding contract
 *
 * ```
 * startFrame      = Math.round(startSec * fps)
 * endFrame        = Math.round(endSec   * fps)
 * durationInFrames = Math.max(0, endFrame − startFrame)
 * ```
 *
 * Rounding each boundary independently (not rounding the duration) avoids
 * cumulative drift over many scenes. A zero-duration scene (startSec === endSec,
 * or rounding makes them equal) returns `durationInFrames = 0`, which is safe
 * to pass to `<Sequence>` — it renders nothing.
 *
 * @param startSec - Scene start time in seconds (≥ 0).
 * @param endSec   - Scene end time in seconds (≥ startSec).
 * @param fps      - Frames per second of the composition (e.g. 30).
 * @returns `{ startFrame, durationInFrames }` ready for `<Sequence from durationInFrames>`.
 */
export function sceneFrames(
	startSec: number,
	endSec: number,
	fps: number,
): { startFrame: number; durationInFrames: number } {
	const startFrame = Math.round(startSec * fps);
	const endFrame = Math.round(endSec * fps);
	const durationInFrames = Math.max(0, endFrame - startFrame);
	return { startFrame, durationInFrames };
}
