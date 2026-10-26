import {
	canonicalApiBase,
	normaliseCode,
	parsePairingQr,
	parsePairResponse,
	parseSession,
} from '../src/pairing/validation';

const base = 'https://laptop.local:8443/v1';
const session = {
	version: 1,
	apiBase: base,
	deviceId: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
	token: 'a'.repeat(43),
};
test('normalizes actual server alphabet and code length', () => {
	expect(normaliseCode('7k2m - 9qxd')).toBe('7K2M9QXD');
	for (const code of ['0000-OOOO', 'ABCDEFG', 'ABCDEFGHJK', 'x'.repeat(33)])
		expect(() => normaliseCode(code)).toThrow();
});
test('canonicalizes only supported HTTPS API roots', () => {
	expect(canonicalApiBase('https://LAPTOP.local:8443/v1/')).toBe(base);
	for (const url of [
		'http://laptop/v1',
		'https://a:b@laptop/v1',
		'https://laptop/v1?x=1',
		'https://laptop/v1#x',
		'https://laptop/v2',
		'https://0.0.0.0/v1',
		'https://[::]/v1',
		'https://laptop:99999/v1',
		'https://laptop/%76%31',
		'https://laptop\\evil/v1',
	])
		expect(() => canonicalApiBase(url)).toThrow();
});
test('only exact bounded QR shape populates confirmation', () => {
	expect(parsePairingQr(JSON.stringify({ api_base: base, code: '7K2M-9QXD' }))).toEqual({
		apiBase: base,
		code: '7K2M9QXD',
	});
	for (const qr of [
		'null',
		'[]',
		'exp://localhost',
		'{',
		'x'.repeat(2049),
		JSON.stringify({ api_base: base, code: '7K2M9QXD', token: 'bad' }),
	])
		expect(() => parsePairingQr(qr)).toThrow();
});
test('rejects incomplete old or unbound persisted credentials', () => {
	expect(parseSession(session)).toEqual(session);
	for (const value of [
		{ ...session, version: 2 },
		{ ...session, token: '' },
		{ ...session, deviceId: 'bad' },
		{ ...session, apiBase: undefined },
		{ ...session, extra: true },
	])
		expect(() => parseSession(value)).toThrow();
});
test('pair response cannot substitute another origin or malformed credential', () => {
	const value = {
		api_base: base,
		device_id: session.deviceId,
		token: session.token,
		server_time: '2026-10-26T12:00:00Z',
	};
	expect(parsePairResponse(value, base)).toEqual(session);
	expect(() => parsePairResponse({ ...value, api_base: 'https://other/v1' }, base)).toThrow();
	expect(() => parsePairResponse({ ...value, token: '' }, base)).toThrow();
});
