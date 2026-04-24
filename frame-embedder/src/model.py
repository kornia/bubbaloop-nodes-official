"""DINOv3 model wrapper using HuggingFace transformers.

Loads a DINOv3 ViT (or ConvNeXt) backbone via ``AutoModel.from_pretrained`` and
extracts a single fixed-dimensional image embedding by mean-pooling the last
hidden state across all output tokens.

Notes:
    - DINOv3 weights on HuggingFace are gated. Accept the model license on the
      model's HF page and export ``HF_TOKEN`` before first use.
    - Requires ``transformers >= 4.44``.
    - Mean-pooling across every token (CLS included) is kept for wire
      compatibility with the DINOv2/v1-era output dimensionality. DINOv3's
      recommended readout is the CLS token alone; switching would change the
      numerical embedding but not its shape.
"""

import logging
import time

import torch
from transformers import AutoModel

log = logging.getLogger(__name__)


class DinoModel:
    """Wraps a DINOv3 (or DINOv2/v1) ViT for single-image embedding extraction.

    Args:
        model_name: HuggingFace model identifier
            (e.g. ``facebook/dinov3-vitb16-pretrain-lvd1689m``).
        device: ``"cuda"`` or ``"cpu"``.
    """

    def __init__(self, model_name: str, device: str = "cuda"):
        self.device = device
        self.model_name = model_name

        log.info("Loading model %s on %s...", model_name, device)
        t0 = time.monotonic()
        self._model = AutoModel.from_pretrained(model_name)
        self._model.to(device)
        self._model.train(False)  # put model in inference mode (no dropout, etc.)
        elapsed = time.monotonic() - t0
        log.info("Model loaded in %.1fs", elapsed)

        # ViT-S/16 = 384, ViT-B/16 = 768, ViT-L/16 = 1024, ViT-H+/16 = 1280.
        self.embedding_dim = self._model.config.hidden_size

    @torch.no_grad()
    def encode(self, pixel_values: torch.Tensor) -> torch.Tensor:
        """Run forward pass and return a mean-pooled token embedding.

        Args:
            pixel_values: Tensor of shape (1, 3, 224, 224), ImageNet-normalized.

        Returns:
            1D tensor of shape (embedding_dim,) — mean of all tokens in the
            final hidden state (CLS + patch tokens).
        """
        pixel_values = pixel_values.to(self.device)
        outputs = self._model(pixel_values=pixel_values)
        patch_tokens = outputs.last_hidden_state  # (1, num_patches[+1], hidden_size)
        embedding = patch_tokens.mean(dim=1).squeeze(0)  # (hidden_size,)
        return embedding.cpu()
