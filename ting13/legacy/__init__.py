"""
兼容与历史模块。

.. deprecated::
    此包中的代码已迁移到 ting13.core / ting13.sources 新架构。
    请勿在新代码中引用此包，它将在未来版本中移除。
"""
import warnings
warnings.warn(
    "ting13.legacy 已弃用，请使用 ting13.core / ting13.sources 新架构。",
    DeprecationWarning,
    stacklevel=2,
)
