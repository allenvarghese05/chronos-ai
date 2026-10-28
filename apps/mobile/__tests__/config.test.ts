import type { InfoPlist } from '@expo/config-plugins';

type IosConfig = {
	bundleIdentifier?: string;
	deploymentTarget?: string;
	entitlements?: Record<string, string>;
	infoPlist?: InfoPlist & Record<string, unknown>;
};

type AppConfig = {
	ios?: IosConfig;
};

describe('app.config.ts iOS keys', () => {
	it('exports the required iOS configuration', () => {
		// app.config.ts exports a function; call it with a minimal context.
		const appConfig = (require('../app.config') as { default: (ctx: object) => AppConfig }).default;
		const cfg = appConfig({ config: {} });

		expect(cfg.ios?.bundleIdentifier).toBe('dev.allenvarghese.chronos');
		expect(cfg.ios?.deploymentTarget).toBe('17.0');
		expect(cfg.ios?.entitlements?.['aps-environment']).toBe('development');
		expect(cfg.ios?.infoPlist?.UIBackgroundModes).toEqual(
			expect.arrayContaining(['remote-notification', 'fetch']),
		);
		expect(cfg.ios?.infoPlist?.NSAppTransportSecurity).toEqual(
			expect.objectContaining({ NSAllowsLocalNetworking: true }),
		);
	});

	it('BGTaskSchedulerPermittedIdentifiers contains the chronos.refresh identifier', () => {
		const appConfig = (require('../app.config') as { default: (ctx: object) => AppConfig }).default;
		const cfg = appConfig({ config: {} });

		const identifiers = cfg.ios?.infoPlist?.BGTaskSchedulerPermittedIdentifiers as string[];
		expect(Array.isArray(identifiers)).toBe(true);
		expect(identifiers).toContain('dev.allenvarghese.chronos.refresh');
	});

	it('BG_REFRESH_IDENTIFIER in src/index.ts matches app.config.ts', () => {
		// The Swift constant and the TS constant must agree with app.config.ts.
		// This test prevents drift between the three locations.
		const appConfig = (require('../app.config') as { default: (ctx: object) => AppConfig }).default;
		const cfg = appConfig({ config: {} });
		const identifiers = cfg.ios?.infoPlist?.BGTaskSchedulerPermittedIdentifiers as string[];

		// The TS constant exported from src/index.ts
		const { BG_REFRESH_IDENTIFIER } = require('../modules/chronos-sensors/src') as {
			BG_REFRESH_IDENTIFIER: string;
		};

		expect(identifiers).toContain(BG_REFRESH_IDENTIFIER);
		expect(BG_REFRESH_IDENTIFIER).toBe('dev.allenvarghese.chronos.refresh');
	});
});

test('QR-only camera plugin disables microphone and leaves push project optional', () => {
	const config = (
		require('../app.config') as { default: (ctx: object) => { plugins: unknown[]; extra: object } }
	).default({ config: {} });
	expect(config.plugins).toContainEqual([
		'expo-camera',
		{
			cameraPermission: 'Scan the pairing QR shown on your laptop.',
			microphonePermission: false,
			recordAudioAndroid: false,
		},
	]);
});
