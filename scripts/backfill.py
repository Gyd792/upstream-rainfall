#!/usr/bin/env python3
"""Administrator-only shared hourly rainfall backfill."""
import json, os, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DATA_FILE=ROOT/"data"/"rainfall.json"
BEIJING=timezone(timedelta(hours=8))
CITIES={"曲靖":"101290401","陆良":"101290403","宜良":"101290106","石林":"101290107","澄江":"101290702","江川":"101290703","玉溪":"101290701","峨山":"101290708","通海":"101290704","华宁":"101290705","建水":"101290303","蒙自":"101290309","石屏":"101290302","开远":"101290307","弥勒":"101290304","泸西":"101290311"}
def main():
 city=os.environ["BACKFILL_CITY"]; date=os.environ["BACKFILL_DATE"]; hour=int(os.environ["BACKFILL_HOUR"])
 rainfall=float(os.environ["BACKFILL_RAIN_MM"]); r24=os.environ.get("BACKFILL_RAIN24_MM","").strip()
 rain24=float(r24) if r24 else None; note=os.environ.get("BACKFILL_NOTE","管理员人工补录").strip()[:300]
 if city not in CITIES: raise SystemExit("未知地区")
 if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}",date): raise SystemExit("日期格式必须为YYYY-MM-DD")
 datetime.strptime(date,"%Y-%m-%d")
 if not 0<=hour<=23 or not 0<=rainfall<=1000 or (rain24 is not None and not 0<=rain24<=1000): raise SystemExit("小时或降雨量超出允许范围")
 data=json.loads(DATA_FILE.read_text(encoding="utf-8")); stamp=f"{date}T{hour:02d}:00+08:00"
 row={"city":city,"code":CITIES[city],"observed_at":stamp,"date":date,"hour":hour,"collected_at":f"{date}T{hour:02d}:59+08:00","collection_date":date,"collection_hour":hour,"rain_1h_mm":round(rainfall,1),"rain_24h_mm":round(rain24,1) if rain24 is not None else None,"source":"管理员人工补录","source_url":"","field_1h":"manual_backfill","field_24h":"manual_backfill" if rain24 is not None else None,"status":"manual_backfill","backfill_note":note,"backfilled_by":os.environ.get("GITHUB_ACTOR","repository-admin")}
 obs=data.setdefault("hourly_observations",[]); key=(city,date,hour)
 old=next((x for x in obs if (x.get("city"),x.get("collection_date") or x.get("date"),int(x.get("collection_hour",x.get("hour",-1))))==key),None)
 if old: obs[obs.index(old)]=row
 else: obs.append(row)
 obs.sort(key=lambda x:(x.get("collected_at") or x.get("observed_at",""),x.get("city","")))
 now=datetime.now(BEIJING).replace(second=0,microsecond=0); end=now.replace(minute=0)
 expected=[(end-timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(23,-1,-1)]; report=[]
 for name,code in CITIES.items():
  present=set()
  for item in obs:
   if item.get("city")!=name or item.get("rain_1h_mm") is None: continue
   try: present.add(datetime.fromisoformat(item.get("collected_at") or item["observed_at"]).astimezone(BEIJING).strftime("%Y-%m-%dT%H"))
   except Exception: pass
  missing=[h for h in expected if h not in present]; report.append({"city":name,"code":code,"coverage":24-len(missing),"expected":24,"missing_hours":missing})
 data["missing_hours_report"]={"timezone":"Asia/Shanghai (UTC+08:00)","window_start":expected[0],"window_end":expected[-1],"generated_at":now.isoformat(timespec="minutes"),"cities":report}
 data["updated_at"]=now.isoformat(timespec="minutes"); data["last_manual_backfill"]={"city":city,"date":date,"hour":hour,"rain_1h_mm":round(rainfall,1),"rain_24h_mm":round(rain24,1) if rain24 is not None else None,"by":row["backfilled_by"],"note":note,"updated_at":now.isoformat(timespec="minutes")}
 DATA_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
 print(f"Backfilled {city} {date} {hour:02d}:00 = {rainfall:.1f} mm")
if __name__=="__main__": main()
