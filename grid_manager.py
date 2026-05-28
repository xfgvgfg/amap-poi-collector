"""
H3 六边形网格覆盖模块 (grid_manager.py)
========================================

提供基于 Uber H3 系统的六边形网格生成、分辨率选择、递归切分、
边界裁剪过滤以及序列化/反序列化能力，是 POI 采集引擎的底层网格管理模块。

核心功能：
  1. 根据用户设定的搜索半径自动选择最合适的 H3 分辨率
  2. 解析行政区划边界并生成全覆盖的六边形网格
  3. 使用 Shapely 进行网格与边界的精确相交过滤
  4. GridTask 数据类 + 网格状态管理
  5. 四叉树递归切分（解决高密度区域 POI 溢出）
  6. 序列化/反序列化支持断点续传
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import h3
import numpy as np
from shapely.geometry import Polygon, Point, box as shapely_box
from shapely import wkt

from amap_api import AmapClient, parse_polyline_to_coords


# ============================================================================
#  H3 分辨率自动换算
# ============================================================================

# H3 各分辨率的六边形外接圆半径（中心到顶点距离）
# 对于正六边形：外接圆半径 R = 边长 a
# 用户指定的"搜索半径"映射关系：R_search = a * sqrt(3)
# 因此 a = R_search / sqrt(3)，选择 a ≤ R_search / sqrt(3) 的最高分辨率
_RESOLUTION_TABLE: List[Tuple[int, float]] = [
    # (resolution, average_hexagon_edge_length_km)
    # 实际值通过 h3.average_hexagon_edge_length(r, unit='km') 获取
    (3, 68.979),
    (4, 26.072),
    (5, 9.854),
    (6, 3.725),
    (7, 1.406),
    (8, 0.531),
    (9, 0.201),
    (10, 0.076),
    (11, 0.029),
    (12, 0.011),
    (13, 0.004),
    (14, 0.002),
]


def select_resolution(search_radius_km: float) -> int:
    """根据用户设定的搜索半径自动选择最合适的 H3 分辨率。

    映射规则：
      - 用户搜索半径 R 对应六边形的"外接圆半径"
      - 正六边形中：R = 边长 a
      - 选择条件：a ≤ R / √3   (即六边形完全包含在以 R 为半径的圆内)
      - 在满足条件的分辨率中选择最高者（六边形最小，精度最高）

    Args:
        search_radius_km: 用户设定的搜索半径，单位公里，范围 1~50。

    Returns:
        最合适的 H3 分辨率 (3~14)。

    Raises:
        ValueError: 搜索半径超出支持范围。

    Examples:
        >>> select_resolution(3.0)
        7
        >>> select_resolution(10.0)
        5
    """
    if search_radius_km < 0.5:
        raise ValueError(f"搜索半径过小 ({search_radius_km} km)，最低支持 0.5 km")
    if search_radius_km > 100:
        raise ValueError(f"搜索半径过大 ({search_radius_km} km)，最高支持 100 km")

    max_edge = search_radius_km / math.sqrt(3)

    for res, edge_km in _RESOLUTION_TABLE:
        if edge_km <= max_edge:
            return res

    return _RESOLUTION_TABLE[-1][0]


def estimate_grid_count(search_radius_km: float, boundary_area_km2: float) -> int:
    """预估目标区域需要的网格数量。

    Args:
        search_radius_km: 搜索半径（公里）。
        boundary_area_km2: 目标区域面积（平方公里）。

    Returns:
        预估的网格数量。
    """
    res = select_resolution(search_radius_km)
    cell_area = _RESOLUTION_TABLE[res - 3][1] ** 2 * (3 * math.sqrt(3) / 2)
    estimated = int(boundary_area_km2 / cell_area * 1.2)  # 预留 20% 边界冗余
    return max(estimated, 1)


def resolution_to_edge_km(resolution: int) -> float:
    """获取指定 H3 分辨率的平均边长（公里）。

    Args:
        resolution: H3 分辨率 (3~14)。

    Returns:
        平均边长（公里）。
    """
    for res, edge in _RESOLUTION_TABLE:
        if res == resolution:
            return edge
    raise ValueError(f"不支持的分辨率: {resolution}")


def resolution_to_circumradius_km(resolution: int) -> float:
    """获取指定 H3 分辨率的六边形外接圆半径（中心到顶点距离，公里）。"""
    return resolution_to_edge_km(resolution)


# ============================================================================
#  GridTask 数据类与状态枚举
# ============================================================================


class GridStatus:
    """网格状态常量。"""
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    NEED_SPLIT = "need_split"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class GridTask:
    """单个六边形网格的采集任务单元。

    每个 GridTask 对应一个 H3 六边形网格，包含该网格的几何信息、
    采集状态和已完成的关键词集合。

    字段说明：
      - hex_id: H3 cell ID (如 "8731aa428ffffff")，全局唯一
      - resolution: 该网格的 H3 分辨率
      - center_lng / center_lat: 六边形中心坐标（GCJ-02）
      - vertices: 六边形顶点列表 [(lng, lat), ...]，用于高德 polygon 搜索
      - polygon_str: 顶点坐标串 "lng1,lat1|lng2,lat2|..."，直接用于 API 调用
      - status: 当前采集状态
      - keywords_done: 该网格已完成采集的关键词集合（多关键词场景下使用）
      - parent_id: 父网格 hex_id（若为切分产生的子网格）
      - split_depth: 递归切分深度（0 表示初始网格，≥1 表示切分子网格）
      - poi_count: 该网格最后一次搜索返回的 POI 数量
    """
    hex_id: str
    resolution: int
    center_lng: float = 0.0
    center_lat: float = 0.0
    vertices: List[Tuple[float, float]] = field(default_factory=list)
    polygon_str: str = ""
    status: str = GridStatus.PENDING
    keywords_done: Set[str] = field(default_factory=set)
    parent_id: Optional[str] = None
    split_depth: int = 0
    poi_count: int = 0

    def __post_init__(self) -> None:
        if not self.vertices:
            boundary = h3.cell_to_boundary(self.hex_id)
            # cell_to_boundary 返回 (lat, lng) 格式 → 转为 (lng, lat)
            self.vertices = [(lng, lat) for lat, lng in boundary]
        if not self.polygon_str:
            self._build_polygon_str()
        if not self.center_lng or not self.center_lat:
            lat, lng = h3.cell_to_latlng(self.hex_id)
            self.center_lat = lat
            self.center_lng = lng

    def _build_polygon_str(self) -> None:
        """将顶点坐标列表构建为高德多边形搜索 API 要求的字符串格式。

        格式: "lng1,lat1|lng2,lat2|...|lng1,lat1"（首尾闭合）
        """
        if not self.vertices:
            return
        parts = [f"{lng},{lat}" for lng, lat in self.vertices]
        parts.append(parts[0])
        self.polygon_str = "|".join(parts)

    def mark_keyword_done(self, keyword: str) -> None:
        """标记一个关键词在该网格上已完成采集。

        Args:
            keyword: 关键词字符串。
        """
        self.keywords_done.add(keyword)

    def is_keyword_done(self, keyword: str) -> bool:
        """检查指定关键词在该网格上是否已完成。

        Args:
            keyword: 关键词字符串。

        Returns:
            是否已完成。
        """
        return keyword in self.keywords_done

    def all_keywords_done(self, keywords: List[str]) -> bool:
        """检查所有关键词在该网格上是否均已完成。

        Args:
            keywords: 关键词列表。

        Returns:
            是否全部完成。
        """
        return all(kw in self.keywords_done for kw in keywords)

    def to_dict(self) -> dict:
        """将 GridTask 序列化为字典（用于 JSON 断点续传）。"""
        return {
            "hex_id": self.hex_id,
            "resolution": self.resolution,
            "center_lng": self.center_lng,
            "center_lat": self.center_lat,
            "vertices": [[lng, lat] for lng, lat in self.vertices],
            "polygon_str": self.polygon_str,
            "status": self.status,
            "keywords_done": list(self.keywords_done),
            "parent_id": self.parent_id,
            "split_depth": self.split_depth,
            "poi_count": self.poi_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> GridTask:
        """从字典恢复 GridTask。

        Args:
            data: 序列化的字典数据。

        Returns:
            恢复后的 GridTask 实例。
        """
        vertices = (
            [(lng, lat) for lng, lat in data["vertices"]]
            if "vertices" in data
            else []
        )
        return cls(
            hex_id=data["hex_id"],
            resolution=data["resolution"],
            center_lng=data.get("center_lng", 0.0),
            center_lat=data.get("center_lat", 0.0),
            vertices=vertices,
            polygon_str=data.get("polygon_str", ""),
            status=data.get("status", GridStatus.PENDING),
            keywords_done=set(data.get("keywords_done", [])),
            parent_id=data.get("parent_id"),
            split_depth=data.get("split_depth", 0),
            poi_count=data.get("poi_count", 0),
        )


# ============================================================================
#  网格生成器
# ============================================================================


def _find_main_ring(rings: List[List[Tuple[float, float]]]) -> Tuple[int, List[Tuple[float, float]]]:
    """从多个环中找到面积最大的主环。

    高德行政区划 API 返回的边界可能包含多个环。
    第一个环不一定是最大的（如朝阳区的第0环是一个小飞地）。
    此函数选择面积最大的环作为主边界。

    Args:
        rings: 坐标环列表。

    Returns:
        (index, main_ring) 元组。
    """
    if len(rings) == 1:
        return 0, rings[0]

    best_idx = 0
    best_area = -1.0
    for i, ring in enumerate(rings):
        if len(ring) < 3:
            continue
        lngs = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        width = max(lngs) - min(lngs)
        height = max(lats) - min(lats)
        area = width * height
        if area > best_area:
            best_area = area
            best_idx = i

    return best_idx, rings[best_idx]


class GridGenerator:
    """六边形网格生成器。

    负责根据行政区划边界和 H3 分辨率，生成覆盖目标区域的全部六边形网格，
    并使用 Shapely 进行边界相交过滤。
    """

    def __init__(self, amap_client: Optional[AmapClient] = None) -> None:
        """
        Args:
            amap_client: 高德 API 客户端实例。若为 None，
                则部分功能（如 get_boundary_from_adcode）不可用。
        """
        self._client = amap_client

    @property
    def client(self) -> Optional[AmapClient]:
        return self._client

    @client.setter
    def client(self, value: AmapClient) -> None:
        self._client = value

    # ------------------------------------------------------------------
    #  行政区划边界获取
    # ------------------------------------------------------------------

    def get_boundary_from_adcode(
        self,
        adcode: str,
        name: str = "",
    ) -> Tuple[List[Tuple[float, float]], float]:
        """根据行政区划代码从高德 API 获取区域边界多边形。

        使用行政区划 API 获取指定区域的边界坐标串 (polyline)，
        解析为可用于 H3 网格生成的闭合多边形。

        Args:
            adcode: 行政区划代码，如 "110105"（朝阳区）。
            name: 行政区名称（仅用于日志/错误提示）。

        Returns:
            (boundary_polygon, area_km2) 元组，其中：
              - boundary_polygon: 闭合多边形顶点列表 [(lng, lat), ...]
              - area_km2: 使用 Shapely 估算的区域面积（平方公里）

        Raises:
            RuntimeError: 未配置 AmapClient 或 API 调用失败。
            ValueError: 获取的边界数据为空。
        """
        if self._client is None:
            raise RuntimeError("未配置 AmapClient，无法获取行政区划边界")

        districts = self._client.get_region_list(
            keyword=adcode,
            subdistrict=0,
        )
        if not districts:
            raise ValueError(f"未找到行政区划: {name or adcode}")

        district = districts[0]
        polyline = district.polyline
        if not polyline:
            raise ValueError(f"行政区划 {district.name} 的边界数据为空")

        rings = parse_polyline_to_coords(polyline)
        if not rings:
            raise ValueError(f"无法解析行政区划 {district.name} 的边界坐标")

        main_idx, main_ring = _find_main_ring(rings)

        if len(rings) > 1:
            other_rings = [r for i, r in enumerate(rings) if i != main_idx]
            polygon = Polygon(main_ring, other_rings)
        else:
            polygon = Polygon(main_ring)

        area_km2 = polygon.area * 111.32 * 111.32 * abs(math.cos(math.radians(
            (max(p[1] for p in main_ring) + min(p[1] for p in main_ring)) / 2
        )))

        return main_ring, area_km2

    def get_boundary_from_polyline(
        self,
        polyline: str,
    ) -> Tuple[List[Tuple[float, float]], float]:
        """从高德 polyline 字符串解析边界多边形。

        Args:
            polyline: 高德 API 返回的 boundary polyline 字符串。

        Returns:
            (boundary_polygon, area_km2) 元组。
        """
        rings = parse_polyline_to_coords(polyline)
        if not rings:
            raise ValueError("polyline 解析结果为空")

        main_idx, main_ring = _find_main_ring(rings)

        if len(rings) > 1:
            other_rings = [r for i, r in enumerate(rings) if i != main_idx]
            polygon = Polygon(main_ring, other_rings)
        else:
            polygon = Polygon(main_ring)

        area_km2 = polygon.area * 111.32 * 111.32 * abs(math.cos(math.radians(
            (max(p[1] for p in main_ring) + min(p[1] for p in main_ring)) / 2
        )))

        return main_ring, area_km2

    # ------------------------------------------------------------------
    #  网格生成
    # ------------------------------------------------------------------

    def generate(
        self,
        boundary: List[Tuple[float, float]],
        search_radius_km: float,
        max_grids: int = 10000,
        extra_rings: Optional[List[List[Tuple[float, float]]]] = None,
    ) -> List[GridTask]:
        """生成覆盖指定边界区域的所有 H3 六边形网格任务。

        流程：
          1. 根据搜索半径自动选择 H3 分辨率
          2. 用 h3.polygon_to_cells 生成覆盖区域的全部六边形
          3. 用 Shapely 精确过滤与边界相交的网格
          4. 为每个网格构建 GridTask

        Args:
            boundary: 区域边界多边形顶点列表 [(lng, lat), ...]，
                首尾顶点需闭合。
            search_radius_km: 搜索半径（公里）。
            max_grids: 最大网格数量限制，防止内存爆炸。
            extra_rings: 额外的环（孔洞、飞地等），每个环为顶点列表。

        Returns:
            GridTask 列表。

        Raises:
            ValueError: 超出网格数量上限。
        """
        resolution = select_resolution(search_radius_km)
        all_rings = [boundary] + (extra_rings or [])
        return self._generate_at_resolution(all_rings, resolution, max_grids)

    def _generate_at_resolution(
        self,
        rings: List[List[Tuple[float, float]]],
        resolution: int,
        max_grids: int = 10000,
    ) -> List[GridTask]:
        """在指定 H3 分辨率下生成网格。

        Args:
            rings: 多边形环列表，rings[0] 为外环，rings[1:] 为内环/飞地。
            resolution: H3 分辨率。
            max_grids: 最大网格数量。

        Returns:
            GridTask 列表。
        """
        if len(rings) == 0:
            raise ValueError("rings 不能为空")

        main_idx, outer_ring = _find_main_ring(rings)
        inner_rings = [r for i, r in enumerate(rings) if i != main_idx]

        coords = [[[lng, lat] for lng, lat in outer_ring]]
        for ir in inner_rings:
            coords.append([[lng, lat] for lng, lat in ir])

        h3shape = h3.geo_to_h3shape({
            "type": "Polygon",
            "coordinates": coords,
        })
        cell_ids = list(h3.polygon_to_cells(h3shape, resolution))

        if not cell_ids:
            raise ValueError("目标区域内未生成任何 H3 网格，请检查边界坐标是否正确")

        if len(cell_ids) > max_grids:
            raise ValueError(
                f"网格数量 ({len(cell_ids)}) 超出上限 ({max_grids})，"
                f"请增大搜索半径或缩小目标区域"
            )

        if inner_rings:
            shapely_polygon = Polygon(outer_ring, inner_rings)
        else:
            shapely_polygon = Polygon(outer_ring)

        tasks: List[GridTask] = []
        for hex_id in cell_ids:
            boundary_verts = h3.cell_to_boundary(hex_id)
            vertices_lnglat = [(lng, lat) for lat, lng in boundary_verts]
            hex_polygon = Polygon(vertices_lnglat)

            if not shapely_polygon.intersects(hex_polygon):
                continue

            lat, lng = h3.cell_to_latlng(hex_id)

            task = GridTask(
                hex_id=hex_id,
                resolution=resolution,
                center_lng=lng,
                center_lat=lat,
                vertices=vertices_lnglat,
                split_depth=0,
            )
            tasks.append(task)

        return tasks

    def preview_estimate(
        self,
        boundary: List[Tuple[float, float]],
        search_radius_km: float,
        keyword_count: int = 1,
        extra_rings: Optional[List[List[Tuple[float, float]]]] = None,
    ) -> dict:
        """采集前预览预估信息（网格数、API 调用次数、预估耗时、数据量）。

        Args:
            boundary: 区域边界多边形。
            search_radius_km: 搜索半径。
            keyword_count: 关键词数量。
            extra_rings: 额外的环。

        Returns:
            包含预估数据的字典。
        """
        resolution = select_resolution(search_radius_km)
        edge_km = resolution_to_edge_km(resolution)
        cell_area_km2 = edge_km ** 2 * (3 * math.sqrt(3) / 2)

        rings = [boundary] + (extra_rings or [])
        main_idx, outer_ring = _find_main_ring(rings)
        other_rings = [r for i, r in enumerate(rings) if i != main_idx]
        coords = [[[lng, lat] for lng, lat in outer_ring]]
        for ir in other_rings:
            coords.append([[lng, lat] for lng, lat in ir])

        h3shape = h3.geo_to_h3shape({
            "type": "Polygon",
            "coordinates": coords,
        })
        cells = list(h3.polygon_to_cells(h3shape, resolution))
        grid_count = len(cells)

        if other_rings:
            shapely_polygon = Polygon(outer_ring, other_rings)
        else:
            shapely_polygon = Polygon(outer_ring)
        boundary_area = shapely_polygon.area * 111.32 * 111.32 * abs(
            math.cos(math.radians(
                (max(p[1] for p in boundary) + min(p[1] for p in boundary)) / 2
            ))
        )

        estimated_grids = max(int(grid_count * 1.15), grid_count)
        api_calls_per_grid = 2
        total_api_calls = estimated_grids * keyword_count * api_calls_per_grid
        detail_api_calls_ratio = 0.3
        detail_calls = int(estimated_grids * keyword_count * 25 * detail_api_calls_ratio)
        total_api_calls += detail_calls

        estimated_pois = int(estimated_grids * keyword_count * 25)
        qps = 5
        estimated_seconds = (total_api_calls / qps) * 1.3

        return {
            "resolution": resolution,
            "edge_km": round(edge_km, 4),
            "cell_area_km2": round(cell_area_km2, 4),
            "initial_grid_count": grid_count,
            "estimated_grid_count": estimated_grids,
            "boundary_area_km2": round(boundary_area, 2),
            "keyword_count": keyword_count,
            "estimated_api_calls": total_api_calls,
            "estimated_detail_calls": detail_calls,
            "estimated_poi_count": estimated_pois,
            "estimated_seconds": int(estimated_seconds),
            "estimated_minutes": int(estimated_seconds / 60),
        }


# ============================================================================
#  递归切分
# ============================================================================


def _hexagon_bounding_box(
    vertices: List[Tuple[float, float]],
) -> Tuple[float, float, float, float]:
    """计算六边形的最小外接矩形。

    Args:
        vertices: 六边形顶点列表 [(lng, lat), ...]。

    Returns:
        (min_lng, min_lat, max_lng, max_lat)。
    """
    lngs = [v[0] for v in vertices]
    lats = [v[1] for v in vertices]
    return min(lngs), min(lats), max(lngs), max(lats)


def split_grid_quadtree(
    task: GridTask,
    new_resolution: int,
) -> List[GridTask]:
    """对网格进行四叉树切分，生成 4 个子多边形网格。

    切分策略：
      将六边形的外接矩形四等分，生成 4 个矩形子区域，
      然后为每个子区域使用 H3 polygon_to_cells 生成覆盖六边形。

    使用场景：
      当某个网格 POI 数量超过高德 API 上限（约 800~900 条）时，
      需要将其切分为子网格重新采集。

    Args:
        task: 需要切分的父网格任务。
        new_resolution: 子网格使用的 H3 分辨率（通常为父级 + 1）。

    Returns:
        子 GridTask 列表。
    """
    min_lng, min_lat, max_lng, max_lat = _hexagon_bounding_box(task.vertices)
    mid_lng = (min_lng + max_lng) / 2
    mid_lat = (min_lat + max_lat) / 2

    quadrants = [
        ([
            (min_lng, min_lat),
            (mid_lng, min_lat),
            (mid_lng, mid_lat),
            (min_lng, mid_lat),
            (min_lng, min_lat),
        ], "sw"),
        ([
            (mid_lng, min_lat),
            (max_lng, min_lat),
            (max_lng, mid_lat),
            (mid_lng, mid_lat),
            (mid_lng, min_lat),
        ], "se"),
        ([
            (min_lng, mid_lat),
            (mid_lng, mid_lat),
            (mid_lng, max_lat),
            (min_lng, max_lat),
            (min_lng, mid_lat),
        ], "nw"),
        ([
            (mid_lng, mid_lat),
            (max_lng, mid_lat),
            (max_lng, max_lat),
            (mid_lng, max_lat),
            (mid_lng, mid_lat),
        ], "ne"),
    ]

    # 父六边形的 Shapely 多边形，用于过滤与父区域无交集的子网格
    parent_polygon = Polygon(task.vertices)

    sub_tasks: List[GridTask] = []
    for quad_polygon, direction in quadrants:
        h3shape = h3.geo_to_h3shape({
            "type": "Polygon",
            "coordinates": [[[lng, lat] for lng, lat in quad_polygon]],
        })
        cell_ids = list(h3.polygon_to_cells(h3shape, new_resolution))

        for hex_id in cell_ids:
            boundary_verts = h3.cell_to_boundary(hex_id)
            vertices_lnglat = [(lng, lat) for lat, lng in boundary_verts]
            hex_poly = Polygon(vertices_lnglat)

            if not parent_polygon.intersects(hex_poly):
                continue

            lat, lng = h3.cell_to_latlng(hex_id)

            sub_task = GridTask(
                hex_id=hex_id,
                resolution=new_resolution,
                center_lng=lng,
                center_lat=lat,
                vertices=vertices_lnglat,
                status=GridStatus.PENDING,
                parent_id=task.hex_id,
                split_depth=task.split_depth + 1,
            )
            sub_tasks.append(sub_task)

    return sub_tasks


def split_grid_h3_children(
    task: GridTask,
    max_children: int = 15,
) -> List[GridTask]:
    """使用 H3 原生 cell_to_children 进行切分。

    将当前网格的 H3 分辨率提高 1 级，生成子六边形网格。
    这是最高效的切分方式，因为 H3 的分层结构天然支持父-子网格。

    Args:
        task: 需要切分的父网格任务。
        max_children: 最大子网格数量。H3 res+1 通常产生约 7 个子网格。

    Returns:
        子 GridTask 列表。
    """
    child_res = task.resolution + 1
    cell_ids = list(h3.cell_to_children(task.hex_id))

    if len(cell_ids) > max_children:
        cell_ids = cell_ids[:max_children]

    sub_tasks: List[GridTask] = []
    for hex_id in cell_ids:
        boundary_verts = h3.cell_to_boundary(hex_id)
        vertices_lnglat = [(lng, lat) for lat, lng in boundary_verts]
        lat, lng = h3.cell_to_latlng(hex_id)

        sub_task = GridTask(
            hex_id=hex_id,
            resolution=child_res,
            center_lng=lng,
            center_lat=lat,
            vertices=vertices_lnglat,
            status=GridStatus.PENDING,
            parent_id=task.hex_id,
            split_depth=task.split_depth + 1,
        )
        sub_tasks.append(sub_task)

    return sub_tasks


def split_grid(
    task: GridTask,
    method: str = "h3_children",
) -> List[GridTask]:
    """对需要切分的网格执行递归切分。

    这是外部统一的切分入口，支持两种切分策略：
      - "h3_children": H3 原生子网格切分（推荐，效率高）
      - "quadtree": 四叉树几何切分（更均匀的 4 分区）

    Args:
        task: 需要切分的网格任务。
        method: 切分方法，"h3_children" 或 "quadtree"。

    Returns:
        切分后的子 GridTask 列表。

    Raises:
        ValueError: 不支持的切分方法或已达到最大递归深度。
    """
    MAX_SPLIT_DEPTH = 3
    MAX_RESOLUTION = 14

    if task.split_depth >= MAX_SPLIT_DEPTH:
        return []

    new_resolution = task.resolution + 1
    if new_resolution > MAX_RESOLUTION:
        return []

    if method == "h3_children":
        return split_grid_h3_children(task)
    elif method == "quadtree":
        return split_grid_quadtree(task, new_resolution)
    else:
        raise ValueError(f"不支持的切分方法: {method}")


# ============================================================================
#  序列化与反序列化（断点续传）
# ============================================================================


def serialize_grids(tasks: List[GridTask], filepath: str = "") -> dict:
    """将 GridTask 列表序列化为可 JSON 序列化的字典。

    Args:
        tasks: GridTask 列表。
        filepath: 若提供，同时写入文件。

    Returns:
        包含所有网格信息的字典。
    """
    result = {
        "version": "1.0",
        "timestamp": datetime.now().isoformat(),
        "grid_count": len(tasks),
        "grids": [t.to_dict() for t in tasks],
    }

    if filepath:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def deserialize_grids(data: dict) -> List[GridTask]:
    """从字典恢复 GridTask 列表。

    Args:
        data: serialize_grids 返回的字典。

    Returns:
        恢复后的 GridTask 列表。
    """
    return [GridTask.from_dict(g) for g in data.get("grids", [])]


def load_grids_from_file(filepath: str) -> List[GridTask]:
    """从 JSON 文件加载 GridTask 列表。

    Args:
        filepath: JSON 文件路径。

    Returns:
        恢复后的 GridTask 列表。
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    return deserialize_grids(data)


