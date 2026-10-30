import * as Device from 'expo-device';
import * as TaskManager from 'expo-task-manager';
import { AppState, Platform } from 'react-native';
import ChronosSensors, {
	BG_REFRESH_IDENTIFIER,
	type BGRefreshEvent,
} from '../../modules/chronos-sensors/src';
import { deviceMetadata, notificationProjectId, sessionController } from '../session/native';
import { nativeTelemetry } from '../telemetry/native';
import { createWakeRuntime } from './runtime';
import { parseTick } from './tick';

export const SILENT_PUSH_TASK = 'dev.allenvarghese.chronos.silent-push';
// SDK57 enum values, checked against installed source in wake-bootstrap.test.ts.
// A type-only dependency avoids notifications' eager native Keychain side effects on Simulator.
const Result = { NewData: 0, NoData: 1, Failed: 2 } as const satisfies Record<
	keyof typeof import('expo-notifications').BackgroundNotificationTaskResult,
	import('expo-notifications').BackgroundNotificationTaskResult
>;
let registrationStatus = 'Background push registration has not run.';
let scheduleStatus = 'No refresh requested yet.';
export const wakeSetupStatus = () => ({ registrationStatus, scheduleStatus });
const setupListeners = new Set<() => void>();
export function subscribeWakeSetup(listener: () => void) {
	setupListeners.add(listener);
	return () => {
		setupListeners.delete(listener);
	};
}
function changed() {
	for (const listener of setupListeners) listener();
}
export const wakeRuntime = createWakeRuntime({
	session: sessionController,
	telemetry: () => nativeTelemetry(sessionController),
	metadata: deviceMetadata,
	now: Date.now,
	elapsed: () => performance.now(),
	schedule: () => {
		if (!sessionController.lease()) return;
		const ok = ChronosSensors.scheduleBackgroundRefresh(BG_REFRESH_IDENTIFIER, 15 * 60);
		scheduleStatus = ok
			? 'Background refresh requested; iOS chooses when it runs.'
			: 'Background refresh request unavailable. Foreground sampling remains available.';
		changed();
	},
});

TaskManager.defineTask(SILENT_PUSH_TASK, async ({ data, error }) => {
	if (error) return Result.Failed;
	const tick = parseTick(data);
	if (!tick) return Result.NoData;
	const outcome = await wakeRuntime.run('silent_push', tick);
	return outcome === 'sent'
		? Result.NewData
		: outcome === 'skipped'
			? Result.NoData
			: Result.Failed;
});

const nativeRuns = new Map<string, AbortController>();
function handlePendingRefresh(event?: BGRefreshEvent) {
	if (event?.endedExecutionId) nativeRuns.get(event.endedExecutionId)?.abort();
	const pending = ChronosSensors.takePendingBackgroundRefresh();
	if (!pending) return;
	const abort = new AbortController();
	nativeRuns.set(pending.executionId, abort);
	void wakeRuntime.run('bg_refresh', undefined, abort.signal).then(
		(outcome) => {
			nativeRuns.delete(pending.executionId);
			ChronosSensors.completeBackgroundRefresh(pending.executionId, outcome === 'sent');
		},
		() => {
			nativeRuns.delete(pending.executionId);
			ChronosSensors.completeBackgroundRefresh(pending.executionId, false);
		},
	);
}
// Listener before claim: either availability signal or startup claim drains the pending job once.
ChronosSensors.addListener('backgroundRefresh', handlePendingRefresh);
handlePendingRefresh();

function foreground() {
	wakeRuntime.foreground(AppState.currentState === 'active');
}
AppState.addEventListener('change', (state) => wakeRuntime.foreground(state === 'active'));
sessionController.store.subscribe(foreground);
void sessionController
	.restore()
	.then(foreground)
	.catch(() => {});

// Native notification import can read Keychain during module initialization. Gate it before load.
if (Platform.OS === 'ios' && Device.isDevice && notificationProjectId()) {
	try {
		const Notifications: typeof import('expo-notifications') = require('expo-notifications');
		void Notifications.registerTaskAsync(SILENT_PUSH_TASK).then(
			() => {
				registrationStatus =
					'Background push handler registered; device delivery remains subject to iOS.';
				changed();
			},
			() => {
				registrationStatus =
					'Background push registration failed. Check the signed build and APNs setup.';
				changed();
			},
		);
	} catch {
		registrationStatus = 'Background push registration unavailable in this build.';
	}
} else {
	registrationStatus =
		'Background push needs a signed physical iPhone and configured Expo project. Simulator delivery is not physical acceptance.';
}
