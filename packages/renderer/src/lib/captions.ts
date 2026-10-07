/**
 * Caption line-breaking for karaoke-style WordCaptions.
 *
 * Groups an array of `CaptionWord` objects into display lines of
 * `minPerLine`..`maxPerLine` words. Enforces the no-orphan rule:
 * the last line must have at least `minPerLine` words; if it would
 * fall short, its words are merged into the preceding line.
 *
 * Edge cases:
 *   - Empty input    → []
 *   - 1–2 words      → single line (fewer than minPerLine; no split)
 *   - Orphan at end  → merged into the previous line (line can exceed maxPerLine)
 */

import type { CaptionWord } from '../schema.js';

export interface CaptionLine {
	words: CaptionWord[];
}

export interface LayoutOptions {
	minPerLine: number;
	maxPerLine: number;
}

/**
 * layoutCaptions — group CaptionWords into display lines.
 *
 * @param words      - Ordered array of caption words (from `ReelProps.captions[sceneIndex]`).
 * @param options    - `{ minPerLine = 3, maxPerLine = 5 }` per docs/07 §4.
 * @returns          - Array of `CaptionLine` objects; never contains an orphan last line
 *                     with fewer than `minPerLine` words (unless the entire input is shorter).
 */
export function layoutCaptions(
	words: CaptionWord[],
	{ minPerLine = 3, maxPerLine = 5 }: Partial<LayoutOptions> = {},
): CaptionLine[] {
	if (words.length === 0) return [];

	// Total words ≤ minPerLine → one line, no split possible.
	if (words.length <= minPerLine) return [{ words: [...words] }];

	const lines: CaptionLine[] = [];
	let i = 0;

	while (i < words.length) {
		const remaining = words.length - i;

		if (remaining <= maxPerLine) {
			// Last chunk: check for orphan.
			const prev = lines.at(-1);
			if (remaining < minPerLine && prev !== undefined) {
				// Merge into the previous line to avoid an orphan.
				prev.words = [...prev.words, ...words.slice(i)];
			} else {
				lines.push({ words: words.slice(i) });
			}
			break;
		}

		// Full chunk — take exactly maxPerLine words.
		lines.push({ words: words.slice(i, i + maxPerLine) });
		i += maxPerLine;
	}

	return lines;
}
