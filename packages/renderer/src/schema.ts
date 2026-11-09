/**
 * ReelProps: the renderer's input contract.
 *
 * The field shapes are GENERATED from the API's Pydantic models into `schema.generated.ts`
 * (`just gen-types`; T3.4). Edit `services/api/src/chronos/media/reel_props.py`, never the
 * generated file. This module only adds what JSON schema cannot express: cross-field
 * refinements and the cover/story/outro discriminated union used by the scene components.
 */

import { z } from 'zod';
import {
	CaptionWordSchema as CaptionWordBaseSchema,
	ReelPropsBaseSchema,
	SceneSchema,
} from './schema.generated';

// ─── Caption word ────────────────────────────────────────────────────────────

export const CaptionWordSchema = CaptionWordBaseSchema.refine(({ s, e }) => e >= s, {
	message: 'CaptionWord: end (e) must be >= start (s)',
});

export type CaptionWord = z.infer<typeof CaptionWordSchema>;

// ─── Per-scene props (discriminated union by "type") ─────────────────────────

const SceneBaseSchema = SceneSchema.omit({ type: true });

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

export const ReelPropsSchema = ReelPropsBaseSchema.extend({
	scenes: z.array(ScenePropsSchema).min(1),
	captions: z.array(z.array(CaptionWordSchema)),
});

export type ReelProps = z.infer<typeof ReelPropsSchema>;
