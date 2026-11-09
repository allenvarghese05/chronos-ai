import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ReelPropsSchema } from '../schema';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, '../../src/fixtures/sample-reel.json');

// ─── Fixture parses ───────────────────────────────────────────────────────────

describe('ReelPropsSchema — valid fixture', () => {
	it('parses sample-reel.json without errors', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(true);
	});

	it('exposes correct top-level fields from the fixture', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		const props = ReelPropsSchema.parse(raw);
		expect(props.localDate).toBe('2026-10-06');
		expect(props.greeting).toBe('Good morning');
		expect(props.tone).toBe('calm');
		expect(props.theme).toBe('dawn');
		expect(props.audio.durationSec).toBe(3.0);
		expect(props.scenes).toHaveLength(1);
		expect(props.captions).toHaveLength(0);
	});
});

// ─── Invalid theme ────────────────────────────────────────────────────────────

describe('ReelPropsSchema — invalid theme', () => {
	it('rejects an unknown theme string', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.theme = 'sunset';
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
		if (!result.success) {
			const paths = result.error.issues.map((i) => i.path.join('.'));
			expect(paths).toContain('theme');
		}
	});
});

// ─── Negative audio duration ─────────────────────────────────────────────────

describe('ReelPropsSchema — invalid audio duration', () => {
	it('rejects durationSec = 0', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.audio.durationSec = 0;
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
	});

	it('rejects negative durationSec', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.audio.durationSec = -5;
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
	});
});

// ─── Scene endSec < startSec ──────────────────────────────────────────────────

describe('ReelPropsSchema — scene time ordering', () => {
	it('rejects a cover scene with endSec < startSec', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.scenes[0].startSec = 5;
		raw.scenes[0].endSec = 2;
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
	});

	it('accepts a scene where endSec === startSec (zero duration)', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.scenes[0].startSec = 1;
		raw.scenes[0].endSec = 1;
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(true);
	});
});

// ─── CaptionWord validation ───────────────────────────────────────────────────

describe('ReelPropsSchema — caption word validation', () => {
	it('rejects a caption word where e < s', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.captions = [[{ w: 'hello', s: 5, e: 3 }]];
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
	});

	it('accepts a caption word where e === s', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.captions = [[{ w: 'hello', s: 2, e: 2 }]];
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(true);
	});
});

// ─── Invalid localDate ────────────────────────────────────────────────────────

describe('ReelPropsSchema — localDate format', () => {
	it('rejects a non-YYYY-MM-DD string', () => {
		const raw = JSON.parse(readFileSync(FIXTURE_PATH, 'utf-8'));
		raw.localDate = '06-10-2026';
		const result = ReelPropsSchema.safeParse(raw);
		expect(result.success).toBe(false);
	});
});
