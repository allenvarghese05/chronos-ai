import * as SQLite from 'expo-sqlite';
import ChronosSensors from '../../modules/chronos-sensors/src';
import { deviceMetadata } from '../session/native';
import type { SessionController } from '../session/store';
import { sample } from '../wake/sample';
import { createTelemetryController } from './controller';
import { createOutbox, type Outbox } from './outbox';

let outbox: Outbox | undefined;
/** No token in SQLite; one private connection, opened only within authenticated controls. */
export function nativeTelemetry(session: SessionController) {
	outbox ??= createOutbox(SQLite.openDatabaseSync('chronos-telemetry.db'));
	return createTelemetryController({
		session,
		outbox,
		now: Date.now,
		sample: (source, context) => {
			const metadata = deviceMetadata();
			return sample(ChronosSensors, source, Date.now, metadata.tz, metadata.app_version, context);
		},
	});
}
