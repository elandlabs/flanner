"""Structured exception hierarchy.

Every error flanner raises is a FlannerError subclass, so callers can
catch the whole family or a specific failure mode.
"""


class FlannerError(Exception):
    """Base class for all flanner errors."""


class DatabaseError(FlannerError):
    """Database is unavailable, uninitialized, or an operation failed."""


class NotFoundError(DatabaseError, ValueError):
    """A requested project, plan file, or version does not exist.

    Subclasses ValueError so pre-hierarchy `except ValueError` callers
    keep working (same pattern as json.JSONDecodeError).
    """


class DuplicateError(DatabaseError, ValueError):
    """An entity with the same identity already exists.

    Subclasses ValueError for backward compatibility; see NotFoundError.
    """


class ValidationError(FlannerError, ValueError):
    """A value outside the vocabulary the schema allows.

    Also a ValueError, matching NotFoundError and DuplicateError, so a
    caller that only knows the stdlib still catches it.
    """


class StorageError(FlannerError):
    """Reading or writing a plan file on disk failed."""


class PlanFileNotFoundError(StorageError, FileNotFoundError):
    """A plan file is missing on disk.

    Subclasses FileNotFoundError for backward compatibility.
    """


class GitError(FlannerError):
    """A git operation failed or the directory is not a git repository."""


class ConfigError(FlannerError):
    """Configuration is missing or invalid (Claude config, Jira config)."""


class JiraError(FlannerError):
    """A Jira URL or linkage is invalid."""


class LinearError(FlannerError):
    """A Linear identifier/workspace is invalid, or a Linear API call failed."""


class MeshError(FlannerError):
    """A mesh provider could not be reached or refused an operation."""


class MeshUnavailableError(MeshError):
    """The provider's control plane is down or rate-limiting.

    Distinct from a refusal: the request may succeed on retry, so callers
    fail closed for new access while leaving existing local state intact.
    """


class IdentityUnavailableError(FlannerError):
    """This device has a signing key that cannot be read right now.

    Distinct from having no key at all, and the distinction is the point:
    the first must never be answered by generating a new one, because the
    device id is derived from the key and a new key is a new machine.
    """
