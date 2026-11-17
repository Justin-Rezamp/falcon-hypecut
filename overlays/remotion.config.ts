// Remotion CLI config for the Falcon overlay cards.
// The Python side (`hypecut overlays ...`) passes the browser, codec and
// binaries directory on the command line, so this file stays minimal.
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('png');
Config.setOverwriteOutput(true);
