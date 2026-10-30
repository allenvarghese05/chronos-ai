import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const mockRun = jest.fn<Promise<string>, [string, unknown?, AbortSignal?]>(async () => 'sent');
const mockForeground = jest.fn();
const mockNotificationImport = jest.fn();
const mockRegister = jest.fn(async () => null);
const mockDefine = jest.fn();
const mockPending = jest.fn();
const mockComplete = jest.fn();
const mockListener = jest.fn();
let mockPhysical = false;
let mockProject: string | null = null;
jest.mock('expo-device', () => ({
	get isDevice() {
		return mockPhysical;
	},
}));
jest.mock('expo-task-manager', () => ({ defineTask: (...args: unknown[]) => mockDefine(...args) }));
jest.mock('expo-notifications', () => {
	mockNotificationImport();
	return { registerTaskAsync: mockRegister };
});
jest.mock('../src/session/native', () => ({
	deviceMetadata: () => ({ tz: 'Etc/UTC' }),
	notificationProjectId: () => mockProject,
	sessionController: {
		lease: () => null,
		restore: async () => {},
		store: { subscribe: jest.fn() },
	},
}));
jest.mock('../src/telemetry/native', () => ({ nativeTelemetry: jest.fn() }));
jest.mock('../src/wake/runtime', () => ({
	createWakeRuntime: () => ({ run: mockRun, foreground: mockForeground }),
}));
jest.mock('../modules/chronos-sensors/src', () => ({
	__esModule: true,
	BG_REFRESH_IDENTIFIER: 'dev.allenvarghese.chronos.refresh',
	default: {
		addListener: (...args: unknown[]) => mockListener(...args),
		takePendingBackgroundRefresh: () => mockPending(),
		completeBackgroundRefresh: (...args: unknown[]) => mockComplete(...args),
		scheduleBackgroundRefresh: () => true,
	},
}));
function load() {
	jest.isolateModules(() => require('../src/wake/bootstrap'));
}
beforeEach(() => {
	jest.clearAllMocks();
	mockPhysical = false;
	mockProject = null;
	mockPending.mockReturnValue(null);
	mockRun.mockResolvedValue('sent');
});
test('entry runs bootstrap before Router and installed SDK result mapping matches', () => {
	const entry = readFileSync(join(__dirname, '../index.ts'), 'utf8');
	expect(entry.indexOf("import './src/wake/bootstrap'")).toBeLessThan(
		entry.indexOf("import 'expo-router/entry'"),
	);
	const sdk = readFileSync(
		join(__dirname, '../node_modules/expo-notifications/src/registerTaskAsync.ts'),
		'utf8',
	);
	for (const [key, value] of [
		['NewData', 0],
		['NoData', 1],
		['Failed', 2],
	])
		expect(sdk).toContain(`${key} = ${value}`);
});
test('global definition exists on Simulator without loading notifications side effects', () => {
	load();
	expect(mockDefine).toHaveBeenCalledTimes(1);
	expect(mockNotificationImport).not.toHaveBeenCalled();
});
test('physical build without project also avoids notifications import; supported build registers without prompt', async () => {
	mockPhysical = true;
	load();
	expect(mockNotificationImport).not.toHaveBeenCalled();
	mockProject = '12345678-1234-1234-1234-123456789abc';
	load();
	await Promise.resolve();
	expect(mockRegister).toHaveBeenCalledWith('dev.allenvarghese.chronos.silent-push');
});
test('native pending cold-start execution is claimed after listener and completes its own ID', async () => {
	mockPending.mockReturnValueOnce({ id: 'refresh', executionId: 'A' });
	load();
	expect(mockListener.mock.invocationCallOrder[0]).toBeLessThan(
		mockPending.mock.invocationCallOrder[0],
	);
	await Promise.resolve();
	expect(mockComplete).toHaveBeenCalledWith('A', true);
});
test('queued or failing native work never reports successful upload', async () => {
	mockRun.mockResolvedValueOnce('queued');
	mockPending.mockReturnValueOnce({ id: 'refresh', executionId: 'B' });
	load();
	await Promise.resolve();
	expect(mockComplete).toHaveBeenCalledWith('B', false);
});
test('task rejects errors and non-ticks; maps this invocation outcome honestly', async () => {
	load();
	const task = mockDefine.mock.calls[0][1] as (input: {
		data?: unknown;
		error?: unknown;
	}) => Promise<number>;
	expect(await task({ error: new Error('native') })).toBe(2);
	expect(await task({ data: { data: { type: 'news' } } })).toBe(1);
	expect(mockRun).not.toHaveBeenCalled();
	const data = {
		data: {
			dataString: JSON.stringify({
				type: 'tick',
				tick_seq: 3,
				local_date: '2026-10-30',
				window_start: '2026-10-30T06:00:00-04:00',
			}),
		},
	};
	mockRun
		.mockResolvedValueOnce('sent')
		.mockResolvedValueOnce('queued')
		.mockResolvedValueOnce('skipped')
		.mockResolvedValueOnce('failed');
	for (const value of [0, 2, 1, 2]) expect(await task({ data })).toBe(value);
});

test('native expiration aborts only the active execution signal', () => {
	mockRun.mockImplementationOnce(() => new Promise(() => {}));
	mockPending.mockReturnValueOnce({ id: 'refresh', executionId: 'A' });
	load();
	const signal = mockRun.mock.calls[0][2] as unknown as AbortSignal;
	expect(signal.aborted).toBe(false);
	const listener = mockListener.mock.calls[0][1] as (event: { endedExecutionId: string }) => void;
	listener({ endedExecutionId: 'older' });
	expect(signal.aborted).toBe(false);
	listener({ endedExecutionId: 'A' });
	expect(signal.aborted).toBe(true);
});
