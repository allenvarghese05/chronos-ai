/** Compatibility guard for the security override of Expo CLI's Zod dependency. */
type CliSchema = { safeParse(input: unknown): { success: boolean } };
const { PluginSchema, DevToolsPluginOutputSchema } =
	require('@expo/cli/build/src/start/server/DevToolsPlugin.schema') as {
		PluginSchema: CliSchema;
		DevToolsPluginOutputSchema: CliSchema;
	};
const { createMCPDevToolsExtensionSchema } =
	require('@expo/cli/build/src/start/server/createMCPDevToolsExtensionSchema') as {
		createMCPDevToolsExtensionSchema(plugin: unknown): CliSchema;
	};

const plugin = {
	packageName: 'chronos-test',
	packageRoot: '/tmp/chronos-test',
	cliExtensions: {
		description: 'Sensors',
		entryPoint: 'index.js',
		commands: [
			{
				name: 'read',
				title: 'Read sensors',
				environments: ['cli', 'mcp'],
				parameters: [
					{ name: 'count', type: 'number' },
					{ name: 'confirm', type: 'confirm' },
					{ name: 'label', type: 'text' },
				],
			},
		],
	},
};

describe('Expo CLI schemas with patched Zod', () => {
	it('accepts a devtools plugin and rejects an empty package name', () => {
		expect(PluginSchema.safeParse(plugin).success).toBe(true);
		expect(PluginSchema.safeParse({ ...plugin, packageName: '' }).success).toBe(false);
	});

	it('preserves parameter types and rejects unknown command fields', () => {
		const schema = createMCPDevToolsExtensionSchema(plugin);
		expect(
			schema.safeParse({ command: 'read', count: 2, confirm: true, label: 'sensor' }).success,
		).toBe(true);
		expect(schema.safeParse({ command: 'read', count: '2' }).success).toBe(false);
		expect(schema.safeParse({ command: 'read', extra: true }).success).toBe(false);
	});

	it('accepts text/audio/image output and rejects invalid media URLs', () => {
		expect(
			DevToolsPluginOutputSchema.safeParse([
				{ type: 'text', text: 'ok', level: 'info' },
				{ type: 'audio', url: 'https://example.com/a.mp3' },
				{ type: 'image', url: 'https://example.com/a.png' },
			]).success,
		).toBe(true);
		expect(DevToolsPluginOutputSchema.safeParse([{ type: 'audio', url: 'invalid' }]).success).toBe(
			false,
		);
	});
});
