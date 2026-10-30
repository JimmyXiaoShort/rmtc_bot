#!/usr/bin/env bash
# ============================================================================
# RMTC 参赛程序启动脚本（ros2 launch 的薄封装）
#
# 用法：
#   source /opt/ros/humble/setup.bash
#   source install/setup.bash
#   ./run_contestant.sh seat:=A team_name:=星火队 \
#       gateway:=http://<裁判机IP>:8000 team_token:=<本场令牌> \
#       ros_domain_id:=<赛前公布值>
#
# 不传的参数由 launch 文件回退到环境变量 / 内置默认值。
# 现场只允许修改 config/decision_config.json 的 decision_type 字段。
# ============================================================================
set -euo pipefail

if ! ros2 pkg prefix rmtc_bot >/dev/null 2>&1; then
  echo "未找到 rmtc_bot 包：请先 source /opt/ros/humble/setup.bash 与 install/setup.bash" >&2
  exit 1
fi

exec ros2 launch rmtc_bot contestant.launch.py "$@"
