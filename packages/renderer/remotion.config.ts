import { Config } from '@remotion/cli/config';

// Default render settings for the MorningReel composition.
// Width, height and fps are also set on the Composition component itself via
// calculateMetadata; these Config overrides align the Remotion Studio preview
// with the final render so no extra CLI flags are needed during development.
Config.setVideoImageFormat('png');

// Override the default size/fps so `remotion still` / `remotion render` without
// explicit --width/--height/--fps flags produce 1080×1920 @ 30 fps.
// (Config.setWidth/setHeight/setFps were removed in Remotion 4; use the
// override* variants which are the supported API in 4.0.534.)
Config.overrideWidth(1080);
Config.overrideHeight(1920);
Config.overrideFps(30);
