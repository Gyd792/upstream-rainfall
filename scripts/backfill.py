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
 # Recalculate both daily business periods for the backfilled date from the shared archive.
 prev=(datetime.strptime(date,"%Y-%m-%d").date()-timedelta(days=1)).isoformat()
 morning_keys=[(prev,h) for h in (21,22,23)]+[(date,h) for h in range(8)]
 evening_keys=[(date,h) for h in range(8,21)]
 morning_values=[]; evening_values=[]; total_values=[]; direct_values=[]; statuses=[]
 for name in CITIES:
  by={}
  for item in obs:
   if item.get("city")!=name: continue
   k=(item.get("collection_date") or item.get("date"),int(item.get("collection_hour",item.get("hour",-1))))
   by[k]=item.get("rain_1h_mm")
  mv=[by.get(k) for k in morning_keys]; ev=[by.get(k) for k in evening_keys]
  morning=sum(mv) if all(v is not None for v in mv) else None
  direct=sum(ev) if all(v is not None for v in ev) else None
  candidates=[x for x in obs if x.get("city")==name and (x.get("collection_date") or x.get("date"))==date and int(x.get("collection_hour",x.get("hour",-1)))==20 and x.get("rain_24h_mm") is not None]
  total=candidates[-1].get("rain_24h_mm") if candidates else None
  if morning is not None and total is not None and total>=morning:
   final=round(total-morning,1); status="matched" if direct is not None and abs(direct-final)<=0.1 else ("corrected_by_24h" if direct is not None else "derived_24h_missing_hourly")
  elif direct is not None:
   final=round(direct,1); status="direct_only_no_24h_validation"
  else:
   final=None; status="missing"
  morning_values.append(round(morning,1) if morning is not None else None)
  direct_values.append(round(direct,1) if direct is not None else None)
  evening_values.append(final); total_values.append(total); statuses.append(status)
 records=data.setdefault("records",[])
 def upsert(period,row):
  old=next((x for x in records if x.get("date")==date and x.get("period")==period),None)
  if old: records[records.index(old)]=row
  else: records.append(row)
 upsert("morning",{"date":date,"period":"morning","values":morning_values,"source":"hourly observed precipitation; includes administrator backfill","updated_at":datetime.now(BEIJING).isoformat(timespec="minutes")})
 upsert("evening",{"date":date,"period":"evening","values":evening_values,"evening_direct_mm":direct_values,"total_24h_mm":total_values,"validation_status":statuses,"source":"hourly observed precipitation; includes administrator backfill and 24-hour validation","updated_at":datetime.now(BEIJING).isoformat(timespec="minutes")})
 records.sort(key=lambda x:(x.get("date",""),x.get("period","")))
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
