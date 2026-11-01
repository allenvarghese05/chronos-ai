import { Link, useFocusEffect } from 'expo-router';
import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { AppState, Button, ScrollView, Switch, Text, TextInput, View } from 'react-native';
import { useSession } from '../session/provider';
import { ui } from '../ui';
import { WakePermissions } from '../wake/permissions';
import {
	createSettingsController,
	type SettingsController,
	type SettingsState,
} from './controller';
import { ruleFields } from './draft';

export type SettingsActions = Pick<
	SettingsController,
	| 'refresh'
	| 'reload'
	| 'editField'
	| 'editPreference'
	| 'saveRules'
	| 'savePreferences'
	| 'testPush'
>;
/** Fixture-renderable settings. Complete fetched rules stay intact behind selected leaf controls. */
export function SettingsView({
	state,
	actions,
	permissions,
}: {
	state: SettingsState;
	actions: SettingsActions;
	permissions?: ReactNode;
}) {
	const wake = state.wake,
		prefs = state.preferenceDraft;
	return (
		<ScrollView contentContainerStyle={ui.page} keyboardShouldPersistTaps="handled">
			<Text style={ui.title}>Settings</Text>
			<Text style={ui.text}>
				Save changes explicitly. Active rules apply to subsequent processing; stored day metadata
				and past evaluations are not rewritten.
			</Text>
			{!!state.error && (
				<Text accessibilityRole="alert" style={ui.notice}>
					{state.error}
				</Text>
			)}
			{!!state.message && (
				<Text accessibilityLiveRegion="polite" style={ui.host}>
					{state.message}
				</Text>
			)}
			{state.issues.map((issue) => (
				<Text key={issue.path} accessibilityRole="alert" style={ui.notice}>
					{issue.path}: {issue.message}
				</Text>
			))}
			<Button
				title={state.busy ? 'Refreshing…' : 'Refresh settings'}
				disabled={state.busy || state.saving || state.testing}
				onPress={() => void actions.refresh()}
			/>
			{(state.dirtyRules || state.dirtyPreferences || state.conflict) && (
				<>
					<Text style={ui.notice}>
						Unsaved changes are preserved during refresh. Reloading discards drafts; reapply changes
						explicitly afterward.
					</Text>
					<Button
						title="Discard drafts and reload active settings"
						disabled={state.busy || state.saving}
						onPress={() => void actions.reload()}
					/>
				</>
			)}
			<Text style={ui.subtitle}>Wake rules</Text>
			{wake ? (
				<>
					<Text style={ui.label}>
						Active version {wake.version} · source {wake.source} · document version{' '}
						{wake.rules.version}
					</Text>
					<Text style={ui.text}>
						Saved {wake.created_at}. YAML{' '}
						{wake.yaml_stale ? 'has changed on disk' : 'matches its last promoted version'}. API
						saves override YAML until it is deliberately promoted again.
					</Text>
					{state.conflict && (
						<Text accessibilityRole="alert" style={ui.notice}>
							Rules changed on the laptop. Reload active rules before reapplying your edits.
						</Text>
					)}
					<Text style={ui.text}>
						Windows are local clock times. Thresholds are 1–100; strong must be at least confirm.
						Awards are nonnegative whole numbers and cannot exceed their cap.
					</Text>
					{ruleFields(wake.rules).map((field) => (
						<View key={field.key} style={ui.debugGroup}>
							<Text style={ui.label}>{field.label}</Text>
							<TextInput
								accessibilityLabel={field.label}
								value={state.fields[field.key] ?? field.value}
								style={ui.input}
								editable={!state.saving}
								keyboardType={field.kind === 'integer' ? 'number-pad' : 'numbers-and-punctuation'}
								autoCorrect={false}
								autoCapitalize="none"
								maxLength={32}
								onChangeText={(value) => actions.editField(field.key, value)}
							/>
						</View>
					))}
					<Text style={ui.notice}>
						Preserved advanced settings: day boundary {wake.rules.window.day_boundary}; tick
						interval {wake.rules.window.tick_interval_min} min; dedupe{' '}
						{wake.rules.thresholds.dedupe_min} min; maximum confirmation gap{' '}
						{wake.rules.thresholds.confirm_max_gap_min} min. Holiday lists, threshold types,
						conditions and penalties remain unchanged.
					</Text>
					<Button
						title={state.saving ? 'Verifying save…' : 'Save wake rules'}
						disabled={state.saving || state.busy || state.testing || state.conflict}
						onPress={() => void actions.saveRules()}
					/>
				</>
			) : (
				<Text style={ui.notice}>
					Wake settings unavailable. Refresh when the laptop is reachable.
				</Text>
			)}
			<Text style={ui.subtitle}>Reel preferences</Text>
			<Text style={ui.notice}>
				These preferences are stored for the future rendering pipeline. They do not change this
				app’s appearance or render a video yet.
			</Text>
			{prefs && state.preferences ? (
				<>
					<Text style={ui.label}>
						Reel theme: {prefs.theme} · origin {state.preferences.origins.theme}
					</Text>
					<Text style={ui.text}>Automatic uses dawn on weekdays and ink on weekends.</Text>
					{(['auto', 'dawn', 'mono', 'ink'] as const).map((theme) => (
						<Button
							key={theme}
							title={`${prefs.theme === theme ? 'Selected: ' : ''}${theme}`}
							disabled={state.saving}
							onPress={() => actions.editPreference('theme', theme)}
						/>
					))}
					<Text style={ui.label}>
						Render mode: {prefs.render_mode} · origin {state.preferences.origins.render_mode}
					</Text>
					<Text style={ui.text}>
						Eager prepares a reel before wake confirmation. On trigger prioritizes freshness with a
						longer wait. Pipeline execution is not implemented here.
					</Text>
					{(['eager', 'on_trigger'] as const).map((mode) => (
						<Button
							key={mode}
							title={`${prefs.render_mode === mode ? 'Selected: ' : ''}${mode}`}
							disabled={state.saving}
							onPress={() => actions.editPreference('render_mode', mode)}
						/>
					))}
					<Text style={ui.label}>
						Request optional Pexels b-roll · origin {state.preferences.origins.pexels_enabled}
					</Text>
					<Switch
						accessibilityLabel="Request Pexels b-roll"
						value={prefs.pexels_enabled}
						disabled={state.saving}
						onValueChange={(value) => actions.editPreference('pexels_enabled', value)}
					/>
					<Text style={ui.text}>
						Server key: {state.preferences.pexels_key_configured ? 'configured' : 'not configured'}.
						Saved effective capability:{' '}
						{state.preferences.pexels_effective
							? 'enabled for future pipeline'
							: 'unavailable or disabled'}
						. No key is entered here; this screen makes no Pexels request.
					</Text>
					<Button
						title={state.saving ? 'Verifying save…' : 'Save reel preferences'}
						disabled={state.saving || state.busy || state.testing}
						onPress={() => void actions.savePreferences()}
					/>
				</>
			) : (
				<Text style={ui.notice}>Reel preferences unavailable.</Text>
			)}
			<Text style={ui.subtitle}>Test notification</Text>
			<Text style={ui.text}>
				Send one visible notification to the registered phone. Expo acceptance is not delivery
				confirmation. Missing physical-device registration is expected in this Simulator setup. No
				automatic retries; wait at least a minute between attempts.
			</Text>
			<Button
				title={state.testing ? 'Sending one test…' : 'Send test push'}
				disabled={state.testing || state.saving}
				onPress={() => void actions.testPush()}
			/>
			{permissions}
			<Link href="/" style={ui.host}>
				Connection, pairing and revoke controls
			</Link>
		</ScrollView>
	);
}
/** Native screen requests settings only while focused and active; no permission prompt on mount. */
export function SettingsScreen() {
	const { controller: session, session: paired } = useSession();
	const [controller, setController] = useState<SettingsController | null>(null),
		[state, setState] = useState<SettingsState | null>(null);
	useEffect(() => {
		if (!paired) return;
		setState(null);
		const next = createSettingsController({ lease: session.lease });
		setController(next);
		setState(next.status());
		const stop = next.subscribe(() => setState(next.status()));
		return () => {
			stop();
			next.dispose();
		};
	}, [session, paired]);
	useFocusEffect(
		useCallback(() => {
			if (!controller) return;
			controller.activate(AppState.currentState === 'active');
			const listener = AppState.addEventListener('change', (value) =>
				controller.activate(value === 'active'),
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
				<Text style={ui.text}>Opening settings…</Text>
			</View>
		);
	return <SettingsView state={state} actions={controller} permissions={<WakePermissions />} />;
}
