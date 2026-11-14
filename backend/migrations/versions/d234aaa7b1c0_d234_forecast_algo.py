"""D234 磁盘容量趋势预测：alert_rules.algorithm 枚举追加 'forecast'。

为什么敢动 ENUM（而 f0c1a2d3b4e5 当年不敢动 host_status）：
MySQL 的 ENUM **尾部追加**值是纯元数据变更——行数据与索引都不动、瞬间完成；
当年回避的是"改既有枚举值语义/顺序"那类需要重写行的变更，两者风险量级不同。
即便如此仍做两道保险：
  · upgrade 幂等：先查 information_schema 的 COLUMN_TYPE，已含 'forecast' 直接跳过
    （重复执行/多环境漂移都不炸）；
  · downgrade 先把存量 forecast 规则退回 threshold，再收缩枚举（否则 MODIFY 失败）。

forecast 的语义见 app/engine/detect.py::_detect_forecast 与
app/services/capacity.py（拟合与 ETA 计算的完整方法论）。

Revision ID: d234aaa7b1c0
Revises: e2f3a4b5c6d7
"""
from sqlalchemy import text

from alembic import op

revision = "d234aaa7b1c0"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def _column_type() -> str:
    row = op.get_bind().execute(text(
        "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alert_rules' "
        "AND COLUMN_NAME = 'algorithm'")).fetchone()
    return (row[0] or "") if row else ""


def _enum_with_forecast() -> str:
    """以库里的真实 COLUMN_TYPE 为底稿追加 'forecast'——原样保留字符集/排序规则
    （不同环境可能是 utf8mb4_general_ci / utf8mb4_0900_ai_ci，写死必踩一处）。"""
    cur = _column_type()
    assert cur.startswith("enum(") and cur.endswith(")"), f"algorithm 列不是 ENUM：{cur}"
    inner = cur[len("enum("):-1]
    assert "'forecast'" not in inner, inner
    return f"enum({inner},'forecast')"


def upgrade() -> None:
    if "forecast" in _column_type():
        return
    new_type = _enum_with_forecast()
    # NOT NULL 与原列一致（D2 建表即 nullable=False、无 server_default，Python 侧默认 threshold）
    op.execute(f"ALTER TABLE alert_rules MODIFY COLUMN algorithm {new_type} NOT NULL")


def downgrade() -> None:
    if "forecast" not in _column_type():
        return
    op.get_bind().execute(text(
        "UPDATE alert_rules SET algorithm = 'threshold' WHERE algorithm = 'forecast'"))
    cur = _column_type()
    shrunk = cur.replace(",'forecast'", "")
    op.execute(f"ALTER TABLE alert_rules MODIFY COLUMN algorithm {shrunk} NOT NULL")
