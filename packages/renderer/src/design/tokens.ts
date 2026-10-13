/**
 * Design tokens for the Morning Reel.
 *
 * All visual values that can be tweaked come from here:
 * – Type scale (1.25 ratio, base 16 px)
 * – Three palettes: dawn / mono / ink
 * – 24 px grid
 * – iOS safe areas for the vertical player chrome
 * – Remotion spring configs
 *
 * Components import ONLY from this file; no hard-coded numbers in scene files.
 *
 * References: docs/07-media-pipeline.md §6
 */

import type { ReelProps } from '../schema';

// ─── Grid ─────────────────────────────────────────────────────────────────────

/** Base grid unit in px. All spacing should be a multiple of this. */
export const GRID = 24;

// ─── Safe areas (iOS vertical player chrome) ───────────────────────────────────

/**
 * Top safe area: status bar + iOS player chrome above content.
 * Content must not start until SAFE_TOP pixels from the top of the 1920 px frame.
 */
export const SAFE_TOP = 220;

/**
 * Bottom safe area: home indicator + gesture zone below content.
 * Content must end at least SAFE_BOTTOM pixels from the bottom of the 1920 px frame.
 */
export const SAFE_BOTTOM = 220;

/**
 * Side margin — applied symmetrically to left and right.
 */
export const SAFE_SIDE = 96;

// ─── Type scale ───────────────────────────────────────────────────────────────

/**
 * Modular scale ratio. Each step multiplies the previous size by this value.
 * Example at base 16: 16 → 20 → 25 → 31.25 → 39.06 → 48.83 → 61 → 76.
 */
export const TYPE_SCALE = 1.25;

/**
 * Named font sizes (px) used by scene components.
 *
 * Minimums from docs/07 §6:
 *   headline ≥ 64 px, bullets ≥ 40 px, captions ≥ 52 px
 */
export const FONT_SIZE = {
	/** "Morning Reel" wordmark on CoverScene. */
	wordmark: 24,
	/** Kicker chip above the headline in StoryScene. */
	kicker: 20,
	/** Caption karaoke words (active + inactive). */
	caption: 52,
	/** Story scene bullet points. */
	bullet: 40,
	/** Story scene headline. */
	headline: 64,
	/** Cover scene greeting (h1 treatment). */
	h1: 76,
} as const satisfies Record<string, number>;

// ─── Font families ────────────────────────────────────────────────────────────

export const FONT_FAMILY = {
	/** Main text — Inter Tight 500/700 (self-hosted). */
	sans: '"Inter Tight", sans-serif',
	/** Kicker chip, timestamps — JetBrains Mono 500 (self-hosted). */
	mono: '"JetBrains Mono", monospace',
} as const;

// ─── Colour palettes ──────────────────────────────────────────────────────────

export type Theme = ReelProps['theme'];

interface Palette {
	/** Array of 4 gradient stops for the animated background. */
	background: readonly [string, string, string, string];
	/** Semi-transparent surface for cards/chips. */
	surface: string;
	/** Primary text colour (must satisfy ≥ 4.5:1 contrast against surface). */
	text: string;
	/** Accent highlight — active caption word, kicker border. */
	accent: string;
	/** De-emphasised text (kicker label, attribution). */
	kicker: string;
	/** Scrim over media layers to ensure text readability. */
	scrim: string;
}

export const THEMES = {
	/** Warm sunrise gradient — default on weekdays. */
	dawn: {
		background: ['#FF6B35', '#F7931E', '#FFD700', '#FF8C42'],
		surface: 'rgba(0,0,0,0.35)',
		text: '#FFFFFF',
		accent: '#FFD700',
		kicker: '#FFE0B2',
		scrim: 'rgba(0,0,0,0.45)',
	},
	/** Near-black with a single cool accent — high-focus reading mode. */
	mono: {
		background: ['#0D0D0D', '#1A1A1A', '#262626', '#0D0D0D'],
		surface: 'rgba(255,255,255,0.06)',
		text: '#F5F5F5',
		accent: '#00E5FF',
		kicker: '#B0BEC5',
		scrim: 'rgba(0,0,0,0.55)',
	},
	/** Paper-white — default on weekends; high legibility in bright morning light. */
	ink: {
		background: ['#F5F0E8', '#EDE8E0', '#E8E3DB', '#F0EBE3'],
		surface: 'rgba(0,0,0,0.08)',
		text: '#1A1A1A',
		accent: '#1A237E',
		kicker: '#5C6BC0',
		scrim: 'rgba(255,255,255,0.45)',
	},
} as const satisfies Record<Theme, Palette>;

// ─── Remotion spring configs ──────────────────────────────────────────────────

/**
 * Default spring for text stagger-in (words, bullets).
 * damping 14 / mass 0.8 / stiffness 100 → snappy with minimal overshoot.
 */
export const SPRING_TEXT = {
	damping: 14,
	mass: 0.8,
	stiffness: 100,
} as const;

/**
 * Slower spring for media Ken Burns / scene entrance.
 * Very gentle; almost linear at the start of a clip.
 */
export const SPRING_MEDIA = {
	damping: 24,
	mass: 1.2,
	stiffness: 60,
} as const;

// ─── Transition ───────────────────────────────────────────────────────────────

/** Duration in frames for the cross-scene slide/fade transition. */
export const TRANSITION_FRAMES = 12;

// ─── Composition defaults ─────────────────────────────────────────────────────

export const COMPOSITION = {
	width: 1080,
	height: 1920,
	fps: 30,
	/** Extra frames appended after the last audio sample so the end frame is not cut. */
	tailFrames: 15,
} as const;
