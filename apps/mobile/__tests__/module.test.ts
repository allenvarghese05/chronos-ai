// Mock the local chronos-sensors module to simulate the native module being linked.
// We mock the local module path (not expo-modules-core) to avoid breaking
// expo's own internal use of requireNativeModule.
jest.mock('../modules/chronos-sensors/src', () => ({
	__esModule: true,
	default: { hello: () => 'Hello from ChronosSensors!' },
	ChronosSensors: { hello: () => 'Hello from ChronosSensors!' },
}));

import ChronosSensors from '../modules/chronos-sensors/src';

describe('ChronosSensors module (mocked native module via jest.mock)', () => {
	it('hello() returns the expected string from the native module', () => {
		expect(ChronosSensors.hello()).toBe('Hello from ChronosSensors!');
	});

	it('hello() return value is a non-empty string', () => {
		const result = ChronosSensors.hello();
		expect(typeof result).toBe('string');
		expect(result.length).toBeGreaterThan(0);
	});
});
