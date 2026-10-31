# RMTC 参赛机器人程序

RMTC 机甲大师图灵对决赛参赛程序，基于 ROS 2 Humble + Python 实现。

## 目录结构

```
rmtc_bot/
├── README.md
├── decision_config.json         # 现场唯一可改文件（仅 decision_type）
├── run_contestant.sh            # 启动脚本
├── requirements.txt
├── package.xml                  # ROS 2 包描述
├── setup.py                     # ament_python 入口
├── rmtc_bot/
│   ├── __init__.py
│   ├── config.py               # 接入配置（域名/席位/令牌/网关）
│   ├── constants.py            # 协议常量集中定义
│   ├── models.py               # 内部数据结构
│   ├── comm/
│   │   ├── __init__.py
│   │   └── ros_bridge.py       # ROS 2 客户端封装
│   ├── perception/
│   │   ├── __init__.py
│   │   ├── parser.py           # 感知 JSON 解析（含 9×9 相对位置）
│   │   ├── world_model.py      # 世界模型
│   │   └── fortress.py         # 堡垒模型（规则 §8.5）
│   ├── decision/
│   │   ├── __init__.py
│   │   ├── fsm.py              # 决策状态机
│   │   ├── collector.py        # 阶段一采集策略
│   │   ├── combat.py           # 阶段二对抗/任务策略
│   │   ├── fortress_policy.py  # 堡垒策略叠加
│   │   ├── event_policy.py     # 事件响应（消费 LLM 结构化结果）
│   │   └── registry.py         # decision_type 登记与分派
│   ├── action/
│   │   ├── __init__.py
│   │   ├── builder.py          # 动作消息构造 + tick/session 对齐
│   │   └── guard.py            # 校验/错误码规避
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── gateway.py          # OpenAI 兼容网关封装
│   │   ├── quota.py            # 配额与在途限制
│   │   └── prompts.py          # 事件/任务 Prompt + 输出解析
│   ├── logging_utils/
│   │   ├── __init__.py
│   │   └── monitor.py          # 结构化日志、指标、脱敏
│   └── main.py                 # 主循环装配
├── config/
│   └── decision_config.json    # 默认 decision_config（安装到 share）
├── launch/
│   └── contestant.launch.py
├── scripts/
│   ├── run_contestant.sh
│   └── selftest_local.py
└── tests/
    ├── test_position_parse.py
    ├── test_action_schema.py
    └── test_session_reset.py
```

## 快速启动

```bash
source /opt/ros/humble/setup.bash
colcon build --packages-select rmtc_bot
source install/setup.bash
./run_contestant.sh seat:=A team_name:=队伍名称 \
    gateway:=http://<裁判机IP>:8000 team_token:=<本场令牌>
```

## 现场策略切换

编辑 `decision_config.json`，将 `decision_type` 改为已登记枚举之一：
- `auto`：自动判定阶段（默认）
- `collect`：强制第一阶段采集
- `combat`：强制第二阶段对抗
- `safe`：保守/降级模式
