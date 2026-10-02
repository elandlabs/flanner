"""Credentials on this machine that an agent could use if it read them.

Presence, names and scope only. No secret value is read into the result,
printed or stored: a file is checked for existence, structured files are
read for their key names and labels (profiles, hosts, contexts), and a
`.env` file for its variable names. Where a scope cannot be read locally the
credential counts as wide (Curb PRD §9.4), which is almost always.

Beyond the sources the PRD lists (gh, git, AWS, gcloud, Docker, kubeconfig,
SSH keys, `.env` files), three more are counted because they are the same
reach in practice: environment variables an agent inherits (E3 leaked one
through `/proc/self/environ`), package-registry tokens (E2 was an npm
token), and the agents' own logins.
"""

from __future__ import annotations

import configparser
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import agent_paths

#: Variable names that usually hold a secret. Matched on the name only.
SECRET_NAME = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|PASS|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|"
    r"CLIENT_?SECRET|CREDENTIALS?|AUTH|PAT|SESSION_?KEY)(?:$|_)",
    re.IGNORECASE,
)
#: Names that match the pattern but never hold one.
NOT_SECRET = frozenset(
    {
        "GPG_AGENT_INFO",
        "SSH_AUTH_SOCK",
        "XAUTHORITY",
        "PASSWORD_STORE_DIR",
        "AUTH_SOCK",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
    }
)
#: How deep a project is searched for `.env` files, and what is never entered.
DOTENV_DEPTH = 4
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", "dist", "build", ".next"}
)
TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")


@dataclass(frozen=True)
class Credential:
    kind: str
    category: str
    #: What the terminal may say: a category, never a location or a name.
    label: str
    paths: tuple[Path, ...] = ()
    #: Reached by running something (an environment variable, `gh auth token`,
    #: an SSH agent) rather than by opening a file.
    via_shell: bool = False
    names: tuple[str, ...] = ()
    identity: str | None = None
    expires: str | None = None
    wide: bool = True
    scope_known: bool = False
    notes: tuple[str, ...] = field(default=())


