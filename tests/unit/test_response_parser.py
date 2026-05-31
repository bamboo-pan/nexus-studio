import json
from pathlib import Path

from aistudio_api.domain.models import parse_chunk_usage, parse_response_chunk, parse_text_output
from aistudio_api.infrastructure.gateway.stream_parser import IncrementalJSONStreamParser, classify_chunk


ROOT = Path(__file__).resolve().parents[1]


def test_parse_text_output_from_stream_bundle():
    raw = (ROOT / "test_output.json").read_text(encoding="utf-8")
    output = parse_text_output(raw)

    assert output.text == "你好！有什么我可以帮你的吗？"
    assert output.thinking.startswith('The user said "你好"')
    assert "Option 3 (Friendly)" in output.thinking
    assert output.usage["prompt_tokens"] == 5
    assert output.usage["completion_tokens"] == 161
    assert output.usage["total_tokens"] == 166
    assert output.usage["completion_tokens_details"]["reasoning_tokens"] == 153
    assert output.response_id
    assert output.candidates[0].finish_reason == 1


def test_parse_response_chunk_and_classify_chunk():
    raw = json.loads((ROOT / "test_output.json").read_text(encoding="utf-8"))
    final_chunk = raw[0][-1]

    candidate = parse_response_chunk(final_chunk)
    usage = parse_chunk_usage(final_chunk)
    assert candidate.finish_reason == 1
    assert candidate.safety_ratings
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 161
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 153

    ctype, text = classify_chunk(raw[0][1])
    assert ctype == "thinking"
    assert 'standard Chinese greeting meaning "Hello."' in text


def test_stream_parser_extracts_real_chunks():
    raw = (ROOT / "test_output.json").read_text(encoding="utf-8")
    parser = IncrementalJSONStreamParser()

    chunks = list(parser.feed(raw))
    assert len(chunks) == 10
    assert classify_chunk(chunks[2])[0] == "thinking"
    assert classify_chunk(chunks[7])[0] == "body"
