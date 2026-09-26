"""线索实体归并。

同一场所内文字内容相同的标识视为同一实体：不同角度、不同时间、
不同上报人拍到的照片都归并到同一案件，避免重复立案与矛盾结论。
"""

import hashlib


def normalize_text(text: str) -> str:
    """忽略大小写、空白与标点，只保留字母数字及文字字符。"""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def merge_key(venue_id: str, sign_text: str) -> str:
    """归并指纹：场所 + 规范化牌面文字。"""
    norm = normalize_text(sign_text)
    if not norm:
        raise ValueError("牌面文字为空，无法归并")
    raw = f"{venue_id}|{norm}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
