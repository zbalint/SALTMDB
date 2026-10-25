import os

from hypothesis import HealthCheck, settings


settings.register_profile(
    "ci",
    parent=settings.get_profile("default"),
    max_examples=100,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "fuzz",
    parent=settings.get_profile("default"),
    max_examples=5000,
    deadline=None,
)
# shortcut: disable per-example deadlines because shared-host wall-clock variance flakes runs;
# upgrade when a per-example time regression matters.
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
