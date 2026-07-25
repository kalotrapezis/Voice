"""Self-check for the speech-model RAM gate (python3 test_model_ram.py)."""
import voice_keyboard as vk


def with_mem(total, avail):
    vk.mem_gb = lambda: (total, avail)


real_mem = vk.mem_gb
try:
    # roomy machine: everything fine
    with_mem(32, 24)
    assert all(vk.model_ram_check(k)[0] == "ok" for k in vk.WHISPER_MODELS)

    # 8 GB box with half free: large-q5 (3.9 GB) eats more than half of 4 GB
    with_mem(8, 4)
    assert vk.model_ram_check("small")[0] == "ok"
    assert vk.model_ram_check("large-q5")[0] == "warn"

    # busy machine: not enough free RAM right now
    with_mem(8, 2)
    assert vk.model_ram_check("medium")[0] == "block"

    # tiny machine: can't ever run the big ones
    with_mem(4, 4)
    assert vk.model_ram_check("large-q5")[0] == "block"

    # unmeasurable /proc/meminfo must not block anyone
    with_mem(0, 0)
    assert vk.model_ram_check("medium")[0] == "ok"

    # selection falls back to small when the model can't run / isn't installed
    vk.load_config = lambda: {"STT_MODEL": "large-q5"}
    with_mem(4, 4)
    assert vk.whisper_model().endswith("ggml-small.bin")
finally:
    vk.mem_gb = real_mem

print("ok")
