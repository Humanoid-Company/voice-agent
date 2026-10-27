import base64

from server.config import Config
from server.tts import RespeecherTTS, SonioxTTS

CFG = Config(respeecher_api_key="r", soniox_api_key="s")


def test_respeecher_wire():
    tts = RespeecherTTS(CFG, "olesia-conversation")
    start = tts._start_messages("c1", "Привіт.")
    assert start[0]["transcript"] == "Привіт." and start[0]["continue"] is True
    assert start[1]["transcript"] == "" and start[1]["continue"] is False
    assert start[0]["voice"]["id"] == "olesia-conversation"
    assert start[0]["output_format"] == {"encoding": "pcm_s16le", "sample_rate": 24000}
    assert tts._cancel_message("c1") == {"context_id": "c1", "cancel": True}
    pcm = b"\x01\x00\x02\x00"
    ctx, msg = tts._route({"type": "chunk", "context_id": "c1", "data": base64.b64encode(pcm).decode()})
    assert (ctx, msg.kind, msg.audio) == ("c1", "audio", pcm)
    assert tts._route({"type": "done", "context_id": "c1"})[1].kind == "end"
    assert tts._route({"type": "error", "context_id": "c1", "error": "boom"})[1].error == "boom"


def test_soniox_wire():
    tts = SonioxTTS(CFG, "Adrian")
    cfg_msg, text_msg = tts._start_messages("s1", "Привіт.")
    assert cfg_msg["voice"] == "Adrian" and cfg_msg["stream_id"] == "s1" and cfg_msg["audio_format"] == "pcm_s16le"
    assert text_msg == {"stream_id": "s1", "text": "Привіт.", "text_end": True}
    assert tts._cancel_message("s1") == {"stream_id": "s1", "cancel": True}
    pcm = b"\x01\x00"
    # the last chunk carries audio and audio_end together: the audio must not be lost
    _, msg = tts._route({"stream_id": "s1", "audio": base64.b64encode(pcm).decode(), "audio_end": True})
    assert msg.kind == "audio" and msg.audio == pcm
    assert tts._route({"stream_id": "s1", "terminated": True})[1].kind == "end"
    assert tts._route({"stream_id": "s1", "error_code": 400, "error_type": "t", "error_message": "m"})[1].kind == "error"
