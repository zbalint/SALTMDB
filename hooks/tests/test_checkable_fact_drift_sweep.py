import importlib.util
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "saltmdb-checkable-fact-drift-sweep.py"
sys.path.insert(0, str(HOOK.parent))
spec = importlib.util.spec_from_file_location("drift_sweep", HOOK)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TestDriftSweepPrompt(unittest.TestCase):
    def test_metadata_update_step_uses_memory_id(self):
        prompt = module.DRIFT_SWEEP_PROMPT
        self.assertIn("mcp__saltmdb__update_memory_metadata", prompt)
        self.assertIn("memory_id=<id>", prompt)
        self.assertNotIn("store_memory with entity_id", prompt)
        self.assertIn("Never call consolidate_memories, revise_memory, or supersede_memory", prompt)


if __name__ == "__main__":
    unittest.main()
