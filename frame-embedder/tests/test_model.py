"""DinoModel tests.

Uses a tiny public DINOv1 model (``facebook/dino-vits16``) to exercise the
AutoModel loader path on CPU without needing HF_TOKEN or DINOv3 license
acceptance in CI. Production uses DINOv3 (see config.yaml / node.yaml).
"""

import torch
from src.model import DinoModel

TINY_MODEL = "facebook/dino-vits16"


def test_model_loads_on_cpu():
    """Model should load without errors on CPU."""
    model = DinoModel(model_name=TINY_MODEL, device="cpu")
    assert model is not None


def test_model_encode_returns_correct_shape():
    """Encode should return a 1D tensor of the model's embedding dim."""
    model = DinoModel(model_name=TINY_MODEL, device="cpu")
    dummy_input = torch.randn(1, 3, 224, 224)
    embedding = model.encode(dummy_input)
    assert embedding.ndim == 1
    assert embedding.shape[0] == model.embedding_dim


def test_model_encode_is_deterministic():
    """Same input should produce same output (model is in eval mode)."""
    model = DinoModel(model_name=TINY_MODEL, device="cpu")
    dummy_input = torch.randn(1, 3, 224, 224)
    emb1 = model.encode(dummy_input)
    emb2 = model.encode(dummy_input)
    assert torch.allclose(emb1, emb2)
