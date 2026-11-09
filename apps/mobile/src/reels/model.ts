/**
 * Reel feed and player model (T5.3/T5.4): response validation and pure helpers for the pager,
 * the caption overlay and notification deep links. No React, no native modules: Jest-tested.
 */

export const PUBLIC_ID = /^[0-9A-HJKMNP-TV-Z]{26}$/;

export type ReelSummary = {
	public_id: string;
	local_date: string;
	status: string;
	duration_sec: number | null;
	saved: boolean;
	expires_at: string | null;
	thumbnail_url: string | null;
};

export type SceneSummary = {
	index: number;
	type: 'cover' | 'story' | 'outro';
	title: string | null;
	source: string | null;
	url: string | null;
	start_sec: number;
	end_sec: number;
};

export type ReelDetail = ReelSummary & {
	media_url: string | null;
	captions_url: string | null;
	scenes: SceneSummary[];
};

export type CaptionWord = { w: string; s: number; e: number };
export type CaptionDoc = { scenes: { index: number; words: CaptionWord[] }[] };

function record(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}
const optString = (v: unknown) => v === null || typeof v === 'string';
const optNumber = (v: unknown) => v === null || (typeof v === 'number' && Number.isFinite(v));

export function isReelSummary(v: unknown): v is ReelSummary {
	return (
		record(v) &&
		typeof v.public_id === 'string' &&
		PUBLIC_ID.test(v.public_id) &&
		typeof v.local_date === 'string' &&
		typeof v.status === 'string' &&
		optNumber(v.duration_sec) &&
		typeof v.saved === 'boolean' &&
		optString(v.expires_at) &&
		optString(v.thumbnail_url)
	);
}

export function isReelList(v: unknown): v is ReelSummary[] {
	return Array.isArray(v) && v.length <= 100 && v.every(isReelSummary);
}

function isScene(v: unknown): v is SceneSummary {
	return (
		record(v) &&
		Number.isInteger(v.index) &&
		(v.type === 'cover' || v.type === 'story' || v.type === 'outro') &&
		optString(v.title ?? null) &&
		optString(v.source ?? null) &&
		optString(v.url ?? null) &&
		typeof v.start_sec === 'number' &&
		typeof v.end_sec === 'number' &&
		v.end_sec >= v.start_sec
	);
}

export function isReelDetail(v: unknown): v is ReelDetail {
	if (!record(v) || !isReelSummary(v)) return false;
	const extra: Record<string, unknown> = v;
	return (
		optString(extra.media_url) &&
		optString(extra.captions_url) &&
		Array.isArray(extra.scenes) &&
		extra.scenes.length <= 12 &&
		extra.scenes.every(isScene)
	);
}

export function isCaptionDoc(v: unknown): v is CaptionDoc {
	return (
		record(v) &&
		Array.isArray(v.scenes) &&
		v.scenes.every(
			(s) =>
				record(s) &&
				Number.isInteger(s.index) &&
				Array.isArray(s.words) &&
				s.words.every(
					(w) =>
						record(w) &&
						typeof w.w === 'string' &&
						typeof w.s === 'number' &&
						typeof w.e === 'number',
				),
		)
	);
}

/** Index (position in `scenes`) of the scene playing at `t` seconds. */
export function sceneAt(scenes: SceneSummary[], t: number): number {
	let position = 0;
	scenes.forEach((scene, i) => {
		if (scene.start_sec <= t) position = i;
	});
	return position;
}

/** Where the player should seek when the pager lands on `position`. */
export function seekTarget(scenes: SceneSummary[], position: number): number {
	const scene = scenes[Math.max(0, Math.min(position, scenes.length - 1))];
	return scene ? scene.start_sec + 0.05 : 0;
}

/** The caption line (3–5 words, like the renderer) and active word at `t`, if any. */
export function captionAt(
	doc: CaptionDoc | null,
	t: number,
	maxPerLine = 5,
): { words: CaptionWord[]; active: number } | null {
	if (!doc) return null;
	const words = doc.scenes.flatMap((s) => s.words);
	const current = words.findIndex((w) => w.s <= t && t < w.e);
	const anchor = current >= 0 ? current : words.findLastIndex((w) => w.e <= t);
	if (anchor < 0) return null;
	const start = anchor - (anchor % maxPerLine);
	const line = words.slice(start, start + maxPerLine);
	if (current < 0 && t - (words[anchor]?.e ?? 0) > 1.5) return null;
	return { words: line, active: current >= 0 ? current - start : -1 };
}

/** Signed media URLs must point at the paired laptop's API origin, nothing else. */
export function sameOrigin(url: string, apiBase: string): boolean {
	try {
		return new URL(url).origin === new URL(apiBase).origin;
	} catch {
		return false;
	}
}

/** Reel id from a `chronos://reel/<id>` link or a `reel_ready` notification payload. */
export function reelIdFrom(input: unknown): string | null {
	if (typeof input === 'string') {
		const match = /^chronos:\/\/reel\/([0-9A-HJKMNP-TV-Z]{26})$/.exec(input);
		return match?.[1] ?? null;
	}
	if (record(input) && input.type === 'reel_ready' && typeof input.reel_public_id === 'string') {
		return PUBLIC_ID.test(input.reel_public_id) ? input.reel_public_id : null;
	}
	return null;
}
