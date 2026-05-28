"""
高德地图 POI 采集工具 - GUI 版
===============================
整合 POI 数据采集 + 两地距离测算功能。
纯 GUI 应用，无需浏览器。
"""
import asyncio
import json
import math
import os
import queue
import sys
import threading
import time
from tkinter import ttk, filedialog, messagebox
import tkinter as tk

import pandas as pd

# 将上级目录加入模块搜索路径
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from amap_api import AmapClient, parse_polyline_to_coords
from collector import CollectorEngine, CollectorConfig, CollectorCallbacks
from grid_manager import _find_main_ring


# ============================================================================
#  常量
# ============================================================================

CACHE_FILE = os.path.join(BASE_DIR, "region_cache.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

FONT = ("Microsoft YaHei", 10)
FONT_BOLD = ("Microsoft YaHei", 10, "bold")
FONT_SMALL = ("Microsoft YaHei", 9)


# ============================================================================
#  工具函数
# ============================================================================

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


def save_api_key(api_key: str) -> None:
    import base64
    encoded = base64.b64encode(api_key.encode("utf-8")).decode("utf-8")
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_key": encoded}, f, ensure_ascii=False, indent=2)


def load_region_cache() -> dict:
    if not os.path.exists(CACHE_FILE):
        return {}
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_adcode(province: str, city: str = "", district: str = "") -> tuple:
    cache = load_region_cache()
    provinces = cache.get("provinces", {})
    cities = cache.get("cities", {})
    districts = cache.get("districts", {})

    prov_adcode = prov_name = None
    for code, name in provinces.items():
        if province in name:
            prov_adcode, prov_name = code, name
            break
    if not prov_adcode:
        raise ValueError("未找到省份: %s" % province)
    if not city:
        return prov_adcode, prov_name, "province"

    city_adcode = city_name = None
    prov_cities = cities.get(prov_adcode, {})
    for code, name in prov_cities.items():
        if city in name:
            city_adcode, city_name = code, name
            break
    if not city_adcode:
        if prov_name in ("北京市", "上海市", "天津市", "重庆市"):
            return prov_adcode, prov_name, "province"
        raise ValueError("在 %s 下未找到城市: %s" % (prov_name, city))
    if not district:
        return city_adcode, city_name, "city"

    dist_adcode = dist_name = None
    city_districts = districts.get(city_adcode, {})
    for code, name in city_districts.items():
        if district in name:
            dist_adcode, dist_name = code, name
            break
    if not dist_adcode:
        return city_adcode, city_name, "city"
    return dist_adcode, dist_name, "district"


# ============================================================================
#  Haversine 距离计算
# ============================================================================

def haversine_distance(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ============================================================================
#  POI 采集线程
# ============================================================================

class CollectThread(threading.Thread):
    """在后台线程中运行异步采集，通过队列向 GUI 发送消息。"""

    def __init__(self, api_key: str, province: str, city: str, district: str,
                 keyword: str, poi_type: str, msg_queue: queue.Queue,
                 save_dir: str = ""):
        super().__init__(daemon=True)
        self.api_key = api_key
        self.province = province
        self.city = city
        self.district = district
        self.keyword = keyword
        self.poi_type = poi_type
        self.msg_queue = msg_queue
        self.save_dir = save_dir or SCRIPT_DIR
        self._stop_flag = False

    def stop(self):
        self._stop_flag = True

    def _build_output_filename(self) -> str:
        """根据输入参数构建输出文件名。
        
        如：广东省_购物服务_好客连锁地图信息.xlsx
            广东省_汕头市_潮南区_购物服务_好客连锁地图信息.xlsx
            广东省_汕头市_购物服务_好客连锁地图信息.xlsx
            广东省_好客连锁地图信息.xlsx
        """
        name_parts = [self.province]
        if self.city:
            name_parts.append(self.city)
        if self.district:
            name_parts.append(self.district)
        if self.poi_type:
            name_parts.append(self.poi_type)
        name_parts.append(self.keyword)
        return "%s地图信息.xlsx" % ("_".join(name_parts))

    def _save_to_excel(self, collected: list) -> str:
        """将采集到的数据保存为 Excel 文件。
        
        输出列：名称、地址、省份、城市、区县、经度、纬度、电话、类型
        """
        if not collected:
            return ""

        output_file = self._build_output_filename()
        output_path = os.path.join(self.save_dir, output_file)

        df = pd.DataFrame(collected)

        # 选取需要的列并重命名为中文
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
        return output_path

    def run(self):
        try:
            adcode, region_name, level = resolve_adcode(
                self.province, self.city, self.district
            )
            self.msg_queue.put(("log", "解析结果: %s (%s)" % (region_name, level)))
        except ValueError as e:
            self.msg_queue.put(("error", "行政区划解析失败: %s" % e))
            self.msg_queue.put(("done", 0, ""))
            return

        # 在后台线程中创建事件循环并运行
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            collected = loop.run_until_complete(
                self._collect(adcode, region_name)
            )
            count = len(collected)

            # 保存 Excel
            saved_path = self._save_to_excel(collected)
            if saved_path:
                self.msg_queue.put(("log", "已保存到: %s" % saved_path))
            else:
                self.msg_queue.put(("log", "无数据，跳过导出"))

            self.msg_queue.put(("done", count, saved_path))
        except Exception as e:
            error_msg = str(e)
            if "10005" in error_msg or "INVALID_USER_IP" in error_msg:
                self.msg_queue.put(("error",
                    "API 密钥 IP 受限 (10005)\n"
                    "请登录高德地图控制台 → 应用管理 → 修改密钥 → "
                    "将本机 IP 加入白名单，或关闭 IP 限制"))
            else:
                self.msg_queue.put(("error", "采集异常: %s" % error_msg))
            self.msg_queue.put(("done", 0, ""))
        finally:
            loop.close()

    async def _collect(self, adcode: str, region_name: str):
        client = AmapClient(api_key=self.api_key, qps=5)

        self.msg_queue.put(("log", "正在获取 [%s] 的边界..." % region_name))
        districts = client.get_region_list(keyword=adcode, subdistrict=0)
        if not districts:
            self.msg_queue.put(("error", "无法获取边界"))
            return []
        d = districts[0]
        polyline = d.polyline
        if not polyline:
            self.msg_queue.put(("error", "边界数据为空"))
            return []
        rings = parse_polyline_to_coords(polyline)
        if not rings:
            self.msg_queue.put(("error", "无法解析边界坐标"))
            return []
        _, boundary = _find_main_ring(rings)
        self.msg_queue.put(("log", "边界获取成功, %d 个坐标点" % len(boundary)))

        self.msg_queue.put(("log", "初始化采集引擎..."))
        keywords = [self.keyword]
        poi_types = self.poi_type if self.poi_type else ""

        engine = CollectorEngine(
            config=CollectorConfig(
                api_key=self.api_key,
                search_radius_km=10.0,
                keywords=keywords,
                poi_types=poi_types,
                region_name=region_name,
                adcode=adcode,
                boundary=boundary,
                qps=5,
                max_grids=5000,
            ),
        )
        grid_count = engine.init_grids(boundary)
        self.msg_queue.put(("log", "网格数: %d" % grid_count))
        self.msg_queue.put(("grid_count", grid_count))

        async def _skip_enrich():
            engine._log("info", "跳过详情补充")
        engine._enrich_details = _skip_enrich

        collected = []
        start_time = time.time()

        def on_data(pois):
            for p in pois:
                collected.append(p.to_dict())
            self.msg_queue.put(("poi_count", len(collected)))

        def on_progress(progress, info):
            pct = int(progress * 100)
            total = info.get("total_pois", 0)
            elapsed = time.time() - start_time
            self.msg_queue.put(("progress", (pct, total, int(elapsed))))

        engine.cb = CollectorCallbacks(on_data=on_data, on_progress=on_progress)

        self.msg_queue.put(("log", "开始采集..."))
        self.msg_queue.put(("status", "采集中"))

        await engine.start()
        if engine._main_task:
            try:
                await engine._main_task
            except Exception as e:
                self.msg_queue.put(("error", "采集异常: %s" % e))

        self.msg_queue.put(("status", "完成"))
        return collected


# ============================================================================
#  距离测算函数（同步）
# ============================================================================

def load_xlsx(filepath: str, label: str) -> pd.DataFrame:
    if not os.path.exists(filepath):
        raise FileNotFoundError("文件不存在: %s" % filepath)
    df = pd.read_excel(filepath)
    df.columns = [c.strip() for c in df.columns]
    required = ["名称", "省份", "城市", "区县", "经度", "纬度"]
    for col in required:
        if col not in df.columns:
            raise ValueError("%s 中缺少列 '%s'" % (label, col))
    before = len(df)
    df = df.dropna(subset=["经度", "纬度"])
    df = df[(df["经度"] != 0) & (df["纬度"] != 0)]
    return df, before - len(df)


def calculate_distances(df_a: pd.DataFrame, df_b: pd.DataFrame,
                        label_a: str, label_b: str) -> pd.DataFrame:
    results = []
    grouped_a = df_a.groupby(["省份", "城市", "区县"])
    grouped_b = df_b.groupby(["省份", "城市", "区县"])
    for key, group_a in grouped_a:
        if key not in grouped_b.groups:
            continue
        group_b = grouped_b.get_group(key)
        for _, row_a in group_a.iterrows():
            for _, row_b in group_b.iterrows():
                dist_m = haversine_distance(
                    row_a["经度"], row_a["纬度"],
                    row_b["经度"], row_b["纬度"],
                )
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
                    "省份": key[0], "城市": key[1], "区县": key[2],
                })
    if not results:
        return pd.DataFrame()
    df_result = pd.DataFrame(results)
    df_result = df_result.sort_values("距离(米)", ascending=True).reset_index(drop=True)
    return df_result


