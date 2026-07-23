from backend.models import model_id_from_spec


def test_coordinator_model_spec_is_normalized_for_codex_app_server():
    assert model_id_from_spec("codex/gpt-5.5") == "gpt-5.5"
    assert model_id_from_spec("gpt-5.4") == "gpt-5.4"
