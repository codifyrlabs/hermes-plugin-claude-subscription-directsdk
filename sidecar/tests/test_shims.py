"""The plugin's two lazy Hermes imports resolve to the vendored shims."""
import json


def test_request_body_uses_both_shims():
    import directsdk

    encoded, _manifest, _names = directsdk.request_body({
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "hi"}],
        "extra_body": {"reasoning": {"enabled": True, "effort": "high"}},
    })
    body = json.loads(encoded)
    assert body["output_config"]["effort"] == "high"


def test_normalize_input_schema_strips_nullable_union():
    import directsdk

    schema = {"type": "object", "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
    out = directsdk.normalize_input_schema(schema)
    assert out["properties"]["v"].get("anyOf") is None
