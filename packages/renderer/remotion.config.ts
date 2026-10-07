import { Config } from '@remotion/cli/config';

// Default composition settings — MorningReel overrides durationInFrames via
// calculateMetadata. Width/height/fps are set here so the Remotion Studio
// preview matches the final render without any extra flags.
Config.setVideoImageFormat('png');
Config.setWidth(1080);
Config.setHeight(1920);
Config.setFps(30);
