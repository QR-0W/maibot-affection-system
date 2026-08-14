"""好感度系统插件配置模型

通过 PluginConfigBase 定义插件的所有可调节参数。
MaiBot 运行时会根据此模型自动生成 config.toml 并在 WebUI 中渲染配置页面。
"""

from pydantic import Field

from maibot_sdk.config import PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件设置"
    __ui_icon__ = "heart"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用好感度系统",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="1.1.0",
        description="配置版本号",
        json_schema_extra={"label": "配置版本"},
    )


class CallBehaviorSection(PluginConfigBase):
    """工具调用行为配置 —— 影响 LLM 感知和调用好感度插件的频率。"""

    __ui_label__ = "调用行为"
    __ui_icon__ = "activity"
    __ui_order__ = 5

    inject_affection_hint: bool = Field(
        default=True,
        description=(
            "是否在角色回复前，自动把当前用户的好感度信息注入到 LLM 上下文中，"
            "让 LLM 更自然地感知好感度并按照对应等级回应。"
            "该机制能显著提高好感度对角色言行的影响，也会增加少量 token 消耗。"
        ),
        json_schema_extra={"label": "注入好感度提示"},
    )
    inject_probability: float = Field(
        default=1.0,
        description=(
            "好感度提示注入的概率(0~1)。默认 1.0 表示每次都注入；"
            "设置为较低值时仅按概率注入以节省 token。"
        ),
        json_schema_extra={
            "label": "注入概率",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
        },
    )
    encourage_tool_calls: bool = Field(
        default=False,
        description=(
            "是否在注入的好感度提示末尾附上「可调用 adjust_affection 工具」的提醒。"
            "好感度工具已直接暴露给规划模型（无需 tool_search），且插件会自动评价"
            "大多数互动，通常无需开启此提醒。"
        ),
        json_schema_extra={"label": "鼓励调用工具"},
    )


class EvaluationSection(PluginConfigBase):
    """自动评价配置 —— 插件内 LLM 对每轮互动进行上下文综合评价。"""

    __ui_label__ = "自动评价"
    __ui_icon__ = "brain"
    __ui_order__ = 6

    enabled: bool = Field(
        default=True,
        description=(
            "是否启用插件内自动评价：用户消息到达后，由插件调用 LLM，"
            "基于最近对话上下文、角色性格与当前关系等级，综合判断本次互动"
            "是否值得改变好感度、方向和幅度。判断不依赖 Planner 的工具调用意愿。"
        ),
        json_schema_extra={"label": "启用自动评价"},
    )
    model_task: str = Field(
        default="utils",
        description=(
            "自动评价使用的模型任务名，对应 model_config.toml 中的 "
            "[model_task_config.*]（如 utils / planner）。建议使用轻量快速的小模型。"
        ),
        json_schema_extra={"label": "评价模型任务", "placeholder": "utils"},
    )
    max_context_messages: int = Field(
        default=8,
        description="评价时提供给 LLM 的最近对话条数（含用户与角色双方的消息）。",
        json_schema_extra={
            "label": "上下文消息条数",
            "min": 2,
            "max": 30,
            "step": 1,
        },
    )
    max_response_tokens: int = Field(
        default=1024,
        description="自动评价请求的最大输出 token 数。",
        json_schema_extra={
            "label": "最大输出 Token",
            "min": 128,
            "max": 4096,
            "step": 64,
        },
    )
    temperature: float = Field(
        default=0.2,
        description="自动评价请求的温度。较低的值让评价结果更稳定。",
        json_schema_extra={
            "label": "评价温度",
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
        },
    )


class PersonalitySection(PluginConfigBase):
    """角色性格配置 —— 决定好感度变化的方式和幅度。"""

    __ui_label__ = "角色性格"
    __ui_icon__ = "user"
    __ui_order__ = 1

    character_name: str = Field(
        default="麦麦",
        description="角色的名字，用于好感度显示和回应中",
        json_schema_extra={"label": "角色名称", "placeholder": "麦麦"},
    )
    personality_type: str = Field(
        default="默认",
        description=(
            "角色的性格类型，影响好感度变化速率和回应风格。\n"
            "- 默认：标准变化速率，中性回应\n"
            "- 傲娇：好感上升慢，口是心非\n"
            "- 温柔：好感上升正常，温暖回应\n"
            "- 高冷：好感上升极慢，冷淡回应\n"
            "- 开朗：好感上升较快，热情回应\n"
            "- 毒舌：好感上升慢，说话带刺但内心柔软"
        ),
        json_schema_extra={
            "label": "性格类型",
            "placeholder": "默认",
            "hint": "可选：默认、傲娇、温柔、高冷、开朗、毒舌",
        },
    )
    affection_gain_multiplier: float = Field(
        default=1.0,
        description="好感度变化倍率。大于1则好感变化更快，小于1则更慢。例如：0.5表示变化速度为正常的一半。",
        json_schema_extra={
            "label": "好感度变化倍率",
            "min": 0.1,
            "max": 5.0,
            "step": 0.1,
        },
    )
    character_personality_hint: str = Field(
        default="",
        description=(
            "角色性格简述，供自动评价参考（例如：傲娇、贪财、口嫌体正直的巫女；"
            "温柔体贴、重视承诺）。留空时自动评价将只能依据性格类型名判断。"
        ),
        json_schema_extra={
            "label": "角色性格简述",
            "placeholder": "例：傲娇、贪财、口嫌体正直的巫女",
        },
    )
    affection_decay_enabled: bool = Field(
        default=False,
        description="是否启用好感度自然衰减。长时间不互动，好感度会缓慢下降。",
        json_schema_extra={"label": "启用好感度衰减"},
    )
    affection_decay_days: int = Field(
        default=30,
        description="好感度自然衰减的触发天数。超过此天数未互动，好感度将开始缓慢下降。",
        json_schema_extra={
            "label": "衰减触发生天数",
            "min": 1,
            "max": 365,
            "step": 1,
        },
    )
    affection_decay_rate: float = Field(
        default=0.5,
        description="每次衰减的好感度点数（触发衰减后才开始每天扣减）。",
        json_schema_extra={
            "label": "每次衰减点数",
            "min": 0.1,
            "max": 5.0,
            "step": 0.1,
        },
    )


