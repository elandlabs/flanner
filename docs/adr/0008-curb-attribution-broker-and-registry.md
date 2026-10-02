# ADR 0008: Agent commits are signed by a broker with per-agent keys, checked against a signed registry

Status: accepted. The threat model below is waiting for its review, which
the R7 release needs (Curb PRD §10.15).

## Context

Teams want to know which commits an agent made. A signature proves who
held a key, not who wrote the code, so Curb offers attribution, not
identity. It has to hold up against other machines claiming to be an
agent, one device revoking another's keys, and the record being changed
afterwards, and it has to say what it cannot do.

## Decision

- **Keys.** Each agent on each device has its own random Ed25519 key,
  never derived from the device's identity key. The private half lives
  only in the OS credential store; with none, attribution is off, because
  an agent can read the user's files.
- **The broker.** Each agent's settings carry git configuration in its
  environment (`GIT_CONFIG_*` for Claude Code's `env`, Codex's
  `shell_environment_policy.set`), so only agent sessions sign:
  `gpg.format = ssh`, `commit.gpgsign = true`, and `gpg.ssh.program =
  flanner-curb-sign`. The broker signs only while the action log shows that
  agent running a shell command, with the agent's current key, and logs
  the commit id, the key and the session. Other ssh-keygen operations pass
  through. Signatures are OpenSSH's SSHSIG, so `git verify-commit`,
  `ssh-keygen` and GitHub read them.
- **Registration** carries two signatures: the device's, on the request,
  and the key's own over the device id, the public key, a fresh nonce and
  any key it replaces. Ownership of a key never moves.
- **The registry** is one document per organization, signed by the issuer
  key devices already trust, listing every key with its owner and status.
  A device keeps the newest one that verifies, names its organization, is
  not older, matches exactly at the same version, and keeps every
  revocation and owner it has seen. Revocations seen once are kept forever.
- **Five states.** Attributed; attributed with a retired key; untrusted
  because revoked; key status unknown when no fresh registry can say, so
  an expired registry never fails open; unattributed.
- **Rotation** replaces a key every 90 days. The retired private half is
  deleted, so it signs nothing new, and its commits stay attributed.

## Threat model

| Threat | Outcome |
|---|---|
| Another machine signs as an agent | Its key is not in the registry, or not registered to that agent's device: unattributed |
| A device registers or replaces another device's key | Refused: the key's own proof binds it to the asking device, and the control plane binds each fingerprint to one device for good |
| A replayed registration | Refused: each nonce is single use |
| An older registry is replayed to undo a revocation | Refused: versions never fall, and known revocations are kept |
| A compromised control plane drops a revocation or moves a key | Refused by the device's acceptance rules, and alerted |
| The registry cannot be refreshed | Known revocations still apply; everything else is "key status unknown", never attributed |
| A commit is backdated to before a revocation | Still untrusted: a commit's date can be forged, so a revoked key's commits all count as untrusted |
| A signature is moved to other content | Fails verification: SSHSIG covers the exact commit payload |
| An agent reads its own key | Not from disk: the key is only in the OS credential store. A process running as the same user may still reach the store |
| The developer, or malware running as the same user, calls the broker inside a session | **Not protected.** The broker labels agent commits; it cannot tell who drove the session. The docs say so |
| The action log is forged to fake a session | Not protected, for the same reason: the log is the user's own |

## Alternatives considered

- **Keys derived from the device identity:** couples commit signing to the
  device's root key, so a leak of either would be a leak of both.
- **The device key alone on registration:** proves which device asked,
  not that it holds the key, so a device could register another's key.
- **A revocation list without a registry:** a device that joins later
  cannot tell whether a key belongs to the organization at all.
- **A registry plus a separate revocation list:** two documents to keep
  consistent and fresh.
- **Treating an expired registry as current:** fails open after a revoke.
- **Global git configuration:** the developer's own commits would go
  through the broker and fail outside agent sessions.

## Consequences

- Agent commits need the action log's hooks; `flanner curb attribution
  --setup` installs them with the git configuration, behind one approval.
- Inside Codex's sandbox the broker must still reach the OS credential
  store; where it cannot, the commit fails to sign rather than signing
  without a key.
- GitHub shows "Verified" only once each new public key is added there,
  through the person's own `gh` sign-in, at setup and at each rotation.
