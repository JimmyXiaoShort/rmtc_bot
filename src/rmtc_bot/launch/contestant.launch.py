# -*- coding: utf-8 -*-
"""RMTC 参赛程序 launch 文件。

所有接入信息通过 launch 参数传入，换场只需改启动命令，无需 export 环境变量：

    ros2 launch rmtc_bot contestant.launch.py \\
        seat:=A team_name:=星火队 \\
        gateway:=http://<裁判机IP>:8000 team_token:=<本场令牌> \\
        ros_domain_id:=<赛前公布值>

不传的参数自动回退到同名环境变量（RMTC_SEAT 等），再回退到内置默认值；
正式比赛建议显式传参，避免依赖 shell 中残留的环境变量。

ROS_DOMAIN_ID 必须在 rclpy.init 之前生效，因此通过 SetEnvironmentVariable
在启动节点前写入环境，而不是作为节点参数。
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue

# 参数名 → (兜底环境变量, 默认值, 说明)
_PARAMS = (
    ("seat", "RMTC_SEAT", "A", "本场席位 A/B/C/D"),
    ("team_name", "RMTC_TEAM_NAME", "RMTC 队伍", "队伍显示名称（1–96 UTF-8 字节）"),
    ("gateway", "RMTC_GATEWAY", "", "LLM 网关基址，如 http://<裁判机IP>:8000"),
    ("team_token", "RMTC_TEAM_TOKEN", "", "本场席位令牌（严禁写入源码/日志）"),
    ("referee_ip", "RMTC_REFEREE_IP", "127.0.0.1", "未给 gateway 时用于拼装网关地址"),
    ("ai_port", "RMTC_AI_PORT", "8000", "未给 gateway 时用于拼装网关端口"),
    ("model", "RMTC_MODEL", "deepseek-flash", "LLM 模型名"),
    ("max_tokens", "RMTC_MAX_TOKENS", "128", "LLM 单次输出上限"),
    ("llm_quota", "RMTC_LLM_QUOTA", "100", "本地软配额，0 表示不限制"),
    ("fort_x0", "RMTC_FORT_X0", "11", "堡垒区域左上角 X"),
    ("fort_x1", "RMTC_FORT_X1", "13", "堡垒区域右下角 X"),
    ("fort_y0", "RMTC_FORT_Y0", "11", "堡垒区域左上角 Y"),
    ("fort_y1", "RMTC_FORT_Y1", "13", "堡垒区域右下角 Y"),
    ("fort_auto_center", "RMTC_FORT_AUTO_CENTER", "1", "1=按实际地图尺寸自动居中 3×3"),
    ("fort_require_move", "RMTC_FORT_REQUIRE_MOVE", "1", "1=必须成功移动才累计驻留"),
    ("fort_retreat_hp", "RMTC_FORT_RETREAT_HP", "40", "低于该血量撤离堡垒"),
    ("fort_resume_hp", "RMTC_FORT_RESUME_HP", "70", "回到该血量以上再来驻留"),
    ("fort_enemy_policy", "RMTC_FORT_ENEMY_POLICY", "strike", "堡垒遇敌策略（strike/contest/avoid）"),
)


def generate_launch_description() -> LaunchDescription:
    declared = [
        DeclareLaunchArgument(
            name,
            default_value=EnvironmentVariable(env, default_value=default),
            description=desc,
        )
        for name, env, default, desc in _PARAMS
    ]

    # ROS_DOMAIN_ID 只能通过环境生效（必须在 rclpy.init 前设置）
    declared.append(
        DeclareLaunchArgument(
            "ros_domain_id",
            default_value=EnvironmentVariable("ROS_DOMAIN_ID", default_value="27"),
            description="ROS 域编号（赛前公布值）",
        )
    )
    set_domain = SetEnvironmentVariable(
        "ROS_DOMAIN_ID", LaunchConfiguration("ros_domain_id")
    )

    node = Node(
        package="rmtc_bot",
        executable="rmtc_bot",
        name="rmtc_bot_node",
        output="screen",
        # 统一按字符串传参：节点侧声明即为字符串，数值由 config.py 转换。
        # 若交给 launch 自动推断类型，ai_port 等会被写成 INTEGER 而与声明冲突。
        parameters=[
            {
                name: ParameterValue(LaunchConfiguration(name), value_type=str)
                for name, _, _, _ in _PARAMS
            }
        ],
    )

    return LaunchDescription(declared + [set_domain, node])
