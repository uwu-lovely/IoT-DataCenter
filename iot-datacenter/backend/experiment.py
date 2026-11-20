"""受控对照实验：相同负载脚本与时长，依次运行三种调控策略。

每组实验之间会重置机房环境、控制器与规则引擎状态，保证起点一致；
结果按时间窗从 room_state 中统计并写入 experiment_runs，可直接导出为报告数据。
"""

import asyncio
import time

import db
from config import CONTROL_MODE_CN, CONTROL_MODES

MODES = CONTROL_MODES
MODE_CN = CONTROL_MODE_CN

# 相对实验开始的负载注入脚本（秒, 机房, 功率）
LOAD_PROFILE = [
    (15, "roomA", 4000),
    (35, "roomB", 3000),
    (80, "roomA", 2500),
]

SETTLE_SECONDS = 8.0


class ExperimentManager:
    def __init__(self, sim, controller, engine, notify) -> None:
        self.sim = sim
        self.ctl = controller
        self.engine = engine
        self.notify = notify  # async callable(dict)
        self.task: asyncio.Task | None = None
        self.state = self._idle_state()
        self.exp_start = 0.0
        self.exp_end = 0.0

    @staticmethod
    def _idle_state() -> dict:
        return {
            "id": None,
            "phase": "idle",
            "elapsed": 0.0,
            "duration": 180.0,
            "runs": [],
            "message": "尚未运行对照实验",
        }

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, duration: float = 180.0) -> bool:
        if self.running:
            return False
        # 同步写入初始状态，避免接口返回上一次实验的旧状态
        self.state = {
            "id": time.strftime("%Y%m%d-%H%M%S"),
            "phase": "preparing",
            "elapsed": 0.0,
            "duration": float(duration),
            "runs": [],
            "message": "实验准备中",
        }
        self.task = asyncio.create_task(self._run(float(duration)))
        return True

    def status(self) -> dict:
        data = dict(self.state)
        data["running"] = self.running
        data["load_profile"] = [
            {"offset": offset, "room": room, "delta_w": delta} for offset, room, delta in LOAD_PROFILE
        ]
        return data

    async def _run(self, duration: float) -> None:
        exp_id = self.state.get("id") or time.strftime("%Y%m%d-%H%M%S")
        self.state["id"] = exp_id
        self.state["duration"] = duration
        self.exp_start = time.time()
        db.resolve_all_open(self.exp_start)
        await self.notify({"type": "experiment", "state": self.status()})

        try:
            for mode in MODES:
                await self._prepare(mode)
                await self._run_phase(exp_id, mode, duration)
            self.state["phase"] = "done"
            self.state["message"] = "三组实验完成，可导出结果"
        except asyncio.CancelledError:
            self.state["phase"] = "cancelled"
            self.state["message"] = "实验已取消"
            raise
        finally:
            self.ctl.mode = "predictive"
            self.exp_end = time.time()
            await self.notify({"type": "experiment", "state": self.status()})

    async def _prepare(self, mode: str) -> None:
        self.ctl.mode = mode
        self.sim.reset_environment()
        self.ctl.reset()
        self.engine.reset()
        self.state["phase"] = mode
        self.state["elapsed"] = 0.0
        self.state["message"] = f"环境复位，准备运行 {MODE_CN[mode]}"
        await self.notify({"type": "experiment", "state": self.status()})
        await asyncio.sleep(SETTLE_SECONDS)

    async def _run_phase(self, exp_id: str, mode: str, duration: float) -> None:
        start = time.time()
        applied = set()
        while True:
            elapsed = time.time() - start
            if elapsed >= duration:
                break

            for idx, (offset, room, delta) in enumerate(LOAD_PROFILE):
                if idx not in applied and elapsed >= offset:
                    applied.add(idx)
                    self.sim.inject_load(room, delta)

            self.state["elapsed"] = round(elapsed, 1)
            self.state["message"] = f"{MODE_CN[mode]}运行中"
            await self.notify({"type": "experiment", "state": self.status()})
            await asyncio.sleep(1.0)

        finished = time.time()
        stats = db.run_stats(start, finished)
        db.insert_experiment_run(exp_id, mode, start, finished, stats)
        self.state["runs"].append(
            {
                "exp_id": exp_id,
                "mode": mode,
                "mode_cn": MODE_CN[mode],
                "started": start,
                "finished": finished,
                **stats,
            }
        )
        await self.notify({"type": "experiment", "state": self.status()})
        await asyncio.sleep(1.0)
