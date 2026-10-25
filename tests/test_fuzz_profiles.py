import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


_PROFILE_PROBE = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])
import conftest  # noqa: F401
from hypothesis import HealthCheck, settings

profiles = {}
for name in ("ci", "fuzz"):
    profile = settings.get_profile(name)
    profiles[name] = {
        "max_examples": profile.max_examples,
        "derandomize": profile.derandomize,
        "database": profile.database is not None,
        "deadline": profile.deadline is None,
        "suppressed_too_slow": HealthCheck.too_slow in profile.suppress_health_check,
        "suppressed_count": len(profile.suppress_health_check),
    }
print(json.dumps(profiles))
"""


class TestFuzzProfiles(unittest.TestCase):
    def _probe_profiles(self, selected_profile: str) -> dict:
        env = os.environ.copy()
        env.update(CI="true", HYPOTHESIS_PROFILE=selected_profile)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _PROFILE_PROBE,
                str(Path(__file__).resolve().parent),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return json.loads(result.stdout)

    def test_profiles_are_isolated_from_ci_environment(self):
        expected = {
            "ci": {
                "max_examples": 100,
                "derandomize": True,
                "database": False,
                "deadline": True,
                "suppressed_too_slow": True,
                "suppressed_count": 1,
            },
            "fuzz": {
                "max_examples": 5000,
                "derandomize": False,
                "database": True,
                "deadline": True,
                "suppressed_too_slow": False,
                "suppressed_count": 0,
            },
        }
        for selected_profile in ("ci", "fuzz"):
            with self.subTest(selected_profile=selected_profile):
                self.assertEqual(self._probe_profiles(selected_profile), expected)
