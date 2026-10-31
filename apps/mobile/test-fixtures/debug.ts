import type { DebugSnapshot, ScorePage, TickPage } from '../src/debug/validation';

export const debugFixture = (): DebugSnapshot => ({
	server_time: '2026-11-01T08:00:00Z',
	server_version: '0.0.1',
	active_rule_version: 1,
	day: {
		local_date: '2026-10-31',
		tz: 'America/Los_Angeles',
		state: 'monitoring',
		actual_wake_local: null,
	},
	latest: {
		event_id: 7,
		evaluation_id: 9,
		received_at: '2026-11-01T07:59:01Z',
		evaluated_at: '2026-11-01T07:59:01Z',
		signals: {
			device_ts: '2026-11-01T07:59:00Z',
			tz: 'America/Los_Angeles',
			wake_source: 'manual',
			seq: 7,
			steps_10m: 0,
			steps_since_window: 0,
			walking_sec_10m: 0,
			stationary_sec_10m: 0,
			walking_conf: 'low',
			is_unlocked: true,
			battery_state: 'unknown',
			battery_level: null,
			app_state: 'active',
			low_power_mode: false,
			pedometer_auth: 'not_determined',
			app_version: '0.0.1',
			ios_version: '18.6',
			tick_seq: null,
		},
		evaluation: {
			gate: 'closed',
			score: 0,
			decision: 'outside_window',
			confirm_reason: null,
			consecutive_high: 0,
			thresholds: { confirm: 70, strong: 85, confirm_max_gap_min: 12 },
			rule_version: 1,
			breakdown: [
				{ rule_id: 'foreground', input: 'manual', points: 0, cap: 40, note: 'Outside window.' },
			],
			penalties: [],
			derived: {
				charger_disconnected: false,
				consecutive_unlocked_ticks: 1,
				minutes_since_window_start: 0,
				ticks_received_today: 1,
				pedometer_unavailable: true,
			},
			notes: ['Outside wake window.'],
		},
	},
});
export const scoreFixture = (): ScorePage => ({
	start: '2026-10-31T08:00:00Z',
	end: '2026-11-01T08:00:00Z',
	next_cursor: null,
	points: [
		{
			event_id: 7,
			evaluation_id: 9,
			at: '2026-11-01T07:59:00Z',
			seq: 7,
			score: 0,
			decision: 'outside_window',
			wake_source: 'manual',
			local_date: '2026-10-31',
		},
	],
});
export const tickFixture = (): TickPage => ({
	start: '2026-10-31T08:00:00Z',
	end: '2026-11-01T08:00:00Z',
	next_cursor: null,
	points: [
		{
			attempt_id: 3,
			at: '2026-11-01T07:58:00Z',
			local_date: '2026-10-31',
			tick_seq: 2,
			status: 'overdue',
		},
	],
});
