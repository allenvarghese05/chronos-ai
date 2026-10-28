import type { Telemetry } from '../wake/buildTelemetry';

export type Database = {
	execSync(sql: string): void;
	runSync(sql: string, ...params: (string | number | null)[]): unknown;
	getFirstSync<T>(sql: string, ...params: (string | number | null)[]): T | null;
	withTransactionSync(operation: () => void): void;
};
export type Row = {
	identity: string;
	seq: number;
	body: string;
	created_ms: number;
	retry_ms: number;
	attempts: number;
};
export const RETENTION_MS = 7_200_000;
export const MAX_ROWS = 256;

/** All operations are short synchronous transactions on one private connection; no await in SQL. */
export function createOutbox(db: Database) {
	const version =
		db.getFirstSync<{ user_version: number }>('PRAGMA user_version')?.user_version ?? 0;
	if (version > 1) throw new Error('Telemetry database version is unsupported.');
	db.execSync('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=200;');
	db.withTransactionSync(() => {
		db.execSync(`CREATE TABLE IF NOT EXISTS telemetry_counters(identity TEXT PRIMARY KEY,next_seq INTEGER NOT NULL);
		CREATE TABLE IF NOT EXISTS telemetry_outbox(identity TEXT NOT NULL,seq INTEGER NOT NULL,
		body TEXT NOT NULL,created_ms INTEGER NOT NULL,retry_ms INTEGER NOT NULL DEFAULT 0,
		attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(identity,seq));
		PRAGMA user_version=1;`);
	});
	const total = () =>
		db.getFirstSync<{ count: number }>('SELECT count(*) AS count FROM telemetry_outbox')?.count ??
		0;
	function prune(now: number): number {
		if (!Number.isSafeInteger(now)) throw new Error('Local telemetry clock unavailable.');
		const before = total();
		db.runSync(
			'DELETE FROM telemetry_outbox WHERE created_ms < ? OR created_ms > ?',
			now - RETENTION_MS,
			now,
		);
		return before - total();
	}
	function enqueue(identity: string, now: number, build: (seq: number) => Telemetry): Row {
		if (!identity || identity.length > 2048) throw new Error('Telemetry identity unavailable.');
		let row!: Row;
		db.withTransactionSync(() => {
			prune(now);
			if (total() >= MAX_ROWS) throw new Error('Telemetry queue is full. Retry delivery first.');
			const seq =
				db.getFirstSync<{ next_seq: number }>(
					'SELECT next_seq FROM telemetry_counters WHERE identity=?',
					identity,
				)?.next_seq ?? 1;
			if (!Number.isSafeInteger(seq) || seq < 1 || seq >= Number.MAX_SAFE_INTEGER)
				throw new Error('Telemetry sequence exhausted.');
			const value = build(seq);
			if (value.seq !== seq) throw new Error('Telemetry sequence mismatch.');
			const body = JSON.stringify(value);
			if (body.length > 32768) throw new Error('Telemetry snapshot is too large.');
			row = { identity, seq, body, created_ms: now, retry_ms: 0, attempts: 0 };
			db.runSync(
				'INSERT INTO telemetry_outbox(identity,seq,body,created_ms) VALUES(?,?,?,?)',
				identity,
				seq,
				body,
				now,
			);
			db.runSync(
				`INSERT INTO telemetry_counters(identity,next_seq) VALUES(?,?)
				ON CONFLICT(identity) DO UPDATE SET next_seq=excluded.next_seq`,
				identity,
				seq + 1,
			);
		});
		return row;
	}
	return {
		enqueue,
		prune,
		head: (identity: string) =>
			db.getFirstSync<Row>(
				'SELECT * FROM telemetry_outbox WHERE identity=? ORDER BY seq LIMIT 1',
				identity,
			),
		count: (identity: string) =>
			db.getFirstSync<{ count: number }>(
				'SELECT count(*) AS count FROM telemetry_outbox WHERE identity=?',
				identity,
			)?.count ?? 0,
		ack(identity: string, seq: number) {
			db.runSync('DELETE FROM telemetry_outbox WHERE identity=? AND seq=?', identity, seq);
		},
		defer(identity: string, seq: number, retryAt: number, attempts: number) {
			db.runSync(
				'UPDATE telemetry_outbox SET retry_ms=?,attempts=? WHERE identity=? AND seq=?',
				retryAt,
				attempts,
				identity,
				seq,
			);
		},
		retire(identity: string) {
			db.runSync('DELETE FROM telemetry_outbox WHERE identity=?', identity);
		},
	};
}
export type Outbox = ReturnType<typeof createOutbox>;
