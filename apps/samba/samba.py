# Copyright (c) Meta Platforms, Inc. and affiliates.

from dataclasses import dataclass
from typing import Optional, Tuple, Union

import torch
from torch import nn
from torch.nn.attention.flex_attention import BlockMask

from torch.distributed._tensor import Replicate, Shard
from torch.distributed.tensor.parallel import (
    ColwiseParallel,
    RowwiseParallel,
    SequenceParallel,
    PrepareModuleInput,
    parallelize_module,
)

from xformers.ops import fmha, AttentionBias
from lingua.transformer import (
    BaseTransformer,
    BaseTransformerArgs,
    RMSNorm,
    cross_entropy,
)
from lingua.lingua.attention.softmax_attention import (
    create_causal_mask
)

def attention_flops_per_token(n_layers, seq_len, dim, causal):
    # Formula from https://github.com/Dao-AILab/flash-attention/blob/main/benchmarks/benchmark_flash_attention.py#L27-L30
    return 3.5 * (4 * n_layers * seq_len * dim // (2 if causal else 1))


def get_num_flop_per_token(
    num_non_embed_params: int, n_layers: int, dim: int, seq_len: int
) -> int:
    return 6 * num_non_embed_params + attention_flops_per_token(
        n_layers, seq_len, dim, True
    )



class StateCache(nn.Module):
    def __init__(
        self, bsz, n_heads, head_dim, state_dim, conv_size, conv_dim, dtype, device
    ):
        super().__init__()
        state_shape = (bsz, n_heads, head_dim, state_dim)
        if conv_size is None:
            conv_shape = (0,)
        else:
            conv_shape = (bsz, conv_dim, conv_size)

        self.register_buffer(
            "conv_cache",
            torch.zeros(conv_shape, dtype=dtype, device=device),
            persistent=False,
        )
        self.register_buffer(
            "state_cache",
            torch.zeros(state_shape, dtype=dtype, device=device),
            persistent=False,
        )

    def reset(self):
        self.conv_cache.zero_()
        self.state_cache.zero_()


@dataclass
class LMSambaArgs(BaseSambaArgs):

    seed: int = 42

    vocab_size: int = -1
    weight_tying: bool = False

    sliding_window: Optional[int] = 2048
    loss_reduction: str = "mean"
    
    vocab_size: int = -1


class LMSamba(BaseSamba):
    def __init__(self, args: LMSambaArgs):
        super().__init__(args)
        self.weight_tying = args.weight_tying
        self.sliding_window = args.sliding_window
        self.loss_reduction = args.loss_reduction

        assert args.vocab_size > 0

        self.tok_embeddings = torch.nn.Embedding(args.vocab_size, args.dim)

        self.norm = RMSNorm(args.dim, eps=args.norm_eps)

        self.output = nn.Linear(
            args.dim,
            args.vocab_size,
            bias=False,
        )

        if args.weight_tying:
            self.output.weight = self.embeddings.tok_embeddings.weight

        self.init_weights()

    def forward(
        self,
        token_values: torch.Tensor,
        target: Optional[torch.Tensor] = None,
        tok_idx: Optional[torch.Tensor] = None,
        cu_seqlens: Optional[torch.Tensor] = None,
        mask: Optional[Union[BlockMask, AttentionBias, torch.Tensor, str]] = None,
        attn_impl: str = "flex_attention",
        ssm_impl: str = "ssm",
    ):
        bsz, seqlen = token_values.shape

        h = self.tok_embeddings(token_values)

        mask = (
            mask
            if mask is not None
            else create_causal_mask(seqlen, attn_impl, self.sliding_window)
        )

        h = super().forward(
            h, tok_idx=tok_idx, 
            mask=mask, 
            attn_impl=attn_impl,
            cu_seqlens=cu_seqlens,
            mask=mask,
            attn_impl=attn_impl,
            ssm_impl=ssm_impl
        )

        logits = self.output(self.norm(h))
        if target is not None:
            return cross_entropy(
                logits.flatten(0,1),
                target.flatten(0,1),
                reduction=self.loss_reduction
            )
        else:
            return logits

    def reset_parameters(self, init_std=None):
        # Either use fixed base std or sqrt model dim
        super().reset_parameters()
        init_std = init_std or (self.dim ** (-0.5))
        self.norm.reset_parameters()
        nn.init.trunc_normal_(
            self.tok_embeddings.weight,
            mean=0.0,
            std=init_std,
            a=-3 * init_std,
            b=3 * init_std,
        )
        if not self.weight_tying:
            nn.init.trunc_normal_(
                self.output.weight,
                mean=0.0,
                std=init_std,
                a=-3 * init_std,
                b=3 * init_std,
            )

    @torch.inference_mode()
    def init_weights(self):
        super().init_weights()

# Optional policy for activation checkpointing. With None, we stick to the default (defined distributed.py: default_no_recompute_ops)
def get_no_recompute_ops():
    return {
        torch.ops.aten.mm.default,
        torch.ops.aten._scaled_mm.default,
        torch.ops.c10d_functional.reduce_scatter_tensor.default,
        torch.ops.mamba_ssm.ssm_chunk_scan_combined_fwd.default,
    }


# Optional and only used for fully shard options (fsdp) is choose. Highly recommanded for large models
def build_fsdp_grouping_plan(model_args: LMSambaArgs):
    group_plan: Tuple[int, bool] = []

    # Grouping and output seperately
    group_plan.append(("tok_embeddings", False))

    # Grouping by layers
    for i in range(model_args.n_layers):
        group_plan.append((f"layers.{i}", False))

    group_plan.append(("output", True))

    return group_plan
