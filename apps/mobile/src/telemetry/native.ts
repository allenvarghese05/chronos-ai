import * as SQLite from 'expo-sqlite';
import ChronosSensors from '../../modules/chronos-sensors/src';
import { deviceMetadata } from '../session/native';
import type { SessionController } from '../session/store';
import { sample } from '../wake/sample';
import { createTelemetryController, type TelemetryController } from './controller';
import { createOutbox, type Outbox } from './outbox';

let outbox: Outbox | undefined;
const controllers = new WeakMap<SessionController, TelemetryController>();
/** No token in SQLite; one private connection, opened only within authenticated controls. */
export function nativeTelemetry(session: SessionController) {
	const existing = controllers.get(session);
	if (existing) return existing;
	outbox ??= createOutbox(SQLite.openDatabaseSync('chronos-telemetry.db'));
	const controller = createTelemetryController({
		session,
		outbox,
		now: Date.now,
		sample: (source, context, tickSeq) => {
			const metadata = deviceMetadata();
			return sample(
				ChronosSensors,
				source,
				Date.now,
				metadata.tz,
				metadata.app_version,
				context,
				tickSeq,
			);
		},
	});
	controllers.set(session, controller);
	return controller;
}
