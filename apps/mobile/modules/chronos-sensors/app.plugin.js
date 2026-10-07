const { createRunOncePlugin } = require('@expo/config-plugins');

/**
 * chronos-sensors app plugin.
 * The module is autolinked by Expo Modules Core via expo-module.config.json.
 * This plugin exists as the required entry point for the plugins[] list in
 * app.config.ts; no additional Xcode changes are needed for hello() in T0.4.
 * T2.1 will extend this plugin to configure CoreMotion entitlements if required.
 */
const withChronosSensors = (config) => config;

module.exports = createRunOncePlugin(withChronosSensors, 'chronos-sensors', '0.1.0');
