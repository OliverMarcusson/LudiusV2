import hashlib
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch

DIR = Path(__file__).resolve().parents[2] / "models" / "omniparser-v2.0" / "icon_detect_v3"
PT_SHA256 = "11c6cbb77f22569fab22d86c76407a83ec81ab89dbfe28279854822d6e3fb00c"
SIZE = 1280


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    pt, onnx = DIR / "model.pt", DIR / "model.onnx"
    if sha256(pt) != PT_SHA256:
        print(f"{pt}: unexpected sha256, refusing to export", file=sys.stderr)
        return 1
    model = torch.jit.load(str(pt), map_location="cpu").eval()
    x = torch.rand(1, 3, SIZE, SIZE)
    names = [f"{kind}{s}" for s in (8, 16, 32) for kind in ("cls", "box")]
    torch.onnx.export(
        model, (x,), str(onnx), input_names=["images"], output_names=names, opset_version=17, dynamo=False
    )
    with torch.no_grad():
        want = [t.numpy() for t in model(x)]
    got = ort.InferenceSession(str(onnx), providers=["CPUExecutionProvider"]).run(None, {"images": x.numpy()})
    diff = max(float(np.abs(a - b).max()) for a, b in zip(want, got, strict=True))
    print("outputs", [tuple(a.shape) for a in got], "max abs diff vs torch", diff)
    print("model.onnx", onnx.stat().st_size, "bytes, sha256", sha256(onnx))
    return 0 if diff < 1e-3 else 1


if __name__ == "__main__":
    sys.exit(main())
