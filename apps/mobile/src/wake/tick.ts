export type Tick = { tickSeq: number; localDate: string; windowStart: string };
const object = (value: unknown): value is Record<string, unknown> =>
	value !== null && typeof value === 'object' && !Array.isArray(value);

/** Decode SDK57's native-normalized envelope; never accept a destination or credential. */
export function parseTick(envelope: unknown): Tick | null {
	if (!object(envelope) || 'actionIdentifier' in envelope) return null;
	const aps = envelope.aps;
	if (object(aps) && ['alert', 'sound', 'badge'].some((key) => key in aps)) return null;
	if (!object(envelope.data)) return null;
	let data: unknown = envelope.data;
	if ('dataString' in envelope.data) {
		if (typeof envelope.data.dataString !== 'string' || envelope.data.dataString.length > 4096)
			return null;
		try {
			data = JSON.parse(envelope.data.dataString);
		} catch {
			return null;
		}
	}
	if (!object(data) || data.type !== 'tick') return null;
	const { tick_seq: tickSeq, local_date: localDate, window_start: start } = data;
	if (typeof tickSeq !== 'number' || !Number.isSafeInteger(tickSeq) || tickSeq < 0) return null;
	if (typeof localDate !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(localDate)) return null;
	const date = Date.parse(`${localDate}T00:00:00Z`);
	if (!Number.isFinite(date) || new Date(date).toISOString().slice(0, 10) !== localDate)
		return null;
	if (
		typeof start !== 'string' ||
		start.length > 40 ||
		!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/.test(start) ||
		!Number.isFinite(Date.parse(start))
	)
		return null;
	return { tickSeq, localDate, windowStart: start };
}
