# -*- coding: utf-8 -*-
"""
RMTC 机甲大师图灵对决赛 —— 协议常量集中定义。

本文件把官方《赛事规则 v1.1》与《选手通信协议 v1.1》中出现的
话题名、动作名、方向名、错误码、数值规则、阶段边界、计分公式等
集中于此，供全工程引用。所有取值均与官方原文一致；原文未明确的
以注释标注「原文未明确」。

【合规提醒】本文件中不得出现任何用于「解析随机事件」的关键词表 /
正则 / 固定句式模板——随机事件必须交由统一 LLM 网关理解（协议第 3 节、
第 5 节）。事件相关的字符串仅用于「组装发给 LLM 的提示词」与「校验 LLM 输出」。
"""

# ---------------------------------------------------------------------------
# 1. 话题名（协议第 2 节「公开话题」）
#    X 为主办方分配的本场 A/B/C/D 席位。
#    全部话题消息类型均为 std_msgs/msg/String。
# ---------------------------------------------------------------------------

TOPIC_PERCEPTION_FMT = "/team_{seat}/perception"      # 订阅本队席位；JSON：当前感知
TOPIC_ACTION_FMT = "/team_{seat}/action"              # 发布本队席位；JSON：当前 Tick 动作
TOPIC_REGISTRATION_FMT = "/team_{seat}/registration"  # 发布本队席位；JSON：队伍显示名称
TOPIC_MISSION = "/referee/mission"                    # 订阅；纯文本：自然语言任务
TOPIC_EVENT_TEXT = "/referee/event_text"              # 订阅；纯文本：自然语言随机事件公告
TOPIC_TEAM_REGISTRY = "/referee/team_registry"        # 订阅；JSON：席位与队名对应关系

# QoS 参数（协议第 2 节 QoS 表）
#   感知、动作：            RELIABLE / VOLATILE        / KEEP_LAST(1)
#   注册、任务、队伍名录：  RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(1)
#   事件公告：              RELIABLE / TRANSIENT_LOCAL / KEEP_LAST(128)
QOS_DEPTH_PERCEPTION_ACTION = 1
QOS_DEPTH_REGISTRY_MISSION = 1
QOS_DEPTH_EVENT = 128

# ---------------------------------------------------------------------------
# 2. 动作与方向（规则第 6 节、协议第 4 节）
#    动作字段必须小写；MOVE 与 ATTACK 必须附带小写方向。
# ---------------------------------------------------------------------------
ACTION_MOVE = "move"
ACTION_ATTACK = "attack"
ACTION_DEFEND = "defend"
ACTION_WAIT = "wait"
ACTIONS = (ACTION_MOVE, ACTION_ATTACK, ACTION_DEFEND, ACTION_WAIT)

# 第一阶段仅允许 MOVE 和 WAIT（规则 §1、§6、§9）
ACTIONS_STAGE1 = (ACTION_MOVE, ACTION_WAIT)
# 第二阶段动作白名单：MOVE / ATTACK / DEFEND / WAIT（规则 §6、§10）
ACTIONS_STAGE2 = ACTIONS
ALLOWED_PHASE1 = frozenset(ACTIONS_STAGE1)
ALLOWED_PHASE2 = frozenset(ACTIONS_STAGE2)

DIR_UP = "up"
DIR_DOWN = "down"
DIR_LEFT = "left"
DIR_RIGHT = "right"
DIRECTIONS = (DIR_UP, DIR_DOWN, DIR_LEFT, DIR_RIGHT)

# 方向 → (dx, dy)；坐标原点 [0,0] 左上角，x 向右增大、y 向下增大（规则 §4.1）
DIR_VECTORS = {
    DIR_UP: (0, -1),
    DIR_DOWN: (0, 1),
    DIR_LEFT: (-1, 0),
    DIR_RIGHT: (1, 0),
}

