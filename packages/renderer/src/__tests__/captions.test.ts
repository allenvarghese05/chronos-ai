import { describe, expect, it } from 'vitest';
import { layoutCaptions } from '../lib/captions.js';
import type { CaptionWord } from '../schema.js';

/**
 * layoutCaptions tests — explicit expected values, no snapshots.
 *
 * Rules under test:
 *   - minPerLine (default 3) ≤ words per line ≤ maxPerLine (default 5)
 *   - Last line must have ≥ minPerLine words; if not, merge into the previous line.
 *   - If total words ≤ minPerLine → single line regardless.
 *   - Empty input → []
 */

/** Make a minimal CaptionWord array from an array of word strings. */
function words(...ws: string[]): CaptionWord[] {
	return ws.map((w, i) => ({ w, s: i, e: i + 0.5 }));
}

/** Extract just the word text from lines for simpler assertions. */
function texts(lines: ReturnType<typeof layoutCaptions>): string[][] {
	return lines.map((l) => l.words.map((w) => w.w));
}

describe('layoutCaptions — empty and small inputs', () => {
	it('empty array → []', () => {
		expect(layoutCaptions([])).toEqual([]);
	});

	it('1 word → single line (< minPerLine, no split)', () => {
		expect(texts(layoutCaptions(words('Hello')))).toEqual([['Hello']]);
	});

	it('2 words → single line (< minPerLine, no split)', () => {
		expect(texts(layoutCaptions(words('Good', 'morning')))).toEqual([['Good', 'morning']]);
	});

	it('3 words → single line (exactly minPerLine, fits in one)', () => {
		expect(texts(layoutCaptions(words('a', 'b', 'c')))).toEqual([['a', 'b', 'c']]);
	});
});

describe('layoutCaptions — single full line', () => {
	it('4 words → single line (≤ maxPerLine)', () => {
		expect(texts(layoutCaptions(words('a', 'b', 'c', 'd')))).toEqual([['a', 'b', 'c', 'd']]);
	});

	it('5 words → single line (= maxPerLine)', () => {
		expect(texts(layoutCaptions(words('a', 'b', 'c', 'd', 'e')))).toEqual([
			['a', 'b', 'c', 'd', 'e'],
		]);
	});
});

describe('layoutCaptions — orphan prevention', () => {
	it('6 words: first line gets 5, last would be 1 (orphan) → merge into prev → [6]', () => {
		// 6 words: take 5 → remaining 1 < min(3) → merge into prev
		const result = texts(layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f')));
		expect(result).toEqual([['a', 'b', 'c', 'd', 'e', 'f']]);
	});

	it('7 words: first line 5, remaining 2 < min(3) → merge → [7]', () => {
		const result = texts(layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f', 'g')));
		expect(result).toEqual([['a', 'b', 'c', 'd', 'e', 'f', 'g']]);
	});

	it('11 words: [5, 5, 1] → orphan fix → [5, 6]', () => {
		const result = texts(
			layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k')),
		);
		expect(result).toEqual([
			['a', 'b', 'c', 'd', 'e'],
			['f', 'g', 'h', 'i', 'j', 'k'],
		]);
	});
});

describe('layoutCaptions — no orphan cases', () => {
	it('8 words: [5, 3] — remaining 3 = minPerLine, no merge', () => {
		const result = texts(layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h')));
		expect(result).toEqual([
			['a', 'b', 'c', 'd', 'e'],
			['f', 'g', 'h'],
		]);
	});

	it('10 words: [5, 5]', () => {
		const result = texts(layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j')));
		expect(result).toEqual([
			['a', 'b', 'c', 'd', 'e'],
			['f', 'g', 'h', 'i', 'j'],
		]);
	});

	it('15 words: [5, 5, 5]', () => {
		const ws = words('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o');
		const result = texts(layoutCaptions(ws));
		expect(result).toEqual([
			['a', 'b', 'c', 'd', 'e'],
			['f', 'g', 'h', 'i', 'j'],
			['k', 'l', 'm', 'n', 'o'],
		]);
	});
});

describe('layoutCaptions — custom options', () => {
	it('minPerLine=2, maxPerLine=3: 7 words → [3, 3, 1] → last 1 < min(2) → merge [3, 4]', () => {
		const result = texts(
			layoutCaptions(words('a', 'b', 'c', 'd', 'e', 'f', 'g'), {
				minPerLine: 2,
				maxPerLine: 3,
			}),
		);
		expect(result).toEqual([
			['a', 'b', 'c'],
			['d', 'e', 'f', 'g'],
		]);
	});

	it('minPerLine=1, maxPerLine=2: 5 words → [2, 2, 1] — last 1 ≥ min(1), no merge', () => {
		const result = texts(
			layoutCaptions(words('a', 'b', 'c', 'd', 'e'), { minPerLine: 1, maxPerLine: 2 }),
		);
		expect(result).toEqual([['a', 'b'], ['c', 'd'], ['e']]);
	});
});
