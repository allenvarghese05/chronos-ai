import { act, type ReactElement } from 'react';
import { Text } from 'react-native';
import Debug from '../app/debug';
import { DebugView } from '../src/debug/screen';
import { SessionProvider } from '../src/session/provider';
import { createSessionStore } from '../src/session/store';
import { debugStateFixture } from '../test-fixtures/debug';

jest.mock('../src/session/native', () => ({
	sessionController: undefined,
	deviceMetadata: () => ({ app_version: '0.0.1', ios_version: '18.6' }),
}));
jest.mock('../src/wake/bootstrap', () => ({ wakeRuntime: { run: jest.fn() } }));
jest.mock('../src/wake/permissions', () => ({ WakePermissions: () => null }));
jest.mock('../src/telemetry/native', () => ({ nativeTelemetry: jest.fn() }));
jest.mock('expo-router', () => ({ Redirect: 'Redirect', useFocusEffect: jest.fn() }));

type Tree = {
	toJSON(): unknown;
	unmount(): void;
	update(element: ReactElement): void;
	root: { findByProps(props: Record<string, unknown>): { props: Record<string, unknown> } };
};
const renderer = require('react-test-renderer') as { create(element: ReactElement): Tree };
const actions = () => ({
	refresh: jest.fn(async () => {}),
	sendNow: jest.fn(async () => {}),
	loadMore: jest.fn(async () => {}),
	setDraft: jest.fn(),
	saveActualWake: jest.fn(async () => {}),
});

test('fixture renders signals, original breakdown, timeline, ticks, queue, annotation and versions', async () => {
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<DebugView
				state={debugStateFixture()}
				actions={actions()}
				appVersion="0.0.1"
				iosVersion="18.6"
				permissions={<Text>Motion: not_determined</Text>}
			/>,
		);
	});
	const text = JSON.stringify(tree.toJSON());
	for (const label of [
		'Wake diagnostics',
		'Latest local signals',
		'Last acknowledged local pair',
		'Latest stored server signals',
		'Last stored breakdown',
		'Stored derived facts',
		'24-hour score timeline',
		'Tick correlation',
		'Outbox',
		'Mark actual wake time',
		'Versions',
		'No matching',
		'Motion: not_determined',
	]) {
		if (label === 'No matching') expect(text.toLowerCase()).toContain('no matching');
		else expect(text).toContain(label);
	}
	expect(text).toContain('America/Los_Angeles');
	expect(text).toContain('not_determined');
	expect(text).toContain('Protected data available (unlock proxy)');
	expect(text).toContain('unknown');
	await act(async () => tree.unmount());
});
test('queued latest and older ACK never borrow the stored server score; empty/partial/stale states explicit', async () => {
	const state = debugStateFixture();
	if (state.snapshot?.latest)
		state.telemetry.latestLocal = { ...state.snapshot.latest.signals, seq: 8 };
	state.telemetry.count = 1;
	state.error = 'Diagnostics unavailable; stored values remain dated.';
	if (state.scores) {
		state.scores.points = [];
		state.scores.next_cursor = 'older';
	}
	if (state.ticks) state.ticks.points = [];
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<DebugView state={state} actions={actions()} appVersion="0.0.1" iosVersion="18.6" />,
		);
	});
	const text = JSON.stringify(tree.toJSON());
	expect(text).toContain('Not acknowledged locally');
	expect(text).toContain('No score samples loaded.');
	expect(text).toContain('Partial history');
	expect(text).toContain('Unsent ticks are not inferred.');
	expect(text).toContain('stored values remain dated');
	await act(async () => tree.unmount());
});
test('send-now and annotation controls call explicit actions; typing never saves automatically', async () => {
	const handlers = actions();
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(
			<DebugView
				state={debugStateFixture()}
				actions={handlers}
				appVersion="0.0.1"
				iosVersion="18.6"
			/>,
		);
	});
	await act(async () => {
		(tree.root.findByProps({ title: 'Send now' }).props.onPress as () => void)();
	});
	expect(handlers.sendNow).toHaveBeenCalledTimes(1);
	(
		tree.root.findByProps({ accessibilityLabel: 'Actual wake time HH:MM' }).props.onChangeText as (
			v: string,
		) => void
	)('07:05');
	expect(handlers.setDraft).toHaveBeenCalledWith('07:05');
	expect(handlers.saveActualWake).not.toHaveBeenCalled();
	await act(async () => {
		(tree.root.findByProps({ title: 'Save actual wake time' }).props.onPress as () => void)();
	});
	expect(handlers.saveActualWake).toHaveBeenCalledTimes(1);
	await act(async () => tree.unmount());
});
test('unpaired protected debug route does not mount native telemetry or sensor controls', async () => {
	const { nativeTelemetry } = require('../src/telemetry/native') as { nativeTelemetry: jest.Mock };
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
				<Debug />
			</SessionProvider>,
		);
	});
	expect(JSON.stringify(tree.toJSON())).toContain('Redirect');
	expect(nativeTelemetry).not.toHaveBeenCalled();
	await act(async () => tree.unmount());
});
