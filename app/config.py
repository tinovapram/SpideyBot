"""Typed configuration with an explicit precedence chain.

Lowest to highest::

    code defaults  ->  config/default.toml  ->  config/local.toml  ->  environment

Two deliberate departures from stock pydantic-settings:

* TOML is read by :class:`TomlFilesSource`, which merges the two files and
  **rejects unknown keys**. An environment can never be checked exhaustively, but
  a config file can — so a typo such as ``daily_download`` fails at startup
  instead of silently keeping the default.
* Secrets arrive through a :class:`~app.secrets.SecretsProvider` (plan D12) rather
  than being read ad hoc, so an OS keyring or Docker secrets can be added later
  without touching callers. Today the provider is the environment, which is also
  where ``.env`` lands after :func:`load_env_file`.
* Environment values are normalised before they are read, so one ``.env`` behaves the
  same on a host and in a container. Docker's ``--env-file`` does not strip quotes and
  ``python-dotenv`` does, which would otherwise make quoting a host-only feature that
  fails only once deployed (see R14 in the plan).
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import quote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app import paths
from app.secrets import EnvSecretsProvider, SecretsProvider

ENV_PREFIX = "SPIDEY_"

# Both quote styles, because either is valid in a `.env` file.
QUOTE_CHARS = ("'", '"')


class _Section(BaseModel):
    """Base for config sections: an unknown key is an error, not a silent default."""

    model_config = ConfigDict(extra="forbid")


# ── configuration sections (from TOML) ──────────────────────────────────


class TierLimits(_Section):
    """One tier's limits. A value of ``0`` means unlimited."""

    link_total_mb: int = Field(ge=0)
    daily_downloads: int = Field(ge=0)
    daily_mb: int = Field(ge=0)
    concurrent: int = Field(ge=1)
    priority: float = Field(ge=0)


class TierTable(_Section):
    free: TierLimits
    pro: TierLimits
    premium: TierLimits


class AdminPolicy(_Section):
    priority: float = Field(default=0.2, ge=0)


class ClientProfile(_Section):
    """The identity a user client presents, shown in the user's Active Sessions."""

    device_model: str
    system_version: str
    app_version: str
    lang_pack: str
    lang_code: str = "en"
    platform: str = "DESKTOP"


class ClientSettings(_Section):
    profile: Literal["desktop", "android", "custom"] = "desktop"
    desktop: ClientProfile
    android: ClientProfile
    custom: ClientProfile | None = None

    @model_validator(mode="after")
    def _custom_must_exist_when_selected(self) -> ClientSettings:
        if self.profile == "custom" and self.custom is None:
            raise ValueError("client.profile = 'custom' requires a [client.custom] section")
        return self

    def active(self) -> ClientProfile:
        if self.profile == "custom":
            assert self.custom is not None  # guaranteed by the validator above
            return self.custom
        return self.desktop if self.profile == "desktop" else self.android


class UxSettings(_Section):
    # Telegram media groups accept at most 10 items, so >10 is not merely unwise,
    # it is undeliverable.
    album_size: int = Field(default=10, ge=1, le=10)
    failure_list_limit: int = Field(default=10, ge=0)
    render_interval: float = Field(default=3.0, gt=0)
    anim_interval: float = Field(default=1.2, gt=0)
    default_magic_word: str = "WOW"
    timer_media_default: bool = True

    # AC-1 requires an Upgrade button on the first /start, but the spec never names a
    # destination. Empty omits the button, so no URL is invented on the operator's behalf;
    # set it and the button appears.
    upgrade_url: str = ""

    @field_validator("upgrade_url")
    @classmethod
    def _upgrade_url_is_a_link(cls, value: str) -> str:
        """Telegram accepts only http(s) and tg:// for URL buttons.

        Checked at startup so a typo fails with a clear message, rather than surfacing as
        an API error when the first user sends /start.
        """
        if not value:
            return value
        if not value.startswith(("https://", "http://", "tg://")):
            raise ValueError("upgrade_url must start with https://, http:// or tg://")
        return value

    @field_validator("default_magic_word")
    @classmethod
    def _single_token(cls, value: str) -> str:
        token = value.strip()
        if not token:
            raise ValueError("default_magic_word must not be empty")
        if any(char.isspace() for char in token):
            raise ValueError("default_magic_word must be a single token without spaces")
        return token


class ReferralSettings(_Section):
    daily_bonus: int = Field(default=10, ge=0)
    bonus_days: int = Field(default=30, ge=1)


class RuntimeSettings(_Section):
    max_concurrent_downloads: int = Field(default=20, ge=1)
    job_lease_seconds: int = Field(default=300, ge=1)
    job_max_attempts: int = Field(default=3, ge=1)
    rate_limit_per_minute: int = Field(default=20, ge=1)
    session_max_live: int = Field(default=50, ge=1)
    session_idle_seconds: int = Field(default=900, ge=1)


