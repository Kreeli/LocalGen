"""sd-cli 输出 → 进度 / 阶段 / 显存不足 的纯函数测试（不启进程、不开窗口）。

这批用例对应《界面与任务可观测性方案》§7 的前三组：feed 的分帧与截断、
track 的阶段判定、OOM 行 → gpu_oom。
"""
from types import SimpleNamespace

from localgen.engine.sdcpp import track
from localgen.engine.windows import WindowsProcess


def collect(chunks):
    lines = []
    pending = b""
    for chunk in chunks:
        pending = WindowsProcess.feed(pending, chunk, lines.append)
    return lines, pending


# ---------- feed：字节流 → 行 ----------

def test_feed_splits_on_newline_and_carriage_return():
    """sd-cli 的进度条靠 \\r 重绘，\\r 必须当换行，否则整条进度只算一行。"""
    lines, pending = collect([b"loading 1/3\rloading 2/3\rloading 3/3\n"])
    assert lines == ["loading 1/3", "loading 2/3", "loading 3/3"]
    assert pending == b""


def test_feed_joins_lines_split_across_chunks():
    """4096 字节一块读，一行被切开时必须接回去，不能各算半行。"""
    lines, pending = collect([b"|===>  2/", b"30 - 22.37s/it", b"\n"])
    assert lines == ["|===>  2/30 - 22.37s/it"]
    assert pending == b""


def test_feed_keeps_partial_tail_pending():
    lines, pending = collect([b"a\nb"])
    assert lines == ["a"]
    assert pending == b"b"


def test_feed_truncates_overlong_line():
    lines, _ = collect([b"x" * 5000])
    assert len(lines) == 1
    assert len(lines[0]) == 4096


def test_feed_decodes_invalid_utf8_without_raising():
    lines, _ = collect([b"\xff\xfe broken\n"])
    assert lines and lines[0].endswith("broken")


def test_feed_keeps_escape_sequences_out_of_the_line_boundary():
    """sd-cli 的进度条带 ESC[K 与退格；它们只能留在行内，不能把行切断。"""
    lines, pending = collect([b"  |===>       |  2/30 - 22.37s/it - ETA 13:1\x1b[K\r"])
    assert len(lines) == 1
    assert lines[0].endswith("ETA 13:1\x1b[K")
    assert pending == b""


# ---------- track：一行日志 → 阶段/步数/备注 ----------

def state(steps=30):
    return SimpleNamespace(steps=steps, progress=None, stage="loading", note=None, gpu_oom=False,
                           eta_marks=[])


def test_track_marks_loading_while_denominator_is_not_steps():
    job = state()
    track(job, "  |##################################################| 340/352 - 1.83GB/s")
    assert job.stage == "loading"
    assert job.progress is None
    assert job.note == "加载权重 340/352"


def test_track_marks_sampling_with_rate_and_eta():
    """单笔速率不猜 ETA（sd-cli 自己的 ETA 首帧能偏 +111%，见 test_eta_* 那组）。"""
    job = state()
    track(job, "  |===>                |  2/30 - 22.37s/it - ETA 13:10")
    assert job.progress == (2, 30)
    assert job.stage == "sampling"
    assert job.note == "22.37s/it"


def test_track_accepts_it_per_second_direction():
    job = state()
    track(job, "  |========>           |  8/30 - 1.03it/s - ETA 00:21")
    assert job.progress == (8, 30)
    assert job.note == "1.03it/s"


def test_track_recognises_sampling_when_strength_shortens_the_run():
    """图生图按 strength 推 t_enc：真机日志是 31 步，不是 --steps。

    只看 `total == steps` 会把整段采样误判成"加载权重 1/31"，界面就停在加载上。
    """
    job = state(steps=50)
    track(job, "  |######                                            |  3/31 -   2.11s/it - ETA 01:38")
    assert job.stage == "sampling"
    assert job.progress == (3, 31)
    assert job.note == "2.11s/it"
    track(job, "  |##################################################| 31/31 - 2.05s/it")
    assert job.progress == (31, 31)


