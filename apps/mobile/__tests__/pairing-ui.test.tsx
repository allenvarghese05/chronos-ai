import { act, type ReactElement } from 'react';
import { Button, TextInput } from 'react-native';
import Pairing from '../app/pairing';
import type { SessionController, SessionState } from '../src/session/store';

type Node = { props: Record<string, unknown> };
type Tree = { root: { findAllByType(type: unknown): Node[] }; toJSON(): unknown; unmount(): void };
const renderer = require('react-test-renderer') as { create(element: ReactElement): Tree };
const mockController = {
	pair: jest.fn(async () => true),
	forget: jest.fn(),
	retryHydration: jest.fn(),
};
let mockState: SessionState & { controller: SessionController };
let mockPermission = false;
const mockRequestPermission = jest.fn(async () => ({ granted: mockPermission }));
jest.mock('../src/session/provider', () => ({ useSession: () => mockState }));
jest.mock('expo-router', () => ({ Link: 'Link', Redirect: 'Redirect' }));
jest.mock('expo-device', () => ({ isDevice: true }));
jest.mock('expo-camera', () => ({
	CameraView: 'CameraView',
	useCameraPermissions: () => [{ granted: mockPermission }, mockRequestPermission],
}));
beforeEach(() => {
	jest.clearAllMocks();
	mockPermission = false;
	mockState = {
		phase: 'unpaired',
		session: null,
		busy: false,
		checking: false,
		message: '',
		retryAt: 0,
		pushStatus: '',
		controller: mockController as unknown as SessionController,
	};
});
async function render() {
	let tree!: Tree;
	await act(async () => {
		tree = renderer.create(<Pairing />);
	});
	return tree;
}
function button(tree: Tree, title: string) {
	const node = tree.root.findAllByType(Button).find((item) => item.props.title === title);
	if (!node) throw new Error(`Missing button ${title}`);
	return node.props.onPress as () => void;
}
async function press(tree: Tree, title: string) {
	await act(async () => {
		button(tree, title)();
	});
}

test('manual form reviews exact host before any network action', async () => {
	const tree = await render();
	const inputs = tree.root.findAllByType(TextInput);
	await act(async () => {
		(inputs[0].props.onChangeText as (v: string) => void)('https://laptop.local:8443/v1');
		(inputs[1].props.onChangeText as (v: string) => void)('7k2m - 9qxd');
	});
	await press(tree, 'Review pairing details');
	expect(mockController.pair).not.toHaveBeenCalled();
	await press(tree, 'Confirm and pair');
	expect(mockController.pair).toHaveBeenCalledWith('https://laptop.local:8443/v1', '7K2M9QXD');
	await act(async () => tree.unmount());
});
test('camera denial leaves both manual inputs usable', async () => {
	const tree = await render();
	await press(tree, 'Scan laptop QR');
	expect(mockRequestPermission).toHaveBeenCalledTimes(1);
	expect(tree.root.findAllByType(TextInput)).toHaveLength(2);
	expect(tree.root.findAllByType('CameraView')).toHaveLength(0);
	await act(async () => tree.unmount());
});
test('one QR callback populates confirmation, unmounts camera and never submits', async () => {
	mockPermission = true;
	const tree = await render();
	await press(tree, 'Scan laptop QR');
	const scan = tree.root.findAllByType('CameraView')[0].props.onBarcodeScanned as (event: {
		data: string;
	}) => void;
	const event = {
		data: JSON.stringify({ api_base: 'https://laptop.local:8443/v1', code: '7K2M9QXD' }),
	};
	await act(async () => {
		scan(event);
		scan(event);
	});
	expect(tree.root.findAllByType('CameraView')).toHaveLength(0);
	expect(mockController.pair).not.toHaveBeenCalled();
	await press(tree, 'Confirm and pair');
	expect(mockController.pair).toHaveBeenCalledTimes(1);
	await act(async () => tree.unmount());
});
test('hydration hides the form and authenticated route redirects', async () => {
	mockState.phase = 'hydrating';
	let tree = await render();
	expect(tree.root.findAllByType(TextInput)).toHaveLength(0);
	await act(async () => tree.unmount());
	mockState.phase = 'authenticated';
	tree = await render();
	expect(tree.root.findAllByType('Redirect')).toHaveLength(1);
	await act(async () => tree.unmount());
});
