import { act, type ReactElement } from 'react';
import Settings from '../app/settings';
import { SessionProvider } from '../src/session/provider';
import { createSessionStore } from '../src/session/store';
import type { SettingsState } from '../src/settings/controller';
import { initialFields } from '../src/settings/draft';
import type { SettingsActions } from '../src/settings/screen';
import { SettingsView } from '../src/settings/screen';
import { preferencesFixture, wakeConfigFixture } from '../test-fixtures/settings';

jest.mock('../src/session/native', () => ({ sessionController: undefined }));
jest.mock('../src/wake/permissions', () => ({ WakePermissions: () => null }));
jest.mock('expo-router', () => ({ Link: 'Link', Redirect: 'Redirect', useFocusEffect: jest.fn() }));
type Tree = {
	toJSON(): unknown;
	unmount(): void;
	root: { findByProps(p: Record<string, unknown>): { props: Record<string, unknown> } };
};
const renderer = require('react-test-renderer') as { create(e: ReactElement): Tree };
function fixture(): SettingsState {
	const wake = wakeConfigFixture(),
		preferences = preferencesFixture();
	return {
		wake,
		preferences,
		fields: initialFields(wake.rules),
		preferenceDraft: {
			theme: preferences.theme,
			render_mode: preferences.render_mode,
			pexels_enabled: preferences.pexels_enabled,
		},
		dirtyRules: false,
		dirtyPreferences: false,
		busy: false,
		saving: false,
		testing: false,
		conflict: false,
		issues: [],
		error: '',
		message: '',
	};
}
const actions = (): SettingsActions => ({
	refresh: jest.fn(async () => {}),
	reload: jest.fn(async () => {}),
	editField: jest.fn(),
	editPreference: jest.fn(),
	saveRules: jest.fn(async () => {}),
	savePreferences: jest.fn(async () => {}),
	testPush: jest.fn(async () => {}),
});

test('settings fixture renders preserving form, origins, future capabilities and explicit actions', async () => {
	let tree!: Tree;
	const calls = actions();
	await act(async () => {
		tree = renderer.create(<SettingsView state={fixture()} actions={calls} />);
	});
	const text = JSON.stringify(tree.toJSON());
	for (const label of [
		'Wake rules',
		'Active version',
		'document version',
		'Confirm threshold',
		'Reel preferences',
		'future rendering pipeline',
		'not configured',
		'Test notification',
		'Send test push',
		'Save wake rules',
	])
		expect(text).toContain(label);
	await act(async () => {
		(tree.root.findByProps({ title: 'Show weight controls' }).props.onPress as () => void)();
	});
	expect(JSON.stringify(tree.toJSON())).toContain('custom.steps: award');
	expect(JSON.stringify(tree.toJSON())).toContain('without-pedometer weight');
	expect(calls.saveRules).not.toHaveBeenCalled();
	expect(calls.testPush).not.toHaveBeenCalled();
	await act(async () => {
		(
			tree.root.findByProps({ accessibilityLabel: 'Confirm threshold' }).props.onChangeText as (
				v: string,
			) => void
		)('71');
		(tree.root.findByProps({ title: 'Save wake rules' }).props.onPress as () => void)();
	});
	expect(calls.editField).toHaveBeenCalledWith(JSON.stringify(['thresholds', 'confirm']), '71');
	expect(calls.saveRules).toHaveBeenCalledTimes(1);
	await act(async () => {
		tree.unmount();
	});
});

test('validation and conflict guidance is visible and conflicts disable saving', async () => {
	const state = fixture();
	state.conflict = true;
	state.dirtyRules = true;
	state.issues = [{ path: 'rules.0', message: 'Check limits' }];
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(<SettingsView state={state} actions={actions()} />);
	});
	expect(JSON.stringify(tree.toJSON())).toContain('Check limits');
	expect(tree.root.findByProps({ title: 'Save wake rules' }).props.disabled).toBe(true);
	expect(JSON.stringify(tree.toJSON())).toContain('Discard drafts');
	await act(async () => {
		tree.unmount();
	});
});

test('settings route is protected while unpaired and does not mount form', async () => {
	const controller = createSessionStore({
		storage: { get: async () => null, set: async () => {}, remove: async () => {} },
		api: jest.fn(),
		metadata: jest.fn(),
		push: jest.fn(),
		now: () => 0,
	});
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<SessionProvider controller={controller}>
				<Settings />
			</SessionProvider>,
		);
	});
	const text = JSON.stringify(tree.toJSON());
	expect(text).toContain('Redirect');
	expect(text).not.toContain('Save wake rules');
	await act(async () => {
		tree.unmount();
	});
});
