"""RED — refuse to boot a real deployment on a published session key.

ROADMAP open item 1, and the highest-severity line in that file:
``FLASK_SECRET_KEY: dev-only-change-me`` is **committed** in both
``docker-compose.yml`` and ``compose.cas.yml``. Flask signs its session cookie
with that key. A published, guessable signing key means anyone who can reach
the frontend can forge a session for any role — including admin — without a
credential.

It was a known nit while nobody was signed in. With four real people on a live
box it is a live authentication bypass, and the compose literal makes it the
DEFAULT rather than an accident: an ``environment:`` entry **overrides**
``env_file``, so a correct `FLASK_SECRET_KEY` in `.env` was silently ignored.
That is the same defect class the compose file already documents for
``CAS_ENABLED`` — a variable that reaches a container only if the compose file
lets it.

**Three layers, because any one alone leaves a hole:**

1. ``quickstart.ps1`` GENERATES the key, so a fresh install has a real one.
2. ``docker-compose`` stops hard-coding it, so ``.env`` actually wins.
3. The app REFUSES TO BOOT on a known default — so a deployment that skipped
   the quickstart, or copied an old compose file, fails loudly instead of
   running forgeable.

Layer 3 is the one tested here. It follows ``validate_startup_auth_config``'s
discipline exactly: scoped to a real deployment (``cas_enabled=True``), never
logging or raising the key's value, and refusing rather than warning — a
warning in a startup log is a control nobody reads.

``validate_startup_session_secret`` does not exist yet — RED half of the TDD
cycle.
"""

from __future__ import annotations

import pytest

from src.settings import Settings


def _settings(**kw: object) -> Settings:
    return Settings(**kw)  # type: ignore[arg-type]


# The literals that actually exist in this repo's history and templates. Each
# one is a value a real deployment could be running RIGHT NOW.
_PUBLISHED_DEFAULTS = [
    "dev-only",  # src/settings.py's own field default
    "dev-only-change-me",  # docker-compose.yml + compose.cas.yml
    "change-me",  # .env.example
]


# --------------------------------------------------- it refuses a real deploy


@pytest.mark.parametrize("published", _PUBLISHED_DEFAULTS)
def test_a_real_deployment_refuses_a_published_key(published: str) -> None:
    from src.settings import validate_startup_session_secret

    with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
        validate_startup_session_secret(
            _settings(cas_enabled=True, api_key_admin="k", flask_secret_key=published)
        )


def test_a_real_deployment_refuses_an_empty_key() -> None:
    """Flask with a falsy ``secret_key`` raises on the first session write, but
    only when someone happens to log in. Refusing at boot turns a runtime 500
    for one unlucky user into a deployment that never starts."""
    from src.settings import validate_startup_session_secret

    with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
        validate_startup_session_secret(
            _settings(cas_enabled=True, api_key_admin="k", flask_secret_key="")
        )


def test_the_refusal_never_contains_the_key_value() -> None:
    """Same rule ``validate_startup_auth_config`` follows: a startup error
    reaches logs, terminals and screenshots. It may name the VARIABLE, never
    the value — otherwise the fix for a leaked secret leaks the next one."""
    from src.settings import validate_startup_session_secret

    secret = "dev-only-change-me"
    with pytest.raises(RuntimeError) as exc:
        validate_startup_session_secret(
            _settings(cas_enabled=True, api_key_admin="k", flask_secret_key=secret)
        )
    assert secret not in str(exc.value)


def test_the_refusal_says_how_to_fix_it() -> None:
    """A refusal that does not name the remedy just moves the outage. The
    operator hitting this is mid-deploy and needs the next command."""
    from src.settings import validate_startup_session_secret

    with pytest.raises(RuntimeError) as exc:
        validate_startup_session_secret(
            _settings(cas_enabled=True, api_key_admin="k", flask_secret_key="dev-only")
        )
    assert "quickstart" in str(exc.value).lower()


# ------------------------------------------------- it does not break local dev


def test_local_dev_still_boots_on_the_default() -> None:
    """``cas_enabled=False`` is the single-operator local-dev default, and it
    must keep booting clean — exactly the scoping
    ``validate_startup_auth_config`` uses. A refusal that also fires on every
    developer's laptop gets disabled, and then protects nobody."""
    from src.settings import validate_startup_session_secret

    validate_startup_session_secret(_settings(flask_secret_key="dev-only"))


def test_a_real_deployment_with_a_real_key_boots() -> None:
    from src.settings import validate_startup_session_secret

    validate_startup_session_secret(
        _settings(
            cas_enabled=True,
            api_key_admin="k",
            flask_secret_key="Ux4c0mS0m3th1ngR34lly+R4nd0m/Base64==",
        )
    )


# --------------------------------------------------------- the other layers


def test_compose_no_longer_hard_codes_the_key() -> None:
    """Layer 2, and the reason the other two were not enough on their own.

    An ``environment:`` entry OVERRIDES ``env_file``, so while the literal
    stayed in compose a correct key in ``.env`` was silently ignored — the
    generator could run, the operator could believe it worked, and the
    container would still boot forgeable. Both compose files must take the
    value from the environment.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    for name in ("docker-compose.yml", "compose.cas.yml"):
        # COMMENTS ARE STRIPPED FIRST, deliberately. The assertion is about the
        # configuration, not the prose: both files now explain the defect in a
        # comment that necessarily quotes the old literal, and a bare substring
        # search would fail on the documentation of the very fix it is checking
        # for. Assert on what compose will actually read.
        config = "\n".join(
            line
            for line in (root / name).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        for setting in [
            line for line in config.splitlines() if "FLASK_SECRET_KEY" in line
        ]:
            assert "${FLASK_SECRET_KEY" in setting, (
                f"{name} hard-codes a session key ({setting.strip()!r}); an "
                "environment: entry overrides env_file, so .env cannot fix it"
            )


def test_quickstart_generates_the_key() -> None:
    """Layer 1. Without generation the refusal above just blocks every fresh
    install — a control that only makes the product harder to start."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    text = (root / "scripts" / "quickstart.ps1").read_text(encoding="utf-8")
    generated = text[text.index("foreach ($key in @(") :]
    assert "FLASK_SECRET_KEY" in generated.split("}")[0], (
        "quickstart.ps1 does not generate FLASK_SECRET_KEY alongside PII_KEY "
        "and SKILL_HASH_SALT"
    )


def test_quickstart_replaces_a_published_key_not_just_a_blank_one() -> None:
    """The gap that nearly shipped the fix broken.

    ``quickstart.ps1`` generated secrets only when the value was missing or
    blank. But a ``.env`` created while the compose files hard-coded
    ``dev-only-change-me`` already HOLDS that value — so the generator would
    skip precisely the deployments that most need rotating. Combined with the
    boot refusal, that leaves them unable to start with no working remedy: the
    operator runs the quickstart, it reports success, and the app still
    refuses.

    The generator must therefore treat a known-published value as absent.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    text = (root / "scripts" / "quickstart.ps1").read_text(encoding="utf-8")
    assert "dev-only-change-me" in text, (
        "quickstart.ps1 does not know the published defaults, so it cannot "
        "rotate a .env that already carries one"
    )
    assert "IsNullOrWhiteSpace($value) -or $isPublished" in text, (
        "quickstart.ps1 still regenerates only blank values — a .env holding "
        "a published default would be skipped"
    )
