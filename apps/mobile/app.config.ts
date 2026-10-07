import type { ConfigContext, ExpoConfig } from 'expo/config';

export default (_ctx: ConfigContext): ExpoConfig => ({
	name: 'Chronos',
	slug: 'chronos',
	version: process.env.VERSION ?? '0.0.1',
	orientation: 'portrait',
	icon: './assets/icon.png',
	userInterfaceStyle: 'automatic',
	assetBundlePatterns: ['**/*'],
	ios: {
		supportsTablet: false,
		bundleIdentifier: 'dev.allenvarghese.chronos',
		deploymentTarget: '17.0',
		infoPlist: {
			NSMotionUsageDescription: "Chronos reads step and walking history to notice when you're up.",
			UIBackgroundModes: ['remote-notification', 'fetch'],
			BGTaskSchedulerPermittedIdentifiers: ['dev.allenvarghese.chronos.refresh'],
			NSAppTransportSecurity: { NSAllowsLocalNetworking: true },
		},
		entitlements: { 'aps-environment': 'development' },
	},
	plugins: [
		'expo-router',
		'expo-dev-client',
		'expo-notifications',
		'expo-secure-store',
		'expo-video',
		'./modules/chronos-sensors/app.plugin.js',
	],
	scheme: 'chronos',
});
