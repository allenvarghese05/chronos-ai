import { useFocusEffect } from 'expo-router';
import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { AppState, Button, ScrollView, Text, TextInput, View } from 'react-native';
import { deviceMetadata } from '../session/native';
import { useSession } from '../session/provider';
import { nativeTelemetry } from '../telemetry/native';
import { ui } from '../ui';
import { wakeRuntime } from '../wake/bootstrap';
import type { Telemetry } from '../wake/buildTelemetry';
import { WakePermissions } from '../wake/permissions';
import {
	createDebugController,
	type DebugController,
	type DebugState,
	MAX_HISTORY,
} from './controller';

const signalKeys: (keyof Telemetry)[] = [
	'seq',
	'device_ts',
	'tz',
	'wake_source',
	'pedometer_auth',
	'steps_10m',
	'steps_since_window',
	'walking_sec_10m',
	'stationary_sec_10m',
	'walking_conf',
	'is_unlocked',
	'battery_state',
	'battery_level',
	'app_state',
	'low_power_mode',
	'tick_seq',
	'app_version',
	'ios_version',
];
const value = (item: unknown) => (item === undefined || item === null ? 'unknown' : String(item));
function Signals({ sample }: { sample: Telemetry }) {
	return (
		<View style={ui.debugGroup}>
			{signalKeys.map((key) => (
				<Text key={key} style={ui.text}>
					{key === 'is_unlocked' ? 'Protected data available (unlock proxy)' : key}:{' '}
					{value(sample[key])}
				</Text>
			))}
		</View>
	);
}
function Stamp({ at, tz }: { at: string; tz?: string }) {
	let label = at;
	try {
		label = new Intl.DateTimeFormat('en', {
			dateStyle: 'medium',
			timeStyle: 'long',
			timeZone: tz ?? 'UTC',
		}).format(new Date(at));
	} catch {
		/* Keep original aware instant visible. */
	}
	return (
		<Text style={ui.label}>
			{label} · {at}
		</Text>
	);
}

export type DebugActions = Pick<
	DebugController,
	'refresh' | 'sendNow' | 'loadMore' | 'setDraft' | 'saveActualWake'
