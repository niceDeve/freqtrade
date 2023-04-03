from abc import ABC, abstractmethod
from typing import Optional, Tuple

import pandas as pd
import torch


class PyTorchDataConvertor(ABC):

    @abstractmethod
    def convert_x(self, df: pd.DataFrame, device: Optional[str] = None) -> Tuple[torch.Tensor, ...]:
        """
        :param df: "*_features" dataframe.
        :param device: The device to use for training (e.g. 'cpu', 'cuda').
        :returns: tuple of tensors.
        """

    @abstractmethod
    def convert_y(self, df: pd.DataFrame, device: Optional[str] = None) -> Tuple[torch.Tensor, ...]:
        """
        :param df: "*_labels" dataframe.
        :param device: The device to use for training (e.g. 'cpu', 'cuda').
        :returns: tuple of tensors.
        """


class DefaultPyTorchDataConvertor(PyTorchDataConvertor):

    def __init__(
            self,
            target_tensor_type: Optional[torch.dtype] = None,
            squeeze_target_tensor: bool = False
    ):
        self._target_tensor_type = target_tensor_type
        self._squeeze_target_tensor = squeeze_target_tensor

    def convert_x(self, df: pd.DataFrame, device: Optional[str] = None) -> Tuple[torch.Tensor, ...]:
        x = torch.from_numpy(df.values).float()
        if device:
            x = x.to(device)

        return x,

    def convert_y(self, df: pd.DataFrame, device: Optional[str] = None) -> Tuple[torch.Tensor, ...]:
        y = torch.from_numpy(df.values)

        if self._target_tensor_type:
            y = y.to(self._target_tensor_type)

        if self._squeeze_target_tensor:
            y = y.squeeze()

        if device:
            y = y.to(device)

        return y,
