from pathlib import Path
import json
import sys
from html.parser import HTMLParser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
from engine import export_markdown


class Parser(HTMLParser):
    def __init__(self): super().__init__(); self.elements=[]
    def handle_starttag(self, tag, attrs): self.elements.append({"tag":tag,"attrs":dict(attrs)})


parser=Parser()
parser.feed((ROOT/"static"/"index.html").read_text(encoding="utf-8"))
ws=app.run_baseline()
data={"elements":parser.elements,
      "bootstrap":{"workspace":ws.public(),"workspaces":[{"id":"official","title":ws.title,"kind":"official","turn_count":28}],
                   "categories":app.CATEGORIES,"statuses":app.STATUSES,"rules":app.RULES,"model":app.MODEL.public()},
      "exports":{str(i+1):export_markdown(ws,t["snapshot"]) for i,t in enumerate(ws.turns)}}
target=ROOT/".test-tmp"/"ui-fixture.json"
target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
print("Prepared local UI test fixture.")
