# Copyright 2024 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0


import torch
import torch.distributed as tdist
from torch.distributed.checkpoint.state_dict import StateDictOptions, get_model_state_dict
from torch.distributed.fsdp import FullyShardedDataParallel, ShardingStrategy


class WrapperModel(torch.nn.Module):

    def __init__(self):
        super().__init__()
        self.model = CounterExampleModel()
        self.fc1 = self.model.fc1
        self.fc2 = self.model.fc2


class CounterExampleModel(torch.nn.Module):
    """Small classification model.

    Args:
        num_features (int): number of input features (default: 1)
        num_classes (int): number of classes (default: 2)
    """

    def __init__(
        self,
        num_features: int = 1,
        num_classes: int = 2,
        num_hidden: int = 8,
        device: str = 'cpu',
        bias: bool = True,
    ) -> None:

        self.num_features = num_features
        self.num_classes = num_classes

        fc1 = torch.nn.Linear(num_features, num_hidden, device=device, bias=bias)
        fc2 = torch.nn.Linear(num_hidden, num_classes, device=device, bias=bias)

        net = torch.nn.Sequential(
            torch.nn.AdaptiveAvgPool2d(1),
            torch.nn.Flatten(),
            fc1,
            torch.nn.ReLU(),
            fc2,
            torch.nn.Softmax(dim=-1),
        )
        super().__init__()

        self.net = net
        # Important: It is crucial that the FC layers are bound to `self`
        # for the optimizer surgery tests.
        # These tests attempt to perform surgery on `fc1` layer, and we want
        # to make sure that post-surgery, self.fc1 refers to the same parameters
        # as self.net[1]
        self.fc1 = fc1
        self.fc2 = fc2


if __name__ == '__main__':
    tdist.init_process_group(backend='gloo')

    bare_torch_model = WrapperModel()

    fsdp_config = {
        'use_orig_params': True,
        'state_dict_type': 'full',
    }

    for elem in ['model', 'fc1', 'fc2']:
        inner_module = getattr(bare_torch_model, elem)
        inner_module.to(f'cuda:{tdist.get_rank()}')

        print(f'wrapping {elem=} {inner_module=}')
        wrapped_inner_module = FullyShardedDataParallel(
            inner_module,
            use_orig_params=True,
            sharding_strategy=ShardingStrategy.FULL_SHARD,
        )
        setattr(bare_torch_model, elem, wrapped_inner_module)

    wrapped_state_dict = get_model_state_dict(
        bare_torch_model,
        submodules=None,
        options=StateDictOptions(full_state_dict=True),
    )

    inner_state_dict = get_model_state_dict(
        bare_torch_model.model,
        submodules=None,
        options=StateDictOptions(full_state_dict=True),
    )

    if tdist.get_rank() == 0:
        print(f'{bare_torch_model=}')
        print(f"{inner_state_dict['fc2.bias']=}")
        print(f"{wrapped_state_dict['fc2.bias']=}")
        assert len(inner_state_dict['fc2.bias']) != 0
        assert len(wrapped_state_dict['fc2.bias']) != 0
