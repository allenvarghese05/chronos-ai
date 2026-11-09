/**
 * Pure helpers for scene layout and motion (T4.4). Kept free of React so vitest can cover them.
 */

import type { CaptionWord } from '../schema';
import type { CaptionLine } from './captions';

/** Index of the line being spoken at `timeSec` (the last line whose first word has started). */
export function activeLineIndex(lines: CaptionLine[], timeSec: number): number {
	let active = 0;
	for (let i = 0; i < lines.length; i += 1) {
		const first = lines[i]?.words[0];
		if (first !== undefined && first.s <= timeSec) active = i;
	}
	return active;
}

/** True while `word` is being spoken (inclusive start, exclusive end). */
export function isWordActive(word: CaptionWord, timeSec: number): boolean {
	return word.s <= timeSec && timeSec < word.e;
}

/**
 * Headline font size that keeps long titles within three lines at 1080 px wide.
 * Never below the 56 px legibility floor (docs/07 §6 requires ≥ 64 px for normal headlines;
 * only very long titles step down).
 */
export function headlineSize(title: string, base = 72): number {
	const n = title.length;
	if (n <= 32) return base;
	if (n <= 48) return Math.max(64, base - 8);
	return 56;
}

/** Opacity for a fade-in / fade-out of `fadeFrames` at both ends of a scene. */
export function fadeOpacity(frame: number, durationInFrames: number, fadeFrames = 12): number {
	if (durationInFrames <= 0) return 0;
	const fade = Math.max(1, Math.min(fadeFrames, Math.floor(durationInFrames / 3)));
	const fadeIn = Math.min(1, Math.max(0, frame / fade));
	const fadeOut = Math.min(1, Math.max(0, (durationInFrames - frame) / fade));
	return Math.min(fadeIn, fadeOut);
}

/** Ken Burns zoom from 1.0 to `maxScale` across the scene (slow, linear). */
export function kenBurnsScale(frame: number, durationInFrames: number, maxScale = 1.08): number {
	if (durationInFrames <= 0) return 1;
	const t = Math.min(1, Math.max(0, frame / durationInFrames));
	return 1 + (maxScale - 1) * t;
}
