import pytest

from aistudio_api.domain.model_capabilities import (
    clear_dynamic_model_capabilities,
    get_model_metadata,
    get_model_capabilities,
    list_model_metadata,
    plan_image_generation,
    register_dynamic_models,
    validate_chat_capabilities,
)


SQUARE_PROMPT_SUFFIX = "Use a square 1:1 composition."


def test_model_metadata_exposes_capabilities_and_image_sizes():
    metadata = get_model_metadata("gemini-3.1-flash-image-preview")

    assert metadata["capabilities"]["image_output"] is True
    assert metadata["capabilities"]["file_input"] is False
    assert metadata["capabilities"]["file_input_mime_types"] == []
    assert metadata["capabilities"]["structured_output"] is False
    assert metadata["capabilities"]["tool_calls"] is False
    assert "media_resolution" in metadata["capabilities"]["unsupported_generation_fields"]
    assert "media_resolution" in metadata["unsupported_generation_fields"]
    assert metadata["image_generation"]["response_formats"] == ["b64_json", "url"]
    assert {item["size"] for item in metadata["image_generation"]["sizes"]} == {
        "512x512",
        "1024x1024",
        "1024x1792",
        "1792x1024",
    }
    assert metadata["image_generation"]["defaults"] == {
        "size": "1024x1024",
        "n": 1,
        "response_format": "b64_json",
    }
    parameters = metadata["image_generation"]["parameters"]
    assert parameters["n"] == {"type": "integer", "minimum": 1, "maximum": 10, "default": 1}
    assert parameters["response_format"]["enum"] == ["b64_json", "url"]
    assert set(parameters["size"]["enum"]) == {item["size"] for item in metadata["image_generation"]["sizes"]}
    assert {"quality", "style", "background", "output_format"} <= set(metadata["image_generation"]["unsupported_fields"])
    assert metadata["image_generation"]["ignored_fields"] == ["user"]


def test_model_metadata_exposes_pro_image_sizes_only_for_pro_model():
    flash_metadata = get_model_metadata("gemini-3.1-flash-image-preview")
    pro_metadata = get_model_metadata("gemini-3-pro-image-preview")
    flash_sizes = {item["size"] for item in flash_metadata["image_generation"]["sizes"]}
    pro_sizes = {item["size"]: item for item in pro_metadata["image_generation"]["sizes"]}

    pro_only_sizes = {
        "2048x2048",
        "1536x2816",
        "2816x1536",
        "4096x4096",
        "2304x4096",
        "4096x2304",
    }
    assert flash_sizes.isdisjoint(pro_only_sizes)
    assert pro_only_sizes <= set(pro_sizes)
    assert pro_sizes["2048x2048"]["output_image_size"] == "2K"
    assert pro_sizes["2816x1536"]["output_image_size"] == "2K"
    assert pro_sizes["4096x4096"]["output_image_size"] == "4K"

    listed = {item["id"]: item for item in list_model_metadata()}
    listed_flash_sizes = {item["size"] for item in listed["gemini-3.1-flash-image-preview"]["image_generation"]["sizes"]}
    listed_pro_sizes = {item["size"] for item in listed["gemini-3-pro-image-preview"]["image_generation"]["sizes"]}
    assert listed_flash_sizes.isdisjoint(pro_only_sizes)
    assert pro_only_sizes <= listed_pro_sizes


def test_chat_capability_validation_rejects_image_input_for_gemma():
    with pytest.raises(ValueError, match="image input"):
        validate_chat_capabilities(
            "gemma-4-31b-it",
            has_image_input=True,
            uses_tools=False,
            uses_search=False,
            uses_thinking=False,
            stream=False,
        )


def test_model_metadata_exposes_structured_output_for_text_model():
    metadata = get_model_metadata("gemini-3-flash-preview")

    assert metadata["capabilities"]["text_output"] is True
    assert metadata["capabilities"]["file_input"] is True
    assert "application/pdf" in metadata["capabilities"]["file_input_mime_types"]
    assert metadata["capabilities"]["structured_output"] is True
    assert metadata["capabilities"]["tool_calls"] is True


def test_model_metadata_includes_new_gemini_flash_model():
    metadata = get_model_metadata("gemini-3.5-flash")

    assert metadata["id"] == "gemini-3.5-flash"
    assert metadata["capabilities"]["text_output"] is True
    assert metadata["capabilities"]["streaming"] is True
    assert any(item["id"] == "gemini-3.5-flash" for item in list_model_metadata())


