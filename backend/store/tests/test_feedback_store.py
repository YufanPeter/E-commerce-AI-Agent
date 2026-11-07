from __future__ import annotations

from store.feedback_store import MAX_STORED_ANSWER_CHARS, FeedbackStore


def test_saves_question_and_answer_for_later_review(tmp_path):
    store = FeedbackStore(db_path=tmp_path / "f.sqlite3")
    store.save("m1", "down", session_id="s", user_id="u", query="问", answer="答")
    row = store.get("m1")
    assert (row["rating"], row["query"], row["answer"], row["user_id"]) == ("down", "问", "答", "u")


def test_changing_mind_overwrites_the_rating(tmp_path):
    store = FeedbackStore(db_path=tmp_path / "f.sqlite3")
    store.save("m1", "up", query="问")
    store.save("m1", "down")
    row = store.get("m1")
    assert row["rating"] == "down"
    assert row["query"] == "问"


def test_very_long_answer_is_truncated(tmp_path):
    store = FeedbackStore(db_path=tmp_path / "f.sqlite3")
    store.save("m1", "up", answer="字" * (MAX_STORED_ANSWER_CHARS + 10))
    assert len(store.get("m1")["answer"]) == MAX_STORED_ANSWER_CHARS
