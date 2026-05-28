"""
核心采集引擎 (collector.py)
===========================

将 amap_api 和 grid_manager 整合为完整的 POI 异步采集引擎。
支持关键词循环、网格调度、溢出递归切分、详情补充、去重、
暂停/继续/停止、断点续传和 GUI 实时回调。
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

import pandas as pd

from amap_api import (
    AmapClient,
    AmapApiError,
    PoiBasic,
    PoiDetail,
)
from grid_manager import (
    GridGenerator,
    GridQueue,
    GridStatus,
    GridTask,
    build_checkpoint,
    load_checkpoint,
    save_checkpoint,
    split_grid,
)


# ============================================================================
#  日志配置
# ============================================================================

logger = logging.getLogger("amap_collector")


# ============================================================================
#  状态枚举
# ============================================================================


class EngineState(Enum):
    IDLE = "idle"
    ESTIMATING = "estimating"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ENRICHING = "enriching"
    COMPLETED = "completed"
    ERROR = "error"


# ============================================================================
#  回调容器
# ============================================================================


@dataclass
class CollectorCallbacks:
    """采集引擎向外部的回调函数集合，供 GUI 层绑定。

    所有回调均为可选的，不设置则静默跳过。
    """

    on_data: Optional[Callable[[List[PoiBasic]], None]] = None
    on_progress: Optional[Callable[[float, Dict[str, Any]], None]] = None
    on_log: Optional[Callable[[str, str], None]] = None
    on_status: Optional[Callable[[EngineState], None]] = None
    on_estimate: Optional[Callable[[Dict[str, Any]], None]] = None
    on_checkpoint: Optional[Callable[[str], None]] = None
    on_complete: Optional[Callable[[Dict[str, Any]], None]] = None


# ============================================================================
#  配置数据类
# ============================================================================


@dataclass
class CollectorConfig:
    """采集任务的全部配置参数。"""

    api_key: str = ""
    search_radius_km: float = 3.0
    keywords: List[str] = field(default_factory=list)
    poi_types: str = ""
    adcode: str = ""
    region_name: str = ""
    boundary: List[Tuple[float, float]] = field(default_factory=list)
    extra_rings: List[List[Tuple[float, float]]] = field(default_factory=list)
    qps: int = 5
    split_threshold: int = 850
    detail_interval: float = 0.2
    checkpoint_dir: str = "checkpoints"
    max_grids: int = 10000
    max_split_depth: int = 3

    def to_dict(self) -> dict:
        return {
            "api_key_masked": self.api_key[:4] + "****" if self.api_key else "",
            "search_radius_km": self.search_radius_km,
            "keywords": self.keywords,
            "adcode": self.adcode,
            "region_name": self.region_name,
            "qps": self.qps,
            "split_threshold": self.split_threshold,
        }


# ============================================================================
#  采集引擎
# ============================================================================


class CollectorEngine:
    """POI 异步采集引擎主控制器。

    完整采集流程：
      1. 初始化网格 (init_grids)
      2. 预估统计 (estimate)
      3. 主采集循环 (每个关键词 × 每个网格)
         - 多边形搜索 → 溢出检测 → 递归切分 → 去重写入
      4. POI 详情补充 (enrich_details)
      5. 导出 (export_csv)
      6. 清理

    使用 asyncio 实现异步协调，利用 run_in_executor 封装同步 API 调用。
    """

    def __init__(
        self,
        config: Optional[CollectorConfig] = None,
        callbacks: Optional[CollectorCallbacks] = None,
    ) -> None:
        self.config = config or CollectorConfig()
        self.cb = callbacks or CollectorCallbacks()

        self._client: Optional[AmapClient] = None
        self._grid_generator: Optional[GridGenerator] = None
        self._grid_queue: Optional[GridQueue] = None

        self.df: pd.DataFrame = pd.DataFrame()
        self._poi_id_set: Set[str] = set()

        self._state: EngineState = EngineState.IDLE
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_event = asyncio.Event()
        self._main_task: Optional[asyncio.Task] = None
        self._executor = None

        self._start_time: float = 0.0
        self._total_api_calls: int = 0
        self._total_pois_collected: int = 0
        self._total_pois_enriched: int = 0
        self._current_keyword: str = ""
        self._current_grid_hex: str = ""
        self._keyword_index: int = 0

    # ------------------------------------------------------------------
    #  属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> EngineState:
        return self._state

    @state.setter
    def state(self, value: EngineState) -> None:
        self._state = value
        self._emit_status(value)

    @property
    def progress(self) -> float:
        if self._grid_queue is None or self._grid_queue.total_count == 0:
            return 0.0
        return self._grid_queue.progress

    @property
    def is_running(self) -> bool:
        return self.state in (EngineState.RUNNING, EngineState.ENRICHING)

    @property
    def is_paused(self) -> bool:
        return self.state == EngineState.PAUSED

    @property
    def is_stopped(self) -> bool:
        return self.state in (EngineState.STOPPED, EngineState.COMPLETED, EngineState.IDLE)

    @property
    def poi_count(self) -> int:
        return len(self._poi_id_set)

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time == 0:
            return 0.0
        return time.time() - self._start_time

    # ------------------------------------------------------------------
    #  初始化
    # ------------------------------------------------------------------

    def _ensure_client(self) -> AmapClient:
        if self._client is None:
            self._client = AmapClient(
                api_key=self.config.api_key,
                qps=self.config.qps,
            )
            self._grid_generator = GridGenerator(amap_client=self._client)
        return self._client

    def _ensure_executor(self):
        if self._executor is None:
            import concurrent.futures
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        return self._executor

    # ------------------------------------------------------------------
    #  网格初始化
    # ------------------------------------------------------------------

    def init_grids(self, boundary: List[Tuple[float, float]], extra_rings: Optional[List[List[Tuple[float, float]]]] = None) -> int:
        """根据边界初始化网格任务队列。

        Args:
            boundary: 区域边界顶点列表。
            extra_rings: 额外的环（孔洞/飞地）。

        Returns:
            生成的网格数量。
        """
        self._ensure_client()
        assert self._grid_generator is not None
        tasks = self._grid_generator.generate(
            boundary=boundary,
            search_radius_km=self.config.search_radius_km,
            max_grids=self.config.max_grids,
            extra_rings=extra_rings or None,
        )
        self._grid_queue = GridQueue(tasks)
        self._log("info", f"网格初始化完成: {len(tasks)} 个六边形")
        return len(tasks)

    # ------------------------------------------------------------------
    #  预估
    # ------------------------------------------------------------------

    def estimate(self, keyword_count: int) -> Dict[str, Any]:
        """采集前预估统计。

        Args:
            keyword_count: 关键词数量。

        Returns:
            预估数据字典。
        """
        self._ensure_client()
        assert self._grid_generator is not None
        boundary = self.config.boundary
        if not boundary:
            raise RuntimeError("请先设置 boundary")
        result = self._grid_generator.preview_estimate(
            boundary=boundary,
            search_radius_km=self.config.search_radius_km,
            keyword_count=keyword_count,
            extra_rings=self.config.extra_rings or None,
        )
        self._emit_estimate(result)
        return result

    # ------------------------------------------------------------------
    #  主采集入口
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动异步采集任务。"""
        if self.is_running:
            self._log("warning", "引擎已在运行中")
            return

        self._ensure_client()
        self._ensure_executor()
        self._stop_event.clear()

        self.df = pd.DataFrame()
        self._poi_id_set.clear()
        self._total_api_calls = 0
        self._total_pois_collected = 0
        self._total_pois_enriched = 0
        self._start_time = time.time()

        self._main_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """采集主协程。"""
        try:
            self.state = EngineState.RUNNING
            self._log("info", "采集引擎启动")

            keywords = self.config.keywords
            if not keywords:
                self._log("error", "关键词列表为空")
                return

            for kw_index, keyword in enumerate(keywords):
                await self._check_pause_stop()
                if self._stop_event.is_set():
                    break

                self._keyword_index = kw_index
                self._current_keyword = keyword
                self._log("info", f"开始关键词 [{kw_index + 1}/{len(keywords)}]: {keyword}")

                await self._collect_keyword(keyword)
                self._log("info", f"关键词 [{keyword}] 采集完成")

            if not self._stop_event.is_set():
                await self._enrich_details()

            if not self._stop_event.is_set():
                self.state = EngineState.COMPLETED
                elapsed = time.time() - self._start_time
                summary = {
                    "total_pois": self.poi_count,
                    "total_api_calls": self._total_api_calls,
                    "total_enriched": self._total_pois_enriched,
                    "elapsed_seconds": int(elapsed),
                    "keywords": self.config.keywords,
                }
                self._emit_complete(summary)
                self._log("info",
                    f"采集完成: {self.poi_count} 条POI, "
                    f"API调用 {self._total_api_calls} 次, "
                    f"耗时 {int(elapsed)}s"
                )
            else:
                self.state = EngineState.STOPPED
                self._save_checkpoint_now()
                self._log("info", "采集已停止，检查点已保存")

        except Exception as e:
            self.state = EngineState.ERROR
            self._log("error", f"采集异常: {e}")
            logger.exception("采集引擎异常")
            self._save_checkpoint_now()

    async def _collect_keyword(self, keyword: str) -> None:
        """采集单个关键词下的所有网格。

        对每个网格执行多边形搜索，检测溢出，必要时切分后重新入队。
        """
        assert self._grid_queue is not None
        loop = asyncio.get_event_loop()

        while not self._grid_queue.is_empty and not self._stop_event.is_set():
            await self._check_pause_stop()
            if self._stop_event.is_set():
                break

            task = self._grid_queue.pop()
            if task is None:
                break

            if not self._should_process_grid(task, keyword):
                continue

            self._current_grid_hex = task.hex_id
            task.status = GridStatus.RUNNING
            self._emit_progress()

            self._log("info",
                f"[{keyword}] 网格 {task.hex_id[:12]}... "
                f"(res={task.resolution}, depth={task.split_depth})"
            )

            try:
                result = await loop.run_in_executor(
                    self._executor,
                    lambda: self._client.search_poi_polygon_all_pages(
                        key=self.config.api_key,
                        polygon_str=task.polygon_str,
                        keywords=keyword,
                        types=self.config.poi_types,
                    ),
                )
                self._total_api_calls += 1

                pois = result.pois
                task.poi_count = len(pois)

                self._log("info",
                    f"  → 返回 {len(pois)} 条"
                    + (f" (触发切分阈值)" if len(pois) >= self.config.split_threshold else "")
                )

                if len(pois) >= self.config.split_threshold and task.split_depth < self.config.max_split_depth:
                    task.status = GridStatus.NEED_SPLIT
                    sub_tasks = split_grid(task, method="h3_children")
                    self._grid_queue.add_split_tasks(sub_tasks)
                    self._log("info",
                        f"  → 切分为 {len(sub_tasks)} 个子网格 "
                        f"(depth={task.split_depth + 1})"
                    )
                else:
                    if task.split_depth >= self.config.max_split_depth:
                        self._log("warning", f"  → 已达最大递归深度，接受 {len(pois)} 条数据")

                    new_count = self._merge_pois(pois, keyword)
                    if new_count > 0:
                        self._total_pois_collected += new_count
                        self._emit_data(pois)

                    task.status = GridStatus.DONE
                    task.mark_keyword_done(keyword)
                    self._emit_progress()
                    self._log("info",
                        f"  → 新增 {new_count} 条 (累计 {self.poi_count} 条)"
                    )

            except AmapApiError as e:
                task.status = GridStatus.FAILED
                self._log("error", f"  → API错误: {e}")
                self._emit_progress()

            except Exception as e:
                task.status = GridStatus.FAILED
                self._log("error", f"  → 未知错误: {e}")
                logger.exception(f"网格 {task.hex_id} 采集异常")
                self._emit_progress()

            if self._total_api_calls % 10 == 0:
                self._save_checkpoint_now()

        self._save_checkpoint_now()

    def _should_process_grid(self, task: GridTask, keyword: str) -> bool:
        """判断是否应处理此网格。

        跳过条件：
          - 已完成（DONE）或失败（FAILED）
          - 所有关键词已完成（多关键词场景）
          - 当前关键词已在该网格上完成
        """
        if task.status in (GridStatus.DONE, GridStatus.FAILED, GridStatus.SKIPPED):
            return False
        if task.is_keyword_done(keyword):
            return False
        return True

    # ------------------------------------------------------------------
    #  POI 详情补充
    # ------------------------------------------------------------------

    async def _enrich_details(self) -> None:
        """补充所有缺失 rating 和 cost 的 POI 详情。

        通过 POI 详情接口逐条查询，控制请求间隔 0.2 秒/条。
        """
        if self.df.empty:
            self._log("info", "无 POI 数据需要补充详情")
            return

        missing = self.df[
            (self.df["rating"].isna() | (self.df["rating"] == ""))
            | (self.df["cost"].isna() | (self.df["cost"] == ""))
        ]
        poi_ids = missing["poi_id"].unique().tolist()

        if not poi_ids:
            self._log("info", "所有 POI 的 rating/cost 已完整")
            return

        self.state = EngineState.ENRICHING
        self._log("info", f"开始补充 POI 详情: {len(poi_ids)} 条")
        loop = asyncio.get_event_loop()

        enriched = 0
        for i, pid in enumerate(poi_ids):
            await self._check_pause_stop()
            if self._stop_event.is_set():
                break

            try:
                detail = await loop.run_in_executor(
                    self._executor,
                    lambda pid_=pid: self._client.get_poi_detail(
                        key=self.config.api_key,
                        poi_id=pid_,
                    ),
                )
                self._total_api_calls += 1

                if detail is not None:
                    idx = self.df.index[self.df["poi_id"] == pid]
                    if not idx.empty:
                        row_idx = idx[0]
                        if detail.rating:
                            self.df.at[row_idx, "rating"] = detail.rating
                        if detail.cost:
                            self.df.at[row_idx, "cost"] = detail.cost
                        enriched += 1

            except AmapApiError:
                pass

            if (i + 1) % 50 == 0:
                self._log("info", f"详情补充进度: {i + 1}/{len(poi_ids)}")

            await asyncio.sleep(self.config.detail_interval)

        self._total_pois_enriched = enriched
        self._log("info", f"详情补充完成: {enriched}/{len(poi_ids)} 条成功")

    # ------------------------------------------------------------------
    #  DataFrame 去重管理
    # ------------------------------------------------------------------

    FIELD_ORDER = [
        "poi_id", "name", "type", "typecode", "address",
        "location", "longitude", "latitude",
        "pname", "cityname", "adname",
        "tel", "opentime_today", "opentime_week", "tag",
        "rating", "cost", "parking_type",
        "business_area", "alias",
        "keywords", "search_region",
    ]

    def _merge_pois(self, pois: List[PoiBasic], keyword: str) -> int:
        """将新采集的 POI 合并到全局 DataFrame。

        按 poi_id 去重：已存在的跳过，不存在的追加。

        Args:
            pois: 新采集的 POI 列表。
            keyword: 当前搜索关键词（注入到每条 POI）。

        Returns:
            新增的 POI 数量。
        """
        if not pois:
            return 0

        for p in pois:
            p.keywords = keyword
            p.search_region = self.config.region_name

        new_dicts = []
        for p in pois:
            if p.poi_id and p.poi_id not in self._poi_id_set:
                self._poi_id_set.add(p.poi_id)
                new_dicts.append(p.to_dict())

        if not new_dicts:
            return 0

        new_df = pd.DataFrame(new_dicts)

        if self.df.empty:
            self.df = new_df
        else:
            self.df = pd.concat([self.df, new_df], ignore_index=True)

        return len(new_dicts)

    # ------------------------------------------------------------------
    #  暂停 / 继续 / 停止
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """暂停采集。调用后引擎将等待当前网格完成后再暂停。"""
        if self.state == EngineState.RUNNING:
            self._pause_event.clear()
            self.state = EngineState.PAUSED
            self._log("info", "采集已暂停")

    def resume(self) -> None:
        """继续采集。"""
        if self.state == EngineState.PAUSED:
            self._pause_event.set()
            self.state = EngineState.RUNNING
            self._log("info", "采集已继续")

    def stop(self) -> None:
        """停止采集。保存检查点后退出。"""
        if self.is_running or self.is_paused:
            self.state = EngineState.STOPPING
            self._log("info", "正在停止采集...")
            self._stop_event.set()
            self._pause_event.set()

    async def wait_for_completion(self) -> None:
        """等待采集任务完全结束。"""
        if self._main_task is not None:
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

    async def _check_pause_stop(self) -> None:
        """检查暂停/停止信号。"""
        await self._pause_event.wait()
        if self._stop_event.is_set():
            raise asyncio.CancelledError()

    # ------------------------------------------------------------------
    #  断点续传
    # ------------------------------------------------------------------

    def _checkpoint_path(self) -> str:
        os.makedirs(self.config.checkpoint_dir, exist_ok=True)
        adcode = self.config.adcode or "unknown"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self.config.checkpoint_dir, f"checkpoint_{adcode}_{ts}.json")

    def _save_checkpoint_now(self) -> None:
        """立即保存检查点。"""
        if self._grid_queue is None:
            return

        path = self._checkpoint_path()
        try:
            tasks = self._grid_queue.to_list()
            save_checkpoint(
                filepath=path,
                tasks=tasks,
                collected_poi_ids=self._poi_id_set,
                completed_keywords=self.config.keywords[:self._keyword_index],
                config_snapshot={
                    "adcode": self.config.adcode,
                    "region_name": self.config.region_name,
                    "keywords": self.config.keywords,
                    "search_radius_km": self.config.search_radius_km,
                    "keyword_index": self._keyword_index,
                    "qps": self.config.qps,
                    "split_threshold": self.config.split_threshold,
                },
            )
            self._emit_checkpoint(path)
        except Exception as e:
            self._log("error", f"保存检查点失败: {e}")

    def save_checkpoint_sync(self) -> str:
        """同步保存检查点（供 GUI 在非采集状态调用）。"""
        path = self._checkpoint_path()
        tasks = self._grid_queue.to_list() if self._grid_queue else []
        save_checkpoint(
            filepath=path,
            tasks=tasks,
            collected_poi_ids=self._poi_id_set,
            completed_keywords=[],
            config_snapshot=self.config.to_dict(),
        )
        return path

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """列出所有检查点文件。"""
        os.makedirs(self.config.checkpoint_dir, exist_ok=True)
        files = []
        for fname in sorted(os.listdir(self.config.checkpoint_dir)):
            if fname.startswith("checkpoint_") and fname.endswith(".json"):
                fpath = os.path.join(self.config.checkpoint_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    files.append({
                        "path": fpath,
                        "filename": fname,
                        "timestamp": data.get("timestamp", ""),
                        "grid_count": data.get("grid_count", 0),
                        "poi_count": data.get("collected_poi_count", 0),
                        "config": data.get("config_snapshot", {}),
                    })
                except Exception:
                    continue
        return files

    def restore_from_checkpoint(self, filepath: str) -> bool:
        """从检查点恢复状态。

        Args:
            filepath: 检查点 JSON 文件路径。

        Returns:
            是否恢复成功。
        """
        try:
            data = load_checkpoint(filepath)

            restored_grids = data.get("grids", [])
            restored_poi_ids = data.get("collected_poi_ids", set())
            config_snap = data.get("config_snapshot", {})

            self._grid_queue = GridQueue(restored_grids)
            self._poi_id_set = restored_poi_ids
            self._keyword_index = config_snap.get("keyword_index", 0)

            if config_snap.get("search_radius_km"):
                self.config.search_radius_km = config_snap["search_radius_km"]
            if config_snap.get("keywords"):
                self.config.keywords = config_snap["keywords"]

            self._log("info",
                f"已恢复检查点: {len(restored_grids)} 网格, "
                f"{len(restored_poi_ids)} 条POI"
            )
            return True

        except Exception as e:
            self._log("error", f"恢复检查点失败: {e}")
            return False

    # ------------------------------------------------------------------
    #  导出
    # ------------------------------------------------------------------

    def export_csv(self, filepath: str, include_empty: bool = False) -> int:
        """将 DataFrame 导出为 CSV（UTF-8 with BOM）。

        Args:
            filepath: 导出路径。
            include_empty: 是否包含空 DataFrame 的列头。

        Returns:
            导出的行数。
        """
        if self.df.empty:
            if not include_empty:
                self._log("warning", "无数据可导出")
                return 0
            empty_df = pd.DataFrame(columns=self.FIELD_ORDER)
            empty_df.to_csv(filepath, index=False, encoding="utf-8-sig")
            return 0

        export_df = self.df[self.FIELD_ORDER].copy()
        export_df = export_df.fillna("")

        export_df.to_csv(filepath, index=False, encoding="utf-8-sig")

        count = len(export_df)
        self._log("info", f"已导出 {count} 条数据到 {filepath}")
        return count

    def get_dataframe_snapshot(self) -> pd.DataFrame:
        """获取当前 DataFrame 的快照副本。"""
        return self.df.copy()

    # ------------------------------------------------------------------
    #  统计信息
    # ------------------------------------------------------------------

    def get_statistics(self) -> Dict[str, Any]:
        """获取当前的采集统计信息。"""
        return {
            "state": self.state.value,
            "progress": self.progress,
            "total_pois": self.poi_count,
            "total_api_calls": self._total_api_calls,
            "total_enriched": self._total_pois_enriched,
            "elapsed_seconds": int(self.elapsed_seconds),
            "current_keyword": self._current_keyword,
            "current_grid": self._current_grid_hex,
            "keyword_index": self._keyword_index,
            "total_keywords": len(self.config.keywords),
        }

    # ------------------------------------------------------------------
    #  回调触发
    # ------------------------------------------------------------------

    def _emit_data(self, pois: List[PoiBasic]) -> None:
        if self.cb.on_data:
            try:
                self.cb.on_data(pois)
            except Exception as e:
                logger.warning(f"on_data 回调异常: {e}")

    def _emit_progress(self) -> None:
        if self.cb.on_progress:
            try:
                info = self.get_statistics()
                self.cb.on_progress(self.progress, info)
            except Exception as e:
                logger.warning(f"on_progress 回调异常: {e}")

    def _emit_log(self, level: str, message: str) -> None:
        if self.cb.on_log:
            try:
                self.cb.on_log(level, message)
            except Exception as e:
                logger.warning(f"on_log 回调异常: {e}")

    def _emit_status(self, state: EngineState) -> None:
        if self.cb.on_status:
            try:
                self.cb.on_status(state)
            except Exception as e:
                logger.warning(f"on_status 回调异常: {e}")

    def _emit_estimate(self, estimate: dict) -> None:
        self._emit_log("info",
            f"预估: 网格={estimate.get('estimated_grid_count', '?')}, "
            f"API={estimate.get('estimated_api_calls', '?')}次, "
            f"POI≈{estimate.get('estimated_poi_count', '?')}条, "
            f"耗时≈{estimate.get('estimated_minutes', '?')}分钟"
        )
        if self.cb.on_estimate:
            try:
                self.cb.on_estimate(estimate)
            except Exception as e:
                logger.warning(f"on_estimate 回调异常: {e}")

    def _emit_checkpoint(self, path: str) -> None:
        if self.cb.on_checkpoint:
            try:
                self.cb.on_checkpoint(path)
            except Exception as e:
                logger.warning(f"on_checkpoint 回调异常: {e}")

    def _emit_complete(self, summary: dict) -> None:
        if self.cb.on_complete:
            try:
                self.cb.on_complete(summary)
            except Exception as e:
                logger.warning(f"on_complete 回调异常: {e}")

    def _log(self, level: str, message: str) -> None:
        getattr(logger, level, logger.info)(message)
        self._emit_log(level, message)

    # ------------------------------------------------------------------
    #  清理
    # ------------------------------------------------------------------

    def close(self) -> None:
        """释放所有资源。"""
        if self._client:
            self._client.close()
            self._client = None
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
        self._main_task = None
        self._log("info", "引擎已关闭")
