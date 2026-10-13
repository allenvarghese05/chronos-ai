/**
 * Design token assertions.
 *
 * Ensures FONT_FAMILY values include the correct generic fallbacks so browsers
 * and Remotion's headless Chromium always fall back gracefully when the
 * self-hosted fonts have not yet loaded.
 */

import { describe, expect, it } from 'vitest';
import { FONT_FAMILY } from '../design/tokens';

describe('FONT_FAMILY — generic fallbacks', () => {
	it('sans includes "Inter Tight" as the primary family', () => {
		expect(FONT_FAMILY.sans).toContain('Inter Tight');
	});

	it('sans includes a generic sans-serif fallback', () => {
		expect(FONT_FAMILY.sans).toContain('sans-serif');
	});

	it('mono includes "JetBrains Mono" as the primary family', () => {
		expect(FONT_FAMILY.mono).toContain('JetBrains Mono');
	});

	it('mono includes a generic monospace fallback', () => {
		expect(FONT_FAMILY.mono).toContain('monospace');
	});
});