# 幻术事件的 MOVE 方向反转映射（规则 §8.3：up/down、left/right 互换；ATTACK 方向不反转）
ILLUSION_FLIP = {
    DIR_UP: DIR_DOWN,
    DIR_DOWN: DIR_UP,
    DIR_LEFT: DIR_RIGHT,
    DIR_RIGHT: DIR_LEFT,
}

# ---------------------------------------------------------------------------
# 3. 席位（规则 §2.2、协议第 1 节）
# ---------------------------------------------------------------------------
SEATS = ("A", "B", "C", "D")
# 第一阶段单车使用 A 席位
SEAT_STAGE1 = "A"

# ---------------------------------------------------------------------------
# 4. 阶段 / Tick 边界（规则第 3 节、协议第 4 节）
#    第一阶段：100 ms/Tick，100 Tick，动作 Tick 为 0–99，最终状态 tick=100
#    第二阶段：500 ms/Tick，500 Tick，动作 Tick 为 0–499，最终状态 tick=500
# ---------------------------------------------------------------------------
STAGE1_TICK_MIN = 0
STAGE1_TICK_MAX = 99            # 第一阶段有效动作 Tick 上界
STAGE1_FINAL_TICK = 100         # 最终状态（发布后不接收新动作）
STAGE1_TOTAL_TICKS = 100
STAGE1_TICK_MS = 100

STAGE2_TICK_MIN = 0
STAGE2_TICK_MAX = 499           # 第二阶段有效动作 Tick 上界
STAGE2_FINAL_TICK = 500         # 最终状态（发布后不接收新动作）
STAGE2_TOTAL_TICKS = 500
STAGE2_TICK_MS = 500

# 标准地图尺寸（规则 §4.1；各场实际尺寸随本队感知提供）
MAP_STAGE1_STD = (20, 20)
MAP_STAGE2_STD = (24, 24)

# 视野：横纵各 ±4 格的 9×9 正方形；vision_radius 整数 0–4（规则 §4.1、协议第 3 节）
VISION_RADIUS_MAX = 4
REL_POS_ROWS = 9
REL_POS_COLS = 9
REL_POS_SELF_ROW = 5            # 1 固定在第 5 行（行列从 1 开始）
REL_POS_SELF_COL = 5
REL_POS_CENTER_INDEX = 4        # 0 基下标
REL_POS_TOTAL_CHARS = 89        # 81 个格子字符 + 8 个换行符，无末尾换行
REL_POS_SELF_CHAR = "1"
REL_POS_TARGET_CHAR = "2"
REL_POS_EMPTY_CHAR = "0"

# ---------------------------------------------------------------------------
# 5. 基础属性（规则 §4.3）
# ---------------------------------------------------------------------------
HP_MAX = 100                    # 最大/初始生命 100 HP
MOVE_SPEED = 1                  # 移动速度 1 格/Tick
ATTACK_DAMAGE_NORMAL = 20       # 普通攻击 20 HP
ATTACK_DAMAGE_ON_DEFEND = 10    # 防御后的普通受击 10 HP/次
ATTACK_RANGE_MIN = 1            # 普通攻击范围：指定方向直线 1–2 格
ATTACK_RANGE_MAX = 2
RESPAWN_WAIT_TICKS = 3          # 普通死亡复活等待 3 Tick
RESPAWN_PROTECT_TICKS = 2       # 普通复活出生保护 2 Tick

# ---------------------------------------------------------------------------
# 6. 资源（规则 §4.2）
# ---------------------------------------------------------------------------
RESOURCE_NORMAL_VALUE = 10      # 普通资源单个价值 10，每场初始数量 12
RESOURCE_HIGH_VALUE = 20        # 高价值资源单个价值 20，每场初始数量 4
RESOURCE_HIGH_THRESHOLD = 20    # 接口中 value >= 20 归为高价值资源
RESOURCE_TOTAL_VALUE = 200      # 合计总价值 200
RESOURCE_TOTAL_COUNT = 16       # 合计 16 个

