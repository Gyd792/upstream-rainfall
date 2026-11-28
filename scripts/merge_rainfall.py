#!/usr/bin/env python3
import json, sys
from pathlib import Path
from datetime import datetime

def ts(v):
    try: return datetime.fromisoformat(str(v).replace("Z","+00:00")).isoformat()
    except Exception: return str(v or "")

def obs_key(x):
    t=ts(x.get("observed_at"))
    if t: return (str(x.get("city","")),t[:13])
    d=x.get("observation_date") or x.get("date"); h=x.get("observation_hour",x.get("hour"))
    return (str(x.get("city","")),f"{d}T{int(h):02d}") if d is not None and h is not None else None

def merge_obs(a,b):
    out={}
    for x in list(a or [])+list(b or []):
        k=obs_key(x)
        if k is None: continue
        p=out.get(k)
        rank=lambda z:(z.get("rain_1h_mm") is not None,ts(z.get("observed_at")),ts(z.get("collected_at")))
        if p is None or rank(x)>=rank(p): out[k]=x
    return sorted(out.values(),key=lambda x:(ts(x.get("observed_at")),x.get("city","")))

def merge_summaries(a,b,keys):
    out={}
    for x in list(a or [])+list(b or []):
        k=tuple(x.get(f) for f in keys); p=out.get(k)
        if p is None: out[k]=x; continue
        newer,older=(x,p) if ts(x.get("snapshot_at") or x.get("updated_at"))>=ts(p.get("snapshot_at") or p.get("updated_at")) else (p,x)
        z=dict(older); z.update(newer)
        for f in ("values","total_24h_mm","validation_status"):
            av,bv=older.get(f),newer.get(f)
            if isinstance(av,list) and isinstance(bv,list):
                z[f]=[bv[i] if i<len(bv) and bv[i] is not None else (av[i] if i<len(av) else None) for i in range(max(len(av),len(bv)))]
        out[k]=z
    return list(out.values())

def main():
    local_path,remote_path=map(Path,sys.argv[1:3])
    local=json.loads(local_path.read_text(encoding="utf-8"))
    remote=json.loads(remote_path.read_text(encoding="utf-8")) if remote_path.exists() else {}
    d=dict(remote); d.update(local)
    d["hourly_observations"]=merge_obs(remote.get("hourly_observations"),local.get("hourly_observations"))
    d["records"]=merge_summaries(remote.get("records"),local.get("records"),("date","period"))
    d["summary_history"]=merge_summaries(remote.get("summary_history"),local.get("summary_history"),("date","period","snapshot_at"))
    for f,k in (("last_collection","checked_at"),("missing_hours_report","generated_at"),("backfill_report","checked_at")):
        a,b=remote.get(f),local.get(f)
        if isinstance(a,dict) and isinstance(b,dict): d[f]=b if ts(b.get(k))>=ts(a.get(k)) else a
    d["updated_at"]=max(ts(remote.get("updated_at")),ts(local.get("updated_at")))
    local_path.write_text(json.dumps(d,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
if __name__=="__main__": main()
