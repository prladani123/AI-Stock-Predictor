from datetime import datetime

from banknifty_foundation.market_memory import FileMarketMemoryRepository, SimilarityEngine


def test_committed_memory_manifest_is_parseable_and_complete_in_count():
    repository = FileMarketMemoryRepository()
    days = repository.days()
    assert len(days) == 50
    assert days[0].trading_date.isoformat() == "2026-09-18"
    assert all(day.causal_evidence_grade in {"A+", "A", "A-", "B", "B-", "C+", "C"} for day in days)
    assert all(day.data_quality for day in days)


def test_episode_state_never_contains_forward_outcomes():
    episodes = FileMarketMemoryRepository().episodes()
    assert episodes
    assert all(datetime.fromisoformat(episode.state["source_max_timestamp"]) <= episode.as_of for episode in episodes)
    assert all("return_30m" not in episode.state for episode in episodes)
    assert all("30" in episode.outcome for episode in episodes)
    assert all("maximum_up_excursion" in episode.outcome["30"] for episode in episodes)


def test_similarity_ignores_unknown_groups_and_returns_real_matches():
    episodes = FileMarketMemoryRepository().episodes()
    current = {**episodes[-1].state, "_as_of": episodes[-1].as_of}
    result = SimilarityEngine().search(current, episodes)
    assert result["available"] is True
    assert result["matchedEpisodes"] == 14
    assert all(datetime.fromisoformat(match["asOf"]) < episodes[-1].as_of for match in result["topMatches"])
