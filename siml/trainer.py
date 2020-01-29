from collections import OrderedDict
import enum
import random
import time

import ignite
import numpy as np
import matplotlib.pyplot as plt
import optuna
import pandas as pd
import torch
import torch.nn.functional as functional
from tqdm import tqdm

from . import datasets
from . import networks
from . import setting
from . import util


class Trainer():

    @classmethod
    def read_settings(cls, settings_yaml):
        """Read settings.yaml to generate Trainer object.

        Parameters
        ----------
            settings_yaml: str or pathlib.Path
                setting.yaml file name.
        Returns
        --------
            trainer: siml.Trainer
                Generater Trainer object.
        """
        main_setting = setting.MainSetting.read_settings_yaml(settings_yaml)
        return cls(main_setting)

    def __init__(self, main_setting, *, optuna_trial=None):
        """Initialize Trainer object.

        Parameters
        ----------
            main_setting: siml.setting.MainSetting object
                Setting descriptions.
            model: siml.networks.Network object
                Model to be trained.
            optuna_trial: optuna.Trial
                Optuna trial object. Used for pruning.
        Returns
        --------
            None
        """
        self.setting = main_setting
        self._update_setting_if_needed()
        self.optuna_trial = optuna_trial

    def train(self):
        """Perform training.

        Parameters
        ----------
        None

        Returns
        --------
        loss: float
            Loss value after training.
        """

        print(f"Ouput directory: {self.setting.trainer.output_directory}")
        self.setting.trainer.output_directory.mkdir(parents=True)

        self._prepare_training()

        setting.write_yaml(
            self.setting,
            self.setting.trainer.output_directory / 'settings.yml')

        print(
            self._display_mergin('epoch')
            + self._display_mergin('train_loss')
            + self._display_mergin('validation_loss')
            + self._display_mergin('elapsed_time'))
        with open(self.log_file, 'w') as f:
            f.write('epoch, train_loss, validation_loss, elapsed_time\n')
        self.pbar = tqdm(
            initial=0, leave=False,
            total=len(self.train_loader)
            * self.setting.trainer.log_trigger_epoch,
            desc=self.desc.format(0), ncols=80, ascii=True)
        self.start_time = time.time()

        self.trainer.run(
            self.train_loader, max_epochs=self.setting.trainer.n_epoch)
        self.pbar.close()

        df = pd.read_csv(
            self.log_file, header=0, index_col=None, skipinitialspace=True)
        validation_loss = np.min(df['validation_loss'])

        return validation_loss

    def _display_mergin(self, input_string, reference_string=None):
        if not reference_string:
            reference_string = input_string
        return input_string.ljust(
            len(reference_string) + self.setting.trainer.display_mergin, ' ')

    def _prepare_training(self):
        self.set_seed()

        if len(self.setting.trainer.input_names) == 0:
            raise ValueError('No input_names fed')
        if len(self.setting.trainer.output_names) == 0:
            raise ValueError('No output_names fed')

        # Define model
        self.model = networks.Network(self.setting.model, self.setting.trainer)
        self.element_wise = self._determine_element_wise()
        self.loss = self._create_loss_function()

        # Manage settings
        if self.optuna_trial is None \
                and self.setting.trainer.prune:
            self.setting.trainer.prune = False
            print('No optuna.trial fed. Set prune = False.')

        if self._is_gpu_supporting():
            if self.setting.trainer.data_parallel:
                if self.setting.trainer.time_series:
                    raise ValueError(
                        'So far both data_parallel and time_series cannot be '
                        'True')
                self.device = 'cuda:0'
                self.output_device = self.device
                gpu_count = torch.cuda.device_count()
                self.model = torch.nn.DataParallel(self.model)
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

        self._generate_trainer()

        # Manage restart and pretrain
        self._load_pretrained_model_if_needed()
        self._load_restart_model_if_needed()

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

    def _separate_data(self, data, descriptions, *, axis=-1):
        data_dict = {}
        index = 0
        data = np.swapaxes(data, 0, axis)
        for description in descriptions:
            data_dict.update({
                description['name']:
                np.swapaxes(data[index:index+description['dim']], 0, axis)})
            index += description['dim']
        return data_dict

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

    def _load_restart_model_if_needed(self):
        if self.setting.trainer.restart_directory is None:
            return
        snapshot = self._select_snapshot(
            self.setting.trainer.restart_directory, method='latest')
        checkpoint = torch.load(snapshot)
        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        self.epoch = checkpoint['epoch']
        self.loss = checkpoint['loss']
        print(f"{snapshot} loaded for restart.")
        return

    def _select_snapshot(self, path, method='best'):
        if not path.exists():
            raise ValueError(f"{path} doesn't exist")

        if path.is_file():
            return path
        elif path.is_dir():
            snapshots = path.glob('snapshot_epoch_*')
            if method == 'latest':
                return max(snapshots, key=lambda p: p.stat().st_ctime)
            elif method == 'best':
                df = pd.read_csv(
                    path / 'log.csv', header=0, index_col=None,
                    skipinitialspace=True)
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

    def _generate_trainer(self):

        self._check_data_dimension()
        if self.element_wise:
            if self.setting.trainer.element_batch_size > 0:
                batch_size = self.setting.trainer.element_batch_size
                validation_batch_size = \
                    self.setting.trainer.validation_element_batch_size
            else:
                if self.setting.trainer.simplified_model:
                    batch_size = self.setting.trainer.batch_size
                    validation_batch_size \
                        = self.setting.trainer.validation_batch_size
                else:
                    raise ValueError(
                        'element_batch_size is '
                        f"{self.setting.trainer.element_batch_size} < 1 "
                        'while element_wise is set to be true.')
        else:
            if self.setting.trainer.element_batch_size > 1 \
                    and self.setting.trainer.batch_size > 1:
                raise ValueError(
                    'batch_size cannot be > 1 when element_batch_size > 1.')
            batch_size = self.setting.trainer.batch_size
            validation_batch_size = self.setting.trainer.validation_batch_size

        if self.setting.trainer.support_inputs:
            if self.setting.trainer.time_series:
                self.collate_fn = datasets.collate_fn_time_with_support
            else:
                self.collate_fn = datasets.collate_fn_with_support
            self.prepare_batch = datasets.prepare_batch_with_support
        else:
            if self.element_wise:
                self.collate_fn = datasets.collate_fn_element_wise
                self.prepare_batch = datasets.prepare_batch_without_support
            else:
                if self.setting.trainer.time_series:
                    self.collate_fn = datasets.collate_fn_time_without_support
                else:
                    self.collate_fn = datasets.collate_fn_without_support
                self.prepare_batch = datasets.prepare_batch_without_support

        if self.setting.trainer.lazy:
            self.train_loader, self.validation_loader = \
                self._get_data_loaders(
                    datasets.LazyDataset, batch_size, validation_batch_size)
        else:
            if self.element_wise:
                self.train_loader, self.validation_loader = \
                    self._get_data_loaders(
                        datasets.ElementWiseDataset, batch_size,
                        validation_batch_size)
            else:
                self.train_loader, self.validation_loader = \
                    self._get_data_loaders(
                        datasets.OnMemoryDataset, batch_size,
                        validation_batch_size)

        self.optimizer = self._create_optimizer()

        self.trainer = self._create_supervised_trainer()
        self.evaluator = self._create_supervised_evaluator()

        self.desc = "loss: {:.5e}"
        tick = max(len(self.train_loader) // 100, 1)

        @self.trainer.on(ignite.engine.Events.ITERATION_COMPLETED(every=tick))
        def log_training_loss(engine):
            self.pbar.desc = self.desc.format(engine.state.output)
            self.pbar.update(tick)

        self.log_file = self.setting.trainer.output_directory / 'log.csv'
        self.plot_file = self.setting.trainer.output_directory / 'plot.png'

        @self.trainer.on(
            ignite.engine.Events.EPOCH_COMPLETED(
                every=self.setting.trainer.log_trigger_epoch))
        def log_training_results(engine):
            self.pbar.refresh()

            self.evaluator.run(self.train_loader)
            train_loss = self.evaluator.state.metrics['loss']

            self.evaluator.run(self.validation_loader)
            validation_loss = self.evaluator.state.metrics['loss']

            elapsed_time = time.time() - self.start_time

            # Print log
            tqdm.write(
                self._display_mergin(f"{engine.state.epoch}", 'epoch')
                + self._display_mergin(f"{train_loss:.5e}", 'train_loss')
                + self._display_mergin(
                    f"{validation_loss:.5e}", 'validation_loss')
                + self._display_mergin(f"{elapsed_time:.2f}", 'elapsed_time'))
            self.pbar.n = self.pbar.last_print_n = 0

            # Save checkpoint
            torch.save(
                {
                    'epoch': engine.state.epoch,
                    'validation_loss': validation_loss,
                    'model_state_dict': self.model.state_dict(),
                    'optimizer_state_dict': self.optimizer.state_dict()
                },
                self.setting.trainer.output_directory
                / f"snapshot_epoch_{engine.state.epoch}.pth")

            # Write log
            with open(self.log_file, 'a') as f:
                f.write(
                    f"{engine.state.epoch}, {train_loss:.5e}, "
                    f"{validation_loss:.5e}, {elapsed_time:.2f}\n")

            # Plot
            fig = plt.figure()
            df = pd.read_csv(
                self.log_file, header=0, index_col=None, skipinitialspace=True)
            plt.plot(df['epoch'], df['train_loss'], label='train loss')
            plt.plot(
                df['epoch'], df['validation_loss'], label='validation loss')
            plt.xlabel('epoch')
            plt.ylabel('loss')
            plt.yscale('log')
            plt.legend()
            plt.savefig(self.plot_file)
            plt.close(fig)

        # Add early stopping
        class StopTriggerEvents(enum.Enum):
            EVALUATED = 'evaluated'

        @self.trainer.on(
            ignite.engine.Events.EPOCH_COMPLETED(
                every=self.setting.trainer.stop_trigger_epoch))
        def fire_stop_trigger(engine):
            self.evaluator.fire_event(StopTriggerEvents.EVALUATED)

        def score_function(engine):
            return -engine.state.metrics['loss']

        self.evaluator.register_events(*StopTriggerEvents)
        self.early_stopping_handler = ignite.handlers.EarlyStopping(
            patience=self.setting.trainer.patience,
            score_function=score_function,
            trainer=self.trainer)
        self.evaluator.add_event_handler(
            StopTriggerEvents.EVALUATED, self.early_stopping_handler)

        # Add pruning setting
        if self.optuna_trial is not None:
            pruning_handler = optuna.integration.PyTorchIgnitePruningHandler(
                self.optuna_trial, 'loss', self.trainer)
            self.evaluator.add_event_handler(
                StopTriggerEvents.EVALUATED, pruning_handler)

        return

    def _create_supervised_trainer(self):

        def update_with_element_batch(x, y, model, optimizer):
            y_pred = model(x)

            optimizer.zero_grad()
            for _y_pred, _y in zip(y_pred, y):
                split_y_pred = torch.split(
                    _y_pred, self.setting.trainer.element_batch_size)
                split_y = torch.split(
                    _y, self.setting.trainer.element_batch_size)
                for syp, sy in zip(split_y_pred, split_y):
                    optimizer.zero_grad()
                    loss = self.loss(y_pred, y)
                    loss.backward(retain_graph=True)
                    self.optimizer.step()

            loss = self.loss(y_pred, y)
            return loss

        def update_standard(x, y, model, optimizer):
            optimizer.zero_grad()
            y_pred = model(x)
            loss = self.loss(y_pred, y, x['original_shapes'])
            loss.backward()
            self.optimizer.step()
            return loss

        if not self.element_wise \
                and self.setting.trainer.element_batch_size > 0:
            update_function = update_with_element_batch
        else:
            update_function = update_standard

        def update_model(engine, batch):
            self.model.train()
            x, y = self.prepare_batch(
                batch, device=self.device, output_device=self.output_device,
                non_blocking=self.setting.trainer.non_blocking)
            loss = update_function(x, y, self.model, self.optimizer)
            return loss.item()

        return ignite.engine.Engine(update_model)

    def _create_supervised_evaluator(self):

        def _inference(engine, batch):
            self.model.eval()
            with torch.no_grad():
                x, y = self.prepare_batch(
                    batch, device=self.device,
                    output_device=self.output_device,
                    non_blocking=self.setting.trainer.non_blocking)
                y_pred = self.model(x)
                return y_pred, y, {'original_shapes': x['original_shapes']}

        evaluator_engine = ignite.engine.Engine(_inference)

        metrics = {'loss': ignite.metrics.Loss(self.loss)}

        for name, metric in metrics.items():
            metric.attach(evaluator_engine, name)
        return evaluator_engine

    def _get_data_loaders(
            self, dataset_generator, batch_size, validation_batch_size):
        x_variable_names = self.setting.trainer.input_names
        y_variable_names = self.setting.trainer.output_names
        train_directories = self.setting.data.train
        validation_directories = self.setting.data.validation
        supports = self.setting.trainer.support_inputs
        num_workers = self.setting.trainer.num_workers

        train_dataset = dataset_generator(
            x_variable_names, y_variable_names,
            train_directories, supports=supports)
        validation_dataset = dataset_generator(
            x_variable_names, y_variable_names,
            validation_directories, supports=supports)

        print(f"num_workers for data_loader: {num_workers}")
        train_loader = torch.utils.data.DataLoader(
            train_dataset, collate_fn=self.collate_fn,
            batch_size=batch_size, shuffle=True, num_workers=num_workers)
        validation_loader = torch.utils.data.DataLoader(
            validation_dataset, collate_fn=self.collate_fn,
            batch_size=validation_batch_size, shuffle=False,
            num_workers=num_workers)

        return train_loader, validation_loader

    def _create_optimizer(self):
        optimizer_name = self.setting.trainer.optimizer.lower()
        if optimizer_name == 'adam':
            return torch.optim.Adam(
                self.model.parameters(),
                **self.setting.trainer.optimizer_setting)
        else:
            raise ValueError(f"Unknown optimizer name: {optimizer_name}")

    def _create_loss_function(self, pad=None):
        loss_name = self.setting.trainer.loss_function.lower()
        if loss_name == 'mse':
            loss_core = functional.mse_loss
        else:
            raise ValueError(f"Unknown loss function name: {loss_name}")

        def loss_function_with_padding(y_pred, y, original_shapes):
            concatenated_y_pred = torch.cat([
                _yp[:_l[0]] for _yp, _l in zip(y_pred, original_shapes)])
            return loss_core(concatenated_y_pred, y)

        def loss_function_without_padding(y_pred, y, original_shapes=None):
            return loss_core(y_pred.view(y.shape), y)

        def loss_function_time_with_padding(y_pred, y, original_shapes):
            concatenated_y_pred = torch.cat([
                y_pred[:s[0], i_batch, :s[1]].reshape(-1)
                for i_batch, s in enumerate(original_shapes)])
            concatenated_y = torch.cat([
                y[:s[0], i_batch, :s[1]].reshape(-1)
                for i_batch, s in enumerate(original_shapes)])
            return loss_core(concatenated_y_pred, concatenated_y)

        if pad is None:
            if self.element_wise or self.setting.trainer.batch_size == 1:
                return loss_function_without_padding
            else:
                if self.setting.trainer.time_series:
                    return loss_function_time_with_padding
                else:
                    return loss_function_with_padding
        else:
            if pad:
                if self.setting.trainer.time_series:
                    return loss_function_time_with_padding
                else:
                    return loss_function_with_padding
            else:
                return loss_function_without_padding

    def _check_data_dimension(self):
        variable_names = self.setting.trainer.input_names
        directories = self.setting.data.train

        data_directories = []
        for directory in directories:
            data_directories += util.collect_data_directories(
                directory, required_file_names=[f"{variable_names[0]}.npy"])
        # Check data dimension correctness
        if len(data_directories) > 0:
            data_wo_concatenation = {
                variable_name:
                util.load_variable(data_directories[0], variable_name)
                for variable_name in variable_names}
            for input_setting in self.setting.trainer.inputs:
                if input_setting['name'] in data_wo_concatenation and \
                        (data_wo_concatenation[input_setting['name']].shape[-1]
                         != input_setting['dim']):
                    setting_dim = input_setting['dim']
                    actual_dim = data_wo_concatenation[
                        input_setting['name']].shape[-1]
                    raise ValueError(
                        f"{input_setting['name']} dimension incorrect: "
                        f"{setting_dim} vs {actual_dim}")
        return

    def _load_data(
            self, variable_names, directories, *,
            return_dict=False, supports=None):
        data_directories = []
        for directory in directories:
            data_directories += util.collect_data_directories(
                directory, required_file_names=[f"{variable_names[0]}.npy"])

        if supports is None:
            supports = []

        data = [
            util.concatenate_variable([
                util.load_variable(data_directory, variable_name)
                for variable_name in variable_names])
            for data_directory in data_directories]
        support_data = [
            [
                util.load_variable(data_directory, support)
                for support in supports]
            for data_directory in data_directories]
        if len(data) == 0:
            raise ValueError(f"No data found for: {directories}")
        if self.setting.trainer.element_wise \
                or self.setting.trainer.simplified_model:
            if len(support_data[0]) > 0:
                raise ValueError(
                    'Cannot use support_input if '
                    'element_wise or simplified_model is True')
            if return_dict:
                if self.setting.trainer.time_series:
                    return {
                        data_directory: d[:, None]
                        for data_directory, d
                        in zip(data_directories, data)}
                else:
                    return {
                        data_directory:
                        d for data_directory, d in zip(data_directories, data)}
            else:
                return np.concatenate(data), None
        if return_dict:
            if len(supports) > 0:
                if self.setting.trainer.time_series:
                    return {
                        data_directory: [d[:, None], [s]]

                        for data_directory, d, s
                        in zip(data_directories, data, support_data)}
                else:
                    return {
                        data_directory: [d[None, :], [s]]

                        for data_directory, d, s
                        in zip(data_directories, data, support_data)}
            else:
                if self.setting.trainer.time_series:
                    return {
                        data_directory: d[:, None]
                        for data_directory, d
                        in zip(data_directories, data)}
                else:
                    return {
                        data_directory: d[None, :]

                        for data_directory, d in zip(data_directories, data)}
        else:
            return data, support_data
