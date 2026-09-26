"""规范、场所目录、人员能力与处理时限等基础资料的读取。

资料文件沿用 context.json 的约定：domain / kind / version 三个必备字段。
规范注册表是只追加的：换版只能新增版本，不允许改写历史版本。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import DomainError, DuplicateError, NotFoundError

DOMAIN = "signage-correction"


def _load(path: str | Path, kind: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("domain") != DOMAIN or data.get("kind") != kind or "version" not in data:
        raise ValueError(f"{kind} 资料缺少必要字段")
    return data


@dataclass(frozen=True)
class Term:
    zh: str
    en: str


@dataclass(frozen=True)
class StandardVersion:
    standard_id: str
    title: str
    version: str
    effective_from: str
    supersedes: str | None
    terms: tuple[Term, ...]


class StandardRegistry:
    """译写规范的只追加版本表。换版 = 追加一个新版本。"""

    def __init__(self, versions=()):
        self._by_key: dict[tuple[str, str], StandardVersion] = {}
        for v in versions:
            self.publish(v)

    def validate_new(self, sv: StandardVersion) -> None:
        key = (sv.standard_id, sv.version)
        if key in self._by_key:
            raise DuplicateError(f"规范 {sv.standard_id} 已存在版本 {sv.version}")
        if sv.supersedes is not None and (sv.standard_id, sv.supersedes) not in self._by_key:
            raise DomainError(f"被替代版本 {sv.supersedes} 不存在")

    def publish(self, sv: StandardVersion) -> StandardVersion:
        self.validate_new(sv)
        self._by_key[(sv.standard_id, sv.version)] = sv
        return sv

    def has(self, standard_id: str, version: str) -> bool:
        return (standard_id, version) in self._by_key

    def get(self, standard_id: str, version: str) -> StandardVersion:
        try:
            return self._by_key[(standard_id, version)]
        except KeyError:
            raise NotFoundError(f"规范 {standard_id} 没有版本 {version}") from None

    def current(self, standard_id: str) -> StandardVersion:
        versions = self.versions(standard_id)
        if not versions:
            raise NotFoundError(f"未知规范 {standard_id}")
        return max(versions, key=lambda v: v.effective_from)

    def versions(self, standard_id: str | None = None) -> list[StandardVersion]:
        out = [v for (sid, _), v in self._by_key.items() if standard_id is None or sid == standard_id]
        return sorted(out, key=lambda v: v.effective_from)


@dataclass(frozen=True)
class Unit:
    unit_id: str
    name: str


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    category: str
    owner_unit_id: str


@dataclass(frozen=True)
class Person:
    person_id: str
    name: str
    role: str  # coordinator / expert / volunteer
    affiliation: str
    domains: tuple[str, ...]
    max_active_cases: int


def load_standards(path: str | Path) -> StandardRegistry:
    data = _load(path, "translation-standards")
    versions = []
    for std in data["standards"]:
        for v in std["versions"]:
            versions.append(
                StandardVersion(
                    standard_id=std["standard_id"],
                    title=std["title"],
                    version=v["version"],
                    effective_from=v["effective_from"],
                    supersedes=v.get("supersedes"),
                    terms=tuple(Term(zh=t["zh"], en=t["en"]) for t in v.get("terms", [])),
                )
            )
    return StandardRegistry(versions)


def load_venues(path: str | Path) -> tuple[dict[str, Venue], dict[str, Unit]]:
    data = _load(path, "venue-catalog")
    units = {u["unit_id"]: Unit(unit_id=u["unit_id"], name=u["name"]) for u in data["units"]}
    venues = {}
    for v in data["venues"]:
        if v["owner_unit_id"] not in units:
            raise ValueError(f"场所 {v['venue_id']} 的权属单位未登记")
        venues[v["venue_id"]] = Venue(
            venue_id=v["venue_id"],
            name=v["name"],
            category=v["category"],
            owner_unit_id=v["owner_unit_id"],
        )
    return venues, units


def load_personnel(path: str | Path) -> dict[str, Person]:
    data = _load(path, "personnel")
    return {
        p["person_id"]: Person(
            person_id=p["person_id"],
            name=p["name"],
            role=p["role"],
            affiliation=p["affiliation"],
            domains=tuple(p["domains"]),
            max_active_cases=p["max_active_cases"],
        )
        for p in data["people"]
    }


def load_slas(path: str | Path) -> dict[str, int]:
    data = _load(path, "processing-slas")
    return {s["stage"]: s["hours"] for s in data["stages"]}
