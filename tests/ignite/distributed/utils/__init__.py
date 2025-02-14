import pytest
import torch
import torch.distributed as dist

import ignite.distributed as idist
from ignite.distributed.utils import sync
from ignite.engine import Engine, Events


def _sanity_check():
    from ignite.distributed.utils import _model

    assert _model.get_world_size() == _model.get_nnodes() * _model.get_nproc_per_node()
    assert _model.get_local_rank() < _model.get_nproc_per_node()
    assert _model.get_rank() < _model.get_world_size()
    assert _model.get_node_rank() < _model.get_nnodes()


def _test_distrib_config(local_rank, backend, ws, true_device, rank=None, true_init_method=None):
    assert idist.backend() == backend, f"{idist.backend()} vs {backend}"

    this_device = idist.device()
    assert isinstance(this_device, torch.device)
    if backend in ("nccl", "horovod") and "cuda" in this_device.type:
        true_device = torch.device(f"{true_device}:{local_rank}")
        assert this_device == true_device, f"{this_device} vs {true_device}"
    elif backend in ("gloo", "horovod"):
        assert this_device == torch.device(true_device)
    elif backend == "xla-tpu":
        assert true_device in this_device.type

    if rank is None:
        if idist.model_name() == "native-dist":
            rank = dist.get_rank()

    if rank is not None:
        assert idist.get_rank() == rank

    assert idist.get_world_size() == ws
    assert idist.get_local_rank() == local_rank

    assert idist.model_name() in ("native-dist", "xla-dist", "horovod-dist")

    _sanity_check()

    if idist.model_name() == "native-dist":
        from ignite.distributed.utils import _model

        if true_init_method is not None:
            assert _model._init_method == true_init_method


def _test_sync(cls):
    from ignite.distributed.utils import _SerialModel, _set_model

    _set_model(_SerialModel())

    sync()

    from ignite.distributed.utils import _model

    assert isinstance(_model, cls), f"{type(_model)} vs {cls}"


def _test_distrib__get_max_length(device):
    ws = idist.get_world_size()
    x = "_test_distrib__get_max_length" * (idist.get_rank() + 2)

    from ignite.distributed.utils import _model

    res = _model._get_max_length(x, device)
    assert res == len("_test_distrib__get_max_length" * (ws + 1))


def _test_distrib_all_reduce(device):

    res = idist.all_reduce(10)
    assert res == 10 * idist.get_world_size()

    t = torch.tensor(10, device=device)
    res = idist.all_reduce(t)
    assert res.item() == 10 * idist.get_world_size()

    rank = idist.get_rank()
    t = torch.tensor(rank * 2.0 + 1.0, device=device)
    res = idist.all_reduce(t)
    assert res.item() == sum([i * 2.0 + 1.0 for i in range(idist.get_world_size())])

    t = torch.tensor(rank * 2.0 + 1.0, device=device)
    res = idist.all_reduce(t, "MIN").item()
    true_val = min([i * 2 + 1 for i in range(idist.get_world_size())])
    assert res == true_val, f"{res} vs {true_val}"

    t = torch.tensor(rank * 2.0 + 1.0, device=device)
    res = idist.all_reduce(t, "MAX").item()
    true_val = max([i * 2.0 + 1.0 for i in range(idist.get_world_size())])
    assert res == true_val, f"{res} vs {true_val}"

    t = torch.tensor(rank * 2.0 + 1.0, device=device)
    res = idist.all_reduce(t, "PRODUCT").item()
    true_val = 1
    for v in [i * 2.0 + 1.0 for i in range(idist.get_world_size())]:
        true_val *= v
    assert res == true_val, f"{res} vs {true_val}"

    if idist.get_world_size() > 1:
        with pytest.raises(TypeError, match=r"Unhandled input type"):
            idist.all_reduce("abc")

        with pytest.raises(ValueError, match=r"Unsupported reduction operation"):
            idist.all_reduce(10, op="ABC")

        t = torch.tensor([0, 1, 2])
        res = idist.all_reduce(t)
        assert res.device == t.device, f"{res.device} vs {t.device}"


