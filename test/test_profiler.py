import collections
import gc
import unittest

import torch
import torch.nn as nn
from torch.testing._internal.common_utils import (
    TestCase, run_tests, TEST_WITH_ASAN, IS_WINDOWS)
from torch.autograd.profiler import profile
from torch.autograd import kineto_available

import torch.profiler

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


@unittest.skipIf(not HAS_PSUTIL, "Requires psutil to run")
@unittest.skipIf(TEST_WITH_ASAN, "Cannot test with ASAN")
@unittest.skipIf(IS_WINDOWS, "Test is flaky on Windows")
@unittest.skipIf(not torch.cuda.is_available(), "CUDA is required")
class TestProfilerCUDA(TestCase):
    def test_mem_leak(self):
        """Checks that there's no memory leak when using profiler with CUDA
        """
        t = torch.rand(1, 1).cuda()
        p = psutil.Process()
        last_rss = collections.deque(maxlen=5)
        for outer_idx in range(10):
            with profile(use_cuda=True):
                for _ in range(1024):
                    t = torch.mm(t, t)

            gc.collect()
            torch.cuda.empty_cache()
            last_rss.append(p.memory_info().rss)

        # with CUDA events leaking the increase in memory was ~7 MB between
        # profiler invocations above
        is_increasing = all(
            [last_rss[idx] > last_rss[idx - 1] for idx in range(1, len(last_rss))])
        max_diff = -1
        for idx in range(1, len(last_rss)):
            max_diff = max(max_diff, last_rss[idx] - last_rss[idx - 1])
        self.assertTrue(not (is_increasing and max_diff > 100 * 1024),
                        msg='memory usage is increasing, {}'.format(str(last_rss)))

class TestProfiler(TestCase):
    def test_source(self):
        """Checks that source code attribution works for eager, TS and autograd mode
        """
        # avoid automatic inlining
        prev_opt = torch._C._get_graph_executor_optimize()
        torch._C._set_graph_executor_optimize(False)

        @torch.jit.script
        def ts_method_2(x, y):
            return torch.matmul(x, y)

        @torch.jit.script
        def ts_method_1(x, y, z):
            a = x + z
            w = ts_method_2(x, y) + a
            return w.sum()

        class DummyModule(nn.Module):
            def __init__(self):
                super(DummyModule, self).__init__()
                self.conv = torch.nn.Conv2d(3, 2, kernel_size=1, stride=2, padding=3, bias=False)

            def forward(self, x):
                return self.conv(x)

        mod = DummyModule()

        with profile(with_stack=True, use_kineto=kineto_available()) as p:
            x = torch.randn(10, 10, requires_grad=True)
            y = torch.randn(10, 10, requires_grad=True)
            z = x + y
            w = ts_method_1(x, y, z)
            v = 2 * w
            v.backward()
            a = torch.randn(2, 3, 2, 2, requires_grad=True)
            b = mod(a)
            c = b.sum()
            c.backward()

        print(p.key_averages(
            group_by_stack_n=5).table(
            sort_by="self_cpu_time_total", row_limit=-1))

        for e in p.function_events:
            if "aten::add" in e.name or "AddBackward" in e.name:
                self.assertTrue(any(["test_profiler" in entry for entry in e.stack]))
                self.assertTrue(any([(
                    "test_source" in entry or
                    "ts_method_1" in entry or
                    "ts_method_2" in entry) for entry in e.stack]))

        torch._C._set_graph_executor_optimize(prev_opt)

    def payload(self):
        x = torch.randn(10, 10).cuda()
        y = torch.randn(10, 10).cuda()
        z = torch.mm(x, y)
        z = z + y
        z = z.cpu()

    @unittest.skipIf(not kineto_available(), "Kineto is required")
    @unittest.skipIf(not torch.cuda.is_available(), "CUDA is required")
    def test_kineto(self):
        with profile(use_cuda=True, use_kineto=True):
            self.payload()

        # rerun to avoid initial start overhead
        with profile(use_cuda=True, use_kineto=True) as p:
            self.payload()
        print(p.key_averages().table(
            sort_by="self_cuda_time_total", row_limit=-1))
        found_gemm = False
        found_memcpy = False
        for e in p.function_events:
            if "gemm" in e.name:
                found_gemm = True
            if "Memcpy" in e.name or "memcpy" in e.name:
                found_memcpy = True
        self.assertTrue(found_gemm)
        self.assertTrue(found_memcpy)
        # p.export_chrome_trace("/tmp/test_trace.json")


    @unittest.skipIf(not kineto_available(), "Kineto is required")
    @unittest.skipIf(not torch.cuda.is_available(), "CUDA is required")
    def test_profiler_kineto_api(self):
        called_num = [0]
        def test_output_fn(p):
            print(p.key_averages().table(
                sort_by="self_cuda_time_total", row_limit=-1))
            # p.export_chrome_trace("/tmp/test_trace_" + str(called_num[0]) + ".json")
            called_num[0] += 1

        with profile(use_cuda=True, use_kineto=True):
            self.payload()

        with torch.profiler.profile(
            activities=[
                torch.profiler.ProfilerActivity.CPU,
                torch.profiler.ProfilerActivity.CUDA],
            enable_pred=torch.profiler.EnablePred(
                wait=1,
                warmup=1,
                active=2,
                output_fn=test_output_fn)
        ) as p:
            for idx in range(8):
                self.payload()
                p.next_step()

        self.assertEqual(called_num[0], 2)


if __name__ == '__main__':
    run_tests()