def find(home: Path, cwd: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    """Every credential source Curb knows, as found under `home` and `cwd`."""
    found: list[Credential] = []
    for finder in (
        _aws,
        _gcloud,
        _azure,
        _gh,
        _git,
        _docker,
        _kube,
        _ssh,
        _registries,
        _agent_logins,
    ):
        found.extend(finder(home, env, platform))
    found.extend(_dotenv(cwd))
    found.extend(_environment(env))
    return found


def _config_home(home: Path, env: Mapping[str, str], platform: str) -> Path:
    if platform == "win32":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming")
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config")


def _ini_sections(path: Path) -> list[str]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return []
    return parser.sections()


def _ini_keys(path: Path) -> dict[str, list[str]]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return {}
    return {section: list(parser[section].keys()) for section in parser.sections()}


def _aws(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    found = []
    shared = Path(env.get("AWS_SHARED_CREDENTIALS_FILE") or home / ".aws" / "credentials")
    if shared.is_file():
        keyed = [
            section
            for section, keys in _ini_keys(shared).items()
            if "aws_access_key_id" in keys or "aws_secret_access_key" in keys
        ]
        found.append(
            Credential(
                "aws",
                "cloud",
                "AWS credentials file",
                (shared,),
                names=tuple(keyed),
                identity=", ".join(keyed) or None,
            )
        )
    cache = home / ".aws" / "sso" / "cache"
    tokens = sorted(cache.glob("*.json")) if cache.is_dir() else []
    if tokens:
        expiry = _latest_expiry(tokens)
        found.append(
            Credential("aws-sso", "cloud", "AWS SSO token cache", tuple(tokens), expires=expiry)
        )
    return found


def _latest_expiry(paths: Iterable[Path]) -> str | None:
    """The latest `expiresAt` among cached tokens: metadata, read without the token."""
    latest: str | None = None
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = data.get("expiresAt") if isinstance(data, dict) else None
        if isinstance(value, str) and (latest is None or value > latest):
            latest = value
    return latest


def _gcloud(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    base = Path(env.get("CLOUDSDK_CONFIG") or _config_home(home, env, platform) / "gcloud")
    files = [
        base / "application_default_credentials.json",
        base / "credentials.db",
        base / "access_tokens.db",
    ]
    legacy = base / "legacy_credentials"
    present = [path for path in files if path.is_file()]
    if legacy.is_dir():
        present.append(legacy)
    if not present:
        return []
    account = None
    active = base / "configurations" / "config_default"
    if active.is_file():
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            parser.read(active, encoding="utf-8")
            account = parser.get("core", "account", fallback=None)
        except (configparser.Error, OSError, UnicodeDecodeError):
            account = None
    return [
        Credential("gcloud", "cloud", "Google Cloud credentials", tuple(present), identity=account)
    ]


def _azure(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    base = Path(env.get("AZURE_CONFIG_DIR") or home / ".azure")
    files = [
        base / name
        for name in ("msal_token_cache.json", "accessTokens.json", "msal_token_cache.bin")
    ]
    present = tuple(path for path in files if path.is_file())
    return [Credential("azure", "cloud", "Azure CLI tokens", present)] if present else []


def _gh(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    base = (
        Path(env["GH_CONFIG_DIR"])
        if env.get("GH_CONFIG_DIR")
        else (_config_home(home, env, platform) / ("GitHub CLI" if platform == "win32" else "gh"))
    )
    hosts = base / "hosts.yml"
    if not hosts.is_file():
        return []
    users: list[str] = []
    stored_in_file = False
    try:
        data = yaml.safe_load(hosts.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        data = {}  # a malformed file must not stop the scan
    if isinstance(data, dict):
        for host, entry in data.items():
            if not isinstance(entry, dict):
                continue
            stored_in_file |= "oauth_token" in entry
            for user in (entry.get("users") or {}) if isinstance(entry.get("users"), dict) else {}:
                stored_in_file |= "oauth_token" in (entry["users"][user] or {})
            if isinstance(entry.get("user"), str):
                users.append(f"{entry['user']}@{host}")
    if stored_in_file:
        return [
            Credential(
                "gh",
                "git host",
                "GitHub CLI token",
                (hosts,),
                names=tuple(users),
                identity=", ".join(users) or None,
            )
        ]
    return [
        Credential(
            "gh",
            "git host",
            "GitHub CLI token (in the OS keyring, readable with `gh auth token`)",
            via_shell=True,
            names=tuple(users),
            identity=", ".join(users) or None,
        )
    ]


def _git(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    found = []
    for path in (
        home / ".git-credentials",
        _config_home(home, env, platform) / "git" / "credentials",
    ):
        if not path.is_file():
            continue
        hosts: list[str] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        if lines:
            for line in lines:
                match = re.match(r"\s*[a-z]+://(?:([^:@/\s]+)(?::[^@/\s]*)?@)?([^/\s:]+)", line)
                if match:
                    hosts.append(
                        f"{match.group(1)}@{match.group(2)}" if match.group(1) else match.group(2)
                    )
        found.append(
            Credential(
                "git",
                "git host",
                "Git stored credentials",
                (path,),
                names=tuple(hosts),
                identity=", ".join(hosts) or None,
            )
        )
    return found


def _docker(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    path = Path(env.get("DOCKER_CONFIG") or home / ".docker") / "config.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    raw_auths = data.get("auths")
    auths = raw_auths if isinstance(raw_auths, dict) else {}
    inline = sorted(
        host
        for host, entry in auths.items()
        if isinstance(entry, dict) and (entry.get("auth") or entry.get("identitytoken"))
    )
    found = []
    if inline:
        found.append(
            Credential(
                "docker",
                "container registry",
                "Docker registry logins",
                (path,),
                names=tuple(inline),
            )
        )
    helper = data.get("credsStore") or data.get("credHelpers")
    if helper and auths:
        helped = sorted(host for host in auths if host not in inline)
        if helped:
            found.append(
                Credential(
                    "docker-helper",
                    "container registry",
                    "Docker registry logins (in a credential helper)",
                    via_shell=True,
                    names=tuple(helped),
                )
            )
    return found


def _kube(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    separator = ";" if platform == "win32" else ":"
    configured = env.get("KUBECONFIG")
    paths = (
        [Path(p) for p in configured.split(separator) if p]
        if configured
        else [home / ".kube" / "config"]
    )
    present = tuple(path for path in paths if path.is_file())
    if not present:
        return []
    contexts: list[str] = []
    for path in present:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            data = {}  # a malformed kubeconfig must not stop the scan
        for entry in data.get("contexts", []) if isinstance(data, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                contexts.append(entry["name"])
    return [Credential("kube", "kubernetes", "Kubernetes config", present, names=tuple(contexts))]


def _ssh(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    found = []
    base = home / ".ssh"
    if base.is_dir():
        keys = []
        for path in sorted(base.iterdir()):
            if not path.is_file() or path.suffix == ".pub":
                continue
            name = path.name
            if (
                name.startswith("id_")
                or name.endswith(".pem")
                or name.endswith(".key")
                or (base / f"{name}.pub").is_file()
            ):
                keys.append(path)
        if keys:
            found.append(
                Credential(
                    "ssh",
                    "SSH",
                    "SSH private keys",
                    tuple(keys),
                    names=tuple(path.name for path in keys),
                )
            )
    if env.get("SSH_AUTH_SOCK"):
        found.append(
            Credential(
                "ssh-agent", "SSH", "SSH agent (keys usable without the files)", via_shell=True
            )
        )
    return found


def _registries(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    found = []
    npmrc = Path(env.get("NPM_CONFIG_USERCONFIG") or home / ".npmrc")
    if npmrc.is_file() and _has_line(npmrc, re.compile(r"(_authToken|_auth|_password)\s*=")):
        found.append(Credential("npm", "package registry", "npm registry token", (npmrc,)))
    pypirc = home / ".pypirc"
    if pypirc.is_file():
        sections = [s for s, keys in _ini_keys(pypirc).items() if "password" in keys]
        if sections:
            found.append(
                Credential(
                    "pypi",
                    "package registry",
                    "PyPI upload credentials",
                    (pypirc,),
                    names=tuple(sections),
                )
            )
    netrc = home / ("_netrc" if platform == "win32" else ".netrc")
    if netrc.is_file() and _has_line(netrc, re.compile(r"\bpassword\b")):
        found.append(Credential("netrc", "git host", "netrc passwords", (netrc,)))
    return found


def _has_line(path: Path, pattern: re.Pattern[str]) -> bool:
    try:
        return any(pattern.search(line) for line in path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return False


def _agent_logins(home: Path, env: Mapping[str, str], platform: str) -> list[Credential]:
    found = []
    claude = agent_paths.claude_config_dir() / ".credentials.json"
    if claude.is_file():
        found.append(Credential("claude-login", "AI provider", "Claude Code login", (claude,)))
    codex = agent_paths.codex_home() / "auth.json"
    if codex.is_file():
        found.append(Credential("codex-login", "AI provider", "Codex login", (codex,)))
    return found


def dotenv_files(cwd: Path) -> list[Path]:
    """`.env` files under the project, four levels down, templates left out."""
    found: list[Path] = []
    root_depth = len(cwd.parts)
    for current, dirs, filenames in os.walk(cwd):
        here = Path(current)
        if len(here.parts) - root_depth >= DOTENV_DEPTH:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found.extend(
            here / filename
            for filename in filenames
            if (filename == ".env" or filename.startswith(".env."))
            and not filename.endswith(TEMPLATE_SUFFIXES)
        )
    return found


def _dotenv(cwd: Path) -> list[Credential]:
    files: list[Path] = []
    names: list[str] = []
    for path in dotenv_files(cwd):
        secret = _secret_names(path)
        if secret:
            files.append(path)
            names.extend(secret)
    if not files:
        return []
    return [
        Credential(
            "dotenv",
            "project .env",
            "Project .env files",
            tuple(files),
            names=tuple(sorted(set(names))),
        )
    ]


def _secret_names(path: Path) -> list[str]:
    """Variable names in a `.env` file that look like secrets. Never values."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S?)", line)
        if match and match.group(2) and is_secret_name(match.group(1)):
            names.append(match.group(1))
    return names


def is_secret_name(name: str) -> bool:
    return name.upper() not in NOT_SECRET and bool(SECRET_NAME.search(name))


def _environment(env: Mapping[str, Any]) -> list[Credential]:
    names = sorted(name for name, value in env.items() if value and is_secret_name(name))
    if not names:
        return []
    return [
        Credential(
            "env",
            "environment",
            "Secrets in the environment agents inherit",
            via_shell=True,
            names=tuple(names),
        )
    ]
