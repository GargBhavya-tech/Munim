from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CORE = ["engine", "actions", "llm", "gate", "memory", "vision", "audit", "voice", "judge", "api", "eval"]
PACKS = ["paytm_merchant", "paytm_consumer", "demo_lender"]
PACK_DIRS = ["playbooks", "mocks", "kb", "policy", "personas", "scenarios/dev", "scenarios/heldout", "labelled"]


def test_core_packages_exist():
    for name in CORE:
        assert (ROOT / "core" / name / "__init__.py").is_file(), name


def test_pack_layout_exists():
    for pack in PACKS:
        for sub in PACK_DIRS:
            assert (ROOT / "packs" / pack / sub).is_dir(), f"{pack}/{sub}"


def test_top_level_dirs_exist():
    for name in ["jobs", "field", "integrations", "models/indic_nli", "dashboard", "tests"]:
        assert (ROOT / name).is_dir(), name


def test_env_is_ignored_and_example_lists_keys():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignore
    example = (ROOT / ".env.example").read_text()
    for key in ["SARVAM_API_KEY", "FALLBACK_LLM_KEY", "MAPS_API_KEY"]:
        assert key in example
