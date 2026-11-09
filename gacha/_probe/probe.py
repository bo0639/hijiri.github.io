"""一時: 確認先候補のページを取得して保存する（調査用、マージ前に削除）。"""
import os, sys, time, urllib.request
sys.path.insert(0, "scripts")
from update_gacha_lineup import fetch
out = "gacha/_probe/pages"
os.makedirs(out, exist_ok=True)
log = []
for line in open("gacha/_probe/urls.txt", encoding="utf-8"):
    key, url = line.rstrip("\n").split("\t")
    try:
        body = fetch(url)
        open(f"{out}/{key}.html", "w", encoding="utf-8").write(body)
        log.append(f"{key}\tOK\t{len(body)}\t{url}")
    except Exception as e:
        log.append(f"{key}\tNG\t{e}\t{url}")
    time.sleep(1)
open("gacha/_probe/result.txt", "w", encoding="utf-8").write("\n".join(log) + "\n")
print("\n".join(log))
