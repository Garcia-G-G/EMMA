"""Local sentence embedder for the brain's kNN: multilingual-e5-small, int8 ONNX.

No LLM, no network at inference, no Ollama. Runs on ``onnxruntime`` +
``tokenizers``, both already locked dependencies. Measured on an M3 (BRAIN-1
Part 3): ~0.7 s to load once, ~2.5 ms p50 / ~5 ms p95 per utterance warm.

The model lives in ``~/.emma/models/multilingual-e5-small``; ``fetch()`` pulls
the pinned revision and checks every file's SHA-256. Installing it is BRAIN-2's
installer step; until then a missing model means the kNN source is absent and
the gate defers (``available()`` is False) — never a crash.
"""

from __future__ import annotations

import hashlib
import threading
import urllib.request
from pathlib import Path

import numpy as np
import numpy.typing as npt

MODEL_DIR = Path.home() / ".emma" / "models" / "multilingual-e5-small"
REPO = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
ONNX_FILE = "onnx/model_qint8_avx512_vnni.onnx"
TOKENIZER_FILE = "tokenizer.json"
SHA256 = {  # of the files at REVISION, measured 2026-10-13
    ONNX_FILE: "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88",
    TOKENIZER_FILE: "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
}
DIM = 384
_PREFIX = "query: "  # e5 convention for symmetric similarity
_MAX_TOKENS = 64  # utterances are short; bounds latency on a long transcript

Vector = npt.NDArray[np.float32]


class Embedder:
    """Lazy, thread-safe. One instance per process is enough."""

    def __init__(self, model_dir: Path = MODEL_DIR, threads: int = 2) -> None:
        self.model_dir = model_dir
        self.threads = threads
        self._lock = threading.Lock()
        self._session: object | None = None
        self._tok: object | None = None
        self._needs_type_ids = False

    def available(self) -> bool:
        return (self.model_dir / ONNX_FILE).exists() and (self.model_dir / TOKENIZER_FILE).exists()

    def _load(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        session = ort.InferenceSession(
            str(self.model_dir / ONNX_FILE), so, providers=["CPUExecutionProvider"]
        )
        tok = Tokenizer.from_file(str(self.model_dir / TOKENIZER_FILE))
        tok.enable_truncation(_MAX_TOKENS)
        self._needs_type_ids = any(i.name == "token_type_ids" for i in session.get_inputs())
        self._session, self._tok = session, tok

    def embed(self, texts: list[str]) -> npt.NDArray[np.float32]:
        """(n, 384) L2-normalized rows. Cosine similarity is a dot product."""
        with self._lock:
            if self._session is None:
                self._load()
            session, tok = self._session, self._tok
        assert session is not None and tok is not None
        enc = tok.encode_batch([_PREFIX + t for t in texts])  # type: ignore[attr-defined]
        width = max(len(e.ids) for e in enc)
        ids = np.zeros((len(enc), width), dtype=np.int64)
        mask = np.zeros((len(enc), width), dtype=np.int64)
        for i, e in enumerate(enc):
            ids[i, : len(e.ids)] = e.ids
            mask[i, : len(e.ids)] = e.attention_mask
        feeds = {"input_ids": ids, "attention_mask": mask}
        if self._needs_type_ids:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, feeds)[0]  # type: ignore[attr-defined]
        summed = (hidden * mask[..., None]).sum(axis=1)
        mean = summed / np.maximum(mask.sum(axis=1, keepdims=True), 1)
        norm = np.linalg.norm(mean, axis=1, keepdims=True)
        out: npt.NDArray[np.float32] = (mean / np.maximum(norm, 1e-12)).astype(np.float32)
        return out

    def embed_one(self, text: str) -> Vector:
        row: Vector = self.embed([text])[0]
        return row


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(model_dir: Path = MODEL_DIR, expected: dict[str, str] | None = None) -> dict[str, str]:
    """Download the pinned revision into ``model_dir``; return {file: sha256}.

    If ``expected`` has a non-empty hash for a file and it doesn't match, the
    file is deleted and ``ValueError`` raised — a swapped model never loads.
    """
    want = expected if expected is not None else SHA256
    got: dict[str, str] = {}
    for rel in (ONNX_FILE, TOKENIZER_FILE):
        dst = model_dir / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{rel}"
            tmp = dst.with_name(dst.name + ".part")
            with urllib.request.urlopen(url, timeout=120) as r, tmp.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(dst)
        digest = _sha256(dst)
        if want.get(rel) and want[rel] != digest:
            dst.unlink()
            raise ValueError(f"{rel}: sha256 {digest} != pinned {want[rel]}")
        got[rel] = digest
    return got