def test_track_does_not_fall_back_to_loading_mid_sampling():
    """采样开始后再出现分母≠steps 的加载行（VAE 权重）不能把阶段退回去。"""
    job = state()
    track(job, "|##################################################| 340/352 - 1.83GB/s")
    track(job, "  |===>                |  1/30 - 34.12s/it - ETA 16:29")
    track(job, "|####################                              | 12/352 - 1.80GB/s")
    assert job.progress == (1, 30)
    assert job.stage == "sampling"


def test_track_does_not_read_throughput_as_a_rate():
    """`1.83GB/s` 里含 `B/s`，速率正则必须锚在 `- ` 之后，否则加载条会被读成采样。"""
    job = state(steps=426)
    track(job, "  |##################################################| 340/426 - 1.83GB/s")
    assert job.stage == "loading"
    assert job.progress is None
    assert job.note == "加载权重 340/426"


def test_track_keeps_loading_for_bars_without_a_rate():
    """只有加载条没有速率，任何分母都不该被当成采样（哪怕恰好等于 steps）。"""
    job = state(steps=352)
    track(job, "|##################################################| 340/352 - 1.83GB/s")
    assert job.stage == "loading" and job.progress is None


def test_track_reports_decoding_after_last_step():
    job = state()
    track(job, "  |==================================================| 30/30 - 1.03it/s")
    track(job, "[I] decoding 1 latents --- image.cpp:655")
    assert job.stage == "decoding"
    assert job.note is None
    track(job, "[I] latent 1 decoded, taking 9.52s --- image.cpp:721")
    assert job.stage == "decoding"
    assert job.note == "9.5 s"


def test_track_ignores_decoding_line_before_sampling_started():
    """根本没开始采样时（例如只加载了权重）别把阶段说成解码。"""
    job = state()
    track(job, "[I] decoding 1 latents --- image.cpp:655")
    assert job.stage == "loading"


def test_track_ignores_decoded_timing_before_sampling_started():
    """两个解码分支必须一样对待：加载阶段 sd-cli 也会"解码"参考图。"""
    job = state()
    track(job, "[I] init image decoded, taking 0.42s --- image.cpp:721")
    assert job.stage == "loading"
    assert job.note is None


def test_track_flags_gpu_oom():
    job = state()
    track(job, "[E] ggml_backend_cuda_buffer_type_alloc_buffer: CUDA error: out of memory")
    assert job.gpu_oom is True
    assert job.progress is None


def test_track_flags_gpu_oom_from_the_model_manager_wording():
    """真机日志（768×1024 + 参考图，6 GB 卡）报的是这两句，不是 CUDA 的 out of memory。"""
    job = state(steps=50)
    track(job, "[W] model manager cannot make enough memory available on CUDA0: "
               "need 1463.98 MB device / 439.98 MB budget, available 771.37 MB device")
    assert job.gpu_oom is True
    track(job, "[E] MOSS-Image segment 1/1 (graph) failed during workspace capacity check "
               "--- ggml_runner.cpp:959")
    assert job.gpu_oom is True


def test_track_flags_gpu_oom_from_the_allocated_capacity_wording():
    """同一族里还有 `allocated capacity check`（job 01f58924 实录），别漏。"""
    job = state(steps=50)
    track(job, "[E] MOSS-Image segment 1/1 (graph) failed during allocated capacity check "
               "--- ggml_runner.cpp:959")
    assert job.gpu_oom is True


def test_track_flags_gpu_oom_from_the_ggml_allocator_without_cuda():
    """ggml 的分配失败常常不带 "cuda" 字样，旧判定会漏。"""
    job = state()
    track(job, "[E] ggml_backend_cuda_buffer_type_alloc_buffer: failed to allocate 1463.98 MB")
    assert job.gpu_oom is False      # 这句没有 out of memory，不猜
    track(job, "[E] ggml_backend_sched: failed to allocate graph: out of memory")
    assert job.gpu_oom is True


def test_track_does_not_flag_oom_for_unrelated_memory_chatter():
    job = state()
    track(job, "[I] model manager memory on CUDA0: reported free 771.37 MB / total 6140.50 MB")
    assert job.gpu_oom is False


