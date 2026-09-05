"""OCR engine: RapidOCR (PP-OCRv5) with a Polish-capable Latin recognition model.

Runs on the CPU. A DirectML path is still here behind use_gpu=True — it reads a
page in roughly a third of the time with identical output — but the app does not
use it.
"""

import os
import re
import sys
import threading

import requests

# The recognition model that ships with RapidOCR is Chinese and mangles Polish
# ("dlugopisy zelowe" instead of "długopisy żelowe"). PP-OCRv5's Latin model
# covers 46 Latin-script languages including Polish.
REC_MODEL_URL = (
    "https://huggingface.co/PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx/"
    "resolve/main/inference.onnx"
)
REC_CONFIG_URL = (
    "https://huggingface.co/PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx/"
    "resolve/main/inference.yml"
)

# Detection downscales to this long side. 1280 costs ~30% more time for +0.4%
# recognised characters on these leaflets, so 736 stays.
DET_LIMIT_SIDE_LEN = 736
REC_BATCH_NUM = 16

_engine = None
_engine_lock = threading.Lock()
_backend = None


def current_backend():
    """'DirectML', 'CPU', or None before the engine is built."""
    return _backend


def _log(msg):
    print(f"[OCR] {msg}", file=sys.stderr, flush=True)


def ensure_models(models_dir):
    """Download the Latin recognition model and build its character dictionary."""
    os.makedirs(models_dir, exist_ok=True)
    model_path = os.path.join(models_dir, "latin_rec.onnx")
    config_path = os.path.join(models_dir, "latin_rec.yml")
    keys_path = os.path.join(models_dir, "latin_dict.txt")

    if not os.path.isfile(model_path):
        _log("Pobieram model latin_PP-OCRv5_mobile_rec (8 MB)...")
        resp = requests.get(REC_MODEL_URL, timeout=120)
        resp.raise_for_status()
        with open(model_path, "wb") as f:
            f.write(resp.content)

    if not os.path.isfile(config_path):
        resp = requests.get(REC_CONFIG_URL, timeout=60)
        resp.raise_for_status()
        with open(config_path, "wb") as f:
            f.write(resp.content)

    if not os.path.isfile(keys_path):
        # The dictionary is not embedded in the ONNX metadata, so extract it
        # from the inference config. Order matters: CTC decoding indexes into it.
        with open(config_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        start = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("character_dict:")
        )
        chars = []
        for line in lines[start + 1:]:
            match = re.match(r"^  - (.*)$", line)
            if not match:
                break
            char = match.group(1)
            if len(char) >= 2 and char[0] == char[-1] and char[0] in "'\"":
                char = char[1:-1]
            chars.append(char)
        with open(keys_path, "w", encoding="utf-8") as f:
            f.write("\n".join(chars) + "\n")
        _log(f"Słownik znaków: {len(chars)} pozycji")

    return model_path, keys_path


def _patch_execution_provider(use_gpu):
    """Force RapidOCR's ONNX sessions onto DirectML.

    RapidOCR hardcodes CUDA-or-CPU, so the session class it imported has to be
    swapped before any session is constructed.
    """
    import onnxruntime as ort

    ort.set_default_logger_severity(3)
    available = ort.get_available_providers()
    if not use_gpu or "DmlExecutionProvider" not in available:
        return "CPU"

    import rapidocr_onnxruntime.utils as rapid_utils

    base_session = ort.InferenceSession

    class DirectMLSession(base_session):
        def __init__(self, path, sess_options=None, providers=None, **kwargs):
            providers = [
                ("DmlExecutionProvider", {"device_id": 0}),
                "CPUExecutionProvider",
            ]
            super().__init__(path, sess_options=sess_options,
                             providers=providers, **kwargs)

    rapid_utils.InferenceSession = DirectMLSession
    return "DirectML"


def get_engine(models_dir, use_gpu=False):
    """Build the OCR engine once per process.

    Must be called from the main thread before any worker thread uses it:
    building an ONNX Runtime session from a background thread deadlocks here.
    """
    global _engine, _backend
    with _engine_lock:
        if _engine is not None:
            return _engine

        model_path, keys_path = ensure_models(models_dir)
        backend = _patch_execution_provider(use_gpu)

        import cv2
        from rapidocr_onnxruntime import RapidOCR
        from rapidocr_onnxruntime.ch_ppocr_v3_rec import TextRecognizer

        if backend == "CPU":
            # One thread per process; the caller spreads pages over processes.
            cv2.setNumThreads(1)

        # det_model_path=None is required: passing any det_* argument without it
        # raises KeyError: 'model_path' in rapidocr-onnxruntime 1.2.3.
        engine = RapidOCR(
            det_model_path=None,
            det_limit_side_len=DET_LIMIT_SIDE_LEN,
            use_cls=False,
        )
        # RapidOCR ignores rec_keys_path, which would leave the Chinese
        # dictionary decoding a Latin model. Replace the recognizer outright.
        engine.text_recognizer = TextRecognizer({
            "use_cuda": False,
            "model_path": model_path,
            "keys_path": keys_path,
            "rec_img_shape": [3, 48, 320],
            "rec_batch_num": REC_BATCH_NUM,
        })

        _log(f"Backend: {backend}")
        _backend = backend
        _engine = engine
        return _engine


def ocr_image_bytes(image_bytes, models_dir, use_gpu=False):
    """Return (text, boxes) for one page image.

    boxes is a list of (text, score, [x0, y0, x1, y1]) for highlighting hits.
    """
    import cv2
    import numpy as np

    engine = get_engine(models_dir, use_gpu)
    buf = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return None, []

    result, _ = engine(img)
    if not result:
        return "", []

    boxes = []
    parts = []
    for polygon, text, score in result:
        parts.append(text)
        xs = [point[0] for point in polygon]
        ys = [point[1] for point in polygon]
        boxes.append((text, float(score),
                      [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]))
    return " ".join(parts), boxes
