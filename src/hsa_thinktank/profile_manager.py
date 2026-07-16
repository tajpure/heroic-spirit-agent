"""Explicit bootstrap and health checks for one-HSA-per-Hermes-Profile."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import BackendError, BackendUnavailable
from .models import HSAProfile, content_hash


@dataclass(frozen=True)
class ProfileHealth:
    hsa_id: str
    profile_name: str
    command: str | None
    profile_home: str
    exists: bool
    soul_present: bool
    soul_matches_hsa: bool
    soul_catalog_fingerprint: str | None
    soul_content_hash: str | None
    memory_enabled: bool
    user_profile_enabled: bool
    external_memory_provider: str
    terminal_backend: str


_SOUL_MARKER = re.compile(
    r"<!-- hsa-profile: (?P<hsa>[a-z0-9-]+); "
    r"catalog-fingerprint: (?P<fingerprint>[0-9a-f]{64}) -->"
)


def hermes_profile_name(hsa_id: str) -> str:
    return f"hsa-{hsa_id}"


def hermes_profile_home(hsa_id: str) -> Path:
    root = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    # HERMES_HOME may itself point at a named profile. Bootstrap always targets
    # the canonical default profile root so profiles do not become nested.
    if root.parent.name == "profiles":
        root = root.parent.parent
    return root / "profiles" / hermes_profile_name(hsa_id)


def render_soul(profile: HSAProfile) -> str:
    principles = "\n".join(
        f"- [{item.id}] {item.rule} (sources: {', '.join(item.source_ids)}; "
        f"confidence: {item.confidence:.2f})"
        for item in profile.principles
    )
    limits = "\n".join(f"- {item}" for item in profile.domain_limits) or "- 未声明"
    epistemic = "\n".join(f"- {item}" for item in profile.epistemic_rules)
    forbidden = "\n".join(f"- {item}" for item in profile.forbidden_claims)
    sources = "\n".join(f"- [{item.id}] {item.title}: {item.url}" for item in profile.sources)
    return f"""<!-- hsa-profile: {profile.id}; catalog-fingerprint: {profile.fingerprint} -->

# Identity

You are the persistent Hermes runtime for HSA `{profile.id}` ({profile.display_name}).
You are an evidence-constrained decision model inspired by public material. You are not the
real person, do not represent them, and must never claim consciousness, authorization, private
memories, or fabricated quotations.

# Decision kernel

{profile.summary}

{principles}

# Domain limits

{limits}

# Epistemic rules

{epistemic}

# Forbidden claims

{forbidden}

# Sources

{sources}

# Persistent operation

- Private Hermes memory is allowed, but memory is historical context rather than verified fact.
- Native memory writes are immediately durable inside this profile. Write only stable, sourced facts.
- Use only tools enabled for the current invocation.
- Never contact another HSA directly. Delegated workers are research tools and have no vote.
- Preserve uncertainty and cite evidence, memory IDs, and tool artifacts in structured outputs.
"""


def native_memory_fingerprint(hsa_id: str) -> str:
    """Hash profile-local identity, config and memory without exposing content."""

    home = hermes_profile_home(hsa_id)
    paths = [
        home / "SOUL.md",
        home / "config.yaml",
        home / "memories" / "MEMORY.md",
        home / "memories" / "USER.md",
    ]
    snapshot = {
        str(path.relative_to(home)): path.read_text(encoding="utf-8") if path.exists() else ""
        for path in paths
    }
    return content_hash(snapshot)


class HermesProfileManager:
    def __init__(self, executable: str = "hermes") -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise BackendUnavailable(f"Hermes executable not found: {executable}")
        self.executable = resolved

    def bootstrap(
        self,
        profile: HSAProfile,
        *,
        clone_from: str = "default",
        terminal_backend: str = "local",
        overwrite_soul: bool = False,
    ) -> ProfileHealth:
        if terminal_backend != "local":
            raise ValueError(
                "HSA profiles require terminal_backend='local'; execution tools remain "
                "disabled unless the current run explicitly grants them"
            )
        name = hermes_profile_name(profile.id)
        home = hermes_profile_home(profile.id)
        created = False
        if not home.exists():
            self._run(
                [
                    self.executable,
                    "profile",
                    "create",
                    name,
                    "--clone-from",
                    clone_from,
                    "--clone",
                    "--description",
                    f"Persistent Hermes runtime for {profile.display_name}",
                ]
            )
            created = True
        soul_path = home / "SOUL.md"
        expected_soul = render_soul(profile)
        if created or overwrite_soul or not soul_path.exists():
            home.mkdir(parents=True, exist_ok=True)
            _atomic_write(soul_path, expected_soul)
        else:
            current_soul = soul_path.read_text(encoding="utf-8")
            if current_soul != expected_soul:
                raise BackendError(
                    f"existing SOUL.md content or catalog fingerprint does not match "
                    f"HSA {profile.id}; rerun with --overwrite-soul after reviewing "
                    "the dry-run plan"
                )

        command = shutil.which(name)
        if command is None:
            self._run([self.executable, "profile", "alias", name])
            command = shutil.which(name)
        if command is None:
            raise BackendError(f"Hermes profile was created but alias is unavailable: {name}")

        self._run([command, "config", "set", "memory.memory_enabled", "true"])
        self._run([command, "config", "set", "memory.user_profile_enabled", "true"])
        self._run([command, "config", "set", "memory.provider", ""])
        self._run([command, "config", "set", "terminal.backend", terminal_backend])
        return self.health(profile.id)

    def health(self, hsa_id: str) -> ProfileHealth:
        name = hermes_profile_name(hsa_id)
        command = shutil.which(name)
        home = hermes_profile_home(hsa_id)
        values = _read_config_values(home / "config.yaml")
        soul_path = home / "SOUL.md"
        soul_present = soul_path.is_file()
        soul_matches_hsa = False
        soul_catalog_fingerprint = None
        soul_content_hash = None
        if soul_present:
            soul_content = soul_path.read_text(encoding="utf-8")
            soul_content_hash = content_hash(soul_content)
            match = _SOUL_MARKER.search(soul_content)
            if match is not None:
                soul_matches_hsa = match.group("hsa") == hsa_id
                soul_catalog_fingerprint = match.group("fingerprint")
        return ProfileHealth(
            hsa_id=hsa_id,
            profile_name=name,
            command=command,
            profile_home=str(home),
            exists=home.is_dir(),
            soul_present=soul_present,
            soul_matches_hsa=soul_matches_hsa,
            soul_catalog_fingerprint=soul_catalog_fingerprint,
            soul_content_hash=soul_content_hash,
            memory_enabled=_as_bool(values.get("memory.memory_enabled")),
            user_profile_enabled=_as_bool(values.get("memory.user_profile_enabled")),
            external_memory_provider=values.get("memory.provider", ""),
            terminal_backend=values.get("terminal.backend", "unknown"),
        )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-1000:]
            raise BackendError(f"Hermes profile command failed ({result.returncode}): {detail}")
        return result


def _read_config_values(path: Path) -> dict[str, str]:
    """Read the simple nested scalar keys used by Hermes without a YAML dependency."""

    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    section: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indentation = len(line) - len(line.lstrip())
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip().strip("'\"")
        if indentation == 0:
            if raw_value:
                values[key] = raw_value
                section = None
            else:
                section = key
        elif section and raw_value:
            values[f"{section}.{key}"] = raw_value
    return values


def _as_bool(value: str | None) -> bool:
    return value is not None and value.lower() in {"true", "yes", "on", "1"}


def _atomic_write(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
