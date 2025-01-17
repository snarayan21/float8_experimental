# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD 3-Clause license found in the
# LICENSE file in the root directory of this source tree.
import itertools
from typing import Optional

import fire
import pandas as pd

import torch
import torch.nn as nn
import torch.utils.benchmark as benchmark

# estimating TOPs for matmuls in fp32, fp16, fp8
# assuming A * B = C, with A being M * K, B being K * N, C being M * N

# H100 SXM specs: bottom of https://www.nvidia.com/en-us/data-center/h100/
h100_peak_flops_float32 = 67e12
h100_peak_flops_fp16_tc = 989e12
h100_peak_tops_float8_tc = 1979e12

dtype_to_peak_tops = {
    torch.float32: h100_peak_flops_float32,
    torch.float16: h100_peak_flops_fp16_tc,
    torch.bfloat16: h100_peak_flops_fp16_tc,
    torch.float8_e4m3fn: h100_peak_tops_float8_tc,
    torch.float8_e5m2: h100_peak_tops_float8_tc,
}


def benchmark_fn_in_sec(f, *args, **kwargs):
    # Manual warmup
    for _ in range(4):
        f(*args, **kwargs)
    t0 = benchmark.Timer(
        stmt="f(*args, **kwargs)", globals={"args": args, "kwargs": kwargs, "f": f}
    )
    measurement = t0.blocked_autorange()
    return measurement.mean


def do_benchmarks(tops, peak_tops, f, *args, **kwargs):
    time_sec = benchmark_fn_in_sec(f, *args, **kwargs)
    tops_sec = float(tops) / time_sec
    pct_top_peak = tops_sec / peak_tops
    return time_sec, tops_sec, pct_top_peak


@torch.inference_mode()
def run(n_limit: Optional[int] = None):
    device = "cuda"

    # LLaMa 2 70B single-node weight shapes
    # assumes fused attn.wqkv and ffn.w13
    # source: https://fburl.com/gsheet/g8onr7rh
    name_to_shapes_70b = {
        "attn.wqkv": (8192, 1280),
        "attn.w0": (1024, 8192),
        "ffn.w13": (8192, 7168),
        "ffn.w2": (3584, 8192),
    }

    headers = ("name", "shape", "dtype", "ref_time_s", "fp8_time_s", "fp8_speedup")
    results = []

    name_to_shapes = name_to_shapes_70b
    bsz_and_seq_len = ((4, 4096),)
    dtypes = torch.bfloat16, torch.float16

    for idx, (dtype, (name, (K, N))) in enumerate(
        itertools.product(dtypes, name_to_shapes.items())
    ):
        if n_limit is not None and idx >= n_limit:
            break

        # source: Xiao Sun, these are realistic for LLaMa 70B training
        bsz, seq_len = 4, 4096

        M = bsz * seq_len
        print("M, K, N:", M, K, N)
        tops = 2 * M * N * K
        print(f"tops: {tops:.2E}")

        # raw torch.mm
        A = torch.randn(M, K, device=device, dtype=dtype)
        m_ref = nn.Sequential(nn.Linear(K, N, dtype=dtype, device=device, bias=False))
        ref_time_sec, ref_tops_sec, ref_pct_top_peak = do_benchmarks(
            tops, dtype_to_peak_tops[dtype], m_ref, A
        )
        print(
            f"{dtype} time_sec {ref_time_sec:.2E}, tops/sec {ref_tops_sec:.2E}, pct_peak {ref_pct_top_peak:.3f}"
        )

        del A

        # raw float8 matmul (upper bound for what we can achive in eager mode)
        # TODO(future): add e5m2
        d1, d2, d3 = torch.float8_e4m3fn, torch.float8_e4m3fn, dtype
        A = torch.zeros(M, K, device=device, dtype=d1)
        B = torch.zeros(K, N, device=device, dtype=d2).t().contiguous().t()

        def do_matmul(A, B):
            return torch._scaled_mm(A, B, out_dtype=d3, use_fast_accum=False)

        fp8_time_sec, fp8_tops_sec, fp8_pct_top_peak = do_benchmarks(
            tops, dtype_to_peak_tops[d1], do_matmul, A, B
        )
        print(
            f"fp8 time_sec {fp8_time_sec:.2E}, tops/sec {fp8_tops_sec:.2E}, pct_peak {fp8_pct_top_peak:.3f}"
        )

        del A, B

        results.append(
            [
                name,
                (M, K, N),
                dtype,
                ref_time_sec,
                fp8_time_sec,
                ref_time_sec / fp8_time_sec,
            ]
        )

    data_pd = pd.DataFrame(results, columns=headers)
    print(data_pd)


def main() -> None:
    fire.Fire(run)


if __name__ == "__main__":
    main()  # pragma: no cover
