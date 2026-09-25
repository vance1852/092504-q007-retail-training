"""发布零售库存与承诺协调服务使用的分配规则集。

规则集有稳定版本号，每个分配方案都会记录所采用的规则版本，
并在方案轨迹中逐条引用规则编号，保证方案可解释、可审计。
"""

from __future__ import annotations

RULE_SET_VERSION = "retail-allocation-v1"

RULES = (
    {
        "rule_id": "R1",
        "title": "过期批次禁配",
        "description": "保质截止日早于承诺营业日的批次不得参与分配，截止日当天仍可分配。",
    },
    {
        "rule_id": "R2",
        "title": "先到期先出",
        "description": "同一存放位置内，合格批次按保质截止日升序分配。",
    },
    {
        "rule_id": "R3",
        "title": "货架优先",
        "description": "顾客承诺优先占用货架库存，货架不足时再动用后仓库存。",
    },
    {
        "rule_id": "R4",
        "title": "优先级与锁定排序",
        "description": "候选承诺按主管锁定、优先级、承诺时间先后排序处理；锁定承诺已分得的库存在重算中被保留。",
    },
    {
        "rule_id": "R5",
        "title": "不足部分延迟",
        "description": "可用库存不足时，已分得的数量先生成方案，缺口标记为延迟并等待补货。",
    },
    {
        "rule_id": "R6",
        "title": "营业日结转",
        "description": "门店关闭期间到达的请求，按所在地营业时间结转到下一营业日处理。",
    },
    {
        "rule_id": "R7",
        "title": "版本核对落账",
        "description": "确认方案必须匹配当前库存版本，全部扣减在同一事务内完成，失败不留部分扣减。",
    },
    {
        "rule_id": "R8",
        "title": "取消释放未履约",
        "description": "取消承诺只释放尚未履约的数量，已履约部分保持扣减。",
    },
)


def published_rules() -> dict[str, object]:
    """返回当前发布的规则集，供查询接口和方案解释使用。"""

    return {"version": RULE_SET_VERSION, "rules": [dict(rule) for rule in RULES]}
