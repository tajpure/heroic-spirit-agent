"""Load and validate packaged or user-supplied catalogs."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from .errors import CatalogError
from .models import HSAProfile, OrganizationSpec


T = TypeVar("T", bound=BaseModel)


class Catalog:
    def __init__(
        self,
        profiles: Iterable[HSAProfile],
        organizations: Iterable[OrganizationSpec],
    ) -> None:
        profile_values = list(profiles)
        organization_values = list(organizations)
        _require_unique_ids(profile_values, "HSA profile")
        _require_unique_ids(organization_values, "organization")
        self.profiles = {profile.id: profile for profile in profile_values}
        self.organizations = {organization.id: organization for organization in organization_values}
        if not self.profiles:
            raise CatalogError("catalog contains no HSA profiles")
        if not self.organizations:
            raise CatalogError("catalog contains no organizations")
        self._validate_references()

    @classmethod
    def builtin(cls) -> "Catalog":
        root = resources.files("hsa_thinktank").joinpath("catalog")
        return cls(
            _load_resources(root.joinpath("hsas"), HSAProfile),
            _load_resources(root.joinpath("organizations"), OrganizationSpec),
        )

    @classmethod
    def from_directory(cls, root: str | Path) -> "Catalog":
        root = Path(root)
        return cls(
            _load_paths(root / "hsas", HSAProfile),
            _load_paths(root / "organizations", OrganizationSpec),
        )

    def profile(self, profile_id: str) -> HSAProfile:
        try:
            return self.profiles[profile_id]
        except KeyError as exc:
            raise CatalogError(f"unknown HSA profile: {profile_id}") from exc

    def organization(self, organization_id: str) -> OrganizationSpec:
        try:
            return self.organizations[organization_id]
        except KeyError as exc:
            raise CatalogError(f"unknown organization: {organization_id}") from exc

    def _validate_references(self) -> None:
        for organization in self.organizations.values():
            missing = sorted(
                member.hsa_id
                for member in organization.members
                if member.hsa_id not in self.profiles
            )
            if missing:
                raise CatalogError(
                    f"organization {organization.id} references unknown profiles: "
                    + ", ".join(missing)
                )


def _load_resources(directory, model_type: type[T]) -> list[T]:
    try:
        entries = sorted(
            (entry for entry in directory.iterdir() if entry.name.endswith(".json")),
            key=lambda entry: entry.name,
        )
    except (FileNotFoundError, TypeError) as exc:
        raise CatalogError(f"missing packaged catalog directory: {directory}") from exc
    return [
        _parse_model(entry.name, entry.read_text(encoding="utf-8"), model_type) for entry in entries
    ]


def _load_paths(directory: Path, model_type: type[T]) -> list[T]:
    if not directory.is_dir():
        raise CatalogError(f"missing catalog directory: {directory}")
    return [
        _parse_model(str(path), path.read_text(encoding="utf-8"), model_type)
        for path in sorted(directory.glob("*.json"))
    ]


def _parse_model(label: str, raw: str, model_type: type[T]) -> T:
    try:
        return model_type.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise CatalogError(f"invalid catalog entry {label}: {exc}") from exc


def _require_unique_ids(values: list[T], label: str) -> None:
    ids = [value.id for value in values]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise CatalogError(f"duplicate {label} ids: {', '.join(duplicates)}")
