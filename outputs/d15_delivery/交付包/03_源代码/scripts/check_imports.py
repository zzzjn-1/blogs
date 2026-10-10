import sys, traceback
sys.path.insert(0, r"D:\podcast-ai\CosyVoice\third_party\Matcha-TTS")
sys.path.insert(0, r"D:\podcast-ai\CosyVoice")

tests = [
    "from lightning import Callback",
    "from matcha.utils.pylogger import get_pylogger",
    "from matcha.models.components.flow_matching import BASECFM",
    "from matcha.models.components.decoder import Decoder",
    "from matcha.hifigan.models import Generator",
    "from cosyvoice.flow.flow_matching import CausalConditionalCFM",
    "from cosyvoice.cli.cosyvoice import CosyVoice2",
]
import importlib
for t in tests:
    try:
        exec(t, {})
        print("OK   ", t)
    except Exception as e:
        print("FAIL ", t, "->", type(e).__name__, str(e)[:200])
print("lightning version:", importlib.import_module("lightning").__version__)
print("DONE")