def build_checkpoint(
    tasks: List[GridTask],
    collected_poi_ids: Set[str],
    completed_keywords: List[str],
    config_snapshot: dict,
) -> dict:
    """构建完整的断点续传检查点数据。

    Args:
        tasks: 当前所有网格任务（含状态）。
        collected_poi_ids: 已采集的 POI ID 集合。
        completed_keywords: 已完成的关键词列表。
        config_snapshot: 任务配置快照（adcode, keywords, radius 等）。

    Returns:
        完整的检查点字典，可直接写入 JSON 文件。
    """
    return {
        "version": "1.0",
        "timestamp": datetime.now().isoformat(),
        "config_snapshot": config_snapshot,
        "completed_keywords": completed_keywords,
        "grid_count": len(tasks),
        "collected_poi_count": len(collected_poi_ids),
        "collected_poi_ids": list(collected_poi_ids),
        "grids": [t.to_dict() for t in tasks],
    }


def save_checkpoint(
    filepath: str,
    tasks: List[GridTask],
    collected_poi_ids: Set[str],
    completed_keywords: List[str],
    config_snapshot: dict,
) -> None:
    """将断点续传数据保存到文件。

    Args:
        filepath: 保存路径。
        tasks: 网格任务列表。
        collected_poi_ids: 已采集 POI ID 集合。
        completed_keywords: 已完成关键词。
        config_snapshot: 配置快照。
    """
    data = build_checkpoint(
        tasks, collected_poi_ids, completed_keywords, config_snapshot,
    )
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_checkpoint(filepath: str) -> dict:
    """从文件加载断点续传数据。

    Args:
        filepath: 检查点文件路径。

    Returns:
        包含 grids, collected_poi_ids, config_snapshot 的字典。
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        "grids": deserialize_grids(data),
        "collected_poi_ids": set(data.get("collected_poi_ids", [])),
        "completed_keywords": data.get("completed_keywords", []),
        "config_snapshot": data.get("config_snapshot", {}),
        "timestamp": data.get("timestamp", ""),
    }


# ============================================================================
#  网格队列管理器
# ============================================================================


class GridQueue:
    """网格任务队列管理器。

    管理所有网格任务的增删改查、状态更新和进度统计，
    支持多关键词场景下的网格任务复用。
    """

    def __init__(self, tasks: Optional[List[GridTask]] = None) -> None:
        """
        Args:
            tasks: 初始网格任务列表。
        """
        self._tasks: Dict[str, GridTask] = {}
        self._queue: List[str] = []

        if tasks:
            for t in tasks:
                self._tasks[t.hex_id] = t
                if t.status == GridStatus.PENDING:
                    self._queue.append(t.hex_id)

    # ------------------------------------------------------------------
    #  基本操作
    # ------------------------------------------------------------------

    def add(self, task: GridTask) -> None:
        """添加一个网格任务。

        Args:
            task: 网格任务。
        """
        self._tasks[task.hex_id] = task
        if task.status == GridStatus.PENDING:
            self._queue.append(task.hex_id)

    def add_all(self, tasks: List[GridTask]) -> None:
        """批量添加网格任务。

        Args:
            tasks: 网格任务列表。
        """
        for t in tasks:
            self.add(t)

    def pop(self) -> Optional[GridTask]:
        """从队列头部取出一个待处理的网格任务。

        Returns:
            GridTask 或 None（队列为空时）。
        """
        while self._queue:
            hex_id = self._queue.pop(0)
            task = self._tasks.get(hex_id)
            if task and task.status == GridStatus.PENDING:
                return task

        return None

    def get(self, hex_id: str) -> Optional[GridTask]:
        """根据 hex_id 获取网格任务。

        Args:
            hex_id: H3 cell ID。

        Returns:
            GridTask 或 None。
        """
        return self._tasks.get(hex_id)

    def update_status(self, hex_id: str, status: str) -> None:
        """更新网格状态。

        Args:
            hex_id: H3 cell ID。
            status: 新状态。
        """
        task = self._tasks.get(hex_id)
        if task:
            task.status = status

    def mark_keyword_done(self, hex_id: str, keyword: str) -> None:
        """标记某关键词在该网格上完成。

        Args:
            hex_id: H3 cell ID。
            keyword: 关键词。
        """
        task = self._tasks.get(hex_id)
        if task:
            task.mark_keyword_done(keyword)

    def add_split_tasks(self, sub_tasks: List[GridTask]) -> None:
        """将切分后的子网格任务加入队列头部（深度优先）。

        Args:
            sub_tasks: 子网格任务列表。
        """
        for t in sub_tasks:
            self._tasks[t.hex_id] = t

        self._queue = [t.hex_id for t in sub_tasks] + self._queue

    # ------------------------------------------------------------------
    #  状态查询
    # ------------------------------------------------------------------

    @property
    def total_count(self) -> int:
        """网格总数（含已完成的和切分产生的子网格）。"""
        return len(self._tasks)

    @property
    def pending_count(self) -> int:
        """待处理的网格数量。"""
        return sum(1 for t in self._tasks.values() if t.status == GridStatus.PENDING)

    @property
    def done_count(self) -> int:
        """已完成的网格数量。"""
        return sum(1 for t in self._tasks.values() if t.status == GridStatus.DONE)

    @property
    def failed_count(self) -> int:
        """失败的网格数量。"""
        return sum(1 for t in self._tasks.values() if t.status == GridStatus.FAILED)

    @property
    def is_empty(self) -> bool:
        """队列是否为空（全部处理完成）。"""
        return self.pending_count == 0

    @property
    def progress(self) -> float:
        """采集进度 (0.0 ~ 1.0)。

        计算方式：完成的网格数 / 总网格数（含切分产生的）。
        """
        if self.total_count == 0:
            return 0.0
        return self.done_count / self.total_count

    # ------------------------------------------------------------------
    #  关键词管理
    # ------------------------------------------------------------------

    def get_pending_for_keyword(self, keyword: str) -> List[GridTask]:
        """获取指定关键词下待处理的网格列表。

        在需要按关键词逐个处理的场景下使用。

        Args:
            keyword: 关键词。

        Returns:
            该关键词尚未完成的待处理网格列表。
        """
        pending: List[GridTask] = []
        for task in self._tasks.values():
            if task.status == GridStatus.PENDING and not task.is_keyword_done(keyword):
                pending.append(task)
        return pending

    def all_keywords_done_for_all(self, keywords: List[str]) -> bool:
        """检查所有关键词在所有网格上是否均已完成。

        Args:
            keywords: 关键词列表。

        Returns:
            是否全部完成。
        """
        for task in self._tasks.values():
            if task.status == GridStatus.DONE:
                continue
            if not task.all_keywords_done(keywords):
                return False
        return True

    # ------------------------------------------------------------------
    #  序列化
    # ------------------------------------------------------------------

    def to_list(self) -> List[GridTask]:
        """导出所有网格任务（按队列顺序）。

        Returns:
            GridTask 列表。
        """
        ordered = []
        seen = set()
        for hex_id in self._queue:
            if hex_id not in seen:
                seen.add(hex_id)
                if hex_id in self._tasks:
                    ordered.append(self._tasks[hex_id])
        for hex_id, task in self._tasks.items():
            if hex_id not in seen:
                ordered.append(task)
        return ordered

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "grid_count": self.total_count,
            "pending": self.pending_count,
            "done": self.done_count,
            "progress": self.progress,
            "tasks": serialize_grids(self.to_list()),
        }
