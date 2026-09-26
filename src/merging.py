"""实体归并：不同角度、不同人拍摄的同一标识归入同一案件。

归并键是「场所 + 归一化后的标识文字」。完全归一化相等直接命中；
同一场所下文字高度近似（容忍错别字、大小写、空白差异）也归并，
避免同一标识被拆成两个案件、进而产生两条整改结论。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(text: str) -> str:
    return _WS.sub(" ", _NON_WORD.sub(" ", text.strip().lower())).strip()


def sign_signature(venue_id: str, sign_text: str) -> str:
    return f"{venue_id}::{normalize_text(sign_text)}"


def text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


class EntityResolver:
    def __init__(self, threshold: float = 0.85):
        self._threshold = threshold

    def find_match(
        self,
        *,
        venue_id: str,
        sign_text: str,
        candidates: Iterable[tuple[str, str, str]],
    ) -> str | None:
        """在候选案件（case_id, venue_id, sign_text）中找归并目标。"""
        norm = normalize_text(sign_text)
        best_case: str | None = None
        best_score = 0.0
        for case_id, cand_venue, cand_text in candidates:
            if cand_venue != venue_id:
                continue
            if normalize_text(cand_text) == norm:
                return case_id
            score = text_similarity(sign_text, cand_text)
            if score >= self._threshold and score > best_score:
                best_case, best_score = case_id, score
        return best_case
