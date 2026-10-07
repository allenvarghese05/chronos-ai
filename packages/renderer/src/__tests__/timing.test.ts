import { describe, expect, it } from 'vitest';
import { sceneFrames } from '../lib/timing.js';

/**
 * sceneFrames(startSec, endSec, fps) tests.
 * All expected values are derived manually — no snapshots.
 *
 * Rounding contract:
 *   startFrame      = Math.round(startSec * fps)
 *   endFrame        = Math.round(endSec   * fps)
 *   durationInFrames = Math.max(0, endFrame − startFrame)
 */

describe('sceneFrames — basic cases at 30 fps', () => {
	it('scene [0, 1] → startFrame=0, durationInFrames=30', () => {
		expect(sceneFrames(0, 1, 30)).toEqual({ startFrame: 0, durationInFrames: 30 });
	});

	it('scene [1, 2] → startFrame=30, durationInFrames=30', () => {
		expect(sceneFrames(1, 2, 30)).toEqual({ startFrame: 30, durationInFrames: 30 });
	});

	it('scene [0, 3] → startFrame=0, durationInFrames=90 (fixture cover scene)', () => {
		expect(sceneFrames(0, 3, 30)).toEqual({ startFrame: 0, durationInFrames: 90 });
	});
});

describe('sceneFrames — zero duration', () => {
	it('scene [0, 0] → durationInFrames=0 (zero-duration, safe to pass to <Sequence>)', () => {
		expect(sceneFrames(0, 0, 30)).toEqual({ startFrame: 0, durationInFrames: 0 });
	});

	it('scene [2, 2] → startFrame=60, durationInFrames=0', () => {
		expect(sceneFrames(2, 2, 30)).toEqual({ startFrame: 60, durationInFrames: 0 });
	});
});

describe('sceneFrames — fractional seconds and rounding', () => {
	it('scene [0, 0.5] at 30 fps → durationInFrames=15', () => {
		expect(sceneFrames(0, 0.5, 30)).toEqual({ startFrame: 0, durationInFrames: 15 });
	});

	it('scene [0, 1/3] at 30 fps → durationInFrames=10 (Math.round(10)=10)', () => {
		// 1/3 * 30 = 10.0 exactly
		expect(sceneFrames(0, 1 / 3, 30)).toEqual({ startFrame: 0, durationInFrames: 10 });
	});

	it('scene [0, 0.1] at 30 fps → durationInFrames=3 (Math.round(3.0)=3)', () => {
		expect(sceneFrames(0, 0.1, 30)).toEqual({ startFrame: 0, durationInFrames: 3 });
	});

	it('scene [0.1, 0.2] at 30 fps → startFrame=3, durationInFrames=3', () => {
		// 0.1*30=3, 0.2*30=6 → dur=3
		expect(sceneFrames(0.1, 0.2, 30)).toEqual({ startFrame: 3, durationInFrames: 3 });
	});

	it('scene [0, 0.016667] at 30 fps → durationInFrames=1 (single frame)', () => {
		// 0.016667 * 30 ≈ 0.5001 → Math.round = 1
		expect(sceneFrames(0, 0.016_667, 30)).toEqual({ startFrame: 0, durationInFrames: 1 });
	});
});

describe('sceneFrames — non-standard fps', () => {
	it('scene [0, 1] at 24 fps → durationInFrames=24', () => {
		expect(sceneFrames(0, 1, 24)).toEqual({ startFrame: 0, durationInFrames: 24 });
	});

	it('scene [0, 60] at 30 fps → durationInFrames=1800 (1-min reel)', () => {
		expect(sceneFrames(0, 60, 30)).toEqual({ startFrame: 0, durationInFrames: 1800 });
	});
});
