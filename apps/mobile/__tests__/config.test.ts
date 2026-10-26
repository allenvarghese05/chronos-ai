type IosConfig = {
	bundleIdentifier?: string;
	deploymentTarget?: string;
	entitlements?: Record<string, string>;
	infoPlist?: Record<string, unknown>;
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