>;
/** Pure fixture-renderable view. Every signal/result keeps its own original identity/time. */
export function DebugView({
	state,
	actions,
	appVersion,
	iosVersion,
	permissions,
}: {
	state: DebugState;
	actions: DebugActions;
	appVersion: string;
	iosVersion: string;
	permissions?: ReactNode;
}) {
	const latest = state.snapshot?.latest;
	const local = state.telemetry.latestLocal;
	const acknowledged = state.telemetry.acknowledged;
	const day = state.snapshot?.day;
	return (
		<ScrollView contentContainerStyle={ui.page}>
			<Text style={ui.title}>Wake diagnostics</Text>
			<Text style={ui.text}>
				Timestamped snapshots, not a continuous sensor stream. Send now takes one manual sample
				through the shared queue.
			</Text>
			{!!state.error && (
				<Text accessibilityRole="alert" style={ui.notice}>
					{state.error}
				</Text>
			)}
			{!!state.message && (
				<Text accessibilityLiveRegion="polite" style={ui.notice}>
					{state.message}
				</Text>
			)}
			<Text style={ui.label}>
				Last diagnostics refresh:{' '}
				{state.loadedAt === null ? 'unavailable' : new Date(state.loadedAt).toISOString()}
			</Text>
			<View style={ui.debugGroup}>
				<Button
					title={state.sending ? 'Sending…' : 'Send now'}
					disabled={state.sending || state.telemetry.busy}
					onPress={() => void actions.sendNow()}
				/>
				<Button
					title={state.busy ? 'Refreshing…' : 'Refresh diagnostics'}
					disabled={state.busy}
					onPress={() => void actions.refresh()}
				/>
			</View>
			<Text style={ui.subtitle}>Outbox</Text>
			<Text style={ui.text}>
				Queued: {state.telemetry.count}; last sequence: {state.telemetry.lastSeq ?? 'none'}
			</Text>
			<Text style={ui.notice}>{state.telemetry.message}</Text>
			{[...new Set(state.telemetry.diagnostics)].map((note) => (
				<Text key={note} style={ui.notice}>
					{note}
				</Text>
			))}
			<Text style={ui.subtitle}>Latest local signals</Text>
			{local ? (
				<>
					<Stamp at={local.device_ts} tz={local.tz} />
					<Text style={ui.text}>
						{acknowledged?.sample.seq === local.seq
							? 'Acknowledged locally.'
							: 'Not acknowledged locally; check the outbox status. Server evidence below is separate.'}
					</Text>
					<Signals sample={local} />
				</>
			) : (
				<Text style={ui.text}>
					No local snapshot in this process. Stored server evidence may still be available.
				</Text>
			)}
			<Text style={ui.subtitle}>Last acknowledged local pair</Text>
			{acknowledged ? (
				<>
					<Text style={ui.text}>
						Sequence {acknowledged.sample.seq} → evaluation {acknowledged.result.evaluation_id} ·
						score {acknowledged.result.score}/100.
					</Text>
					<Stamp at={acknowledged.sample.device_ts} tz={acknowledged.sample.tz} />
				</>
			) : (
				<Text style={ui.text}>No acknowledgement retained in this process.</Text>
			)}
			<Text style={ui.subtitle}>Latest stored server signals</Text>
			{latest ? (
				<>
					<Text style={ui.text}>
						Event {latest.event_id}; sequence {latest.signals.seq}; evaluation{' '}
						{latest.evaluation_id}.
					</Text>
					<Stamp at={latest.signals.device_ts} tz={latest.signals.tz} />
					<Text style={ui.label}>
						Received: {latest.received_at}; evaluated: {latest.evaluated_at}
					</Text>
					<Signals sample={latest.signals} />
				</>
			) : (
				<Text style={ui.text}>Stored server signals unavailable or no events yet.</Text>
			)}
			<Text style={ui.subtitle}>Last stored breakdown</Text>
			{latest ? (
				<View style={ui.debugGroup}>
					<Text style={ui.host}>
						{latest.evaluation.score}/100 · {latest.evaluation.decision} · gate{' '}
						{latest.evaluation.gate}
					</Text>
					<Text style={ui.text}>
						Rule version {latest.evaluation.rule_version}; consecutive high{' '}
						{latest.evaluation.consecutive_high}; confirmation{' '}
						{latest.evaluation.confirm_reason ?? 'none'}.
					</Text>
					<Text style={ui.text}>
						Thresholds: confirm {latest.evaluation.thresholds.confirm}; strong{' '}
						{latest.evaluation.thresholds.strong}; max gap{' '}
						{latest.evaluation.thresholds.confirm_max_gap_min} min.
					</Text>
					{latest.evaluation.breakdown.length === 0 && (
						<Text style={ui.text}>No rule contributions for this evaluation.</Text>
					)}
					{latest.evaluation.breakdown.map((row) => (
						<Text key={row.rule_id} style={ui.text}>
							{row.rule_id}: {value(row.input)} → +{row.points} (cap {row.cap})
							{row.note ? ` · ${row.note}` : ''}
						</Text>
					))}
					<Text style={ui.label}>Penalties</Text>
					{latest.evaluation.penalties.length === 0 && <Text style={ui.text}>None.</Text>}
					{latest.evaluation.penalties.map((row) => (
						<Text key={row.rule_id} style={ui.text}>
							{row.rule_id}: {row.points}
							{row.note ? ` · ${row.note}` : ''}
						</Text>
					))}
					<Text style={ui.label}>Stored derived facts</Text>
					{Object.entries(latest.evaluation.derived).map(([key, item]) => (
						<Text key={key} style={ui.text}>
							{key}: {value(item)}
						</Text>
					))}
					{[...new Set(latest.evaluation.notes)].map((note) => (
						<Text key={note} style={ui.notice}>
							{note}
						</Text>
					))}
				</View>
			) : (
				<Text style={ui.text}>
					No stored evaluation available. Refresh when the laptop is reachable.
				</Text>
			)}
			<Text style={ui.subtitle}>24-hour score timeline</Text>
			<Text style={ui.text}>
				Fixed 0–100 scale. Gaps mean no samples; an actual score of zero is shown as zero. Original
				decisions are retained.
			</Text>
			{state.scores && (
				<Text style={ui.label}>
					{state.scores.start} → {state.scores.end} (24 elapsed hours)
				</Text>
			)}
			{!state.scores || state.scores.points.length === 0 ? (
				<Text style={ui.text}>No score samples loaded.</Text>
			) : (
				[...state.scores.points].reverse().map((point) => (
					<View
						key={point.event_id}
						style={ui.debugGroup}
						accessible
						accessibilityLabel={`${point.at}, score ${point.score} of 100, ${point.decision}, ${point.wake_source}, sequence ${point.seq}`}
					>
						<Stamp at={point.at} tz={day?.tz} />
						<Text style={ui.text}>
							{point.score}/100 · {point.decision} · {point.wake_source} · seq {point.seq} /
							evaluation {point.evaluation_id}
						</Text>
						<View style={ui.scoreTrack}>
							<View style={[ui.scoreFill, { width: `${point.score}%` }]} />
						</View>
					</View>
				))
			)}
			{state.scores?.next_cursor ? (
				<>
					<Text style={ui.notice}>
						Partial history: {state.scores.points.length} loaded
						{state.scores.points.length >= MAX_HISTORY
							? '; display limit reached. Refresh for the newest interval.'
							: '; older points remain.'}
					</Text>
					<Button
						title="Load older scores"
						disabled={state.busy || state.scores.points.length >= MAX_HISTORY}
						onPress={() => void actions.loadMore('scores')}
					/>
				</>
			) : (
				state.scores && (
					<Text style={ui.label}>All recorded scores in this interval are loaded.</Text>
				)
			)}
			<Text style={ui.subtitle}>Tick correlation</Text>
			<Text style={ui.text}>
				An Expo-accepted tick is overdue after 60 seconds without matching accepted silent-push
				telemetry. This does not prove APNs delivery or explain a missed wake. Offline, queued or
				throttled events may appear later.
			</Text>
			{state.ticks && (
				<Text style={ui.label}>
					{state.ticks.start} → {state.ticks.end}
				</Text>
			)}
			{!state.ticks || state.ticks.points.length === 0 ? (
				<Text style={ui.text}>
					No recorded tick attempts loaded. Unsent ticks are not inferred.
				</Text>
			) : (
				state.ticks.points.map((point) => (
					<View key={point.attempt_id} style={ui.debugGroup}>
						<Stamp at={point.at} tz={day?.tz} />
						<Text style={ui.text}>
							{point.local_date} · tick {point.tick_seq ?? 'unknown'} ·{' '}
							{point.status === 'overdue'
								? 'Overdue — no matching telemetry yet'
								: point.status === 'awaiting'
									? 'Awaiting correlation'
									: point.status === 'send_failed'
										? 'Push send failed'
										: 'Matching telemetry received'}
						</Text>
					</View>
				))
			)}
			{state.ticks?.next_cursor ? (
				<>
					<Text style={ui.notice}>
						Partial tick history: {state.ticks.points.length} loaded
						{state.ticks.points.length >= MAX_HISTORY
							? '; display limit reached.'
							: '; older attempts remain.'}
					</Text>
					<Button
						title="Load older ticks"
						disabled={state.busy || state.ticks.points.length >= MAX_HISTORY}
						onPress={() => void actions.loadMore('ticks')}
					/>
				</>
			) : (
				state.ticks && (
					<Text style={ui.label}>All recorded tick attempts in this interval are loaded.</Text>
				)
			)}
			<Text style={ui.subtitle}>Mark actual wake time</Text>
			{day ? (
				<>
					<Text style={ui.text}>
						Server day: {day.local_date} · {day.tz}. State: {day.state}. Saved wake time:{' '}
						{day.actual_wake_local ?? 'not set'}.
					</Text>
					<Text style={ui.notice}>
						Record your actual wake time as HH:MM. This annotation does not confirm the day or
						change scoring. It stores a wall-clock time without an offset; repeated DST times are
						ambiguous.
					</Text>
					<TextInput
						accessibilityLabel="Actual wake time HH:MM"
						style={ui.input}
						placeholder="07:05"
						value={state.draft}
						maxLength={5}
						autoCorrect={false}
						autoCapitalize="none"
						onChangeText={actions.setDraft}
					/>
					<Button
						title={state.saving ? 'Verifying wake time…' : 'Save actual wake time'}
						disabled={state.saving}
						onPress={() => void actions.saveActualWake()}
					/>
				</>
			) : (
				<Text style={ui.text}>
					No current server day available for annotation. Send a sample and refresh.
				</Text>
			)}
			<Text style={ui.subtitle}>Versions</Text>
			<Text style={ui.text}>
				App: {appVersion}; iOS: {iosVersion}; server:{' '}
				{state.snapshot?.server_version ?? 'unavailable'}; active rules:{' '}
				{state.snapshot?.active_rule_version ?? 'unavailable'}; last evaluation rules:{' '}
				{latest?.evaluation.rule_version ?? 'unavailable'}.
			</Text>
			{permissions}
		</ScrollView>
	);
}

