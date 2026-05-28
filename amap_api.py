"""
高德地图 API 封装模块 (amap_api.py)
====================================

提供对高德地图 Web 服务 API 的统一封装，包含：
  - 行政区划查询
  - POI 多边形搜索
  - POI 周边搜索
  - POI 详情查询

所有异常统一包装为自定义异常体系，支持请求间隔控制与速率限制。
返回值使用数据类 (dataclass) 封装，便于后续类型推导与 IDE 智能提示。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Dict, List, Optional, Sequence

import requests


# ============================================================================
#  自定义异常体系
# ============================================================================


class AmapApiError(Exception):
    """高德 API 调用的基类异常，所有自定义 API 异常均继承自此类。"""

    def __init__(self, message: str, status_code: Optional[str] = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class NetworkError(AmapApiError):
    """网络请求失败（超时 / 连接错误 / DNS 解析失败）。"""

    def __init__(self, message: str = "网络请求失败，请检查网络连接") -> None:
        super().__init__(message)


class APIKeyError(AmapApiError):
    """API Key 无效或未授权。"""

    def __init__(self, message: str = "API Key 无效或未授权，请检查密钥是否正确") -> None:
        super().__init__(message, status_code="10001")


class QuotaExceeded(AmapApiError):
    """当日配额超出或账户余额不足。"""

    def __init__(self, message: str = "当日 API 配额已用尽或账户余额不足") -> None:
        super().__init__(message, status_code="20800")


class QPSError(AmapApiError):
    """请求频率超限（超出 QPS 限制），触发时间：10003。"""

    def __init__(self, message: str = "请求频率超限，请降低并发或增大请求间隔") -> None:
        super().__init__(message, status_code="10003")


class InvalidParameters(AmapApiError):
    """请求参数错误（缺少必填参数 / 参数格式非法）。"""

    def __init__(self, message: str = "请求参数错误，请检查输入参数") -> None:
        super().__init__(message, status_code="20000")


class NoDataError(AmapApiError):
    """查询结果为空（高德返回空数据或查询区域无 POI）。"""

    def __init__(self, message: str = "查询结果为空") -> None:
        super().__init__(message)


class ServerError(AmapApiError):
    """高德服务端异常（5xx 或非预期响应）。"""

    def __init__(self, message: str = "高德服务端异常，请稍后重试") -> None:
        super().__init__(message)


# 状态码 -> 异常类的映射表
_STATUS_CODE_MAP: Dict[str, type[AmapApiError]] = {
    "10001": APIKeyError,
    "10003": QPSError,
    "10004": APIKeyError,
    "20000": InvalidParameters,
    "20001": InvalidParameters,
    "20002": InvalidParameters,
    "20003": InvalidParameters,
    "20800": QuotaExceeded,
    "22000": InvalidParameters,
}


def _raise_by_status(info: dict) -> None:
    """根据高德 API 返回的 JSON 中的 status / infocode 字段抛出对应异常。"""
    status = info.get("status", "0")
    infocode = info.get("infocode", "")
    info_str = info.get("info", "")

    if status == "1":
        return

    if infocode in _STATUS_CODE_MAP:
        raise _STATUS_CODE_MAP[infocode](f"[{infocode}] {info_str}")
    if info_str == "INVALID_USER_KEY":
        raise APIKeyError(f"[{infocode}] {info_str}")
    raise AmapApiError(f"未知错误 [{infocode}]: {info_str}", status_code=infocode)


# ============================================================================
#  数据类定义
# ============================================================================


@dataclass
class PoiBasic:
    """POI 基础信息（从多边形 / 周边搜索返回的字段抽取）。"""

    poi_id: str = ""
    name: str = ""
    type: str = ""  # 大类，如"餐饮服务"
    typecode: str = ""  # 细分类代码
    address: str = ""
    location: str = ""  # "lng,lat" 原始字符串
    longitude: float = 0.0
    latitude: float = 0.0
    pname: str = ""  # 省份名
    cityname: str = ""  # 城市名
    adname: str = ""  # 区县名
    tel: str = ""
    opentime_today: str = ""  # 当日营业时间
    opentime_week: str = ""  # 周营业时间
    tag: str = ""
    rating: str = ""  # 评分（搜索接口可能没有，需要详情补充）
    cost: str = ""  # 人均消费
    parking_type: str = ""  # 停车类型 0=未知 1=有 2=无
    business_area: str = ""  # 商圈
    alias: str = ""  # 别名

    # 以下字段由外部注入，非高德原始返回
    keywords: str = ""  # 搜索时使用的关键词
    search_region: str = ""  # 搜索时选中的行政区

    @classmethod
    def from_amap_json(cls, item: dict) -> PoiBasic:
        """从高德 POI 搜索 API 返回的单个 JSON 对象构造 PoiBasic。"""
        location_raw = item.get("location", "")
        lng, lat = 0.0, 0.0
        if location_raw:
            parts = location_raw.split(",")
            if len(parts) == 2:
                try:
                    lng, lat = float(parts[0]), float(parts[1])
                except ValueError:
                    pass

        opentime = item.get("opentime", "")
        opentime_week_val = item.get("opentime_week", "")

        return cls(
            poi_id=item.get("id", ""),
            name=item.get("name", ""),
            type=item.get("type", ""),
            typecode=item.get("typecode", ""),
            address=item.get("address", ""),
            location=location_raw,
            longitude=lng,
            latitude=lat,
            pname=item.get("pname", ""),
            cityname=item.get("cityname", ""),
            adname=item.get("adname", ""),
            tel=item.get("tel", ""),
            opentime_today=opentime if opentime else "",
            opentime_week=opentime_week_val if opentime_week_val else "",
            tag=item.get("tag", ""),
            rating=item.get("rating", ""),
            cost=item.get("cost", ""),
            parking_type=item.get("parking_type", ""),
            business_area=item.get("business_area", ""),
            alias=item.get("alias", ""),
        )

    def to_dict(self) -> dict:
        """转为字典，用于后续构建 DataFrame 或序列化。"""
        return {
            "poi_id": self.poi_id,
            "name": self.name,
            "type": self.type,
            "typecode": self.typecode,
            "address": self.address,
            "location": self.location,
            "longitude": self.longitude,
            "latitude": self.latitude,
            "pname": self.pname,
            "cityname": self.cityname,
            "adname": self.adname,
            "tel": self.tel,
            "opentime_today": self.opentime_today,
            "opentime_week": self.opentime_week,
            "tag": self.tag,
            "rating": self.rating,
            "cost": self.cost,
            "parking_type": self.parking_type,
            "business_area": self.business_area,
            "alias": self.alias,
            "keywords": self.keywords,
            "search_region": self.search_region,
        }


@dataclass
class PoiDetail:
    """POI 详情（通过 get_poi_detail 接口获取的详细数据）。

    此接口返回的数据包含 cost（人均消费）和 rating（评分）等字段，
    而这些字段在基础搜索接口中通常为空或不可用。
    """

    poi_id: str = ""
    cost: str = ""  # 人均消费
    rating: str = ""  # 评分
    name: str = ""
    type: str = ""
    typecode: str = ""
    address: str = ""
    location: str = ""
    pname: str = ""
    cityname: str = ""
    adname: str = ""
    tel: str = ""
    tag: str = ""
    parking_type: str = ""
    business_area: str = ""
    alias: str = ""
    website: str = ""  # 官网
    photos: list = field(default_factory=list)  # 照片列表

    @classmethod
    def from_amap_json(cls, item: dict) -> PoiDetail:
        """从高德 POI 详情 API 返回的 JSON 构造 PoiDetail。"""
        return cls(
            poi_id=item.get("id", ""),
            cost=item.get("cost", ""),
            rating=item.get("rating", ""),
            name=item.get("name", ""),
            type=item.get("type", ""),
            typecode=item.get("typecode", ""),
            address=item.get("address", ""),
            location=item.get("location", ""),
            pname=item.get("pname", ""),
            cityname=item.get("cityname", ""),
            adname=item.get("adname", ""),
            tel=item.get("tel", ""),
            tag=item.get("tag", ""),
            parking_type=item.get("parking_type", ""),
            business_area=item.get("business_area", ""),
            alias=item.get("alias", ""),
            website=item.get("website", ""),
            photos=item.get("photos", []),
        )


@dataclass
class DistrictInfo:
    """行政区划信息（从 get_region_list 返回）。"""

    adcode: str = ""  # 行政区代码
    name: str = ""  # 名称
    level: str = ""  # 级别：province / city / district / street
    center: str = ""  # "lng,lat"
    polyline: str = ""  # 边界坐标串（多段线，多个闭合环以 | 分隔）
    citycode: List[str] = field(default_factory=list)  # 城市编码

    @classmethod
    def from_amap_json(cls, item: dict) -> DistrictInfo:
        return cls(
            adcode=item.get("adcode", ""),
            name=item.get("name", ""),
            level=item.get("level", ""),
            center=item.get("center", ""),
            polyline=item.get("polyline", ""),
            citycode=(
                item.get("citycode", [])
                if isinstance(item.get("citycode"), list)
                else [item.get("citycode", "")]
            ),
        )


@dataclass
class SearchResult:
    """搜索返回的统一包装结果。"""

    pois: List[PoiBasic] = field(default_factory=list)
    total_count: int = 0  # 高德 API 返回的总条数（不一定准确，仅供参考）
    page: int = 1
    page_size: int = 25
    keyword: str = ""
    is_complete: bool = True  # 当前页是否完整返回


# ============================================================================
#  请求间隔控制器
# ============================================================================


class RateLimiter:
    """请求频率控制器，用于控制连续请求之间的最小间隔。

    高德个人开发者 Key 的 QPS 限制通常为 5（即每秒 5 次请求），
    企业开发者 Key 通常为 50。

    使用方法：
        limiter = RateLimiter(qps=5)
        limiter.wait()  # 在每次请求前调用
    """

    def __init__(self, qps: int = 5) -> None:
        """
        Args:
            qps: 每秒允许的最大请求数（Queries Per Second）。默认 5。
        """
        if qps <= 0:
            raise ValueError("qps 必须大于 0")
        self.interval: float = 1.0 / qps
        self._last_request_time: float = 0.0

    def wait(self) -> None:
        """阻塞当前线程，直到满足 QPS 限制的时间间隔。"""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self.interval:
            sleep_time = self.interval - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.monotonic()

    @property
    def qps(self) -> int:
        return int(1.0 / self.interval) if self.interval > 0 else 0

    @qps.setter
    def qps(self, value: int) -> None:
        if value <= 0:
            raise ValueError("qps 必须大于 0")
        self.interval = 1.0 / value


# ============================================================================
#  高德 API 客户端
# ============================================================================


class AmapClient:
    """高德地图 Web 服务 API 的统一客户端。

    提供对以下接口的封装：
      - 行政区划查询：``get_region_list()``
      - POI 多边形搜索：``get_poi_polygon()``
      - POI 周边搜索：``get_poi_around()``
      - POI 详情查询：``get_poi_detail()``

    使用示例：:

        client = AmapClient(api_key="your_key", qps=5)
        districts = client.get_region_list("北京市")
        result = client.get_poi_polygon(
            polygon_str="116.3,39.9|116.5,39.9|116.5,40.0|116.3,40.0",
            keywords="餐饮"
        )
    """

    # 高德 API 基础地址
    BASE_URL = "https://restapi.amap.com/v3"

    def __init__(
        self,
        api_key: str = "",
        qps: int = 5,
        timeout: int = 15,
        max_retries: int = 3,
    ) -> None:
        """
        Args:
            api_key: 高德 Web 服务 API Key。
            qps: 每秒允许的最大请求数。个人 Key 建议 5，企业 Key 可调至 50。
            timeout: 单次 HTTP 请求超时时间（秒）。
            max_retries: 网络异常时的最大重试次数（不包括业务逻辑错误）。
        """
        self._api_key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = requests.Session()
        self._limiter = RateLimiter(qps=qps)

    # ------------------------------------------------------------------
    #  属性
    # ------------------------------------------------------------------

    @property
    def api_key(self) -> str:
        return self._api_key

    @api_key.setter
    def api_key(self, value: str) -> None:
        self._api_key = value

    @property
    def qps(self) -> int:
        return self._limiter.qps

    @qps.setter
    def qps(self, value: int) -> None:
        self._limiter.qps = value

    # ------------------------------------------------------------------
    #  底层请求方法
    # ------------------------------------------------------------------

    def _request(self, endpoint: str, params: dict) -> dict:
        """发送 HTTP GET 请求到高德 API，处理频率控制、重试和异常。"""
        url = f"{self.BASE_URL}/{endpoint}"
        request_params = {**params, "key": self._api_key}

        last_exception: Optional[Exception] = None

        for attempt in range(1, self._max_retries + 2):  # 首次 + max_retries 次重试
            try:
                # 1) 频率控制等待
                self._limiter.wait()

                # 2) 发起请求
                resp = self._session.get(
                    url, params=request_params, timeout=self._timeout
                )
                resp.raise_for_status()

                # 3) 解析 JSON
                data = resp.json()

                # 4) 检查业务状态码
                _raise_by_status(data)

                return data

            except (requests.ConnectionError, requests.Timeout) as e:
                last_exception = NetworkError(f"网络异常 (尝试 {attempt}/{self._max_retries + 1}): {e}")
                if attempt <= self._max_retries:
                    backoff = 2 ** (attempt - 1)  # 指数退避: 1s, 2s, 4s
                    time.sleep(backoff)
                continue

            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status >= 500 and attempt <= self._max_retries:
                    last_exception = ServerError(f"服务端错误 {status} (尝试 {attempt}/{self._max_retries + 1})")
                    backoff = 2 ** (attempt - 1)
                    time.sleep(backoff)
                    continue
                raise ServerError(f"HTTP 错误: {status}") from e

            except (QPSError,) as e:
                # QPS 超限则自动降速等待后重试
                if attempt <= self._max_retries:
                    time.sleep(2.0 * attempt)
                    last_exception = e
                    continue
                raise

        # 所有重试均失败
        raise NetworkError(f"请求失败，已重试 {self._max_retries} 次") from last_exception

    # ------------------------------------------------------------------
    #  1. 行政区划查询
    # ------------------------------------------------------------------

    def get_region_list(
        self,
        keyword: str,
        subdistrict: int = 1,
    ) -> List[DistrictInfo]:
        """获取行政区域信息（省 / 市 / 区县三级）。

        对应高德 API: ``config/district``

        Args:
            keyword: 搜索关键词，如 "北京市"、"朝阳区"。支持省市区的名称或 adcode。
            subdistrict: 子级返回层数。
                0 = 不返回下级行政区；
                1 = 返回下一级（默认）；
                2 = 返回下两级；
                3 = 返回下三级。

        Returns:
            区划信息列表，每个元素为 :class:`DistrictInfo`。

        Raises:
            APIKeyError: API Key 无效
            InvalidParameters: 参数错误
            NetworkError: 网络异常（含重试耗尽）
        """
        data = self._request(
            endpoint="config/district",
            params={
                "keywords": keyword,
                "subdistrict": str(subdistrict),
                "extensions": "all",
                "output": "JSON",
            },
        )

        districts_raw = data.get("districts", [])
        if not districts_raw:
            raise NoDataError(f"未找到匹配的行政区划: {keyword}")

        # 第一个 district 是搜索目标本身
        top = districts_raw[0]

        # 当 subdistrict=0 时，返回查询目标本身（而不是子级）
        if subdistrict == 0:
            return [DistrictInfo.from_amap_json(top)]

        # 当 subdistrict>=1 时，返回子级列表
        children = top.get("districts", [])

        result: List[DistrictInfo] = []
        for child in children:
            result.append(DistrictInfo.from_amap_json(child))

        return result

    # ------------------------------------------------------------------
    #  2. POI 多边形搜索
    # ------------------------------------------------------------------

    def get_poi_polygon(
        self,
        key: str,
        polygon_str: str,
        keywords: str = "",
        types: str = "",
        offset: int = 25,
        page: int = 1,
    ) -> SearchResult:
        """根据多边形范围搜索 POI。

        对应高德 API: ``place/polygon``

        Args:
            key: 高德 Web 服务 API Key（覆盖实例的 api_key，用于灵活切换）。
            polygon_str: 多边形顶点坐标串，格式为 "lng1,lat1|lng2,lat2|...|lng1,lat1"。
                注意：多边形首尾顶点必须相同以闭合，建议至少 4 个点。
            keywords: 搜索关键词，支持多个以 "|" 分隔，如 "餐饮|购物"。
            types: POI 类型代码，如 "050000"（餐饮），可与 keywords 配合使用。
            offset: 每页记录数，最大 25。高德 API 限制。
            page: 当前页码，从 1 开始。
                高德 API 对翻页有限制（通常最多翻 100 页），
                实际返回数据量约 800~900 条以内会截断。

        Returns:
            :class:`SearchResult`，包含当前页的 POI 列表。

        Raises:
            APIKeyError: API Key 无效
            QuotaExceeded: 当日配额用尽
            InvalidParameters: 参数格式错误（如 polygon_str 不合法）
            NetworkError: 网络异常
        """
        if not polygon_str:
            raise InvalidParameters("polygon_str 不能为空")

        params: dict = {
            "polygon": polygon_str,
            "offset": str(offset),
            "page": str(page),
            "extensions": "all",
            "output": "JSON",
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types

        data = self._request(endpoint="place/polygon", params=params)

        count_str = data.get("count", "0")
        total_count = int(count_str) if count_str.isdigit() else 0

        pois_raw: list = data.get("pois", [])
        pois_list = _parse_poi_list(pois_raw)

        return SearchResult(
            pois=pois_list,
            total_count=total_count,
            page=page,
            page_size=offset,
            keyword=keywords,
            is_complete=len(pois_raw) < offset,
        )

    # ------------------------------------------------------------------
    #  3. POI 周边搜索
    # ------------------------------------------------------------------

    def get_poi_around(
        self,
        key: str,
        location: str,
        radius: int = 1000,
        keywords: str = "",
        types: str = "",
        offset: int = 25,
        page: int = 1,
    ) -> SearchResult:
        """根据中心点和半径进行周边 POI 搜索。

        对应高德 API: ``place/around``

        Args:
            key: 高德 Web 服务 API Key。
            location: 中心点坐标 "lng,lat"。
            radius: 搜索半径，单位米，取值范围 1~50000。默认为 1000。
            keywords: 搜索关键词，支持多个以 "|" 分隔。
            types: POI 类型代码。
            offset: 每页记录数，最大 25。
            page: 当前页码，从 1 开始。

        Returns:
            :class:`SearchResult`，包含当前页的 POI 列表。

        Raises:
            APIKeyError: API Key 无效
            QuotaExceeded: 当日配额用尽
            InvalidParameters: 参数格式错误
            NetworkError: 网络异常
        """
        if not location:
            raise InvalidParameters("location 不能为空")

        params: dict = {
            "location": location,
            "radius": str(radius),
            "offset": str(offset),
            "page": str(page),
            "extensions": "all",
            "output": "JSON",
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types

        data = self._request(endpoint="place/around", params=params)

        count_str = data.get("count", "0")
        total_count = int(count_str) if count_str.isdigit() else 0

        pois_raw: list = data.get("pois", [])
        pois_list = _parse_poi_list(pois_raw)

        return SearchResult(
            pois=pois_list,
            total_count=total_count,
            page=page,
            page_size=offset,
            keyword=keywords,
            is_complete=len(pois_raw) < offset,
        )

    # ------------------------------------------------------------------
    #  4. POI 详情查询
    # ------------------------------------------------------------------

    def get_poi_detail(
        self,
        key: str,
        poi_id: str,
    ) -> Optional[PoiDetail]:
        """根据 POI ID 获取单个 POI 的详细信息。

        对应高德 API: ``place/detail``

        此接口返回的数据包含基础搜索接口缺失的字段，如：
          - ``cost``：人均消费
          - ``rating``：评分
          - ``photos``：照片列表
          - ``website``：官网

        Args:
            key: 高德 Web 服务 API Key。
            poi_id: POI 的唯一标识 ID，从基础搜索返回的 ``poi_id`` 字段获取。

        Returns:
            :class:`PoiDetail` 如果查询成功；如果对应的 POI 不存在则返回 ``None``。

        Raises:
            APIKeyError: API Key 无效
            QuotaExceeded: 当日配额用尽
            NetworkError: 网络异常
        """
        if not poi_id:
            raise InvalidParameters("poi_id 不能为空")

        data = self._request(
            endpoint="place/detail",
            params={"id": poi_id, "output": "JSON"},
        )

        pois_raw: list = data.get("pois", [])
        if not pois_raw:
            return None

        return PoiDetail.from_amap_json(pois_raw[0])

    # ------------------------------------------------------------------
    #  5. 批量查询与辅助方法
    # ------------------------------------------------------------------

    def get_poi_detail_batch(
        self,
        key: str,
        poi_ids: Sequence[str],
        batch_size: int = 20,
        interval: float = 0.0,
    ) -> Dict[str, PoiDetail]:
        """批量获取 POI 详情。

        高德详情 API 不支持真正的批量请求（一次只能查一个），
        此方法在客户端层实现遍历 + 频率控制的批量查询。

        Args:
            key: 高德 Web 服务 API Key。
            poi_ids: POI ID 列表。
            batch_size: 每批数量（仅用于进度报告，实际仍逐个查询）。
            interval: 每批之间的额外等待时间（秒），用于进一步降低请求密度。

        Returns:
            poi_id 到 PoiDetail 的映射字典。查询失败的 ID 不会出现在结果中。
        """
        result: Dict[str, PoiDetail] = {}
        count = 0

        for pid in poi_ids:
            try:
                detail = self.get_poi_detail(key, pid)
                if detail is not None and detail.poi_id:
                    result[detail.poi_id] = detail
            except AmapApiError:
                continue

            count += 1
            if count % batch_size == 0 and interval > 0:
                time.sleep(interval)

        return result

    def search_poi_polygon_all_pages(
        self,
        key: str,
        polygon_str: str,
        keywords: str = "",
        types: str = "",
        offset: int = 25,
        max_pages: int = 40,
    ) -> SearchResult:
        """自动翻页获取多边形范围内所有 POI。

        当单个网格返回数据量可能超过一页时，使用此方法自动翻页至全部获取完毕
        或达到翻页上限。高德 API 实际上限约 800~900 条（约 36 页 × 25 条）。

        Args:
            key: 高德 Web 服务 API Key。
            polygon_str: 多边形坐标串。
            keywords: 搜索关键词。
            types: POI 类型。
            offset: 每页数量。
            max_pages: 最大翻页数，防止无限循环。默认 40 页 ≈ 1000 条。

        Returns:
            合并后的 SearchResult。
        """
        all_pois: List[PoiBasic] = []
        seen_ids: set = set()
        page = 1

        while page <= max_pages:
            result = self.get_poi_polygon(
                key=key,
                polygon_str=polygon_str,
                keywords=keywords,
                types=types,
                offset=offset,
                page=page,
            )

            dedup_count = 0
            for poi in result.pois:
                if poi.poi_id and poi.poi_id not in seen_ids:
                    seen_ids.add(poi.poi_id)
                    all_pois.append(poi)
                    dedup_count += 1

            if result.is_complete or dedup_count == 0:
                break

            page += 1

        return SearchResult(
            pois=all_pois,
            total_count=len(all_pois),
            page=1,
            page_size=offset,
            keyword=keywords,
            is_complete=True,
        )

    def close(self) -> None:
        """关闭底层 HTTP 会话，释放连接池资源。"""
        self._session.close()


# ============================================================================
#  工具函数（模块级）
# ============================================================================


def _parse_poi_list(pois_raw: list) -> List[PoiBasic]:
    """将高德 API 返回的原始 POI 列表解析为 PoiBasic 列表。"""
    return [PoiBasic.from_amap_json(item) for item in pois_raw]


def parse_polyline_to_coords(polyline: str) -> List[List[tuple]]:
    """将高德行政区划接口返回的边界多段线字符串解析为坐标列表。

    高德返回的 polyline 格式为多段线，多个环以 ``|`` 分隔：
        "lng1,lat1;lng2,lat2;...|lng1,lat1;lng2,lat2;..."

    每个环代表一个闭合多边形（主环或孔洞）。

    Args:
        polyline: 高德 API 返回的 boundary polyline 字符串。

    Returns:
        三维坐标列表：``[[(lng, lat), ...], [(lng, lat), ...]]``，
        每个元素为一个环（闭合多边形）的顶点坐标列表。

    Example:
        >>> parse_polyline_to_coords("116.3,39.9;116.5,39.9;116.3,40.1|116.4,39.95;...")
        [[(116.3, 39.9), (116.5, 39.9), (116.3, 40.1)], [... ]]
    """
    if not polyline:
        return []

    rings: List[List[tuple]] = []
    for ring_str in polyline.split("|"):
        ring_str = ring_str.strip()
        if not ring_str:
            continue
        coords: List[tuple] = []
        for point_str in ring_str.split(";"):
            point_str = point_str.strip()
            if not point_str:
                continue
            parts = point_str.split(",")
            if len(parts) == 2:
                try:
                    coords.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
        if coords:
            rings.append(coords)

    return rings
