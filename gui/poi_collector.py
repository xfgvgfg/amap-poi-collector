"""
高德地图 POI 采集工具 - 命令行版
==================================
支持灵活输入：只需输入省，市/区可选。
实时显示采集进度、百分比、已采集数量。

用法：
  交互模式：  python poi_collector.py
  参数模式：  python poi_collector.py 省 [市] [区] 关键词 [POI类型]

示例：
  python poi_collector.py 广东省 好客连锁
  python poi_collector.py 广东省 汕头市 银行
  python poi_collector.py 广东省 汕头市 潮南区 铭盛烟茶酒
  python poi_collector.py 广东省 购物服务 好客连锁
  (市和区不输入时自动跳过)
"""
import asyncio
import json
import os
import sys
import time
from typing import Dict, Optional, Tuple

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from amap_api import AmapClient, parse_polyline_to_coords
from collector import CollectorEngine, CollectorConfig, CollectorCallbacks
from grid_manager import _find_main_ring


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
CACHE_FILE = os.path.join(BASE_DIR, "region_cache.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load_api_key() -> str:
    if not os.path.exists(CONFIG_FILE):
        return ""
    with open(CONFIG_FILE, "r") as f:
        data = json.load(f)
    raw = data.get("api_key", "")
    if not raw:
        return ""
    import base64
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception:
        return raw


def load_region_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_adcode(province: str, city: str = "", district: str = "") -> Tuple[str, str, str]:
    """根据省/市/区名称查找 adcode。
    
    Returns:
        (adcode, region_name, level)
    """
    cache = load_region_cache()
    provinces = cache.get("provinces", {})
    cities = cache.get("cities", {})
    districts = cache.get("districts", {})

    # 省名称 -> adcode
    prov_adcode = None
    prov_name = None
    for code, name in provinces.items():
        if province in name:
            prov_adcode = code
            prov_name = name
            break
    if not prov_adcode:
        raise ValueError("未找到省份: %s" % province)

    if not city:
        return prov_adcode, prov_name, "province"

    # 市名称 -> adcode
    city_adcode = None
    city_name = None
    prov_cities = cities.get(prov_adcode, {})
    for code, name in prov_cities.items():
        if city in name:
            city_adcode = code
            city_name = name
            break
    if not city_adcode:
        if prov_name in ("北京市", "上海市", "天津市", "重庆市"):
            return prov_adcode, prov_name, "province"
        raise ValueError("在 %s 下未找到城市: %s" % (prov_name, city))

    if not district:
        return city_adcode, city_name, "city"

    # 区名称 -> adcode
    dist_adcode = None
    dist_name = None
    city_districts = districts.get(city_adcode, {})
    for code, name in city_districts.items():
        if district in name:
            dist_adcode = code
            dist_name = name
            break
    if not dist_adcode:
        print("  提示: 未找到区 %s，使用市级别采集" % district)
        return city_adcode, city_name, "city"

    return dist_adcode, dist_name, "district"


async def collect_poi(
    api_key: str,
    adcode: str,
    region_name: str,
    level: str,
    keyword: str,
    poi_type: str,
    output_file: str,
    province: str = "",
    city: str = "",
    district: str = "",
) -> int:
    """执行 POI 采集并导出为 Excel。"""
    client = AmapClient(api_key=api_key, qps=5)

    print("正在获取 [%s] 的行政区划边界..." % region_name)
    districts = client.get_region_list(keyword=adcode, subdistrict=0)
    if not districts:
        print("错误: 无法获取边界")
        return 0
    d = districts[0]
    polyline = d.polyline
    if not polyline:
        print("错误: 边界数据为空")
        return 0
    rings = parse_polyline_to_coords(polyline)
    if not rings:
        print("错误: 无法解析边界坐标")
        return 0
    _, boundary = _find_main_ring(rings)
    print("  边界获取成功, %d 个坐标点" % len(boundary))

    print("初始化采集引擎...")
    engine = CollectorEngine(
        config=CollectorConfig(
            api_key=api_key,
            search_radius_km=10.0,
            keywords=[keyword],
            poi_types=poi_type,
            region_name=region_name,
            adcode=adcode,
            boundary=boundary,
            qps=5,
            max_grids=5000,
        ),
    )
    grid_count = engine.init_grids(boundary)
    print("  网格数: %d" % grid_count)

    async def _skip_enrich():
        engine._log("info", "跳过详情补充")
    engine._enrich_details = _skip_enrich

    print("开始采集...")
    collected = []
    start_time = time.time()

    def on_data(pois):
        for p in pois:
            collected.append(p.to_dict())

    def on_progress(progress, info):
        pct = int(progress * 100)
        total = info.get("total_pois", 0)
        elapsed = time.time() - start_time
        if elapsed >= 60:
            elapsed_str = "%d分%d秒" % (elapsed // 60, elapsed % 60)
        else:
            elapsed_str = "%d秒" % elapsed
        sys.stdout.write(
            "\r  进度: %d%% | 已采集: %d 条 | 耗时: %s" % (pct, total, elapsed_str)
        )
        sys.stdout.flush()

    engine.cb = CollectorCallbacks(
        on_data=on_data,
        on_progress=on_progress,
    )

    await engine.start()
    if engine._main_task:
        try:
            await engine._main_task
        except Exception as e:
            print("\n  采集异常: %s" % e)

    elapsed = time.time() - start_time
    if elapsed >= 60:
        elapsed_str = "%d分%d秒" % (elapsed // 60, elapsed % 60)
    else:
        elapsed_str = "%d秒" % elapsed
    print("\n  采集完成: %d 条, 耗时 %s" % (len(collected), elapsed_str))

    if not collected:
        print("  无数据，跳过导出")
        return 0

    output_path = os.path.join(SCRIPT_DIR, output_file)
    df = pd.DataFrame(collected)

    col_map = {
        "name": "名称",
        "address": "地址",
        "pname": "省份",
        "cityname": "城市",
        "adname": "区县",
        "longitude": "经度",
        "latitude": "纬度",
        "tel": "电话",
        "type": "类型",
    }
    available = {k: v for k, v in col_map.items() if k in df.columns}
    df_out = df[list(available.keys())].copy()
    df_out.rename(columns=available, inplace=True)

    df_out.to_excel(output_path, index=False, engine="openpyxl")
    print("  已保存: %s" % output_file)
    print("  共 %d 条记录" % len(df_out))
    return len(df_out)


async def main():
    api_key = load_api_key()
    if not api_key:
        print("错误: 未找到 API Key（config.json 缺失或为空）")
        print("请先运行 app.py 配置 API Key")
        return

    print("=" * 60)
    print("  高德地图 POI 采集工具")
    print("=" * 60)
    print("")

    # 解析参数（支持命令行参数和交互模式）
    args = sys.argv[1:]

    if len(args) >= 1:
        # 参数模式: python poi_collector.py 省 [市] [区] 关键词 [POI类型]
        province = args[0]
        # 找出关键词（最后不带"省/市/区/县"的那个参数）
        non_region_args = [a for a in args[1:] if not any(s in a for s in ["省", "市", "区", "县"])]
        
        if len(non_region_args) >= 1:
            keyword = non_region_args[-1]
            poi_type = non_region_args[0] if len(non_region_args) >= 2 and non_region_args[0] != keyword else ""
        else:
            keyword = ""
            poi_type = ""

        # 剩余的中间参数是市和区
        region_args = [a for a in args[1:] if a not in (keyword, poi_type) and a != ""]
        city = region_args[0] if len(region_args) >= 1 else ""
        district = region_args[1] if len(region_args) >= 2 else ""
        
        if not keyword:
            # 如果参数模式匹配失败，回退到交互
            print("参数解析失败，切换到交互模式")
            province = city = district = keyword = poi_type = ""
    else:
        province = city = district = keyword = poi_type = ""

    if not province:
        province = input("请输入省/直辖市（如 广东省）: ").strip()
    if not city:
        city = input("请输入地级市（如 汕头市，直接回车跳过）: ").strip()
    if not district:
        district = input("请输入区/县（如 潮南区，直接回车跳过）: ").strip()
    if not keyword:
        keyword = input("请输入关键词（如 好客连锁）: ").strip()
    if not poi_type:
        poi_type = input("请输入POI类型（如 购物服务，直接回车不限类型）: ").strip()

    if not province or not keyword:
        print("错误: 省和关键词不能为空")
        return

    try:
        adcode, region_name, level = resolve_adcode(province, city, district)
    except ValueError as e:
        print("错误: %s" % e)
        return

    level_names = {"province": "省/直辖市", "city": "地级市", "district": "区/县"}
    print("")
    print("  解析结果: %s → %s (%s)" % (region_name, adcode, level_names.get(level, level)))
    print("")

    # 构建文件名
    name_parts = [province]
    if city:
        name_parts.append(city)
    if district:
        name_parts.append(district)
    if poi_type:
        name_parts.append(poi_type)
    name_parts.append(keyword)
    output_file = "%s地图信息.xlsx" % ("_".join(name_parts))

    print("  输出文件: %s" % output_file)
    print("=" * 60)
    print("")

    await collect_poi(
        api_key=api_key,
        adcode=adcode,
        region_name=region_name,
        level=level,
        keyword=keyword,
        poi_type=poi_type,
        output_file=output_file,
        province=province,
        city=city,
        district=district,
    )

    print("")
    print("=" * 60)
    print("采集完毕！")


if __name__ == "__main__":
    asyncio.run(main())
