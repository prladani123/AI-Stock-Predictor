from pathlib import Path


def test_phase6_dependencies_are_minimal_pinned_and_do_not_include_pandas():
    content = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    for dependency in (
        "numpy==2.2.6",
        "scikit-learn==1.7.2",
        "joblib==1.5.2",
        "lightgbm==4.6.0",
    ):
        assert dependency in content
    assert '"pandas' not in content
