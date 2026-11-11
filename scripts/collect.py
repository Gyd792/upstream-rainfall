#!/usr/bin/env python3
"""安全占位：尚未配置经过验证的官方实测降水接口，不写入虚构数值。"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
p=Path("data/rainfall.json")
d=json.loads(p.read_text(encoding="utf-8"))
d["updated_at"]=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
print("尚未配置官方实测降雨接口；保留空数据，不伪造降雨量。")
p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding="utf-8")
