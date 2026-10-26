# D34: Device-bound session available after first unlock

- Date: 2026-10-26, Status: accepted, Task: T2.2
- Owner approved all three T2.2 brief recommendations on 2026-10-26.

| ID | Decision | Choice | Why | Revisit if |
|---|---|---|---|---|
| D34 | Paired session Keychain accessibility | `AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY`, without biometric authentication; supersedes `WHEN_UNLOCKED_THIS_DEVICE_ONLY` in docs/09-security.md for this credential only. | A locked-phone background cold start must read its bearer after the first unlock following reboot. The credential stays device-bound, excluded from migration, and unavailable before that first unlock. | Device cold-start testing in T2.4 reveals an accessibility or threat-model problem. |

This deliberately increases accessibility while the phone is locked after its first unlock;
that tradeoff is required for the approved background wake design. Do not fall back to plaintext,
AsyncStorage, cloud sync, or a less restrictive Keychain mode when Keychain access fails.
A locked/unavailable Keychain read is an error, not proof that the owner revoked the session.
Physical-device locked startup still requires T2.4 verification.