def _test_distrib_all_gather(device):

    res = torch.tensor(idist.all_gather(10), device=device)
    true_res = torch.tensor([10,] * idist.get_world_size(), device=device)
    assert (res == true_res).all()

    t = torch.tensor(idist.get_rank(), device=device)
    res = idist.all_gather(t)
    true_res = torch.tensor([i for i in range(idist.get_world_size())], device=device)
    assert (res == true_res).all()

    x = "test-test"
    if idist.get_rank() == 0:
        x = "abc"
    res = idist.all_gather(x)
    true_res = ["abc",] + ["test-test"] * (idist.get_world_size() - 1)
    assert res == true_res

    base_x = "tests/ignite/distributed/utils/test_native.py" * 2000
    x = base_x
    if idist.get_rank() == 0:
        x = "abc"

    res = idist.all_gather(x)
    true_res = ["abc",] + [base_x] * (idist.get_world_size() - 1)
    assert res == true_res

    t = torch.arange(100, device=device).reshape(4, 25) * (idist.get_rank() + 1)
    in_dtype = t.dtype
    res = idist.all_gather(t)
    assert res.shape == (idist.get_world_size() * 4, 25)
    assert res.dtype == in_dtype
    true_res = torch.zeros(idist.get_world_size() * 4, 25, device=device)
    for i in range(idist.get_world_size()):
        true_res[i * 4 : (i + 1) * 4, ...] = torch.arange(100, device=device).reshape(4, 25) * (i + 1)
    assert (res == true_res).all()

    if idist.get_world_size() > 1:
        with pytest.raises(TypeError, match=r"Unhandled input type"):
            idist.all_reduce([0, 1, 2])


def _test_distrib_broadcast(device):

    rank = idist.get_rank()
    ws = idist.get_world_size()
    for src in range(ws):

        d = 10 if rank == src else 0
        res = idist.broadcast(d, src=src)
        true_res = 10
        assert res == true_res

        if rank == src:
            t = torch.tensor([1.2345, 2.3456], dtype=torch.float, device=device)
        else:
            t = torch.empty(2, device=device)

        res = idist.broadcast(t, src=src)
        true_res = torch.tensor([1.2345, 2.3456], dtype=torch.float, device=device)
        assert (res == true_res).all(), f"{res} vs {true_res}"

        def _test(text):

            if rank == src:
                t = text
            else:
                t = ""

            res = idist.broadcast(t, src=src)
            true_res = text
            assert res == true_res

        _test("test-abcdefg")
        _test("tests/ignite/distributed/utils/test_horovod.py::test_idist_broadcast_hvd" * 200)

        if rank == src:
            t = torch.arange(100, device=device).reshape(4, 25) * (src + 1)
        else:
            t = torch.empty(4, 25, dtype=torch.long, device=device)

        in_dtype = torch.long
        res = idist.broadcast(t, src)
        assert res.shape == (4, 25)
        assert res.dtype == in_dtype
        true_res = torch.arange(100, device=device).reshape(4, 25) * (src + 1)
        assert (res == true_res).all()

    if idist.get_world_size() > 1:
        with pytest.raises(TypeError, match=r"Unhandled input type"):
            idist.broadcast([0, 1, 2], src=0)


def _test_distrib_barrier(device):

    t = torch.tensor([idist.get_rank()], device=device, dtype=torch.float)
    true_res = sum([i for i in range(idist.get_world_size())])

    if idist.get_rank() == 0:
        t += 10.0
    idist.barrier()

    tt = idist.all_reduce(t)
    assert tt.item() == true_res + 10.0


def _test_distrib_one_rank_only(device):
    def _test(barrier):
        # last rank
        rank = idist.get_world_size() - 1

        value = torch.tensor(0).to(device)

        @idist.one_rank_only(rank=rank, with_barrier=barrier)
        def initialize():
            value.data = torch.tensor(100).to(device)

        initialize()

        value_list = idist.all_gather(tensor=value)

        for r in range(idist.get_world_size()):
            if r == rank:
                assert value_list[r].item() == 100
            else:
                assert value_list[r].item() == 0

    _test(barrier=True)
    _test(barrier=False)


def _test_distrib_one_rank_only_with_engine(device):
    def _test(barrier):
        engine = Engine(lambda e, b: b)

        batch_sum = torch.tensor(0).to(device)

        @engine.on(Events.ITERATION_COMPLETED)
        @idist.one_rank_only(with_barrier=barrier)  # ie rank == 0
        def _(_):
            batch_sum.data += torch.tensor(engine.state.batch).to(device)

        engine.run([1, 2, 3], max_epochs=2)

        value_list = idist.all_gather(tensor=batch_sum)

        for r in range(idist.get_world_size()):
            if r == 0:
                assert value_list[r].item() == 12
            else:
                assert value_list[r].item() == 0

    _test(barrier=True)
    _test(barrier=False)