class DbSettings(_Section):
    """Connection details minus the password, which is a secret."""

    host: str = "127.0.0.1"
    port: int = Field(default=55432, ge=1, le=65535)
    name: str = "spideybot"
    user: str = "spideybot"


class DownloadSettings(_Section):
    retention_days: int = Field(default=14, ge=0)
    #: D28. A `Literal` rather than a string with a comment, so a typo is a startup error like
    #: every other unknown value here, instead of silently meaning "compatible".
    video_preference: Literal["compatible", "quality"] = "compatible"


# ── settings sources ───────────────────────────────────────────────────


def _model_of(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel inside an annotation such as ``ClientProfile | None``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for argument in get_args(annotation):
        if isinstance(argument, type) and issubclass(argument, BaseModel):
            return argument
    return None


def _unknown_keys(data: Mapping[str, Any], model: type[BaseModel], prefix: str = "") -> list[str]:
    """Dotted paths of keys in ``data`` that ``model`` does not define."""
    unknown: list[str] = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            unknown.append(f"{prefix}{key}")
            continue
        nested = _model_of(field.annotation)
        if nested is not None and isinstance(value, Mapping):
            unknown.extend(_unknown_keys(value, nested, f"{prefix}{key}."))
    return unknown


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge; ``override`` wins. Neither input is mutated."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


class TomlFilesSource(PydanticBaseSettingsSource):
    """Merges TOML files in order (later wins) and rejects unknown keys."""

    def __init__(self, settings_cls: type[BaseSettings], files: Sequence[Path]) -> None:
        super().__init__(settings_cls)
        self._files = tuple(files)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Values come from __call__; this hook is unused but abstract upstream.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for path in self._files:
            if not path.exists():
                continue
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            unknown = _unknown_keys(data, self.settings_cls)
            if unknown:
                raise ValueError(
                    f"{path.name}: unknown key(s): {', '.join(sorted(unknown))} "
                    "- a typo would otherwise silently keep the default"
                )
            merged = _deep_merge(merged, data)
        return merged


class SecretsSettingsSource(PydanticBaseSettingsSource):
    """Looks up every settings field through the injected secrets provider.

    With the default provider this reads the environment, so it is a no-op today;
    it exists so a keyring or Docker-secrets provider can be dropped in later.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        provider: SecretsProvider | None = None,
        prefix: str = ENV_PREFIX,
    ) -> None:
        super().__init__(settings_cls)
        self._provider = provider or EnvSecretsProvider()
        self._prefix = prefix

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        value = self._provider.get(f"{self._prefix}{field_name.upper()}")
        return value, field_name, value is not None

    def __call__(self) -> dict[str, Any]:
        found: dict[str, Any] = {}
        for name in self.settings_cls.model_fields:
            value, _, present = self.get_field_value(None, name)
            if present:
                found[name] = value
        return found


# ── settings ───────────────────────────────────────────────────────────


def build_dsn(db: DbSettings, password: str) -> str:
    """Assemble an async DSN.

    ``quote`` rather than ``quote_plus``: userinfo is not a query string, so a
    space must become ``%20``. ``quote_plus`` would emit ``+``, which the driver
    would then read as a literal plus sign in the password.
    """
    return (
        f"postgresql+asyncpg://{quote(db.user, safe='')}:"
        f"{quote(password, safe='')}@{db.host}:{db.port}/{db.name}"
    )


def build_dsn_safe(db: DbSettings) -> str:
    """The same DSN with the password masked, for logs and error messages."""
    return f"postgresql+asyncpg://{db.user}:***@{db.host}:{db.port}/{db.name}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        # A blank value means "not set": the shipped .env.example declares every
        # key present-but-blank, so blank must not read as a real value.
        env_ignore_empty=True,
        extra="forbid",
        case_sensitive=False,
    )

    # configuration (TOML)
    tier: TierTable
    admin: AdminPolicy
    client: ClientSettings
    ux: UxSettings
    referral: ReferralSettings
    runtime: RuntimeSettings
    db: DbSettings
    download: DownloadSettings

    # secrets (environment today, any SecretsProvider later)
    tg_api_id: SecretStr
    tg_api_hash: SecretStr
    tg_bot_token: SecretStr
    # Optional: the bundled local cluster generates its own password and keeps it
    # in data/pgpassword, so a fresh checkout runs without editing .env. Required
    # only when pointing at an external database.
    db_password: SecretStr | None = None

    # optional credentials and switches
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    terabox_cookies: SecretStr | None = None
    terabox_jstoken: SecretStr | None = None
    terabox_bdstoken: SecretStr | None = None
    gdl_cookies_from_browser: str | None = None
    gdl_reddit_client_id: SecretStr | None = None
    gdl_reddit_refresh_token: SecretStr | None = None
    reddit_praw_client_id: SecretStr | None = None
    reddit_praw_client_secret: SecretStr | None = None

    # live-test switches; live mode is an environment concern, not bot behaviour
    test_mode: bool = False
    test_phone: str | None = None
    test_phone_b: str | None = None
    test_database_url: SecretStr | None = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: Any) -> Any:
        """Accept ``1,2,3`` (and tolerate ``;``) rather than requiring JSON."""
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            parts = value.replace(";", ",").split(",")
            return [int(part.strip()) for part in parts if part.strip()]
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Highest priority first.

        ``init_settings`` stays on top so tests can inject values directly;
        ``dotenv_settings`` is omitted because :func:`load_env_file` already puts
        ``.env`` into the environment, giving one mechanism instead of two.
        """
        return (
            init_settings,
            env_settings,
            SecretsSettingsSource(settings_cls),
            TomlFilesSource(settings_cls, (paths.DEFAULT_TOML, paths.LOCAL_TOML)),
        )

    @property
    def database_url(self) -> str:
        """Async DSN when a password is configured.

        ``app.dbprovider.resolve_dsn`` is the version to use at runtime: it falls
        back to the bundled cluster's generated password before giving up.
        """
        if self.db_password is None:
            raise ValueError(
                "no database password: set SPIDEY_DB_PASSWORD in .env, "
                "or run `python -m spideybot db init` to create a local cluster"
            )
        return build_dsn(self.db, self.db_password.get_secret_value())

    @property
    def database_url_safe(self) -> str:
        """The same DSN with the password masked, for logs and error messages."""
        return build_dsn_safe(self.db)

    def terabox_cookie_accounts(self) -> list[str]:
        """Operator-supplied fallback cookies, split on ``|``.

        ``|`` rather than ``;`` because a cookie is itself a ``;``-separated list
        of pairs, so ``;`` could never separate accounts.
        """
        if self.terabox_cookies is None:
            return []
        raw = self.terabox_cookies.get_secret_value()
        return [part.strip() for part in raw.split("|") if part.strip()]

    def secret_values(self) -> list[str]:
        """Every secret string, for the log redactor to register.

        Cookie accounts are registered individually so one leaked account cannot
        slip through because the combined value did not match.
        """
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name, None)
            if isinstance(value, SecretStr):
                values.append(value.get_secret_value())
        values.extend(self.terabox_cookie_accounts())
        return [value for value in values if value]


