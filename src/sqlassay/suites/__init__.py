"""Benchmark loaders. One module per suite, each owning its own dialect."""

from sqlassay.suites.bird import BIRD_DEFAULT_ROOT, BIRD_REVISIONS, BirdSuite, load_bird_dev

__all__ = ["BIRD_DEFAULT_ROOT", "BIRD_REVISIONS", "BirdSuite", "load_bird_dev"]
