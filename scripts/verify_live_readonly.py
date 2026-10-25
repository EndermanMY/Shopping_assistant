"""Read-only integration check through this application's authenticated API.

Never creates, enables, resumes or submits a purchase task.
"""
import argparse
import json
from pathlib import Path

import httpx


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=18765)
    parser.add_argument("--sku",default="100327520619")
    args=parser.parse_args()
    root=Path(__file__).resolve().parent.parent
    code=json.loads((root/"data"/"access.json").read_text(encoding="utf-8"))["code"]
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}",timeout=90,trust_env=False) as client:
        response=client.post("/api/auth/login",json={"code":code})
        response.raise_for_status()
        result={}
        for name,path in (("product","/api/products/resolve"),("preflight","/api/products/preflight")):
            response=client.post(path,json={"source":args.sku})
            result[name]={"status":response.status_code,"data":response.json()}
            print(json.dumps({name:result[name]},ensure_ascii=True))
            if not response.is_success:
                break
        folder=root/"data"/"qa"
        folder.mkdir(parents=True,exist_ok=True)
        (folder/"live-readonly.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        client.post("/api/auth/logout")
    return 0 if result.get("preflight",{}).get("status")==200 else 1


if __name__=="__main__":
    raise SystemExit(main())
