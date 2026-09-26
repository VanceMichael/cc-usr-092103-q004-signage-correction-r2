"""上报照片的隐私处理。

公开流转只使用脱敏副本：人脸、车牌、二维码等打码并丢弃其文字，
设备信息抹除，坐标降精度；原片与打码明细只进入内部保险库，
供证据链回溯，不进入公开视图。
"""

import copy
import hashlib

# 需要打码的检测类型
SENSITIVE_KINDS = {"face", "plate", "qr", "person"}


def pseudonym(reporter_id: str, salt: str) -> str:
    """上报人标识的匿名化，事件与公开记录中只出现匿名。"""
    digest = hashlib.sha256(f"{salt}:{reporter_id}".encode("utf-8")).hexdigest()
    return "anon-" + digest[:10]


def sanitize_photo(photo: dict, reporter_id: str, salt: str) -> tuple[dict, dict]:
    """返回 (脱敏副本, 保险库记录)。

    保险库记录保留原片、上报人真实标识与打码明细，仅供内部核对。
    """
    original = copy.deepcopy(photo)
    sanitized = copy.deepcopy(photo)

    redactions = []
    for det in sanitized.get("detections", []):
        if det.get("kind") in SENSITIVE_KINDS:
            det["redacted"] = True
            det.pop("text", None)
            redactions.append({"kind": det["kind"], "box": det.get("box")})

    exif = sanitized.get("exif")
    if isinstance(exif, dict):
        exif.pop("device", None)
        gps = exif.get("gps")
        if isinstance(gps, (list, tuple)) and len(gps) == 2:
            exif["gps"] = [round(float(c), 3) for c in gps]

    vault = {
        "photo_id": photo.get("photo_id"),
        "reporter_id": reporter_id,
        "original": original,
        "redactions": redactions,
    }
    return sanitized, vault
