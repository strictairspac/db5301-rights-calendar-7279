"""Domain errors. HTTP 适配层负责映射为状态码。"""


class DistributionError(Exception):
    """所有分发域异常的基类。"""


class ValidationError(DistributionError):
    """输入不满足签署/投递的格式约束。"""


class NotFoundError(DistributionError):
    """引用的渠道、版本、批次或回执不存在。"""


class ConflictError(DistributionError):
    """状态机不允许该动作（如未确认批次挡住后续批次）。"""