# 实体 type 取值（协议第 3 节字段说明）
ENTITY_ROBOT = "robot"
ENTITY_RESOURCE_NORMAL = "resource_normal"
ENTITY_RESOURCE_HIGH = "resource_high"
ENTITY_OBSTACLE = "obstacle"

# ---------------------------------------------------------------------------
# 7. 计分（规则 §9、§10.2；协议更新说明）
# ---------------------------------------------------------------------------
# 第一阶段得分 = 40 × R1 / 200，最高 40 分
STAGE1_SCORE_MAX = 40.0
STAGE1_SCORE_FORMULA_DENOM = 200.0

# 第二阶段：
#   资源得分 = 30 × min(R2 / 120, 1)
#   对抗得分 = 15 × min(D / 500, 1)   ← v1.1 由 300 调整为 500
#   任务得分 = 5 × M
#   总分最高 60 分
STAGE2_RESOURCE_SCORE_MAX = 30.0
STAGE2_RESOURCE_FORMULA_DENOM = 120.0
STAGE2_COMBAT_SCORE_MAX = 15.0
STAGE2_COMBAT_DAMAGE_FULL = 500.0   # D 达到 500 时对抗分满分（v1.1）
STAGE2_MISSION_SCORE_PER_TASK = 5.0
STAGE2_TOTAL_SCORE_MAX = 60.0

# 有效伤害 D：每 Tick 先累计实际攻击伤害 + 击杀奖励，再按死亡次数每次扣 60，最低 0；
# 可为小数、可下降、可超过 500（不截断）。
DEATH_DEDUCTION = 60.0
KILL_REWARD_TOTAL = 60.0

# 第一阶段种子排位；两阶段分数不相加决定晋级（规则 §1）
# ---------------------------------------------------------------------------
# 8. 任务类型（协议 §3.1）
# ---------------------------------------------------------------------------
MISSION_TYPE_NORMAL_RESOURCES = "normal_resources"
MISSION_TYPE_HIGH_RESOURCES = "high_resources"
MISSION_TYPE_RESOURCE_VALUE = "resource_value"
MISSION_TYPE_EFFECTIVE_DAMAGE = "effective_damage"
MISSION_TYPE_REACH = "reach"
MISSION_TYPES = (
    MISSION_TYPE_NORMAL_RESOURCES,
    MISSION_TYPE_HIGH_RESOURCES,
    MISSION_TYPE_RESOURCE_VALUE,
    MISSION_TYPE_EFFECTIVE_DAMAGE,
    MISSION_TYPE_REACH,
)

# 练习基准任务坐标（规则 §10.1；正式目标通过 mission_text / missions 提供）
MISSION_PRACTICE_REACH_POS = (12, 12)

# ---------------------------------------------------------------------------
# 9. 动作错误码（规则 §12.3、协议第 4 节「错误码」）
# ---------------------------------------------------------------------------
ERR_PARSE = "E_PARSE"           # 消息不是合法 JSON
ERR_SCHEMA = "E_SCHEMA"         # 缺字段 / 类型不符 / 动作或方向非法（含 session_id 非字符串）
ERR_TICK = "E_TICK"             # tick 与当前 Tick 不一致
ERR_SESSION = "E_SESSION"       # 非空场次下 session_id 缺失或不一致
ERR_STATE = "E_STATE"           # 车辆状态或阶段不允许（阵亡、保护期攻击、阶段不允许）
ERR_RATE = "E_RATE"             # 单条动作消息超过 4096 字节
ERR_OOB = "E_OOB"               # 移动执行越界（通过执行回执提供，不增加 invalid_actions）
ERR_BLOCKED = "E_BLOCKED"       # 障碍、占位或移动冲突（同上）

ACTION_MAX_BYTES = 4096         # 单条动作消息上限（E_RATE 阈值）
ACTION_MSG_PER_TICK_MAX = 128   # 每队每 Tick 最多处理消息条数，超出直接忽略