class AffectionRuleSection(PluginConfigBase):
    """好感度变化规则配置 —— 定义不同互动类型对应的好感度变化范围。"""

    __ui_label__ = "变化规则"
    __ui_icon__ = "sliders"
    __ui_order__ = 2

    initial_affection: float = Field(
        default=10.0,
        description="新用户首次互动时的初始好感度（默认 10，对应「正常」等级起点）",
        json_schema_extra={
            "label": "初始好感度",
            "min": -20,
            "max": 100,
            "step": 1,
        },
    )
    positive_interaction_min: float = Field(
        default=0.5,
        description="正面互动（赞美、关心、帮助等）的最低好感度增加值",
        json_schema_extra={"label": "正面互动最小增值", "min": 0.1, "max": 10.0, "step": 0.1},
    )
    positive_interaction_max: float = Field(
        default=3.0,
        description="正面互动的最高好感度增加值",
        json_schema_extra={"label": "正面互动最大增值", "min": 0.1, "max": 10.0, "step": 0.1},
    )
    negative_interaction_min: float = Field(
        default=-3.0,
        description="负面互动（冒犯、争吵、无视等）的最低好感度变化值（负值表示降低）",
        json_schema_extra={"label": "负面互动最大减值", "min": -10.0, "max": -0.1, "step": 0.1},
    )
    negative_interaction_max: float = Field(
        default=-0.5,
        description="负面互动的最高好感度变化值（负值表示降低）",
        json_schema_extra={"label": "负面互动最小减值", "min": -10.0, "max": -0.1, "step": 0.1},
    )
    neutral_interaction_value: float = Field(
        default=0.1,
        description="中性互动（普通聊天）的好感度变化值",
        json_schema_extra={"label": "中性互动增值", "min": 0.0, "max": 2.0, "step": 0.1},
    )
    special_interaction_value: float = Field(
        default=5.0,
        description="特殊互动（送礼、表白、重大事件等）的好感度变化值",
        json_schema_extra={"label": "特殊互动增值", "min": 1.0, "max": 20.0, "step": 0.5},
    )
    cool_down_seconds: int = Field(
        default=60,
        description="同一位用户两次好感度变更之间的最小间隔（秒），防止频繁刷好感",
        json_schema_extra={
            "label": "变更冷却时间（秒）",
            "min": 10,
            "max": 3600,
            "step": 10,
        },
    )
    daily_limit: int = Field(
        default=20,
        description="每位用户每天最多允许的好感度变更次数",
        json_schema_extra={
            "label": "每日变更上限",
            "min": 1,
            "max": 100,
            "step": 1,
        },
    )
    high_affection_negative_damper_threshold: float = Field(
        default=40.0,
        description=(
            "高位好感度阻尼阈值：当好感度高于此值时，负面互动造成的扣分"
            "会按「高位阻尼系数」缩小（亲密关系经得起小摩擦，不会因为一次"
            "口角大幅掉好感）。"
        ),
        json_schema_extra={
            "label": "高位阻尼阈值",
            "min": 0,
            "max": 100,
            "step": 1,
        },
    )
    high_affection_negative_damper_multiplier: float = Field(
        default=0.5,
        description="高位阻尼系数：好感度高于阈值时，单次扣分乘以该系数（0.5 表示扣分减半）。",
        json_schema_extra={
            "label": "高位阻尼系数",
            "min": 0.1,
            "max": 1.0,
            "step": 0.05,
        },
    )
    negative_spiral_limit: int = Field(
        default=3,
        description=(
            "负面螺旋阻尼：同一位用户同一天内连续负面调整达到此次数后，"
            "后续每次扣分再乘以「负面螺旋阻尼系数」，防止短时间内好感度断崖式下跌。"
        ),
        json_schema_extra={
            "label": "负面螺旋次数上限",
            "min": 1,
            "max": 20,
            "step": 1,
        },
    )
    negative_spiral_decay: float = Field(
        default=0.5,
        description="负面螺旋阻尼系数：超过上限后，每次扣分乘以该系数（0.5 表示扣分减半）。",
        json_schema_extra={
            "label": "负面螺旋阻尼系数",
            "min": 0.1,
            "max": 1.0,
            "step": 0.05,
        },
    )


