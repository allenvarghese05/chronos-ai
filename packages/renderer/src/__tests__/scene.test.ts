import { describe, expect, it } from 'vitest';
import { layoutCaptions } from '../lib/captions';
import {
	activeLineIndex,
	fadeOpacity,
	headlineSize,
	isWordActive,
	kenBurnsScale,
} from '../lib/scene';
import type { CaptionWord } from '../schema';

const w = (word: string, s: number, e: number): CaptionWord => ({ w: word, s, e });

describe('activeLineIndex', () => {
	const words = Array.from({ length: 12 }, (_, i) => w(`w${i}`, i, i + 1));
	const lines = layoutCaptions(words);
	it('starts on the first line, even before the first word', () => {
		expect(activeLineIndex(lines, -1)).toBe(0);
		expect(activeLineIndex(lines, 0.5)).toBe(0);
	});
	it('moves to the line whose first word has started', () => {
		expect(activeLineIndex(lines, 5)).toBe(1);
		expect(activeLineIndex(lines, 11.5)).toBe(lines.length - 1);
	});
	it('handles no lines', () => {
		expect(activeLineIndex([], 3)).toBe(0);
	});
});

describe('isWordActive', () => {
	it('is start-inclusive and end-exclusive', () => {
		expect(isWordActive(w('a', 1, 2), 1)).toBe(true);
		expect(isWordActive(w('a', 1, 2), 1.999)).toBe(true);
		expect(isWordActive(w('a', 1, 2), 2)).toBe(false);
	});
});

describe('headlineSize', () => {
	it('steps down for long titles but never below 56 px', () => {
		expect(headlineSize('Short title')).toBe(72);
		expect(headlineSize('x'.repeat(40))).toBe(64);
		expect(headlineSize('x'.repeat(60))).toBe(56);
	});
});

describe('fadeOpacity', () => {
	it('fades in and out over 12 frames', () => {
		expect(fadeOpacity(0, 120)).toBe(0);
		expect(fadeOpacity(6, 120)).toBeCloseTo(0.5);
		expect(fadeOpacity(60, 120)).toBe(1);
		expect(fadeOpacity(120, 120)).toBe(0);
	});
	it('shortens the fade for very short scenes and handles zero length', () => {
		expect(fadeOpacity(3, 9)).toBe(1);
		expect(fadeOpacity(0, 0)).toBe(0);
	});
});

describe('kenBurnsScale', () => {
	it('zooms linearly from 1 to 1.08', () => {
		expect(kenBurnsScale(0, 100)).toBe(1);
		expect(kenBurnsScale(50, 100)).toBeCloseTo(1.04);
		expect(kenBurnsScale(200, 100)).toBeCloseTo(1.08);
		expect(kenBurnsScale(5, 0)).toBe(1);
	});
});
