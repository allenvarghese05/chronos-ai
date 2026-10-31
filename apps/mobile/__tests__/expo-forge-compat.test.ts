/** Real Node/CommonJS consumers: do not substitute Jest's browser crypto implementation. */
import { spawnSync } from 'node:child_process';
import { resolve } from 'node:path';

const imports = `
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const certs = require('@expo/code-signing-certificates');
const cliRequire = createRequire(require.resolve('@expo/cli/package.json'));
const certRequire = createRequire(require.resolve('@expo/code-signing-certificates/package.json'));
`;
const certificate = `
const pair = certs.generateKeyPair();
const pem = certs.convertKeyPairToPEM(pair);
const now = Date.now();
const cert = certs.generateSelfSignedCodeSigningCertificate({
  keyPair: pair,
  validityNotBefore: new Date(now - 86400000),
  validityNotAfter: new Date(now + 86400000),
  commonName: 'Chronos ephemeral compatibility test'
});
const certPem = certs.convertCertificateToCertificatePEM(cert);
const parsed = certs.convertCertificatePEMToCertificate(certPem);
`;
function run(script: string) {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	const result = spawnSync(
		process.execPath,
		['-e', `${imports}\n${script}\nconsole.log('forge-compat-ok');`],
		{
			cwd: resolve(__dirname, '..'),
			env,
			encoding: 'utf8',
			timeout: 15_000,
		},
	);
	expect(result.error).toBeUndefined();
	expect(result.stderr).toBe('');
	expect(result.status).toBe(0);
	expect(result.stdout.trim()).toBe('forge-compat-ok');
}

test('both real Expo consumers resolve patched CommonJS Forge within their declared ranges', () => {
	run(`
const semver = require('semver');
for (const [consumer, loader] of [['@expo/cli', cliRequire], ['@expo/code-signing-certificates', certRequire]]) {
  const version = loader('node-forge/package.json').version;
  const range = require(consumer + '/package.json').dependencies['node-forge'];
  assert.equal(version, '1.4.1');
  assert.ok(semver.satisfies(version, range));
  assert.equal(typeof loader('node-forge').pki.certificateFromPem, 'function');
}
assert.equal(cliRequire.resolve('node-forge'), certRequire.resolve('node-forge'));
`);
});

test('Expo certificate tooling preserves PEM/CSR round trips and rejects invalid or mismatched material', () => {
	run(`${certificate}
const restoredPair = certs.convertKeyPairPEMToKeyPair(pem);
assert.equal(parsed.verify(parsed), true);
certs.validateSelfSignedCertificate(parsed, restoredPair);
assert.equal(certs.convertPublicKeyPEMToPublicKey(pem.publicKeyPEM).n.equals(pair.publicKey.n), true);
assert.equal(certs.convertPrivateKeyPEMToPrivateKey(pem.privateKeyPEM).n.equals(pair.privateKey.n), true);
const csr = certs.generateCSR(restoredPair, 'Chronos ephemeral CSR');
const restoredCSR = certs.convertCSRPEMToCSR(certs.convertCSRToCSRPEM(csr));
assert.equal(restoredCSR.verify(), true);
assert.throws(() => certs.convertCertificatePEMToCertificate('not a PEM certificate'));
assert.throws(() => certs.validateSelfSignedCertificate(parsed, certs.generateKeyPair()));
`);
}, 20_000);

test('actual Expo CLI signs UTF-8 manifests that Node crypto verifies and rejects tampering', () => {
	run(`${certificate}
const { verify } = require('node:crypto');
const { signManifestString } = require('@expo/cli/build/src/utils/codesigning');
const manifest = JSON.stringify({ id: 'chronos-compat', title: 'Chronos ☀️', launchAsset: { url: 'http://127.0.0.1:8081/index.ts.bundle' } });
const signed = signManifestString(manifest, { privateKey: pem.privateKeyPEM, certificateForPrivateKey: certPem });
const signature = Buffer.from(signed, 'base64');
assert.equal(verify('RSA-SHA256', Buffer.from(manifest, 'utf8'), certPem, signature), true);
assert.equal(verify('RSA-SHA256', Buffer.from(manifest.replace('index.ts', 'altered.ts'), 'utf8'), certPem, signature), false);
assert.equal(verify('RSA-SHA256', Buffer.from(manifest, 'utf8'), certPem, signature.subarray(0, signature.length - 1)), false);
const other = certs.generateKeyPair();
assert.throws(() => certs.signBufferRSASHA256AndVerify(other.privateKey, parsed, Buffer.from(manifest)));
`);
}, 20_000);
