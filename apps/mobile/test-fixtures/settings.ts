import type { Preferences, WakeConfig } from '../src/settings/validation';
export function wakeConfigFixture(): WakeConfig {
	return {
		version: 3,
		source: 'api',
		sha256: 'a'.repeat(64),
		yaml_sha256: 'b'.repeat(64),
		yaml_stale: false,
		created_at: '2026-10-31T10:00:00Z',
		created_by: 'api:fixture',
		rules: {
			version: 1,
			window: {
				weekday: { start: '06:00', end: '11:00' },
				weekend: { start: '08:00', end: '14:00' },
				day_boundary: '03:00',
				tick_interval_min: 5,
				holidays_as_weekend: ['2026-12-25'],
			},
			thresholds: { confirm: 70, strong: 85, dedupe_min: 2, confirm_max_gap_min: 12 },
			rules: [
				{
					id: 'custom.steps',
					signal: 'steps_10m',
					points: [
						[35.5, 30],
						[20, 18],
					],
					cap: 30,
					requires: { walking_conf: ['medium', 'high'] },
				},
				{
					id: 'unlocked',
					signal: 'is_unlocked',
					points: [[true, 15]],
					cap: 25,
					bonus_consecutive: { ticks: 2, points: 10 },
				},
				{ id: 'foreground', signal: 'wake_source', points: [['foreground', 40]], cap: 40 },
			],
			penalties: [{ id: 'still', when: { steps_10m: '<10' }, points: -20 }],
			reweight_without_pedometer: { unlocked: 35, foreground: 40 },
		},
	};
}
export function preferencesFixture(): Preferences {
	return {
		theme: 'auto',
		render_mode: 'eager',
		pexels_enabled: false,
		pexels_effective: false,
		pexels_key_configured: false,
		origins: { theme: 'default', render_mode: 'default', pexels_enabled: 'default' },
		rendering_available: false,
	};
}
