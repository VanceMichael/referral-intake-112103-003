import json
from pathlib import Path
import unittest


class FixtureContractTests(unittest.TestCase):
    def test_sources_have_stable_envelopes(self):
        path = Path(__file__).parents[1] / "fixtures" / "sources.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(document["sources"], list)
        self.assertGreaterEqual(len(document["sources"]), 4)
        for source in document["sources"]:
            self.assertTrue(source["source_id"])
            self.assertTrue(source["record_kind"])
            self.assertTrue(source["collected_at"])
            self.assertIsInstance(source["payload"], dict)


if __name__ == "__main__":
    unittest.main()
