"""Offline regression tests for the standalone biome-assets script."""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT=Path(__file__).resolve().parents[1]
MODULE_PATH=_ROOT/"biome_assets.py"
spec=importlib.util.spec_from_file_location("biome_assets_standalone",MODULE_PATH)
biome_assets=importlib.util.module_from_spec(spec)
sys.modules["biome_assets_standalone"]=biome_assets
spec.loader.exec_module(biome_assets)
for name in (
    "connect", "load_catalog", "source_is_free", "normalize", "role_of", "quality",
    "search", "shortlist", "annotate", "BIOMES", "find_direct", "inspect_acquisition",
    "safe_filename", "acquire", "_host_allowed", "_parse_result", "review_candidates",
):
    globals()[name]=getattr(biome_assets,name)

FIXTURE=Path(__file__).parents[1]/"examples"/"biome_assets_sample_catalog.jsonl"

class UtilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/"test.db")
        self.assertEqual(load_catalog(self.db,FIXTURE),9)
    def tearDown(self):
        self.db.close(); self.temp.cleanup()
    def test_free_only(self):
        self.assertEqual(self.db.execute("select count(*) from assets where free_candidate=1").fetchone()[0],8)
        self.assertEqual(self.db.execute("select count(*) from assets where free_verified=1").fetchone()[0],1)
        self.assertFalse(source_is_free({"isFree":False,"licence":"Fab Standard License"})[0])
        self.assertTrue(source_is_free({"licence":"CC0-1.0"})[1])
    def test_roles(self):
        roles={}
        for r in self.db.execute("select raw from assets"):
            a=json.loads(r["raw"]);roles[a["asset_id"]]=role_of(a)
        self.assertEqual(roles["test:oak-mature"],"canopy")
        self.assertEqual(roles["test:fern"],"understory")
        self.assertEqual(roles["test:log"],"debris")
        self.assertEqual(roles["test:soil"],"surface")
        self.assertEqual(roles["test:rock"],"rocks")
    def test_quality_filter(self):
        results=search(self.db,"forest with trees",role="canopy",min_quality=55)
        self.assertNotIn("test:lowpoly",[r["asset"]["asset_id"] for r in results])
        self.assertNotIn("test:priced",[r["asset"]["asset_id"] for r in results])
    def test_plan(self):
        m=shortlist(self.db,"temperate forest with oak trees moss and fern ground cover",min_quality=50)
        self.assertEqual(m["biome"],"forest")
        by_role={i["role"] for i in m["assets"]}
        self.assertTrue({"canopy","understory","groundcover","rocks","debris","surface"}<=by_role)
        self.assertNotIn("test:priced",[i["asset_id"] for i in m["assets"]])
        self.assertEqual(m,shortlist(self.db,"temperate forest with oak trees moss and fern ground cover",min_quality=50))
    def test_cache_agent_assessments(self):
        self.assertEqual(annotate(self.db,[{"asset_id":"test:oak-mature","role":"canopy","approved":False,"confidence":1.0}]),1)
        m=shortlist(self.db,"forest oak tree",min_quality=40)
        self.assertNotIn("test:oak-mature",[a["asset_id"] for a in m["assets"]])
        self.assertEqual(annotate(self.db,[{"asset_id":"invented","role":"canopy","approved":True,"confidence":1.0}]),0)
    def test_download_policy(self):
        rows={json.loads(row["raw"])["asset_id"]:json.loads(row["raw"]) for row in self.db.execute("select raw from assets")}
        self.assertEqual(inspect_acquisition(rows["test:oak-mature"])["method"],"manual_fab")
        self.assertEqual(inspect_acquisition(rows["test:priced"])["method"],"unavailable")
        self.assertFalse(_host_allowed("http://ambientcg.com/malware.zip"))
        self.assertFalse(_host_allowed("https://ambientcg.com.evil.example/bad"))
        self.assertTrue(_host_allowed("https://ambientcg.com/get?file=good.zip"))
        self.assertEqual(find_direct(rows["test:soil"]),None)
        self.assertTrue(safe_filename("ab/cd","https://ambientcg.com/test.zip").endswith(".zip"))
    def test_dry_run_no_writes(self):
        m=shortlist(self.db,"forest",min_quality=40)
        folder=Path(self.temp.name)/"assets"
        out=acquire(self.db,m,folder,limit=2,dry_run=True)
        self.assertFalse(folder.exists())
        self.assertGreater(len(out["results"]),0)
    def test_agent_response(self):
        result=_parse_result(json.dumps({"is_error":False,"result":"```json\n{\"assessments\":[{\"asset_id\":\"test:oak-mature\",\"role\":\"canopy\",\"approved\":true,\"confidence\":0.9}]}\n```"}))
        self.assertEqual(len(result["assessments"]),1)
        m=shortlist(self.db,"forest",min_quality=40)
        with patch.object(biome_assets.subprocess, "run") as run:
            run.return_value=MagicMock(returncode=0,stdout=json.dumps({"result":json.dumps({"assessments":[{"asset_id":"test:oak-mature","role":"canopy","approved":True,"confidence":.9},{"asset_id":"invented","role":"rocks","approved":True,"confidence":.99}]})}),stderr="")
            info=review_candidates(self.db,m)
            self.assertEqual(info["reviewed"],1)
            cmd=run.call_args.args[0]
            self.assertEqual(cmd[:2],["agent","-p"])
            self.assertIn("composer-2.5",cmd)
            self.assertIn("--mode=ask",cmd)

if __name__=="__main__": unittest.main()
