import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { createOutbox, type Database, MAX_ROWS, RETENTION_MS } from '../src/telemetry/outbox';
import type { Telemetry } from '../src/wake/buildTelemetry';

const body = (seq: number) => ({ seq, device_ts: '2026-10-28T12:00:00Z' }) as Telemetry;
function adapter(db: DatabaseSync): Database {
	return {
		execSync: (sql) => db.exec(sql),
		runSync: (sql, ...params) => db.prepare(sql).run(...params),
		getFirstSync: <T>(sql: string, ...params: (string | number | null)[]) =>
			(db.prepare(sql).get(...params) as T | undefined) ?? null,
		withTransactionSync(operation) {
			db.exec('BEGIN');
			try {
				operation();
				db.exec('COMMIT');
			} catch (error) {
				db.exec('ROLLBACK');
				throw error;
			}
		},
	};
}
let folder: string;
beforeEach(() => {
	folder = mkdtempSync(join(tmpdir(), 'chronos-outbox-'));
});
afterEach(() => rmSync(folder, { recursive: true, force: true }));
test('real SQLite reopen preserves immutable body, seq and counter across lost ACK', () => {
	const file = join(folder, 'outbox.db');
	let db = new DatabaseSync(file);
	let queue = createOutbox(adapter(db));
	const first = queue.enqueue('origin|device1', 100, body);
	queue.enqueue('origin|device1', 101, body);
	db.close();
	db = new DatabaseSync(file);
	queue = createOutbox(adapter(db));
	expect(queue.head('origin|device1')).toEqual(first);
	queue.ack('origin|device1', first.seq);
	expect(queue.head('origin|device1')?.seq).toBe(2);
	queue.ack('origin|device1', 2);
	expect(queue.enqueue('origin|device1', 102, body).seq).toBe(3);
	db.close();
});
test('rollback between insert and counter update leaves no partial state', () => {
	const db = new DatabaseSync(':memory:');
	const queue = createOutbox(adapter(db));
	db.exec(
		`CREATE TRIGGER fail_counter BEFORE INSERT ON telemetry_counters BEGIN SELECT RAISE(ABORT,'test interruption'); END;`,
	);
	expect(() => queue.enqueue('identity', 100, body)).toThrow();
	expect(queue.count('identity')).toBe(0);
	db.exec('DROP TRIGGER fail_counter');
	expect(queue.enqueue('identity', 101, body).seq).toBe(1);
	db.close();
});
test('strict retention and backward clock delete rows without sequence reuse', () => {
	const db = new DatabaseSync(':memory:');
	const queue = createOutbox(adapter(db));
	queue.enqueue('identity', 100, body);
	expect(queue.prune(100 + RETENTION_MS)).toBe(0);
	expect(queue.prune(101 + RETENTION_MS)).toBe(1);
	expect(queue.enqueue('identity', 100, body).seq).toBe(2);
	expect(queue.prune(99)).toBe(1);
	expect(queue.enqueue('identity', 99, body).seq).toBe(3);
	db.close();
});
test('identity binds ACK/defer/counters and retire never resets sequence', async () => {
	const db = new DatabaseSync(':memory:');
	const queue = createOutbox(adapter(db));
	const rows = await Promise.all(
		Array.from({ length: 8 }, async () => queue.enqueue('A|1', 100, body)),
	);
	expect(rows.map((row) => row.seq)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
	queue.enqueue('A|2', 100, body);
	queue.enqueue('B|1', 100, body);
	queue.defer('A|2', 1, 200, 1);
	queue.ack('B|1', 1);
	expect(queue.head('A|1')?.retry_ms).toBe(0);
	expect(queue.head('A|2')?.retry_ms).toBe(200);
	queue.retire('A|1');
	expect(queue.enqueue('A|1', 100, body).seq).toBe(9);
	expect(queue.count('A|2')).toBe(1);
	db.close();
});
test('queue cap, oversized snapshot and sequence exhaustion fail explicitly', () => {
	const db = new DatabaseSync(':memory:');
	const queue = createOutbox(adapter(db));
	expect(() =>
		queue.enqueue('identity', 100, (seq) => ({ ...body(seq), app_version: 'x'.repeat(33000) })),
	).toThrow('large');
	db.prepare('INSERT INTO telemetry_counters VALUES(?,?)').run(
		'exhausted',
		Number.MAX_SAFE_INTEGER,
	);
	expect(() => queue.enqueue('exhausted', 100, body)).toThrow('exhausted');
	for (let n = 0; n < MAX_ROWS; n++) queue.enqueue('identity', 100, body);
	expect(() => queue.enqueue('other', 100, body)).toThrow('full');
	db.close();
});
