"""分发模块的应用装配：把 SQL 仓储接到领域服务，并映射 HTTP 错误。"""

from functools import lru_cache

from fastapi import HTTPException

from src.distribution.errors import (
    ConflictError, DistributionError, NotFoundError, ValidationError,
)
from src.distribution.service import DistributionService
from src.services.distribution_repo import SQLRepository


@lru_cache(maxsize=1)
def get_distribution_service() -> DistributionService:
    return DistributionService(SQLRepository())


def raise_http(error: DistributionError) -> None:
    if isinstance(error, ValidationError):
        raise HTTPException(422, str(error))
    if isinstance(error, NotFoundError):
        raise HTTPException(404, str(error))
    if isinstance(error, ConflictError):
        raise HTTPException(409, str(error))
    raise HTTPException(400, str(error))
