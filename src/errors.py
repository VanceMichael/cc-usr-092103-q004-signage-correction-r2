"""领域错误类型。"""


class DomainError(Exception):
    """业务规则被违反。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class DuplicateError(DomainError):
    """唯一性约束冲突（如规范版本重复）。"""


class InvalidTransitionError(DomainError):
    """当前状态不允许该操作。"""


class PermissionDeniedError(DomainError):
    """操作者不具备所需角色或权属。"""


class ConcurrencyConflictError(DomainError):
    """基于期望版本的追加失败：流已被他人推进，需重读后重试。"""


class ClaimConflictError(ConcurrencyConflictError):
    """多人认领同一线索时，后到的认领被拒绝。"""