/** Native adapter only mounts inside the authenticated route; focus never takes a sensor sample. */
export function DebugScreen() {
	const { controller: session, session: paired } = useSession();
	const [controller, setController] = useState<DebugController | null>(null);
	const [state, setState] = useState<DebugState | null>(null);
	const [error, setError] = useState('');
	useEffect(() => {
		if (!paired) return;
		setState(null);
		try {
			const telemetry = nativeTelemetry(session);
			const next = createDebugController({
				lease: session.lease,
				telemetry,
				now: Date.now,
				sendNow: (signal) => wakeRuntime.run('manual', undefined, signal),
			});
			setController(next);
			setState(next.status());
			const unsubscribe = next.subscribe(() => setState(next.status()));
			return () => {
				unsubscribe();
				next.dispose();
			};
		} catch {
			setError('Debug storage unavailable. Unlock the phone or free space, then reopen.');
		}
	}, [session, paired]);
	useFocusEffect(
		useCallback(() => {
			if (!controller) return;
			controller.activate(AppState.currentState === 'active');
			const listener = AppState.addEventListener('change', (next) =>
				controller.activate(next === 'active'),
			);
			return () => {
				listener.remove();
				controller.activate(false);
			};
		}, [controller]),
	);
	if (!state || !controller)
		return (
			<View style={ui.page}>
				<Text style={ui.notice}>{error || 'Opening wake diagnostics…'}</Text>
			</View>
		);
	const metadata = deviceMetadata();
	return (
		<DebugView
			state={state}
			actions={controller}
			appVersion={metadata.app_version}
			iosVersion={metadata.ios_version}
			permissions={<WakePermissions />}
		/>
	);
}