class ResponseSection(PluginConfigBase):
    """回应风格配置 —— 不同好感度等级下角色的回应风格提示。"""

    __ui_label__ = "回应风格"
    __ui_icon__ = "message-circle"
    __ui_order__ = 3

    dislike_response_hint: str = Field(
        default="态度冷淡疏远，回复简短，尽量不主动搭话",
        description="-20~0 不喜等级的回应风格提示（送给 LLM 参考）",
        json_schema_extra={"label": "不喜等级回应风格", "placeholder": "态度冷淡疏远..."},
    )
    normal_response_hint: str = Field(
        default="态度礼貌中立，像对待普通熟人一样交流，不过分热情也不冷淡",
        description="0~10 正常等级的回应风格提示",
        json_schema_extra={"label": "正常等级回应风格", "placeholder": "态度礼貌中立..."},
    )
    familiar_response_hint: str = Field(
        default="态度友好自然，愿意多聊几句，偶尔开些无伤大雅的玩笑",
        description="10~20 熟悉等级的回应风格提示",
        json_schema_extra={"label": "熟悉等级回应风格", "placeholder": "态度友好自然..."},
    )
    close_response_hint: str = Field(
        default="态度温暖亲近，会主动分享自己的想法和感受，回应更加丰富",
        description="20~40 亲近等级的回应风格提示",
        json_schema_extra={"label": "亲近等级回应风格", "placeholder": "态度温暖亲近..."},
    )
    intimate_response_hint: str = Field(
        default="态度亲密真诚，信任对方，会倾诉内心，回应充满关心和体贴",
        description="40~70 亲密等级的回应风格提示",
        json_schema_extra={"label": "亲密等级回应风格", "placeholder": "态度亲密真诚..."},
    )
    adore_response_hint: str = Field(
        default="态度充满依恋和柔情，会撒娇、表达思念，对对方有特别的情感",
        description="70~100 思慕等级的回应风格提示",
        json_schema_extra={"label": "思慕等级回应风格", "placeholder": "态度充满依恋..."},
    )


class ManagedUserEntry(PluginConfigBase):
    """好感度管理表中的单个用户条目。

    用于在 WebUI 中以表格形式查看和手动修改单个用户的好感度。
    """

    user_id: str = Field(
        default="",
        description="用户唯一标识（平台用户 ID，如 QQ 号）",
        json_schema_extra={"label": "用户 ID", "placeholder": "例如 123456789"},
    )
    affection: float = Field(
        default=10.0,
        description="好感度数值，范围 -20 ~ 100（默认 10，对应新用户的初始好感度）",
        json_schema_extra={"label": "好感度", "min": -20, "max": 100, "step": 1},
    )
    locked: bool = Field(
        default=False,
        description="锁定后 LLM 无法自动调整该用户好感度，只能通过这里手动修改",
        json_schema_extra={"label": "锁定"},
    )
    note: str = Field(
        default="",
        description="备注信息，方便识别该用户",
        json_schema_extra={"label": "备注", "placeholder": "可选备注"},
    )


class UserManagementSection(PluginConfigBase):
    """好感度可视化管理配置节。

    在 WebUI 中渲染为可编辑表格，用于查看和手动修改各个用户的好感度。
    """

    __ui_label__ = "好感度管理"
    __ui_icon__ = "users"
    __ui_order__ = 4

    sync_to_config: bool = Field(
        default=True,
        description="运行时好感度变化是否自动同步到下方的管理表，以便在 WebUI 中实时查看最新好感度",
        json_schema_extra={"label": "自动同步到管理表"},
    )
    managed_users: list[ManagedUserEntry] = Field(
        default_factory=list,
        description="好感度管理表：在此查看和手动修改各用户的好感度，保存后立即生效。锁定条目将禁止 LLM 自动调整。",
        json_schema_extra={
            "label": "好感度管理表",
            "hint": "添加一行并填写用户 ID 和好感度后保存即可生效。改动会在保存时写入运行时数据。",
        },
    )


class AffectionConfig(PluginConfigBase):
    """好感度系统插件总配置。

    聚合所有子配置模块，形成完整的插件配置体系。
    """

    plugin: PluginSection = Field(default_factory=PluginSection)
    personality: PersonalitySection = Field(default_factory=PersonalitySection)
    affection_rule: AffectionRuleSection = Field(default_factory=AffectionRuleSection)
    response: ResponseSection = Field(default_factory=ResponseSection)
    user_management: UserManagementSection = Field(default_factory=UserManagementSection)
    call_behavior: CallBehaviorSection = Field(default_factory=CallBehaviorSection)
    evaluation: EvaluationSection = Field(default_factory=EvaluationSection)


def create_config() -> AffectionConfig:
    """创建默认配置实例（供手动生成 config.toml 使用）。"""
    return AffectionConfig()
