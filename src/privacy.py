"""照片隐私处理与受限原件库。

市民上传的照片先入受限证据库（EvidenceVault，仅供内部证据链使用），
再生成脱敏副本：剥离全部拍摄元数据（EXIF、定位、设备、机主等），
并对人脸、车牌等敏感区域做模糊化。公众视图只会引用脱敏副本，
原件引用只出现在内部证据链中。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class PhotoUpload:
    """一次原始上传。metadata 视为不可信隐私数据，脱敏时整体剥离。"""

    content_hash: str
    metadata: Mapping[str, str] = field(default_factory=dict)
    sensitive_regions: tuple[dict, ...] = ()
    captured_at: str | None = None


@dataclass(frozen=True)
class SanitizedPhoto:
    """脱敏副本的描述。original_ref 指向受限库中的原件，属内部信息。"""

    sanitized_id: str
    content_hash: str
    blurred_regions: tuple[dict, ...]
    stripped_metadata_keys: tuple[str, ...]
    original_ref: str


def _digest(content_hash: str) -> str:
    return hashlib.sha256(content_hash.encode("utf-8")).hexdigest()[:12]


class EvidenceVault:
    """受限原件库：运行期 blob 存储，只有内部证据链可以取件。"""

    def __init__(self):
        self._items: dict[str, PhotoUpload] = {}

    def put(self, upload: PhotoUpload) -> str:
        ref = f"orig-{_digest(upload.content_hash)}"
        self._items.setdefault(ref, upload)
        return ref

    def get(self, ref: str) -> PhotoUpload:
        return self._items[ref]


def sanitize_photo(upload: PhotoUpload, vault: EvidenceVault) -> SanitizedPhoto:
    """原件入库，返回剥离元数据、区域已模糊的脱敏副本描述。"""
    original_ref = vault.put(upload)
    blurred = tuple(
        {"kind": region.get("kind", "unknown"), "box": region.get("box"), "action": "blurred"}
        for region in upload.sensitive_regions
    )
    return SanitizedPhoto(
        sanitized_id=f"san-{_digest(upload.content_hash)}",
        content_hash=upload.content_hash,
        blurred_regions=blurred,
        stripped_metadata_keys=tuple(sorted(upload.metadata.keys())),
        original_ref=original_ref,
    )