# ============================================================================
#  GUI 主界面
# ============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("高德地图 POI 采集工具")
        self.geometry("1100x750")
        self.minsize(900, 600)

        self._build_menu()

        # 检查 API Key，如果没有则弹出配置窗口
        self.api_key = load_api_key()
        if not self.api_key:
            self._show_config_dialog(force=True)
        else:
            self._build_ui()

    def _build_menu(self):
        menubar = tk.Menu(self)
        self.config(menu=menubar)

        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="配置 API 密钥", command=lambda: self._show_config_dialog(force=False))
        settings_menu.add_separator()
        settings_menu.add_command(label="退出", command=self.quit)
        menubar.add_cascade(label="设置", menu=settings_menu)

    def _show_config_dialog(self, force=False):
        dialog = tk.Toplevel(self)
        dialog.title("配置高德地图 API 密钥")
        dialog.geometry("520x260")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        if force:
            dialog.protocol("WM_DELETE_WINDOW", self.destroy)

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="高德地图 API 密钥配置",
                 font=("Microsoft YaHei", 14, "bold"),
                 fg="#2c3e50").pack(anchor=tk.W, pady=(0, 10))

        tk.Label(frame, text="请输入高德地图 Web 服务 API Key：",
                 font=FONT).pack(anchor=tk.W, pady=(0, 5))

        entry_var = tk.StringVar()
        if self.api_key:
            entry_var.set(self.api_key)
        entry = ttk.Entry(frame, textvariable=entry_var, width=60, font=("Consolas", 10), show="*")
        entry.pack(fill=tk.X, pady=(0, 5))
        entry.focus_set()

        show_var = tk.BooleanVar(value=False)
        def toggle_show():
            entry.config(show="" if show_var.get() else "*")
        ttk.Checkbutton(frame, text="显示密钥", variable=show_var,
                        command=toggle_show).pack(anchor=tk.W, pady=(0, 15))

        label_hint = tk.Label(frame, text="密钥将加密存储在 config.json 中",
                              font=FONT_SMALL, fg="#7f8c8d")
        label_hint.pack(anchor=tk.W, pady=(0, 10))

        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X)

        def on_save():
            key = entry_var.get().strip()
            if not key:
                messagebox.showwarning("提示", "密钥不能为空", parent=dialog)
                return
            if len(key) < 10:
                ret = messagebox.askyesno("确认", "密钥长度过短，是否继续？", parent=dialog)
                if not ret:
                    return

            save_api_key(key)
            self.api_key = key
            if force:
                self._build_ui()
            messagebox.showinfo("成功", "API 密钥已保存", parent=dialog)
            dialog.destroy()

        def on_cancel():
            if force:
                self.destroy()
            else:
                dialog.destroy()

        ttk.Button(btn_row, text="保存", command=on_save).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(btn_row, text="取消", command=on_cancel).pack(side=tk.LEFT)

    def _build_ui(self):
        # 主容器
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # 标题
        title = tk.Label(main, text="高德地图 POI 采集工具",
                         font=("Microsoft YaHei", 16, "bold"),
                         fg="#2c3e50", anchor=tk.CENTER)
        title.pack(fill=tk.X, pady=(0, 10))

        # 标签页
        self.notebook = ttk.Notebook(main)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_collect_tab()
        self._build_distance_tab()

    # ========================================================================
    #  Tab 1: POI 采集
    # ========================================================================

    def _build_collect_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="  POI 数据采集  ")

        # 输入区域
        input_frame = ttk.LabelFrame(frame, text="采集参数", padding=10)
        input_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(input_frame, text="省/直辖市：", font=FONT).grid(row=0, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_province = ttk.Entry(input_frame, width=20, font=FONT)
        self.entry_province.grid(row=0, column=1, sticky=tk.W, padx=5, pady=3)
        self.entry_province.insert(0, "广东省")

        ttk.Label(input_frame, text="地级市（可选）：", font=FONT).grid(row=0, column=2, sticky=tk.W, padx=5, pady=3)
        self.entry_city = ttk.Entry(input_frame, width=16, font=FONT)
        self.entry_city.grid(row=0, column=3, sticky=tk.W, padx=5, pady=3)

        ttk.Label(input_frame, text="区/县（可选）：", font=FONT).grid(row=1, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_district = ttk.Entry(input_frame, width=20, font=FONT)
        self.entry_district.grid(row=1, column=1, sticky=tk.W, padx=5, pady=3)

        ttk.Label(input_frame, text="关键词：", font=FONT).grid(row=1, column=2, sticky=tk.W, padx=5, pady=3)
        self.entry_keyword = ttk.Entry(input_frame, width=16, font=FONT)
        self.entry_keyword.grid(row=1, column=3, sticky=tk.W, padx=5, pady=3)
        self.entry_keyword.insert(0, "银行")

        ttk.Label(input_frame, text="POI类型（可选）：", font=FONT).grid(row=2, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_poi_type = ttk.Combobox(input_frame, width=18, font=FONT, state="readonly")
        self.entry_poi_type["values"] = [
            "", "餐饮服务", "购物服务", "生活服务",
            "体育休闲服务", "医疗保健服务", "住宿服务",
            "风景名胜", "商务住宅", "政府机构及社会团体",
            "科教文化服务", "交通设施服务", "金融保险服务",
            "公司企业", "道路附属设施", "地名地址信息",
            "公共设施", "事件活动",
        ]
        self.entry_poi_type.set("")
        self.entry_poi_type.grid(row=2, column=1, sticky=tk.W, padx=5, pady=3)

        # 保存目录
        ttk.Label(input_frame, text="保存目录：", font=FONT).grid(row=3, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_save_dir = ttk.Entry(input_frame, width=40, font=FONT)
        self.entry_save_dir.grid(row=3, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=3)
        self.entry_save_dir.insert(0, SCRIPT_DIR)
        ttk.Button(input_frame, text="选择...", command=self._on_select_save_dir).grid(row=3, column=3, padx=5, pady=3)
        input_frame.columnconfigure(1, weight=1)

        # 按钮
        btn_frame = ttk.Frame(input_frame)
        btn_frame.grid(row=2, column=2, columnspan=2, sticky=tk.W, padx=5, pady=3)

        self.btn_start = ttk.Button(btn_frame, text="开始采集", command=self._on_start_collect)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 5))
        self.btn_stop = ttk.Button(btn_frame, text="停止", command=self._on_stop_collect, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)

        # 进度区域
        progress_frame = ttk.LabelFrame(frame, text="采集进度", padding=10)
        progress_frame.pack(fill=tk.X, pady=(0, 10))

        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 5))

        info_frame = ttk.Frame(progress_frame)
        info_frame.pack(fill=tk.X)
        self.label_progress = tk.Label(info_frame, text="0%", font=FONT_BOLD, fg="#2980b9")
        self.label_progress.pack(side=tk.LEFT, padx=(0, 15))
        self.label_status = tk.Label(info_frame, text="状态：就绪", font=FONT)
        self.label_status.pack(side=tk.LEFT, padx=(0, 15))
        self.label_count = tk.Label(info_frame, text="已采集：0 条", font=FONT)
        self.label_count.pack(side=tk.LEFT, padx=(0, 15))
        self.label_elapsed = tk.Label(info_frame, text="耗时：--", font=FONT)
        self.label_elapsed.pack(side=tk.LEFT)

        # 日志区域
        log_frame = ttk.LabelFrame(frame, text="运行日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)

        text_frame = ttk.Frame(log_frame)
        text_frame.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log_text = tk.Text(text_frame, yscrollcommand=scrollbar.set,
                                 font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                                 insertbackground="white", state=tk.DISABLED,
                                 wrap=tk.WORD, height=12)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)

        # 内部状态
        self.collect_queue = queue.Queue()
        self.collect_thread = None
        self.after(100, self._poll_collect_queue)

    def _log(self, text):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _on_select_save_dir(self):
        path = filedialog.askdirectory(
            title="选择保存目录",
            initialdir=self.entry_save_dir.get() or SCRIPT_DIR,
        )
        if path:
            self.entry_save_dir.delete(0, tk.END)
            self.entry_save_dir.insert(0, path)

    def _on_start_collect(self):
        province = self.entry_province.get().strip()
        city = self.entry_city.get().strip()
        district = self.entry_district.get().strip()
        keyword = self.entry_keyword.get().strip()
        poi_type = self.entry_poi_type.get().strip()
        save_dir = self.entry_save_dir.get().strip() or SCRIPT_DIR

        if not province or not keyword:
            messagebox.showwarning("提示", "省和关键词不能为空")
            return

        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress_bar["value"] = 0
        self.label_progress.config(text="0%")
        self.label_status.config(text="状态：初始化")
        self.label_count.config(text="已采集：0 条")
        self.label_elapsed.config(text="耗时：0秒")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

        self.collect_queue = queue.Queue()
        self.collect_thread = CollectThread(
            api_key=self.api_key,
            province=province, city=city, district=district,
            keyword=keyword, poi_type=poi_type,
            save_dir=save_dir,
            msg_queue=self.collect_queue,
        )
        self.collect_thread.start()

    def _on_stop_collect(self):
        if self.collect_thread and self.collect_thread.is_alive():
            self.collect_thread.stop()
            self._log("用户请求停止...")
        self.btn_stop.config(state=tk.DISABLED)

    def _poll_collect_queue(self):
        try:
            while True:
                msg = self.collect_queue.get_nowait()
                msg_type = msg[0]

                if msg_type == "log":
                    self._log(msg[1])
                elif msg_type == "error":
                    self._log("错误: %s" % msg[1])
                    self.label_status.config(text="状态：错误")
                elif msg_type == "progress":
                    pct, total, elapsed = msg[1]
                    self.progress_bar["value"] = pct
                    self.label_progress.config(text="%d%%" % pct)
                    self.label_count.config(text="已采集：%d 条" % total)
                    self.label_elapsed.config(text="耗时：%d秒" % elapsed)
                elif msg_type == "poi_count":
                    self.label_count.config(text="已采集：%d 条" % msg[1])
                elif msg_type == "status":
                    self.label_status.config(text="状态：" + msg[1])
                elif msg_type == "grid_count":
                    pass
                elif msg_type == "done":
                    count = msg[1]
                    saved_path = msg[2] if len(msg) >= 3 else ""
                    self._log("采集完成，共 %d 条记录" % count)
                    if saved_path:
                        self._log("保存路径: %s" % saved_path)
                    self.label_status.config(text="状态：完成" if count > 0 else "状态：无数据")
                    self.progress_bar["value"] = 100
                    self.label_progress.config(text="100%")
                    self.btn_start.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(100, self._poll_collect_queue)

    # ========================================================================
    #  Tab 2: 距离测算
    # ========================================================================

    def _build_distance_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="  距离测算  ")

        # 文件选择区域
        file_frame = ttk.LabelFrame(frame, text="选择文件", padding=10)
        file_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(file_frame, text="文件A：", font=FONT).grid(row=0, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_file_a = ttk.Entry(file_frame, width=50, font=FONT)
        self.entry_file_a.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=3)
        ttk.Button(file_frame, text="浏览...", command=lambda: self._browse_file(self.entry_file_a)).grid(row=0, column=2, padx=5, pady=3)

        ttk.Label(file_frame, text="文件B：", font=FONT).grid(row=1, column=0, sticky=tk.W, padx=5, pady=3)
        self.entry_file_b = ttk.Entry(file_frame, width=50, font=FONT)
        self.entry_file_b.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=3)
        ttk.Button(file_frame, text="浏览...", command=lambda: self._browse_file(self.entry_file_b)).grid(row=1, column=2, padx=5, pady=3)

        file_frame.columnconfigure(1, weight=1)

        # 按钮区域
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill=tk.X, pady=(0, 10))

        self.btn_calc = ttk.Button(btn_row, text="开始测算", command=self._on_calc_distance)
        self.btn_calc.pack(side=tk.LEFT, padx=(0, 5))
        self.btn_export = ttk.Button(btn_row, text="导出 Excel", command=self._on_export_distance, state=tk.DISABLED)
        self.btn_export.pack(side=tk.LEFT, padx=(0, 15))

        self.label_calc_status = tk.Label(btn_row, text="就绪", font=FONT, fg="#7f8c8d")
        self.label_calc_status.pack(side=tk.LEFT)

        # 结果表格
        table_frame = ttk.LabelFrame(frame, text="测算结果", padding=5)
        table_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings",
                                 height=16, selectmode="extended")
        self.tree.heading("#1", text="序号")
        self.tree.heading("#2", text="POI_A")
        self.tree.heading("#3", text="POI_B")
        self.tree.heading("#4", text="距离(米)")
        self.tree.heading("#5", text="省份")
        self.tree.heading("#6", text="城市")
        self.tree.heading("#7", text="区县")
        self.tree.heading("#8", text="A地址")
        self.tree.column("#1", width=50, anchor=tk.CENTER)
        self.tree.column("#2", width=160)
        self.tree.column("#3", width=160)
        self.tree.column("#4", width=90, anchor=tk.E)
        self.tree.column("#5", width=70, anchor=tk.CENTER)
        self.tree.column("#6", width=70, anchor=tk.CENTER)
        self.tree.column("#7", width=70, anchor=tk.CENTER)
        self.tree.column("#8", width=200)

        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.tree.grid(row=0, column=0, sticky=tk.NSEW)
        scroll_y.grid(row=0, column=1, sticky=tk.NS)
        scroll_x.grid(row=1, column=0, sticky=tk.EW)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        # 统计
        stats_frame = ttk.Frame(frame)
        stats_frame.pack(fill=tk.X, pady=(5, 0))
        self.label_stats = tk.Label(stats_frame, text="", font=FONT, fg="#2c3e50", anchor=tk.W)
        self.label_stats.pack(fill=tk.X)

        # 内部状态
        self.df_result = None  # type: pd.DataFrame
        self.label_a_short = ""
        self.label_b_short = ""

    def _browse_file(self, entry):
        path = filedialog.askopenfilename(
            title="选择 Excel 文件",
            filetypes=[("Excel 文件", "*.xlsx"), ("所有文件", "*.*")],
            initialdir=SCRIPT_DIR,
        )
        if path:
            entry.delete(0, tk.END)
            entry.insert(0, path)

    def _on_calc_distance(self):
        file_a = self.entry_file_a.get().strip()
        file_b = self.entry_file_b.get().strip()

        if not file_a or not file_b:
            messagebox.showwarning("提示", "请选择两个文件")
            return

        if not os.path.exists(file_a):
            messagebox.showerror("错误", "文件A不存在:\n%s" % file_a)
            return
        if not os.path.exists(file_b):
            messagebox.showerror("错误", "文件B不存在:\n%s" % file_b)
            return

        self.btn_calc.config(state=tk.DISABLED)
        self.btn_export.config(state=tk.DISABLED)
        self.label_calc_status.config(text="计算中...", fg="#2980b9")

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.label_stats.config(text="")

        # 在后台线程中计算，不阻塞 GUI
        def calc_thread():
            try:
                df_a, dropped_a = load_xlsx(file_a, "文件A")
                df_b, dropped_b = load_xlsx(file_b, "文件B")

                base_a = os.path.splitext(os.path.basename(file_a))[0]
                base_b = os.path.splitext(os.path.basename(file_b))[0]
                label_a = base_a if len(base_a) <= 20 else "POI_A"
                label_b = base_b if len(base_b) <= 20 else "POI_B"

                df_result = calculate_distances(df_a, df_b, label_a, label_b)

                # 回主线程更新 UI
                self.after(0, self._on_calc_done, df_result, label_a, label_b,
                           len(df_a), len(df_b), dropped_a, dropped_b)
            except Exception as e:
                self.after(0, self._on_calc_error, str(e))

        threading.Thread(target=calc_thread, daemon=True).start()

    def _on_calc_done(self, df_result, label_a, label_b,
                      count_a, count_b, dropped_a, dropped_b):
        self.df_result = df_result
        self.label_a_short = label_a
        self.label_b_short = label_b

        for item in self.tree.get_children():
            self.tree.delete(item)

        if df_result.empty:
            self.label_calc_status.config(text="无匹配结果", fg="#e74c3c")
            self.btn_calc.config(state=tk.NORMAL)
            return

        for i, row in df_result.iterrows():
            self.tree.insert("", tk.END, values=(
                i + 1,
                row[label_a],
                row[label_b],
                "%.1f" % row["距离(米)"],
                row.get("省份", ""),
                row.get("城市", ""),
                row.get("区县", ""),
                row.get("%s地址" % label_a, ""),
            ))

        min_dist = df_result["距离(米)"].min()
        max_dist = df_result["距离(米)"].max()
        avg_dist = df_result["距离(米)"].mean()

        stats_text = "文件A: %d条" % count_a
        if dropped_a:
            stats_text += " (剔除%d条无效坐标)" % dropped_a
        stats_text += "  |  文件B: %d条" % count_b
        if dropped_b:
            stats_text += " (剔除%d条无效坐标)" % dropped_b
        stats_text += "  |  匹配: %d对" % len(df_result)
        stats_text += "  |  最近: %.1f米" % min_dist
        stats_text += "  |  最远: %.1f米" % max_dist
        stats_text += "  |  平均: %.1f米" % avg_dist

        self.label_stats.config(text=stats_text)
        self.label_calc_status.config(text="计算完成", fg="#27ae60")
        self.btn_calc.config(state=tk.NORMAL)
        self.btn_export.config(state=tk.NORMAL)

    def _on_calc_error(self, error_msg):
        self.label_calc_status.config(text="错误", fg="#e74c3c")
        self.btn_calc.config(state=tk.NORMAL)
        messagebox.showerror("计算错误", error_msg)

    def _on_export_distance(self):
        if self.df_result is None or self.df_result.empty:
            return

        default_name = "%s_%s_距离测算.xlsx" % (self.label_a_short, self.label_b_short)
        path = filedialog.asksaveasfilename(
            title="保存为",
            defaultextension=".xlsx",
            filetypes=[("Excel 文件", "*.xlsx")],
            initialdir=SCRIPT_DIR,
            initialfile=default_name,
        )
        if not path:
            return

        try:
            self.df_result.to_excel(path, index=False, engine="openpyxl")
            messagebox.showinfo("成功", "已保存到:\n%s" % path)
        except Exception as e:
            messagebox.showerror("错误", "导出失败:\n%s" % e)


# ============================================================================
#  启动
# ============================================================================

if __name__ == "__main__":
    app = App()
    app.mainloop()
