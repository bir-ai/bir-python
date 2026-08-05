"""Machinery behind the deprecation promise in ``docs/site/stability.md``.

The policy says a public name keeps working for one minor release while warning
and naming its replacement, and may only be removed in the release after that.
This module makes that executable rather than aspirational: one message format
so every deprecation reads the same, one warning helper that points at the
caller's line rather than Bir's, and a removal-release check that refuses a
deadline the policy does not allow.

Deprecating a name should be two lines at the definition site::

    @_deprecated(replacement="bir.new_name()", removed_in="0.5.0")
    def old_name(...): ...

or, for a name that is not a function, a module-level ``__getattr__``::

    __getattr__ = _deprecated_attribute_getter(
        __name__,
        {"OldName": _Deprecation(NewName, replacement="bir.NewName", removed_in="0.5.0")},
    )

``DeprecationWarning`` is hidden by default outside ``__main__``, so a CLI
command or an environment variable that is being retired prints
:func:`_deprecation_message` to stderr instead of warning with it. The wording is
the same either way; only the channel differs.
"""

from __future__ import annotations

import functools
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

# Minor releases a deprecated name keeps working before it may be removed. The
# name starts warning in the next minor release, survives that whole release,
# and may go in the one after it — two minor bumps from where it was deprecated.
DEPRECATION_GRACE_MINORS = 2

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True)
class _Deprecation:
    """A deprecated name's replacement, removal release, and current value."""

    value: Any
    replacement: str
    removed_in: str


def _deprecation_message(name: str, *, replacement: str, removed_in: str) -> str:
    """Return the one wording every deprecation uses, whatever the channel.

    Naming both the replacement and the release is the policy, not a courtesy: a
    warning that says only "deprecated" leaves the reader to guess what to do and
    how long they have.
    """

    return f"{name} is deprecated and will be removed in {removed_in}; use {replacement} instead."


def _warn_deprecated(name: str, *, replacement: str, removed_in: str, stacklevel: int) -> None:
    """Warn that ``name`` is deprecated, blaming the caller's line.

    ``stacklevel`` is passed through to :func:`warnings.warn` unchanged and must
    count the frames between the user's call and this function, so the warning
    names the line that used the deprecated thing rather than a line inside Bir.
    A wrapper calling this from inside the function it wraps passes ``3``.
    """

    warnings.warn(
        _deprecation_message(name, replacement=replacement, removed_in=removed_in),
        DeprecationWarning,
        stacklevel=stacklevel,
    )


def _deprecated(*, replacement: str, removed_in: str, name: str | None = None) -> Callable[[_F], _F]:
    """Mark a public callable deprecated, keeping it working.

    The wrapped callable still does exactly what it did; it only warns first.
    ``name`` overrides what the message calls it, for a function whose qualified
    name is not what a user would recognize.
    """

    def decorate(function: _F) -> _F:
        deprecated_name = name or f"{function.__module__}.{function.__qualname__}()"

        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 3 frames: this wrapper, _warn_deprecated, and warnings.warn's own
            # accounting, so the warning lands on the caller.
            _warn_deprecated(deprecated_name, replacement=replacement, removed_in=removed_in, stacklevel=3)
            return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorate


def _deprecated_attribute_getter(
    module_name: str,
    deprecated: Mapping[str, _Deprecation],
) -> Callable[[str], Any]:
    """Build a module ``__getattr__`` that warns for the named attributes.

    Used for a deprecated class, constant, or alias — anything a decorator
    cannot wrap. Attributes not listed raise ``AttributeError`` exactly as an
    ordinary module does, so this never masks a typo.
    """

    def __getattr__(name: str) -> Any:
        entry = deprecated.get(name)
        if entry is None:
            raise AttributeError(f"module {module_name!r} has no attribute {name!r}")
        # 3 frames: this getter, _warn_deprecated, and warnings.warn's own
        # accounting, so the warning lands on the importing line.
        _warn_deprecated(
            f"{module_name}.{name}",
            replacement=entry.replacement,
            removed_in=entry.removed_in,
            stacklevel=3,
        )
        return entry.value

    return __getattr__


def _earliest_removal(current_version: str) -> str:
    """Return the earliest release the policy allows a removal in.

    Deprecating while ``current_version`` is unreleased means the warning first
    ships in the next minor release; the name must survive that release, so the
    earliest removal is the minor after it.
    """

    major, minor = _major_minor(current_version)
    return f"{major}.{minor + DEPRECATION_GRACE_MINORS}.0"


def _check_removal_release(removed_in: str, *, current_version: str) -> None:
    """Raise if ``removed_in`` would break the promised grace period.

    Called where a deprecation is declared so an over-eager deadline fails the
    build rather than shortening a user's migration window in a release.
    """

    earliest = _earliest_removal(current_version)
    if _major_minor(removed_in) < _major_minor(earliest):
        raise ValueError(
            f"removal release {removed_in} is earlier than {earliest}, "
            f"which the deprecation policy requires when deprecating in {current_version}"
        )


def _major_minor(version: str) -> tuple[int, int]:
    """Return a version's (major, minor), ignoring patch and any suffix."""

    parts = version.split(".")
    if len(parts) < 2:
        raise ValueError(f"version {version!r} must have at least major and minor parts")
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"version {version!r} must start with numeric major and minor parts") from exc
