"""读取并校验规范、场所、人员与时限等基础资料。

校验规则与 contracts/ 下各 schema 保持一致，字段缺失或取值非法时抛出 ValueError。
"""

import json
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _require(data: dict, keys: set, label: str) -> None:
    missing = keys - data.keys()
    if missing:
        raise ValueError(f"{label}缺少必要字段: {sorted(missing)}")


def load_standards(path: Path) -> dict:
    """返回译写规范版本库，版本号不得重复且至少有一个现行版。"""
    data = _load(path)
    _require(data, {"standard_id", "title", "versions"}, "译写规范")
    seen = set()
    current = 0
    for v in data["versions"]:
        _require(v, {"version", "effective_from", "status", "terms"}, "规范版本")
        if v["version"] in seen:
            raise ValueError(f"规范版本号重复: {v['version']}")
        seen.add(v["version"])
        if v["status"] == "current":
            current += 1
        for t in v["terms"]:
            _require(t, {"source", "approved"}, "规范词条")
    if current != 1:
        raise ValueError("译写规范须恰好一个现行版本")
    return data


def load_venues(path: Path) -> dict:
    """返回场所目录，场所标识不得重复。"""
    data = _load(path)
    _require(data, {"venues"}, "场所目录")
    seen = set()
    for v in data["venues"]:
        _require(v, {"venue_id", "name", "district", "domain",
                     "owner_unit", "responsible_unit", "grid"}, "场所")
        if v["venue_id"] in seen:
            raise ValueError(f"场所标识重复: {v['venue_id']}")
        seen.add(v["venue_id"])
    return data


def load_personnel(path: Path) -> dict:
    """返回人员能力表，人员标识不得重复，角色限专家或志愿者。"""
    data = _load(path)
    _require(data, {"people"}, "人员能力")
    seen = set()
    for p in data["people"]:
        _require(p, {"person_id", "display_name", "role", "domains",
                     "languages", "capacity", "org"}, "人员")
        if p["role"] not in ("expert", "volunteer"):
            raise ValueError(f"未知人员角色: {p['role']}")
        if p["person_id"] in seen:
            raise ValueError(f"人员标识重复: {p['person_id']}")
        seen.add(p["person_id"])
    return data


def load_sla(path: Path) -> dict:
    """返回处理时限表，环节名称不得重复。"""
    data = _load(path)
    _require(data, {"stages"}, "处理时限")
    seen = set()
    for s in data["stages"]:
        _require(s, {"stage", "days"}, "时限环节")
        if s["stage"] in seen:
            raise ValueError(f"时限环节重复: {s['stage']}")
        seen.add(s["stage"])
    return data