def test_track_ignores_unrelated_lines():
    job = state()
    track(job, "[I] loading tensors completed, taking 0.21s (read: 0.00s)")
    assert job.stage == "loading"
    assert job.progress is None
    assert job.note is None
    assert job.gpu_oom is False


def test_track_survives_real_bytes_with_escape_and_backspace():
    """用日志里的真实字节跑一遍：ESC[K 与退格尾巴不能污染 ETA 的右边界。"""
    lines, _ = collect([b"  |=====>              |  2/30 - 22.37s/it - ETA 13:10\x1b[K\r"
                        b"  |===================>| 30/30 - 1.03it/s\x1b[K\r"
                        b"[I] latent 1 decoded, taking 9.52s\x08\x08 --- image.cpp:721\r"])
    job = state()
    for line in lines:
        track(job, line)
    assert job.progress == (30, 30)
    assert job.stage == "decoding"
    assert job.note == "9.5 s"


def test_track_eta_is_not_swallowed_by_trailing_escape():
    """ETA 是自己算的，不受行尾 ESC 影响；单笔时干脆不显示。"""
    job = state()
    track(job, "  |=====>              |  2/30 - 22.37s/it - ETA 13:10\x1b[K")
    assert job.note == "22.37s/it"


# ---------- ETA：丢首步 + 近 8 步均值 ----------

def test_eta_appears_after_two_marks_and_uses_the_window():
    """两笔 10 s/it，current=3/30 → 均值 10 → 剩 27 步 → ETA 04:30。"""
    job = state()
    track(job, "  |=>                  |  1/30 - 10.00s/it")
    track(job, "  |==>                 |  2/30 - 10.00s/it")
    track(job, "  |===>                |  3/30 - 10.00s/it")
    assert job.note == "10.00s/it · ETA 04:30"


def test_eta_drops_the_cold_first_step():
    """首步 4.5 s/it 是冷启动，剔除后 ETA 不被抬到两倍（真机 8676b1f7 的形态）。"""
    job = state(steps=50)
    for current, per in ((1, 4.54), (2, 1.92), (3, 1.92), (4, 1.92)):
        track(job, f"  |===>                | {current}/50 - {per:.2f}s/it")
    # 近 8 步（去掉首步）= [1.92, 1.92, 1.92] → 1.92 × 46 ≈ 88 s → 01:28
    assert job.note.endswith("ETA 01:28"), job.note
    assert job.eta_marks[0] == 4.54


def test_eta_is_not_guessed_from_a_single_mark():
    """丢首步之后不足 2 笔就不猜 —— 两笔时仍只有首步被剔掉的那一笔。"""
    job = state()
    track(job, "  |=>                  |  1/30 - 9.00s/it")
    assert "ETA" not in job.note
    track(job, "  |==>                 |  2/30 - 9.00s/it")
    assert "ETA" not in job.note
    track(job, "  |===>                |  3/30 - 9.00s/it")
    assert "ETA" in job.note


def test_eta_disappears_on_the_last_step():
    job = state()
    for current in (1, 2, 3):
        track(job, f"  |===>                | {current}/30 - 2.00s/it")
    assert "ETA" in job.note
    track(job, "  |====================| 30/30 - 2.00s/it")
    assert job.note == "2.00s/it"


def test_eta_converts_it_per_second_into_seconds_per_step():
    job = state(steps=20)
    for current in (1, 2, 3):
        track(job, f"  |===>                | {current}/20 - 0.50it/s")
    # 0.5 it/s = 2 s/step，剩 17 步 ≈ 34 s → 00:34
    assert job.note.endswith("ETA 00:34"), job.note


def test_eta_window_keeps_only_the_last_eight_marks():
    job = state(steps=100)
    track(job, "  |=>                  |  1/100 - 99.00s/it")
    for current in range(2, 22):
        track(job, f"  |===>                | {current}/100 - 1.00s/it")
    assert len(job.eta_marks) == 21
    # 近 8 步全是 1.00，剩 79 步 → 01:19
    assert job.note.endswith("ETA 01:19"), job.note