def test_model_metadata_lists_real_playground_default_before_newer_flash_alias():
    ids = [item["id"] for item in list_model_metadata()]

    assert ids.index("gemini-3-flash-preview") < ids.index("gemini-3.5-flash")


def test_register_dynamic_models_makes_discovered_text_model_strictly_available():
    clear_dynamic_model_capabilities()
    try:
        register_dynamic_models(["models/gemini-dynamic-preview"])

        capabilities = get_model_capabilities("gemini-dynamic-preview", strict=True)
        assert capabilities.id == "gemini-dynamic-preview"
        assert capabilities.text_output is True
        assert any(item["id"] == "gemini-dynamic-preview" for item in list_model_metadata())
    finally:
        clear_dynamic_model_capabilities()


def test_chat_capability_validation_rejects_file_input_for_gemma():
    with pytest.raises(ValueError, match="file input"):
        validate_chat_capabilities(
            "gemma-4-31b-it",
            has_image_input=False,
            has_file_input=True,
            file_input_mime_types=("application/pdf",),
            uses_tools=False,
            uses_search=False,
            uses_thinking=False,
            stream=False,
        )


def test_chat_capability_validation_rejects_unsupported_file_mime_type():
    with pytest.raises(ValueError, match="application/x-msdownload"):
        validate_chat_capabilities(
            "gemini-3-flash-preview",
            has_image_input=False,
            has_file_input=True,
            file_input_mime_types=("application/x-msdownload",),
            uses_tools=False,
            uses_search=False,
            uses_thinking=False,
            stream=False,
        )


def test_chat_capability_validation_rejects_structured_output_when_unavailable():
    with pytest.raises(ValueError, match="structured output"):
        validate_chat_capabilities(
            "gemini-3.1-flash-tts-preview",
            has_image_input=False,
            uses_tools=False,
            uses_search=False,
            uses_thinking=False,
            stream=False,
            uses_structured_output=True,
        )


@pytest.mark.parametrize(
    ("size", "output_image_size"),
    [
        ("512x512", "512"),
        ("1024x1024", "1K"),
        ("1024x1792", "1K"),
        ("1792x1024", "1K"),
    ],
)
def test_flash_image_generation_plan_maps_supported_size_to_wire_config(size, output_image_size):
    plan = plan_image_generation("models/gemini-3.1-flash-image-preview", size)

    assert plan.model == "gemini-3.1-flash-image-preview"
    assert plan.generation_config_overrides == {"output_image_size": [None, output_image_size]}


@pytest.mark.parametrize(
    ("size", "output_image_size"),
    [
        ("2048x2048", "2K"),
        ("1536x2816", "2K"),
        ("2816x1536", "2K"),
        ("4096x4096", "4K"),
        ("2304x4096", "4K"),
        ("4096x2304", "4K"),
    ],
)
def test_pro_image_generation_plan_maps_high_resolution_sizes_to_wire_config(size, output_image_size):
    plan = plan_image_generation("models/gemini-3-pro-image-preview", size)

    assert plan.model == "gemini-3-pro-image-preview"
    assert plan.generation_config_overrides == {"output_image_size": [None, output_image_size]}


@pytest.mark.parametrize(
    ("model", "size", "output_image_size"),
    [
        ("models/gemini-3.1-flash-image-preview", "512x512", "512"),
        ("models/gemini-3.1-flash-image-preview", "1024x1024", "1K"),
        ("models/gemini-3-pro-image-preview", "2048x2048", "2K"),
        ("models/gemini-3-pro-image-preview", "4096x4096", "4K"),
    ],
)
def test_square_image_generation_plan_adds_square_prompt_suffix(model, size, output_image_size):
    plan = plan_image_generation(model, size)

    assert plan.generation_config_overrides == {"output_image_size": [None, output_image_size]}
    assert plan.prompt_suffix == SQUARE_PROMPT_SUFFIX
    assert plan.prompt_for("draw") == f"draw\n\n{SQUARE_PROMPT_SUFFIX}"


def test_image_generation_plan_rejects_unsupported_size():
    with pytest.raises(ValueError, match="Supported sizes"):
        plan_image_generation("gemini-3.1-flash-image-preview", "256x256")


def test_flash_image_generation_plan_rejects_pro_high_resolution_size():
    with pytest.raises(ValueError, match="2048x2048"):
        plan_image_generation("gemini-3.1-flash-image-preview", "2048x2048")