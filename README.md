# 高德地图 POI 采集工具

基于高德地图 API 的 POI 数据采集 GUI 工具，支持按省/市/区灵活采集兴趣点数据。

## 功能

- **POI 数据采集** — 输入省/市/区 + 关键词 + POI 类型，自动搜索并导出到 Excel
- **距离测算** — 读取两份 Excel，对同省同市同区的 POI 计算两两距离并排序
- **H3 网格覆盖** — 自动用六边形网格覆盖搜索区域，突破单次 API 查询限制
- **导出格式** — 名称、地址、省份、城市、区县、经度、纬度、电话、类型

## 使用

```
pip install -r requirements.txt
```

编辑项目根目录的 `config.json`，填入你的高德地图 Web 服务 API 密钥：
```json
{
    "api_key": "你的高德地图Web服务API密钥"
}
```

运行 GUI 版：
```
python gui/poi_collector_gui.py
```

运行命令行版：
```
python gui/poi_collector.py
```

## 目录结构

```
├── amap_api.py             高德 API 封装
├── collector.py            采集引擎
├── grid_manager.py         H3 网格管理器
├── region_cache.json       行政区划缓存
├── config.json             API 密钥配置（已含空模板，填入 Key 即可）
├── requirements.txt        依赖清单
└── gui/
    ├── poi_collector_gui.py   GUI 主程序
    ├── poi_collector.py       命令行版
    └── distance_calculator.py 距离测算工具
```

## 技术栈

- Python 3.8+ / tkinter GUI
- 高德地图 Web 服务 API
- Uber H3 六边形网格系统
- Haversine 公式球面距离计算
