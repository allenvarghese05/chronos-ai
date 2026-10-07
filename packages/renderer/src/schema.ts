/**
 * ReelProps — hand-written zod schema mirroring docs/07-media-pipeline.md §4.
 *
 * T3.4 will replace this with a generated schema from `just gen-types` (Pydantic → JSON schema
 * → zod). Until then this file is the single source of truth for the renderer's input contract.
 *
 * Changes here must also be reflected in services/api/src/chronos/content/script/schema.py
 * (and vice versa) until T3.4 automates the sync.
 */

import { z } from 'zod';

// ─── Caption word ────────────────────────────────────────────────────────────

export const CaptionWordSchema = z
	.object({
		/** display text */
		w: z.string().min(1),
		/** start time in seconds (inclusive) */
		s: z.number().nonnegative(),
		/** end time in seconds (inclusive) */
		e: z.number().nonnegative(),
	})
	.refine(({ s, e }) => e >= s, { message: 'CaptionWord: end (e) must be >= start (s)' });

export type CaptionWord = z.infer<typeof CaptionWordSchema>;

// ─── Media attachment ────────────────────────────────────────────────────────

const MediaSchema = z.object({
	type: z.enum(['image', 'video', 'gradient']),
	/** absolute URL or public/ relative path; absent for gradient */
	url: z.string().optional(),
	/** Pexels search query used when type = video */
	query: z.string().optional(),
	/** zero-based index within the search results */
	index: z.number().int().nonnegative().optional(),
});

// ─── Source attribution ──────────────────────────────────────────────────────

const SourceSchema = z.object({
	name: z.string().min(1),
	url: z.string().url(),
});

// ─── Per-scene props (discriminated union by "type") ─────────────────────────

/**
 * A scene base with the shared fields used by all scene types.
 * The `type` discriminant is added by each member of the union below.
 */
const SceneBaseSchema = z.object({
	index: z.number().int().nonnegative(),
	title: z.string().optional(),
	body: z.array(z.string()).optional(),
	media: MediaSchema.optional(),
	source: SourceSchema.optional(),
	startSec: z.number().nonnegative(),
	endSec: z.number().nonnegative(),
});

/** Validate that endSec >= startSec for every scene. */
const withTimeOrder = <T extends z.ZodObject<{ startSec: z.ZodNumber; endSec: z.ZodNumber }>>(
	schema: T,
) =>
	schema.refine(({ startSec, endSec }) => endSec >= startSec, {
		message: 'Scene: endSec must be >= startSec',
		path: ['endSec'],
	});

export const CoverSceneSchema = withTimeOrder(SceneBaseSchema.extend({ type: z.literal('cover') }));
export const StorySceneSchema = withTimeOrder(SceneBaseSchema.extend({ type: z.literal('story') }));
export const OutroSceneSchema = withTimeOrder(SceneBaseSchema.extend({ type: z.literal('outro') }));

export const ScenePropsSchema = z.discriminatedUnion('type', [
	CoverSceneSchema,
	StorySceneSchema,
	OutroSceneSchema,
]);

export type SceneProps = z.infer<typeof ScenePropsSchema>;

// ─── Top-level ReelProps ─────────────────────────────────────────────────────

export const ReelPropsSchema = z.object({
	/** ISO date string for the local morning this reel represents (YYYY-MM-DD). */
	localDate: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'localDate must be YYYY-MM-DD'),

	/** Greeting phrase shown on the cover (e.g. "Good morning"). */
	greeting: z.string().min(1),

	/** Style hint used by scenes and captions to calibrate motion intensity. */
	tone: z.enum(['friendly', 'formal']),

	/** Audio track for the whole reel. */
	audio: z.object({
		/** Path relative to the renderer project root, e.g. "public/audio/voice.mp3". */
		src: z.string().min(1),
		/** Total duration in seconds; used to derive durationInFrames via calculateMetadata. */
		durationSec: z.number().positive(),
	}),

	/** Ordered list of scenes. At least one scene is required. */
	scenes: z.array(ScenePropsSchema).min(1),

	/**
	 * Karaoke-style caption words grouped per scene (outer index matches scenes[]).
	 * An empty array means no captions are rendered.
	 */
	captions: z.array(z.array(CaptionWordSchema)),

	/** Visual theme applied to all scenes. */
	theme: z.enum(['dawn', 'mono', 'ink']),

	/**
	 * Human-readable attribution lines shown in OutroScene and the app's source sheet.
	 * e.g. ["Photos: Pexels", "Voice: Kokoro-82M", "Captions: whisper.cpp"]
	 */
	attribution: z.array(z.string()),
});

export type ReelProps = z.infer<typeof ReelPropsSchema>;
