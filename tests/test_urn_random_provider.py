import argparse
import importlib.util
import os
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URNPOOL = os.path.join(REPO, "pools", "urn-pool", "urnpool.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class UrnRandomProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load("urnpool_random", URNPOOL)

    def test_scored_pool_only(self):
        scored_ids = {"fresh-id", "stale-id"}
        with tempfile.TemporaryDirectory() as tmp:
            hub = os.path.join(tmp, "hub-open.json")
            doc = {"updated_at": time.time(), "providers": [
                {"id": "fresh-id", "sp": 9.0, "fail": 0, "sp_ts": time.time()},
                {"id": "stale-id", "sp": 4.0, "fail": 0, "sp_ts": 0},
                {"id": "rest-id", "sp": None, "fail": 0, "sp_ts": None},
            ]}
            with open(hub, "w", encoding="utf-8") as fh:
                import json
                json.dump(doc, fh)
            self.mod.HUB_OPEN = hub
            wrapper = self.mod.Wrapper(argparse.Namespace(db=os.path.join(tmp, "db.sqlite")))
            picks = {wrapper.random_provider() for _ in range(20)}
        self.assertLessEqual(picks, scored_ids)
        self.assertTrue(picks & scored_ids)


if __name__ == "__main__":
    unittest.main()
