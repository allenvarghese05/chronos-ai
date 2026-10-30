import { getPathFromState } from 'expo-router/build/fork/getPathFromState';
import { getStateFromPath } from 'expo-router/build/react-navigation/core/getStateFromPath';
import * as queryString from 'query-string';

const decode = require('decode-uri-component') as (value: string) => string;

test('patched decoder retains the callable CommonJS shape required by query-string7', () => {
	expect(typeof decode).toBe('function');
	expect(decode('%C3%A5%zz')).toBe('å%zz');
	expect(decode('%F0%9F%98%80')).toBe('😀');
	expect(() => decode(undefined as unknown as string)).toThrow(TypeError);
});
test('real query-string preserves unicode, plus, repeated, empty and malformed query values', () => {
	const parsed = queryString.parse('name=Andr%C3%A9&space=a+b&tag=one&tag=two&empty=&flag&bad=%ZZ');
	expect(parsed).toEqual({
		name: 'André',
		space: 'a b',
		tag: ['one', 'two'],
		empty: '',
		flag: null,
		bad: '%ZZ',
	});
	expect(queryString.parse(queryString.stringify(parsed))).toEqual(parsed);
});
test('fixed decoder handles bounded distinct malformed percent runs through its actual consumer', () => {
	let encoded = '';
	for (let n = 0; n < 4096; n++)
		encoded += `%${Math.floor(n / 256)
			.toString(16)
			.padStart(2, '0')}%${(n % 256).toString(16).padStart(2, '0')}x`;
	encoded += '%zz';
	expect(encoded.length).toBeLessThan(32768);
	const parsed = queryString.parse(`value=${encoded}&neighbor=%E2%9C%93`);
	expect(typeof parsed.value).toBe('string');
	expect((parsed.value as string).endsWith('%zz')).toBe(true);
	expect(parsed.neighbor).toBe('✓');
});
test('actual Expo Router parse and serialization callers round-trip the patched query-string', () => {
	const options = { screens: { Pairing: 'pairing' } };
	const state = getStateFromPath('/pairing?name=Andr%C3%A9&search=a%2Bb&bad=%ZZ', options);
	expect(state?.routes[0]).toMatchObject({
		name: 'Pairing',
		params: { name: 'André', search: 'a+b', bad: '%ZZ' },
	});
	if (!state) throw new Error('Expo Router failed to parse the test route');
	const path = getPathFromState(state, options);
	expect(path).toContain('/pairing?');
	const restored = getStateFromPath(path, options);
	expect(restored?.routes[0].params).toEqual(state.routes[0].params);
});
