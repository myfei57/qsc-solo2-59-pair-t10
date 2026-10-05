"""备件消耗与补货管理。

备件消耗没有固定节拍：平时可能几个月不动一次，检修月又集中更换一批。本子系统
按历史更换记录估计消耗速率，给出最低库存与补货建议，并跟踪采购单到货。

估计口径有两种：

* ``window``：只看最近一个窗口（默认近 30 天）的速率，对检修季的上升反应快，
  但在长期零消耗的月份会估成零；
* ``cycle``：按整个统计周期（默认 180 天）的均值算，平滑掉检修峰，但检修季
  反应滞后。

切换规则由 :mod:`batterystore.spare.rate` 的比值判据决定，并在每条速率结果里
给出当前使用口径与切换原因，调用方不自行拍脑袋。补录的更换事件同样进入速率
统计，但不改动当前库存；补录后建议自动重算，已下发的采购单一字不改，只生成
差异提示。同一备件在不同站（站点/仓库）的消耗既能合并看，也能按站分开看。
"""

from __future__ import annotations

from .events import ReplacementEvent
from .manager import SparePartsManager
from .order import PurchaseLedger, PurchaseOrder
from .parts import PartProfile
from .rate import RateEstimate
from .snapshot import SpareSnapshot
from .stock import StockPolicy

__all__ = [
    "PartProfile",
    "PurchaseLedger",
    "PurchaseOrder",
    "RateEstimate",
    "ReplacementEvent",
    "SparePartsManager",
    "SpareSnapshot",
    "StockPolicy",
]