# ---------------------------------------------------------------------------
# 10. LLM 网关（规则第 11 节、协议第 5 节）
# ---------------------------------------------------------------------------
LLM_CHAT_PATH = "/v1/chat/completions"
LLM_HEALTH_PATH = "/healthz"
LLM_MAX_BODY_BYTES = 2097152    # 请求体上限 2 MiB，超过返回 413
LLM_DEFAULT_PORT = 8000

# 频率与并发（协议 §5「请求频率与配额」）
LLM_IP_POST_LIMIT = 100         # 每个来源 IP 每 10 秒最多提交 100 次 POST
LLM_IP_WINDOW_S = 10.0
LLM_MAX_INFLIGHT = 2            # 每个席位令牌最多同时存在 2 个在途调用
LLM_GATEWAY_READ_TIMEOUT_S = 5.0   # 网关请求读取超时 5 秒
LLM_UPSTREAM_TIMEOUT_S = 30.0      # 上游超时默认 30 秒（504）

# 本地硬超时：必须显著小于动作窗口（阶段二 500 ms），确保不阻塞控制环
LLM_LOCAL_TIMEOUT_S = 1.9       # 参考协议 Python 示例的 timeout=1.9

# 本地软配额估计（赛前公告为准；0 表示不限制。暂定 100）
LLM_DEFAULT_SOFT_QUOTA = 100

# 本地错误码（协议 §5「响应与错误」）
LLM_CODE_MISSING_TOKEN = "missing_token"
LLM_CODE_INVALID_TOKEN = "invalid_token"
LLM_CODE_NOT_FOUND = "not_found"
LLM_CODE_PAYLOAD_TOO_LARGE = "payload_too_large"
LLM_CODE_QUOTA_EXCEEDED = "quota_exceeded"
LLM_CODE_RATE_LIMIT = "rate_limit"
LLM_CODE_CONCURRENCY_LIMIT = "concurrency_limit"
LLM_CODE_AUDIT_UNAVAILABLE = "audit_unavailable"
LLM_CODE_UNSUPPORTED_TE = "unsupported_transfer_encoding"
LLM_CODE_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
LLM_CODE_UPSTREAM_TIMEOUT = "upstream_timeout"

# ---------------------------------------------------------------------------
# 11. 队伍注册（协议第 2 节）
# ---------------------------------------------------------------------------
TEAM_NAME_MIN_BYTES = 1         # name 按 UTF-8 编码 1–96 字节
TEAM_NAME_MAX_BYTES = 96


