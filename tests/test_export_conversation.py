import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from export_conversation import split_case


def test_split_case_last_user_turn_becomes_message():
    messages = [
        {"role": "user", "content": "hola busco piso"},
        {"role": "assistant", "content": "¿Liso o con diseño?"},
        {"role": "user", "content": "liso 2mm"},
        {"role": "assistant", "content": "respuesta mala del bot"},
    ]
    history, message = split_case(messages)
    assert message == "liso 2mm"
    assert history == [
        {"role": "user", "content": "hola busco piso"},
        {"role": "assistant", "content": "¿Liso o con diseño?"},
    ]


def test_split_case_joins_trailing_user_burst_and_drops_bad_reply():
    messages = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola! ¿Qué buscás?"},
        {"role": "user", "content": "piso liso"},
        {"role": "user", "content": "2mm"},
        {"role": "user", "content": "20 m2"},
        {"role": "assistant", "content": "respuesta mala"},
    ]
    history, message = split_case(messages)
    assert message == "piso liso\n2mm\n20 m2"
    assert history[-1] == {"role": "assistant", "content": "¡Hola! ¿Qué buscás?"}


def test_split_case_single_user_message():
    history, message = split_case([{"role": "user", "content": "tenés botas?"}])
    assert history == []
    assert message == "tenés botas?"
