from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallerIdentity:
    """每次请求的可信身份；场景只参与统计，不参与授权或凭证选择。"""

    app_id: str
    subject: str
    principal_type: str
    authorized_party: str
    auth_method: str
    scene: str = "unspecified"
    correlation_id: str | None = None

    @classmethod
    def legacy(cls, app_id: str = "legacy", **metadata) -> CallerIdentity:
        return cls(app_id, "legacy", "legacy", "legacy", "legacy", **metadata)

    @classmethod
    def product(cls, app_id: str, user: str | None, **metadata) -> CallerIdentity:
        """产品由服务密钥认定；用户 ID 由该产品后端在登录校验后声明，只作统计。"""
        if user:
            return cls(app_id, user, "user", app_id, "service_key", **metadata)
        return cls(app_id, app_id, "service", app_id, "service_key", **metadata)
