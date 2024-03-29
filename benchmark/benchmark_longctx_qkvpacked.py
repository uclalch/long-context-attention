from flash_attn import flash_attn_varlen_qkvpacked_func
import torch
import torch.distributed as dist
from long_context_attn import LongContextAttentionQKVPacked, set_seq_parallel_pg
import torch.cuda

import argparse

parser = argparse.ArgumentParser(description="Process some integers.")

parser.add_argument("--nheads", type=int, default=2, help="head number")
parser.add_argument("--batch_size", type=int, default=2, help="batch size")
args = parser.parse_args()


def benchmark(num_iter=100, forward_only=True, log=True):
    dtype = torch.bfloat16
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    device = torch.device(f"cuda:{rank}")
    torch.cuda.set_device(device)

    batch_size = args.batch_size
    seqlen = 1024 * 8
    nheads = args.nheads
    d = 128
    dropout_p = 0
    causal = True
    deterministic = False

    assert seqlen % (2 * world_size) == 0
    assert d % 8 == 0

    qkv = torch.randn(
        batch_size, seqlen, 3, nheads, d, device=device, dtype=dtype, requires_grad=True
    )
    dout = torch.randn(batch_size, seqlen, nheads, d, device=device, dtype=dtype)

    sp_ulysses_degree = min(nheads, world_size)
    sp_ring_degree = world_size // sp_ulysses_degree

    ulysses_pg, ring_pg = set_seq_parallel_pg(
        sp_ulysses_degree, sp_ring_degree, rank, world_size
    )

    longctx_attn = LongContextAttentionQKVPacked(ulysses_pg, ring_pg)

    begin = torch.cuda.Event(enable_timing=True)
    begin.record()

    if forward_only:
        with torch.no_grad():
            for _ in range(num_iter):
                _ = longctx_attn(
                    qkv,
                    dropout_p=dropout_p,
                    causal=causal,
                    window_size=(-1, -1),
                    alibi_slopes=None,
                    deterministic=deterministic,
                    return_attn_probs=False,
                )

    else:
        for _ in range(num_iter):
            qkv.grad = None
            out = longctx_attn(
                qkv,
                dropout_p=dropout_p,
                causal=causal,
                window_size=(-1, -1),
                alibi_slopes=None,
                deterministic=deterministic,
                return_attn_probs=False,
            )
            out.backward(dout)
    end = torch.cuda.Event(enable_timing=True)
    end.record()
    torch.cuda.synchronize(device=device)
    time = begin.elapsed_time(end) / 1000.0

    if rank == 0 and log:
        print(f"{num_iter / time:.3f} iter/s, {time:.3f} sec")


if __name__ == "__main__":
    dist.init_process_group("nccl")
    rank = dist.get_rank()

    forward_only = True

    torch.cuda.empty_cache()
    if rank == 0:
        print(f"long context attention qkvpacked")
    benchmark(forward_only=forward_only, log=False)
    benchmark(forward_only=forward_only, log=True)