"""Upstream-drift canary: the Hermes gateway surface ``hermes_rine`` binds to.

Every other test in this suite drives OUR code. This one asserts that the code we drive
*into* still exists and still has the shape we call it with — the failure class no
behavioural test can reach, because upstream is a moving target that our own suite pins
nothing about. ``rine-openclaw/scripts/canary-latest.sh`` has the same job for that
plugin; the counterpart here is ``scripts/canary-latest.sh``, which re-runs this suite
against whatever Hermes ships on ``main`` today.

Three axes, all DERIVED rather than listed, so a new binding cannot escape the canary by
being added after this file was written:

- every ``@abstractmethod`` the host declares is implemented here, and each override
  accepts every call the host's own signature permits (an override that narrowed it
  ``TypeError``s at the gateway's call site, which kills the wake channel silently);
- every gateway method we CALL binds the argument shape we call it with;
- every ``from gateway… import X`` anywhere in the package still resolves.

🔴 ``tests/conftest.py`` installs ``gateway.*`` doubles so the rest of the suite runs on a
bare host. ``pytest.importorskip`` cannot tell those from the real package — it would hand
back our own stub and every assertion here would become a tautology about code we wrote.
The discriminator is ``__file__``: a module built with ``types.ModuleType`` has none, an
installed one does. So this file measures nothing outside the rig, and says so.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pathlib

import pytest

RIG_HINT = (
    "real Hermes absent (conftest stub) — run in the rig: "
    "docker compose -f compose.hermes.yml run --rm hermes pytest"
)

_ANY = object()


def _host_module(name: str):
    """The installed ``gateway.*`` module, or skip — never the conftest double."""
    try:
        module = importlib.import_module(name)
    except ImportError:
        pytest.skip(RIG_HINT)
    if getattr(module, "__file__", None) is None:
        pytest.skip(RIG_HINT)
    return module


@pytest.fixture
def host_adapter():
    """The real ``BasePlatformAdapter``."""
    return _host_module("gateway.platforms.base").BasePlatformAdapter


def _without_self(sig: inspect.Signature) -> inspect.Signature:
    params = list(sig.parameters.values())
    if params and params[0].name == "self":
        params = params[1:]
    return sig.replace(parameters=params)


def _calls_the_host_permits(sig: inspect.Signature):
    """The fullest and the barest call ``sig`` allows, as ``(args, kwargs)`` pairs.

    Positional-or-keyword parameters are offered positionally, which is the strict
    reading: the signature permits it, so an override that made one keyword-only has
    narrowed the contract even if today's call site happens not to exercise it.
    """
    full_args: list[object] = []
    full_kwargs: dict[str, object] = {}
    bare_args: list[object] = []
    bare_kwargs: dict[str, object] = {}
    for name, param in _without_self(sig).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        required = param.default is param.empty
        if param.kind is param.KEYWORD_ONLY:
            full_kwargs[name] = _ANY
            if required:
                bare_kwargs[name] = _ANY
        else:
            full_args.append(_ANY)
            if required:
                bare_args.append(_ANY)
    return ((full_args, full_kwargs), (bare_args, bare_kwargs))


def _cannot_serve(ours: inspect.Signature, host: inspect.Signature) -> str | None:
    """Why ``ours`` cannot serve a call ``host`` permits, or None."""
    mine = _without_self(ours)
    for (args, kwargs), which in zip(
        _calls_the_host_permits(host), ("fullest", "barest"), strict=True
    ):
        try:
            mine.bind(*args, **kwargs)
        except TypeError as err:
            shape = f"{len(args)} positional + {sorted(kwargs)}"
            return f"{which} host call ({shape}) does not bind: {err}"
    return None


# --- the abstract surface the gateway constructs us against ----------------------------


def test_the_adapter_implements_every_abstract_method_the_host_declares(host_adapter) -> None:
    # A class with a non-empty __abstractmethods__ raises TypeError at construction, and
    # the gateway is what constructs it — so a method upstream ADDS to the ABC takes the
    # whole wake channel down at startup, with nothing in our own suite going red first.
    from hermes_rine.adapter import RineAdapter

    declared = set(getattr(host_adapter, "__abstractmethods__", frozenset()))
    # Instrument check: if upstream stopped using ABC the assertion below would pass for
    # the wrong reason, and this canary would quietly measure nothing from then on.
    assert declared, "host declares no abstract methods — this probe is no longer measuring"
    assert RineAdapter.__abstractmethods__ == frozenset(), (
        f"unimplemented: {sorted(RineAdapter.__abstractmethods__)}; "
        f"host declares {sorted(declared)}"
    )


def test_every_override_accepts_every_call_the_host_signature_permits(host_adapter) -> None:
    # The abstract list is read from the host, not typed here, so a method upstream adds
    # is checked the first time this runs against that version.
    from hermes_rine.adapter import RineAdapter

    problems = []
    for name in sorted(host_adapter.__abstractmethods__):
        ours = getattr(RineAdapter, name, None)
        if ours is None:
            problems.append(f"{name}: not implemented")
            continue
        why = _cannot_serve(
            inspect.signature(ours), inspect.signature(getattr(host_adapter, name))
        )
        if why:
            problems.append(f"{name}: {why}")
    assert not problems, "our overrides narrowed the host contract:\n  " + "\n  ".join(problems)


# --- the gateway methods we call, in the shapes we call them ---------------------------

#: ``(method, positional count, keywords, the call site)``. Every shape ``hermes_rine``
#: actually uses against the host adapter. Exactly-once rests on the first three: a
#: callback the gateway will not take or will not hand back is a message that stays
#: ``new`` forever, and that failure is silent at every layer we own.
_CALLS_WE_MAKE = (
    ("register_post_delivery_callback", 2, (), "_poll._dispatch arms the turn-closing callback"),
    (
        "pop_post_delivery_callback",
        1,
        ("generation",),
        "adapter.pop_post_delivery_callback super()",
    ),
    ("pop_post_delivery_callback", 1, (), "_poll._dispatch disarms it when scheduling failed"),
    ("handle_message", 1, (), "_poll._dispatch hands the turn to the gateway"),
    ("_session_key_profile", 1, (), "_poll._session_key_for mirrors handle_message's key"),
)


@pytest.mark.parametrize(("name", "positional", "keywords", "site"), _CALLS_WE_MAKE)
def test_the_host_still_accepts_the_call_we_make(
    host_adapter, name: str, positional: int, keywords: tuple[str, ...], site: str
) -> None:
    method = getattr(host_adapter, name, None)
    assert method is not None, f"host no longer defines {name} — called by {site}"
    signature = _without_self(inspect.signature(method))
    try:
        signature.bind(*([_ANY] * positional), **dict.fromkeys(keywords, _ANY))
    except TypeError as err:  # pragma: no cover — the drift this file exists to catch
        pytest.fail(f"{site}: {name}{signature} no longer accepts our call: {err}")


# --- every name we import out of the host ----------------------------------------------


def _gateway_imports() -> set[tuple[str, str]]:
    """Every ``from gateway… import name`` in the package, module-level or deferred.

    Read out of the source with ``ast`` rather than listed, because the imports that
    matter most are the ones deferred into function bodies (``platform.py`` defers
    ``PlatformEntry``, ``_poll`` defers ``build_session_key``) — precisely the ones a
    reviewer forgets to add to a hand-written table.
    """
    import hermes_rine

    package = pathlib.Path(hermes_rine.__file__).parent
    found: set[tuple[str, str]] = set()
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gateway"):
                found.update((node.module, alias.name) for alias in node.names)
    return found


def test_every_gateway_name_the_package_imports_still_resolves() -> None:
    imports = _gateway_imports()
    # Instrument check: an empty scan would make the loop below vacuously green, which is
    # exactly how a canary rots into decoration.
    assert imports, "found no gateway imports to check — the scan is broken, not the host"
    missing = [
        f"{module}.{name}"
        for module, name in sorted(imports)
        if not hasattr(_host_module(module), name)
    ]
    assert not missing, "the host no longer exports:\n  " + "\n  ".join(missing)
