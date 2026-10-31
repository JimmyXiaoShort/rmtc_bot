# -*- coding: utf-8 -*-
"""RMTC 参赛程序 ROS2 包构建脚本（ament_python）。"""
from setuptools import find_packages, setup

PACKAGE_NAME = "rmtc_bot"

setup(
    name=PACKAGE_NAME,
    version="1.1.2",
    packages=find_packages(exclude=["tests", "tests.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + PACKAGE_NAME]),
        ("share/" + PACKAGE_NAME, ["package.xml"]),
        ("share/" + PACKAGE_NAME + "/launch", ["launch/contestant.launch.py"]),
        ("share/" + PACKAGE_NAME + "/config", ["config/decision_config.json"]),
    ],
    install_requires=["setuptools", "requests"],
    zip_safe=True,
    maintainer="RMTC Team",
    maintainer_email="team@example.com",
    description="RMTC 参赛程序：感知解析 / 决策 / LLM 事件理解 / ROS2 通信",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "rmtc_bot = rmtc_bot.main:main",
        ],
    },
)