def strip_env_quotes(value: str) -> str:
    """Drop one layer of matching surrounding quotes.

    ``python-dotenv`` strips quotes while parsing a file; Docker's ``--env-file`` and
    ``docker run -e`` do not. Without this, a value quoted the way `.env.example`
    recommends loads correctly on the host and arrives still quoted in a container,
    where it fails validation at startup — a failure that never reproduces locally.

    Only a matching pair wrapping the *whole* value is removed, and nothing inside is
    interpreted, so a value such as 'it's here' keeps every character.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTE_CHARS:
        return value[1:-1]
    return value


def normalise_env_quotes(prefix: str = ENV_PREFIX) -> int:
    """Strip stray quotes from ``SPIDEY_*`` environment values, in place.

    Applied to the environment rather than to each settings source, so flat values,
    nested ones (``SPIDEY_UX__ALBUM_SIZE``) and secrets all behave identically without
    depending on how pydantic-settings happens to assemble them.

    Returns how many values changed, for callers and tests to assert on.
    """
    changed = 0
    for key, raw in list(os.environ.items()):
        if not key.upper().startswith(prefix):
            continue
        stripped = strip_env_quotes(raw)
        if stripped != raw:
            os.environ[key] = stripped
            changed += 1
    return changed


def load_env_file() -> bool:
    """Prepare the process environment: load ``.env``, then normalise quoting.

    Called by the entry points rather than at import, so importing this module in a
    test does not silently pick up the developer's real credentials.

    Normalisation runs whether or not a file was found. In a container the values come
    from Docker and never pass through ``python-dotenv`` at all, so the presence of a
    file cannot be what triggers it.
    """
    from dotenv import load_dotenv

    found = bool(load_dotenv(paths.ENV_FILE))
    normalise_env_quotes()
    return found


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object."""
    return Settings()


def build_settings(**overrides: Any) -> Settings:
    """Construct settings without touching the cache, for tests.

    ``overrides`` are applied as init values, which outrank every other source.
    """
    return Settings(**overrides)


def reset_settings_cache() -> None:
    get_settings.cache_clear()
