/**
 * Generates a 3-second silent WAV file at 16 kHz, mono, 16-bit PCM.
 * Output: public/audio/silence-3s.wav (~96 KB)
 *
 * Run once: node scripts/gen-silence.mjs
 * The WAV is committed to git so render tests work without ffmpeg.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

const SAMPLE_RATE = 16000; // 16 kHz — matches whisper.cpp input
const CHANNELS = 1; // mono
const BIT_DEPTH = 16; // 16-bit PCM
const DURATION_SEC = 3;

const numSamples = SAMPLE_RATE * DURATION_SEC * CHANNELS;
const dataBytes = numSamples * (BIT_DEPTH / 8);
const headerSize = 44;
const totalSize = headerSize + dataBytes;

const buf = Buffer.alloc(totalSize, 0);

// RIFF chunk
buf.write('RIFF', 0, 'ascii');
buf.writeUInt32LE(totalSize - 8, 4); // file size minus RIFF header
buf.write('WAVE', 8, 'ascii');

// fmt sub-chunk
buf.write('fmt ', 12, 'ascii');
buf.writeUInt32LE(16, 16); // sub-chunk size (16 for PCM)
buf.writeUInt16LE(1, 20); // AudioFormat: 1 = PCM
buf.writeUInt16LE(CHANNELS, 22); // NumChannels
buf.writeUInt32LE(SAMPLE_RATE, 24); // SampleRate
buf.writeUInt32LE(SAMPLE_RATE * CHANNELS * (BIT_DEPTH / 8), 28); // ByteRate
buf.writeUInt16LE(CHANNELS * (BIT_DEPTH / 8), 32); // BlockAlign
buf.writeUInt16LE(BIT_DEPTH, 34); // BitsPerSample

// data sub-chunk
buf.write('data', 36, 'ascii');
buf.writeUInt32LE(dataBytes, 40); // sub-chunk size
// rest of buffer is zero-initialised (silence)

const outDir = join(ROOT, 'public', 'audio');
mkdirSync(outDir, { recursive: true });
const outPath = join(outDir, 'silence-3s.wav');
writeFileSync(outPath, buf);

// eslint-disable-next-line no-console
console.log(`Wrote ${outPath} (${buf.length} bytes, ${DURATION_SEC}s @ ${SAMPLE_RATE}Hz mono)`);
