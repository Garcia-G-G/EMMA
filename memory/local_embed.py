"""Memory's own local embedder (Step 42). The brain keeps e5; this one is memory's.

Chosen in Step 42 Part 1 on ``tests/memory_recall_set.py``: e5 failed the
expanded set (recall@1 116/152; at a cosine gate tuned for 0 wrong on one half,
5 wrong on the other), paraphrase-multilingual-MiniLM-L12-v2 did better on every
measure (141/152; 1). Apache-2.0 (model card), commercial use allowed. Pinned
revision, SHA-256 per file, fetched by the installer (step 5.3) or
``python -m emma.brain fetch-model --memory``. Runs offline on onnxruntime, like
e5; no network at recall or at boot.
"""

from __future__ import annotations

from core.brain.embed import Embedder, ModelSpec

MEMORY_MODEL = ModelSpec(
    name="paraphrase-multilingual-MiniLM-L12-v2",
    repo="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    revision="e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
    # Same portable QInt8 graph family the brain's e5 ships (runs on arm64 too).
    onnx_file="onnx/model_qint8_avx512_vnni.onnx",
    sha256={  # of the files at the revision, measured 2026-10-16
        "onnx/model_qint8_avx512_vnni.onnx":
            "783fea82d71a58179b830a4dbd2d58447e640609e98eedf9ffa12622d375a672",
        "tokenizer.json": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
    },
    dim=384,
    max_tokens=128,  # a fact or a note can be a sentence or two
)


def embedder() -> Embedder:
    """A fresh lazy embedder for memory. Not loaded until the first embed."""
    return Embedder(spec=MEMORY_MODEL)
