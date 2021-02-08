from collections import OrderedDict
import io
import pathlib
import random
import re

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as functional

from . import data_parallel
from . import setting


class SimlManager():

    @classmethod
    def read_settings(cls, settings_yaml):
        """Read settings.yaml to generate SimlManager object.

        Parameters
        ----------
            settings_yaml: str or pathlib.Path
                setting.yaml file name.
        Returns
        --------
            trainer: siml.SimlManager
                Generater SimlManager object.
        """
        main_setting = setting.MainSetting.read_settings_yaml(settings_yaml)
        return cls(main_setting)

    def __init__(self, settings, *, optuna_trial=None):
        """Initialize SimlManager object.

        Parameters
        ----------
            settings: siml.setting.MainSetting object or pathlib.Path
                Setting descriptions.
            model: siml.networks.Network object
                Model to be trained.
            optuna_trial: optuna.Trial
                Optuna trial object. Used for pruning.
        Returns
        --------
            None
        """
        if isinstance(settings, pathlib.Path) or isinstance(
                settings, io.TextIOBase):
            self.setting = setting.MainSetting.read_settings_yaml(
                settings)
        elif isinstance(settings, setting.MainSetting):
            self.setting = settings
        else:
            raise ValueError(
                f"Unknown type for settings: {settings.__class__}")

        self._update_setting_if_needed()
        self.optuna_trial = optuna_trial
        return

    def _select_device(self):
        if self._is_gpu_supporting():
            if self.setting.trainer.data_parallel:
                if self.setting.trainer.time_series:
                    raise ValueError(
                        'So far both data_parallel and time_series cannot be '
                        'True')
                self.device = 'cuda:0'
                self.output_device = self.device
                gpu_count = torch.cuda.device_count()
                # TODO: Use DistributedDataParallel
                # torch.distributed.init_process_group(backend='nccl')
                # self.model = torch.nn.parallel.DistributedDataParallel(
                #     self.model)
                self.model = data_parallel.DataParallel(self.model)
                self.model.to(self.device)
                print(f"Data parallel enabled with {gpu_count} GPUs.")
            elif self.setting.trainer.model_parallel:
                self.device = 'cuda:0'
                gpu_count = torch.cuda.device_count()
                self.output_device = f"cuda:{gpu_count-1}"
                print(f"Model parallel enabled with {gpu_count} GPUs.")
            elif self.setting.trainer.gpu_id != -1:
                self.device = f"cuda:{self.setting.trainer.gpu_id}"
                self.output_device = self.device
                self.model.to(self.device)
                print(f"GPU device: {self.setting.trainer.gpu_id}")
            else:
                self.device = 'cpu'
                self.output_device = self.device
        else:
            if self.setting.trainer.gpu_id != -1 \
                    or self.setting.trainer.data_parallel \
                    or self.setting.trainer.model_parallel:
                raise ValueError('No GPU found.')
            self.setting.trainer.gpu_id = -1
            self.device = 'cpu'
            self.output_device = self.device

    def _determine_element_wise(self):
        if self.setting.trainer.time_series:
            return False
        else:
            if self.setting.trainer.element_wise \
                    or self.setting.trainer.simplified_model:
                return True
            else:
                return False

    def set_seed(self):
        seed = self.setting.trainer.seed
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        return

    def _update_setting(self, path, *, only_model=False):
        if path.is_file():
            yaml_file = path
        elif path.is_dir():
            yamls = list(path.glob('*.y*ml'))
            if len(yamls) != 1:
                raise ValueError(f"{len(yamls)} yaml files found in {path}")
            yaml_file = yamls[0]
        if only_model:
            self.setting.model = setting.MainSetting.read_settings_yaml(
                yaml_file).model
        else:
            self.setting = setting.MainSetting.read_settings_yaml(yaml_file)
        if self.setting.trainer.output_directory.exists():
            print(
                f"{self.setting.trainer.output_directory} exists "
                'so reset output directory.')
            self.setting.trainer.output_directory = \
                setting.TrainerSetting([], []).output_directory
        return

    def _update_setting_if_needed(self):
        if self.setting.trainer.restart_directory is not None:
            restart_directory = self.setting.trainer.restart_directory
            self._update_setting(self.setting.trainer.restart_directory)
            self.setting.trainer.restart_directory = restart_directory
        elif self.setting.trainer.pretrain_directory is not None:
            pretrain_directory = self.setting.trainer.pretrain_directory
            self._update_setting(
                self.setting.trainer.pretrain_directory, only_model=True)
            self.setting.trainer.pretrain_directory = pretrain_directory
        elif self.setting.trainer.restart_directory is not None \
                and self.setting.trainer.pretrain_directory is not None:
            raise ValueError(
                'Restart directory and pretrain directory cannot be specified '
                'at the same time.')
        return

    def _load_pretrained_model_if_needed(self, *, model_file=None):
        if self.setting.trainer.pretrain_directory is None \
                and model_file is None:
            return
        if model_file:
            snapshot = model_file
        else:
            snapshot = self._select_snapshot(
                self.setting.trainer.pretrain_directory,
                method=self.setting.trainer.snapshot_choise_method)

        checkpoint = torch.load(snapshot, map_location=self.device)

        if len(self.model.state_dict()) != len(checkpoint['model_state_dict']):
            raise ValueError('Model parameter length invalid')
        # Convert new state_dict in case DataParallel wraps model
        model_state_dict = OrderedDict({
            key: value for key, value in zip(
                self.model.state_dict().keys(),
                checkpoint['model_state_dict'].values())})
        self.model.load_state_dict(model_state_dict)
        print(f"{snapshot} loaded as a pretrain model.")
        return

    def _select_snapshot(self, path, method='best'):
        if not path.exists():
            raise ValueError(f"{path} doesn't exist")

        if path.is_file():
            return path
        elif path.is_dir():
            snapshots = path.glob('snapshot_epoch_*')
            if method == 'latest':
                return max(
                    snapshots, key=lambda p: int(re.search(
                        r'snapshot_epoch_(\d+)', str(p)).groups()[0]))
            elif method == 'best':
                df = pd.read_csv(
                    path / 'log.csv', header=0, index_col=None,
                    skipinitialspace=True)
                if np.any(np.isnan(df['validation_loss'])):
                    return self._select_snapshot(path, method='train_best')
                best_epoch = df['epoch'].iloc[
                    df['validation_loss'].idxmin()]
                return path / f"snapshot_epoch_{best_epoch}.pth"
            elif method == 'train_best':
                df = pd.read_csv(
                    path / 'log.csv', header=0, index_col=None,
                    skipinitialspace=True)
                best_epoch = df['epoch'].iloc[
                    df['train_loss'].idxmin()]
                return path / f"snapshot_epoch_{best_epoch}.pth"
            else:
                raise ValueError(f"Unknown snapshot choise method: {method}")

        else:
            raise ValueError(f"{path} had unknown property.")

    def _is_gpu_supporting(self):
        return torch.cuda.is_available()

    def _create_loss_function(self, pad=None):
        loss_name = self.setting.trainer.loss_function.lower()
        if loss_name == 'mse':
            loss_core = functional.mse_loss
        else:
            raise ValueError(f"Unknown loss function name: {loss_name}")

        def loss_function_dict(y_pred, y, original_shapes=None):
            return torch.mean(torch.stack([
                loss_core(y_pred[key].view(y[key].shape), y[key])
                for key in y.keys()]))

        def loss_function_without_padding(y_pred, y, original_shapes=None):
            return loss_core(y_pred.view(y.shape), y)

        def loss_function_time_with_padding(y_pred, y, original_shapes):
            split_y_pred = torch.split(
                y_pred, list(original_shapes[:, 1]), dim=1)
            concatenated_y_pred = torch.cat([
                sy[:s].reshape(-1)
                for s, sy in zip(original_shapes[:, 0], split_y_pred)])
            split_y = torch.split(
                y, list(original_shapes[:, 1]), dim=1)
            concatenated_y = torch.cat([
                sy[:s].reshape(-1)
                for s, sy in zip(original_shapes[:, 0], split_y)])
            return loss_core(concatenated_y_pred, concatenated_y)

        output_is_dict = isinstance(self.setting.trainer.outputs, dict)

        if self.setting.trainer.time_series:
            if pad is False:
                return loss_function_without_padding
            else:
                return loss_function_time_with_padding
        else:
            if output_is_dict:
                return loss_function_dict
            else:
                return loss_function_without_padding
