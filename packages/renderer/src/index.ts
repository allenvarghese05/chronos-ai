/**
 * Entry point — registers the Remotion root component.
 *
 * This file is the `--entry` for `remotion render` / `remotion still` /
 * `remotion studio`. Do not add any logic here; use Root.tsx instead.
 */

import { registerRoot } from 'remotion';
import { RemotionRoot } from './Root';

registerRoot(RemotionRoot);
