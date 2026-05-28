"""
基于经纬度的两地距离测算工具
================================
读取两份 POI 采集生成的 Excel 文件，
对在同一个省、同一个市、同一个区的两地 POI，
根据经纬度计算距离，按距离从小到大排序输出。

用法：
  python distance_calculator.py 文件A.xlsx 文件B.xlsx [输出文件名.xlsx]

示例：
  python distance_calculator.py 广东省_好客连锁地图信息.xlsx 广东省梅州市_银行地图信息.xlsx
"""
import math
import os
import sys
from typing import List, Tuple

import pandas as pd


# ============================================================================
#  距离计算（Haversine 公式）
# ============================================================================

def haversine_distance(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """计算两点之间的球面距离（米）。
    
    Args:
        lng1, lat1: 第一点经纬度
        lng2, lat2: 第二点经纬度
    
    Returns:
        距离（米）
    """
    R = 6371000  # 地球平均半径（米）
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    a = math.sin(delta_phi / 2) ** 2 + \
        math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


# ============================================================================
#  数据加载
# ============================================================================

def load_xlsx(filepath: str, label: str) -> pd.DataFrame:
    """加载 xlsx 文件，标准化列名。"""
    if not os.path.exists(filepath):
        print("错误: 文件不存在 - %s" % filepath)
        sys.exit(1)

    df = pd.read_excel(filepath)
    
    # 标准化列名（去掉可能的前后空格）
    df.columns = [c.strip() for c in df.columns]

    # 检查必要的列
    required = ["名称", "省份", "城市", "区县", "经度", "纬度"]
    for col in required:
        if col not in df.columns:
            print("错误: %s 中缺少列 '%s'" % (label, col))
            print("现有列:", list(df.columns))
            sys.exit(1)

    # 过滤无效坐标
    before = len(df)
    df = df.dropna(subset=["经度", "纬度"])
    df = df[(df["经度"] != 0) & (df["纬度"] != 0)]
    after = len(df)
    if before != after:
        print("  %s: 剔除 %d 条无效坐标" % (label, before - after))

    print("  %s: 加载 %d 条有效数据" % (label, len(df)))
    return df


# ============================================================================
#  距离测算核心
# ============================================================================

def calculate_distances(
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    label_a: str,
    label_b: str,
) -> pd.DataFrame:
    """计算两地 POI 之间的距离（同省同市同区才计算）。
    
    Returns:
        包含两地名称、地址、坐标、距离的 DataFrame，按距离升序排列。
    """
    results = []
    total_pairs = 0
    matched_pairs = 0

    # 按 (省份, 城市, 区县) 分组匹配
    grouped_a = df_a.groupby(["省份", "城市", "区县"])
    grouped_b = df_b.groupby(["省份", "城市", "区县"])

    for (prov, city, dist), group_a in grouped_a:
        if (prov, city, dist) not in grouped_b.groups:
            continue

        group_b = grouped_b.get_group((prov, city, dist))
        total_pairs += len(group_a) * len(group_b)

        for _, row_a in group_a.iterrows():
            for _, row_b in group_b.iterrows():
                dist_m = haversine_distance(
                    row_a["经度"], row_a["纬度"],
                    row_b["经度"], row_b["纬度"],
                )
                matched_pairs += 1
                results.append({
                    label_a: row_a["名称"],
                    "%s地址" % label_a: row_a.get("地址", ""),
                    "%s经度" % label_a: row_a["经度"],
                    "%s纬度" % label_a: row_a["纬度"],
                    label_b: row_b["名称"],
                    "%s地址" % label_b: row_b.get("地址", ""),
                    "%s经度" % label_b: row_b["经度"],
                    "%s纬度" % label_b: row_b["纬度"],
                    "距离(米)": round(dist_m, 1),
                    "省份": prov,
                    "城市": city,
                    "区县": dist,
                })

    if total_pairs == 0:
        print("  没有找到同省同市同区的匹配对")
        return pd.DataFrame()

    print("  总组合: %d 对, 匹配: %d 对" % (total_pairs, matched_pairs))

    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values("距离(米)", ascending=True).reset_index(drop=True)
    return df_result


# ============================================================================
#  主程序
# ============================================================================

def main():
    print("=" * 60)
    print("  基于经纬度的两地距离测算工具")
    print("=" * 60)
    print("")

    # 解析参数
    args = sys.argv[1:]
    if len(args) < 2:
        print("用法: python distance_calculator.py 文件A.xlsx 文件B.xlsx [输出文件名.xlsx]")
        print("")
        print("示例:")
        print("  python distance_calculator.py 广东省_好客连锁地图信息.xlsx 广东省梅州市_银行地图信息.xlsx")
        print("")
        print("或者直接运行（交互模式）:")
        file_a = input("请输入文件A（如 广东省_好客连锁地图信息.xlsx）: ").strip()
        file_b = input("请输入文件B（如 广东省梅州市_银行地图信息.xlsx）: ").strip()
        output_file = input("请输入输出文件名（直接回车自动生成）: ").strip()
    else:
        file_a = args[0]
        file_b = args[1]
        output_file = args[2] if len(args) >= 3 else ""

    # 自动补全路径
    script_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(script_dir)

    for base_dir in [script_dir, parent_dir, os.getcwd()]:
        path_a = os.path.join(base_dir, file_a)
        if os.path.exists(path_a):
            file_a = path_a
            break
    for base_dir in [script_dir, parent_dir, os.getcwd()]:
        path_b = os.path.join(base_dir, file_b)
        if os.path.exists(path_b):
            file_b = path_b
            break

    if not output_file:
        base_a = os.path.splitext(os.path.basename(file_a))[0]
        base_b = os.path.splitext(os.path.basename(file_b))[0]
        output_file = "%s_%s_距离测算.xlsx" % (base_a, base_b)

    output_path = os.path.join(script_dir, output_file)

    print("")
    print("文件A: %s" % file_a)
    print("文件B: %s" % file_b)
    print("输出:  %s" % output_path)
    print("")

    # 1. 加载数据
    print("[1/3] 加载数据...")
    df_a = load_xlsx(file_a, "文件A")
    df_b = load_xlsx(file_b, "文件B")
    print("")

    # 2. 计算距离
    print("[2/3] 计算距离（同省同市同区才匹配）...")
    label_a = os.path.splitext(os.path.basename(file_a))[0]
    label_b = os.path.splitext(os.path.basename(file_b))[0]
    
    # 简化标签（太长时截断）
    if len(label_a) > 20:
        label_a = "POI_A"
    if len(label_b) > 20:
        label_b = "POI_B"

    df_result = calculate_distances(df_a, df_b, label_a, label_b)
    print("")

    # 3. 导出
    print("[3/3] 导出结果...")
    if df_result.empty:
        print("  无匹配结果，跳过导出")
        return

    df_result.to_excel(output_path, index=False, engine="openpyxl")
    print("  已保存: %s" % output_path)
    print("  共 %d 条记录" % len(df_result))

    # 打印统计摘要
    print("")
    print("=" * 60)
    print("统计摘要")
    print("=" * 60)
    print("  总匹配对数: %d" % len(df_result))
    print("  最近距离: %.1f 米" % df_result["距离(米)"].min())
    print("  最远距离: %.1f 米" % df_result["距离(米)"].max())
    print("  平均距离: %.1f 米" % df_result["距离(米)"].mean())

    print("")
    print("前 10 条最近的匹配:")
    print("-" * 60)
    for i, row in df_result.head(10).iterrows():
        print("  %d. [%s] %s  <->  [%s] %s  ->  %.1f 米" % (
            i + 1,
            label_a, row[label_a],
            label_b, row[label_b],
            row["距离(米)"],
        ))

    print("")
    print("完成！")


if __name__ == "__main__":
    main()
