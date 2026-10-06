# ADR 0006: Every Curb write needs a person's yes, given to the operating system

Status: accepted

## Context

From R3, Curb writes: it adds deny rules and sandbox settings to an agent's
own settings, plants decoy files, and scrubs secrets out of transcripts.
The agent whose reach Curb cuts down can run `flanner` in its shell, and a
prompt-injected agent would happily run `flanner curb fix` with whatever
flags undo a protection, or answer a y/N question typed into its terminal.
So "the person agreed" has to mean something an agent cannot fake from
the shell it already has (Curb PRD §11.2).

## Decision

- **The operating system asks, not Curb.** The process about to write asks
  the OS to confirm a person is present, naming the change: Windows Hello,
  then the account password in the Windows Security prompt; Touch ID or the
  account password in macOS's own prompt; polkit's agent on a Linux
  desktop. None of these reads the terminal that started the command.
- **A yes is a grant:** single use, valid for two minutes, bound to the
  SHA-256 of the exact change (canonical JSON of each file's new
  contents), and held only in the memory of the process that asked. Every
  write path calls `Broker.redeem`, which raises `NoGrant` for a missing,
  spent, expired or mismatched grant, so a write without a grant fails.
- **The prompt shows who asked:** the process chain, such as
  `claude → bash → flanner`. It is a hint, not proof.
- **Abuse limit:** three refused or ignored approvals within ten minutes
  pause requests for an hour, and a desktop notification says so. The
  denials are kept in `~/.flanner/curb/approvals.json`, since the limit has
  to outlive one process.
- **No method, no writes.** Without a desktop session or an approval
  method, Curb stays read-only and prints the steps for a person to make.
- **The prompt's program is the system's own.** Each prompt is a program
  the operating system installs: `pkcheck`, `osascript`, Windows
  PowerShell. Curb runs it from the place the system puts it, found
  through Windows' own system directory or a fixed path, never through
  PATH or the current directory, and on POSIX only when root owns it and
  nobody else can write it. It runs with an environment of Curb's
  choosing: the session and display variables, a fixed PATH, and none of
  the variables that change which code a program loads. The adversary
  has a shell, so it sets all of those; a program it planted on PATH with
  `DISPLAY` set would otherwise have answered the prompt itself.

## Alternatives considered

- **A y/N question in the terminal.** An agent with shell access answers
  it.
- **A token on disk that unlocks writes.** An agent reads it.
- **`sudo`.** It confirms an administrator, not a person, caches its
  answer, and on most desktops can be satisfied from the terminal.
- **Asking in Curb's own desktop window.** Synthetic input can click a
  window Curb draws. The OS prompts are built to refuse it, or need a
  secret an agent does not hold.

## Consequences

- Password fallbacks are weaker than biometrics: a look-alike window could
  phish the password. Reports on password-only machines say so.
- The design gate, that synthetic keyboard and mouse input cannot complete
  an approval, is checked by hand on each OS before each release, and the
  release notes record the result. Automated tests replace the OS prompt
  with a stand-in.
- Headless machines get guided steps only. Approval from another enrolled
  device is a Later item.
- A test lab stands in for the prompt by installing its own `pkcheck` at
  `/usr/bin`, root-owned, in a device image. That is what the fixed place
  and the ownership check allow, and what checking a package signature
  would not. The lab is not a check of this gate; the hand check on each
  OS is.
