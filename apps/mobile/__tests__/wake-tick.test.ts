import { parseTick } from '../src/wake/tick';

const data = {
	type: 'tick',
	tick_seq: 3,
	local_date: '2026-10-30',
	window_start: '2026-10-30T06:00:00-04:00',
};
test('SDK native data and dataString are decoded into a strict destination-free context', () => {
	const expected = { tickSeq: 3, localDate: '2026-10-30', windowStart: data.window_start };
	expect(parseTick({ notification: null, data, aps: { 'content-available': 1 } })).toEqual(
		expected,
	);
	expect(
		parseTick({
			data: {
				dataString: JSON.stringify({ ...data, url: 'https://evil.invalid', token: 'ignored' }),
			},
		}),
	).toEqual(expected);
});
test.each([
	null,
	[],
	{},
	{ data: [] },
	{ data: { dataString: '{' } },
	{ data: { dataString: 'a'.repeat(4097) } },
	{ data, actionIdentifier: 'tap' },
	{ data, aps: { alert: 'visible' } },
	{ data, aps: { sound: 'default' } },
	{ notification: { request: { content: { data } } } },
])('rejects unsupported or visible envelope %j', (input) => expect(parseTick(input)).toBeNull());
test.each([
	{ type: 'news' },
	{ tick_seq: -1 },
	{ tick_seq: 1.5 },
	{ tick_seq: Number.MAX_SAFE_INTEGER + 1 },
	{ local_date: '2026-02-30' },
	{ local_date: 'today' },
	{ window_start: '2026-10-30T06:00:00' },
	{ window_start: 'invalid' },
])('rejects invalid tick fields %j', (override) =>
	expect(parseTick({ data: { ...data, ...override } })).toBeNull(),
);
