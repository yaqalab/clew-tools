import numpy as np

from alefa_stt.segmenter import EnergyVad, Segment, Segmenter, SpeechStarted

from conftest import speechlike


def run(audio, max_seconds=5.0, finalize=True, **kw):
    seg = Segmenter(EnergyVad(), max_seconds, **kw)
    events = seg.feed(audio)
    if finalize:
        events += seg.finalize()
    return events


def segments(events):
    return [e for e in events if isinstance(e, Segment)]


def test_two_utterances_split_on_silence():
    audio = speechlike([(1, False), (1, True), (1, False), (1.5, True), (1, False)])
    events = run(audio)
    pieces = segments(events)
    assert len(pieces) == 2
    assert all(p.speech_final and not p.from_finalize for p in pieces)
    # Starts include up to 300 ms of pre-roll before the onset.
    assert 0.6 <= pieces[0].start <= 1.0
    assert 2.6 <= pieces[1].start <= 3.0
    assert 1.0 <= pieces[0].duration <= 1.6
    starts = [e.timestamp for e in events if isinstance(e, SpeechStarted)]
    assert starts == [p.start for p in pieces]


def test_long_speech_is_cut_at_a_quiet_point_within_the_window():
    # 12 s of talking with a short dip every 1.4 s: never 600 ms of silence.
    pattern = [(0.5, False)] + [(1.3, True), (0.1, False)] * 9 + [(1, False)]
    pieces = segments(run(speechlike(pattern)))
    assert len(pieces) >= 3
    assert all(p.duration <= 5.0 + 1e-9 for p in pieces)
    assert [p.speech_final for p in pieces][-1] is True
    assert not any(p.speech_final for p in pieces[:-1])
    # Pieces follow each other with no gap and no overlap.
    for a, b in zip(pieces, pieces[1:]):
        assert abs(a.start + a.duration - b.start) < 1e-6


def test_clicks_are_not_utterances():
    audio = speechlike([(1, False), (0.1, True), (1, False), (0.12, True), (1, False)])
    assert segments(run(audio)) == []


def test_finalize_flushes_the_utterance_in_progress():
    audio = speechlike([(0.5, False), (1.2, True)])
    seg = Segmenter(EnergyVad(), 5.0)
    assert segments(seg.feed(audio)) == []
    pieces = segments(seg.finalize())
    assert len(pieces) == 1 and pieces[0].from_finalize and pieces[0].speech_final


def test_feeding_in_odd_sized_chunks_gives_the_same_result():
    audio = speechlike([(1, False), (1, True), (1, False), (1.5, True), (1, False)])
    whole = segments(run(audio))
    seg = Segmenter(EnergyVad(), 5.0)
    events = []
    for i in range(0, len(audio), 777):
        events += seg.feed(audio[i : i + 777])
    events += seg.finalize()
    chunked = segments(events)
    assert [(p.start, len(p.audio)) for p in chunked] == [(p.start, len(p.audio)) for p in whole]
    assert np.array_equal(chunked[0].audio, whole[0].audio)


def test_endpointing_controls_the_pause_length():
    audio = speechlike([(0.5, False), (1, True), (0.4, False), (1, True), (1, False)])
    assert len(segments(run(audio, endpointing_ms=600))) == 1
    assert len(segments(run(audio, endpointing_ms=300))) == 2


def test_cut_pieces_lose_the_period_whisper_adds():
    from alefa_stt.engine import continued, tidy
    assert continued("This study examines how learners negotiate.") == "This study examines how learners negotiate"
    assert continued("Is that so?") == "Is that so?"
    assert continued("and then...") == "and then..."
    assert tidy("We agree.  We agree. Fine") == "We agree."


def test_decoder_loops_are_cut_but_real_repetition_is_kept():
    from alefa_stt.engine import tidy
    looped = ("Although the effect on grammatical complexity was limited, "
              "although the effect on grammatical complexity was")
    assert tidy(looped) == "Although the effect on grammatical complexity was limited."
    assert tidy("the more you practice, the more you learn") == "the more you practice, the more you learn"
    assert tidy("We collected data from 42 participants over one academic semester.") == \
        "We collected data from 42 participants over one academic semester."
    assert tidy("so so so so") == "so so so so"  # short stutters are left alone