# ---------------------------------------------------------------------------
# 12. 堡垒（规则 §8.5「天下无双」）
#
# 【官方原文逐字引用（赛事规则 v1.1 §8.5 天下无双）】
#   「地图中心生成 3×3 堡垒，存在 50 Tick。每 Tick 战斗后存活且移动后位于堡垒内
#     才累计驻留，连续 10 Tick 达标；离开或死亡清空尚未完成的驻留进度。达标在该
#     Tick 战斗后授予增益，之后须主动提交 ATTACK 和合法方向。ATTACK 攻击自身横纵
#     各 ±2 格的 5×5 方形内全部存活敌人，包含斜向、不受障碍遮挡，方向不限制目标
#     集合。对非防御目标伤害为 50 HP，防御目标 10 HP，出生保护目标 0 HP。已获得
#     增益后可离开堡垒，死亡复活也保留，但堡垒结束时统一失效。与易伤叠加时按其
#     倍率计算。」
#
# 【同 Tick 结算顺序（规则 v1.1 §5.2 第 7 步，逐字）】
#   「为存活车辆累计堡垒驻留，结算有效回血。」
#   → 官方把「累计堡垒驻留」与「结算有效回血」放在同一步、且**在战斗之后、在死亡
#     扣除之前**（第 8 步才做死亡扣除与任务检查）。
#
# 【原文未明确的细节（本工程不做数值臆造）】
#   (a) 「有效回血」的**每 Tick 回血量**、累计上限：原文未明确。
#       → 本工程不假设回血数值，仅依据感知每帧上报的 self.hp 判断，并在堡垒驻留
#         期间记录观测到的 HP 增量用于答辩与调参。
#   (b) 是否必须「本 Tick 发生成功移动」才累计驻留：原文措辞为「移动后位于堡垒
#       内」，未明确原地 WAIT 是否计入（对比 §8.4 天使恩泽明确写了「原地 WAIT …
#       不触发」）。→ 本工程默认按「需成功移动」实现，并提供配置项关闭。
#   (c) 50 Tick 生命周期的起算 Tick（何时生成/何时消失）：原文未明确。
#       → 提供配置项，默认按「全程存在」（把 [11,11]–[13,13] 视为固定赛场区域）。
#   (d) 堡垒**具体坐标**：官方规则仅称「地图中心」，未公布具体坐标；本工程默认值
#       [11,11]–[13,13] 来自**选手提供的赛场信息**，换场须按官方公告核对/配置。
# ---------------------------------------------------------------------------
FORT_RESIDENCE_TICKS = 10        # 连续驻留达标 Tick 数（§8.5）
FORT_LIFETIME_TICKS = 50         # 堡垒存在 50 Tick（§8.5）
FORT_BUFF_ATTACK_HALFSPAN = 2    # 增益后 ATTACK 覆盖自身横纵各 ±2 格 → 5×5 方形（§8.5）
FORT_BUFF_DMG_NORMAL = 50        # 增益后对非防御目标 50 HP（§8.5）
FORT_BUFF_DMG_DEFEND = 10        # 增益后对防御目标 10 HP（§8.5）
FORT_BUFF_DMG_PROTECTED = 0      # 增益后对出生保护目标 0 HP（§8.5）

# 堡垒范围默认值（**来源：选手提供的赛场信息**，非官方公布）
# 坐标约定：左上角 [0,0]，x 向右、y 向下 → x∈[11,13]、y∈[11,13]，共 3×3 = 9 格。
# 官方规则原文只写「地图中心生成 3×3 堡垒」，未公开具体坐标 → 换场须按官方公告核对。
FORT_X0_DEFAULT = 11
FORT_X1_DEFAULT = 13
FORT_Y0_DEFAULT = 11
FORT_Y1_DEFAULT = 13

# 本地策略参数（与官方数值无关，仅用于决策阈值；可按队伍风格调整）
FORT_RETREAT_HP_DEFAULT = 40     # HP ≤ 该值时低血回撤至堡垒（本地参数）
FORT_RESUME_HP_DEFAULT = 70      # 回血/增益后 HP ≥ 该值时恢复出击（本地参数）
# 敌车占据堡垒时的应对策略（本地参数，三选一）：
#   strike  = 优先打击（若敌在攻击线内先开火），否则视血量争夺空格
#   contest = 优先争夺空格，其次打击
#   avoid   = 避开（不主动进入；已在堡垒内则让常规对抗逻辑接管）
FORT_ENEMY_POLICY_DEFAULT = "strike"
FORT_ENEMY_POLICIES = ("strike", "contest", "avoid")


def fortress_cells(x0: int, x1: int, y0: int, y1: int):
    """由矩形范围生成堡垒格列表（含边界）。默认即 [11,11]–[13,13] 共 9 格。"""
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def center_3x3_cells(map_w: int, map_h: int):
    """
    按「地图中心 3×3」（规则 §8.5 措辞）由**实际地图尺寸**推导堡垒格。

    不硬编码 20/24：start = (size - 2) // 2，使 3 格大致居中。
    24 → start=11 → [11,13]（与选手提供的赛场信息一致）；20 → start=9 → [9,11]。
    仅当显式启用「自动居中标定」时使用；默认使用选手提供的固定 9 格。
    """
    sx = max(0, (map_w - 2) // 2)
    sy = max(0, (map_h - 2) // 2)
    return fortress_cells(sx, sx + 2, sy, sy + 2)
