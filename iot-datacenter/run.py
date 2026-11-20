"""启动入口：内嵌 MQTT Broker + 仿真器 + Web 平台。

用法：  .venv\\Scripts\\python.exe run.py [--mode sim|real]
访问：  http://127.0.0.1:8000
"""

import argparse
import os
import pathlib
import sys

import uvicorn

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="智慧机房环境监测与能耗优化平台")
    parser.add_argument(
        "--mode",
        choices=["sim", "real"],
        help="运行模式：sim=仿真（内置虚拟设备），real=实际监测（外部设备上报）",
    )
    args = parser.parse_args()
    if args.mode:
        os.environ["IOT_MODE"] = args.mode
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="info")
