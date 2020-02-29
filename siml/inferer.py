import io
import pathlib

import femio
import numpy as np
import torch

from . import datasets
from . import networks
from . import prepost
from . import setting
from . import trainer
from . import util


class Inferer(trainer.Trainer):

    def infer(
            self, model=None, *,
            save=True, overwrite=False, output_directory=None,
            preprocessed_data_directory=None, raw_data_directory=None,
            raw_data_basename=None, raw_data_file=None,
            write_simulation=False, write_npy=True, write_yaml=True,
            write_simulation_base=None, write_simulation_stem=None,
            read_simulation_type='fistr', write_simulation_type='fistr',
            converter_parameters_pkl=None, conversion_function=None,
            load_function=None, convert_to_order1=False,
            data_addition_function=None, accomodate_length=None,
            required_file_names=[]):
        """Perform inference.

        Parameters
        ----------
        model: pathlib.Path or io.BufferedIOBase, optional [None]
            Model directory, file path, or buffer. If not fed,
            TrainerSetting.pretrain_directory will be used.
        save: bool, optional [False]
            If True, save inference results.
        output_directory: pathlib.Path, optional [None]
            Output directory name. If not fed, data/inferred will be the
            default output directory base.
        preprocessed_data_directory: pathlib.Path, optional [None]
            Preprocessed data directories. If not fed, DataSetting.test
            will be used.
        raw_data_directory: pathlib.Path, optional [None]
            Raw data directories. If not fed, DataSetting.test
            will be used.
        raw_data_basename: pathlib.Path, optional [None]
            Raw data basename (without extention).
        raw_data_file: pathlib.Path, optional [None]
            Raw data file name.
        write_simulation: bool, optional [False]
            If True, write simulation data file(s) based on the inference.
        write_npy: bool, optional [True]
            If True, write npy files of inferences.
        write_yaml: bool, optional [True]
            If True, write yaml file used to make inference.
        write_simulation_base: pathlib.Path, optional [None]
            Base of simulation data to be used for write_simulation option.
            If not fed, try to find from the input directories.
        read_simulation_type: str, optional ['fistr']
            Simulation file type to read.
        write_simulation_type: str, optional ['fistr']
            Simulation file type to write.
        converter_parameters_pkl: pathlib.Path, optional [None]
            Pickel file of converter parameters. IF not fed,
            DataSetting.preprocessed is used.
        conversion_function: function, optional [None]
            Conversion function to preprocess raw data. It should receive
            two parameters, fem_data and raw_directory. If not fed,
            no additional conversion occurs.
        load_function: function, optional [None]
            Function to load data, which take list of pathlib.Path objects
            (as required files) and pathlib.Path object (as data directory)
            and returns data_dictionary and fem_data (can be None) to be saved.
        required_file_names: List[str], optional [[]]
            Required file names for load_function.
        data_addition_function: function, optional [None]
            Function to add some data at simulation data writing phase.
            If not fed, no data addition occurs.
        accomodate_length: int
            If specified, duplicate initial state to initialize RNN state.
        Returns
        --------
        inference_results: list
            Inference results contains:
                - input variables
                - output variables
                - loss
        """
        self._prepare_inference(
            model,
            converter_parameters_pkl=converter_parameters_pkl)

        # Load data
        if raw_data_directory is None and raw_data_basename is None:
            # Inference based on preprocessed data
            if preprocessed_data_directory is None:
                input_directories = self.setting.data.test
            else:
                if isinstance(preprocessed_data_directory, str) \
                        or isinstance(
                            preprocessed_data_directory, pathlib.Path):
                    input_directories = [preprocessed_data_directory]
                elif isinstance(preprocessed_data_directory, list) \
                        or isinstance(preprocessed_data_directory, set):
                    input_directories = preprocessed_data_directory

            dict_dir_x = self._load_data(
                self.setting.trainer.input_names, input_directories,
                return_dict=True, supports=self.setting.trainer.support_inputs)
            dict_dir_y = self._load_data(
                self.setting.trainer.output_names, input_directories,
                return_dict=True)

        else:
            # Inference based on raw data
            if preprocessed_data_directory is not None:
                raise ValueError(
                    'Both preprocessed_data_directory and raw_data_directory '
                    'cannot be specified at the same time')
            if raw_data_basename is not None:
                if raw_data_directory is not None:
                    raise ValueError(
                        'Both raw_data_basename and raw_data_directory cannot'
                        'be fed at the same time')
                raw_data_directory = raw_data_basename.parent
                raw_data_stem = raw_data_basename.stem
            else:
                raw_data_stem = None

            if write_simulation_base is None:
                write_simulation_base = raw_data_directory
            if write_simulation_stem is None:
                write_simulation_stem = raw_data_stem
            x, y = self._preprocess_data(
                read_simulation_type,
                raw_data_directory=raw_data_directory,
                raw_data_stem=raw_data_stem,
                prepost_converter=self.prepost_converter,
                conversion_function=conversion_function,
                load_function=load_function)
            dict_dir_x = {preprocessed_data_directory: x}
            if y is None:
                dict_dir_y = {}
            else:
                dict_dir_y = {preprocessed_data_directory: y}

        # Perform inference
        inference_results = [
            self._infer_single_directory(
                self.prepost_converter, directory, x, dict_dir_y,
                save=save, convert_to_order1=convert_to_order1,
                overwrite=overwrite, output_directory=output_directory,
                write_simulation=write_simulation, write_npy=write_npy,
                write_yaml=write_yaml,
                write_simulation_base=write_simulation_base,
                write_simulation_stem=write_simulation_stem,
                write_simulation_type=write_simulation_type,
                read_simulation_type=read_simulation_type,
                data_addition_function=data_addition_function,
                accomodate_length=accomodate_length,
                load_function=load_function,
                required_file_names=required_file_names)
            for directory, x in dict_dir_x.items()]
        return inference_results

    def _prepare_inference(
            self, model,
            *, model_directory=None, converter_parameters_pkl=None):
        self.device = 'cpu'

        # Define model
        if model is None:
            if self.setting.trainer.pretrain_directory is None:
                raise ValueError(
                    f'No pretrain directory is specified for inference.')
            else:
                model = self.setting.trainer.pretrain_directory

        if isinstance(model, io.BufferedIOBase):
            model_file = model
        elif isinstance(model, str) or isinstance(model, pathlib.Path):
            model = pathlib.Path(model)
            if model.is_dir():
                self.setting.trainer.pretrain_directory = model
                self._update_setting_if_needed()
                model_file = None
            elif model.is_file():
                model_file = model
        else:
            raise ValueError(
                f"{model} is neither file, directory, nor buffer.")

        self.model = networks.Network(
            self.setting.model, self.setting.trainer)
        self._load_pretrained_model_if_needed(model_file=model_file)

        self.element_wise = self._determine_element_wise()
        self.loss = self._create_loss_function(pad=False)
        self.model.eval()
        if converter_parameters_pkl is None:
            converter_parameters_pkl = self.setting.data.preprocessed \
                / 'preprocessors.pkl'
        self.prepost_converter = prepost.Converter(converter_parameters_pkl)

    def _preprocess_data(
            self, simulation_type, prepost_converter, raw_data_directory,
            *, raw_data_stem=None, conversion_function=None,
            load_function=None):
        if self.setting.conversion.skip_femio:
            dict_data = {}
        else:
            fem_data = femio.FEMData.read_directory(
                simulation_type, raw_data_directory, stem=raw_data_stem,
                save=False)
            dict_data = prepost.extract_variables(
                fem_data, self.setting.conversion.mandatory,
                optional_variables=self.setting.conversion.optional)

        if conversion_function is not None:
            dict_data.update(conversion_function(fem_data, raw_data_directory))

        if load_function is not None:
            data_files = util.collect_files(
                raw_data_directory,
                self.setting.conversion.required_file_names)
            loaded_dict_data, _ = load_function(
                data_files, raw_data_directory)
            dict_data.update(loaded_dict_data)

        converted_dict_data = prepost_converter.preprocess(dict_data)
        input_data = np.concatenate([
            converted_dict_data[input_info['name']]
            for input_info in self.setting.trainer.inputs], axis=1).astype(
            np.float32)
        if np.all([
                output_info['name'] in dict_data
                for output_info in self.setting.trainer.outputs]):
            output_data = np.concatenate(
                [
                    converted_dict_data[output_info['name']]
                    for output_info in self.setting.trainer.outputs
                ], axis=1).astype(np.float32)
        else:
            output_data = None

        if self.setting.trainer.element_wise \
                or self.setting.trainer.simplified_model:
            return input_data, output_data
        else:
            if output_data is None:
                extended_output_data = None
            else:
                extended_output_data = output_data[None, :, :]
            return input_data[None, :, :], extended_output_data

    def infer_simplified_model(
            self, model_path, raw_dict_x, *,
            answer_raw_dict_y=None, model_directory=None,
            converter_parameters_pkl=None, accomodate_length=None):
        """
        Infer with simplified model.

        Parameters
        ----------
            model_path: pathlib.Path
                Model file or directory name.
            raw_dict_x: dict
                Dict of raw x data.
            answer_raw_dict_y: dict, optional [None]
                Dict of answer raw y data.
            model_directory: pathlib.Path
                Model directory name.
            converter_parameters_pkl: pathlib.Path
                Converter parameters pkl data.
            accomodate_length: int
                If specified, duplicate initial state to initialize RNN state.
        """
        self._prepare_inference(
            pathlib.Path(model_path), model_directory=model_directory,
            converter_parameters_pkl=converter_parameters_pkl)

        # Preprocess data
        preprocessed_x = self.prepost_converter.preprocess(raw_dict_x)
        x = np.concatenate(
            [
                preprocessed_x[variable_name]
                for variable_name in self.setting.trainer.input_names],
            axis=-1).astype(np.float32)

        if answer_raw_dict_y is not None:
            answer_preprocessed_y = self.prepost_converter.preprocess(
                answer_raw_dict_y)
            answer_y = np.concatenate(
                [
                    answer_preprocessed_y[variable_name]
                    for variable_name in self.setting.trainer.output_names],
                axis=-1).astype(np.float32)
        else:
            answer_y = None

        _, inversed_dict_y, loss = self._infer_single_data(
            self.prepost_converter, x, answer_y=answer_y,
            accomodate_length=accomodate_length)
        return inversed_dict_y, loss

    def _infer_single_data(
            self, postprocessor, x, *, answer_y=None,
            overwrite=False, supports=None,
            output_directory=None, write_simulation=False, write_npy=True,
            write_simulation_base=None, write_simulation_stem=None,
            write_simulation_type='fistr', read_simulation_type='fistr',
            data_addition_function=None, accomodate_length=None,
            load_function=None, required_file_names=[],
            convert_to_order1=False):

        if self.setting.trainer.time_series and len(x.shape) == 3:
            x = x[:, None, :, :]

        if supports is not None:
            converted_supports = [[
                datasets.pad_sparse(s) for s in supports[0]]]
        else:
            converted_supports = None

        if accomodate_length:
            x = np.concatenate([x[:accomodate_length], x])
        x = torch.from_numpy(x)

        # Inference
        self.model.eval()
        with torch.no_grad():
            inferred_y = self.model({'x': x, 'supports': converted_supports})
        if accomodate_length:
            inferred_y = inferred_y[accomodate_length:]
            x = x[accomodate_length:]

        if len(x.shape) == 2:
            x = x[None, :, :]
            inferred_y = inferred_y[None, :, :]
        dict_var_x = self._separate_data(
            x.numpy(), self.setting.trainer.inputs)
        dict_var_inferred_y = self._separate_data(
            inferred_y.numpy(), self.setting.trainer.outputs)
        if answer_y is not None:
            dict_var_answer_y = self._separate_data(
                answer_y, self.setting.trainer.outputs)
            dict_var_x.update(dict_var_answer_y)

        # Postprocess
        inversed_dict_x, inversed_dict_y = postprocessor.postprocess(
            dict_var_x, dict_var_inferred_y,
            output_directory=output_directory, overwrite=overwrite,
            write_simulation=write_simulation, write_npy=write_npy,
            write_simulation_base=write_simulation_base,
            write_simulation_stem=write_simulation_stem,
            write_simulation_type=write_simulation_type,
            read_simulation_type=read_simulation_type,
            skip_femio=self.setting.conversion.skip_femio,
            load_function=load_function, convert_to_order1=convert_to_order1,
            required_file_names=required_file_names,
            data_addition_function=data_addition_function)

        # Compute loss
        if answer_y is not None:
            with torch.no_grad():
                answer_y = torch.from_numpy(answer_y)
                if len(answer_y.shape) == 2:
                    loss = self.loss(inferred_y[0], answer_y).numpy()
                elif len(answer_y.shape) == 3:
                    loss = self.loss(inferred_y, answer_y).numpy()
                elif len(answer_y.shape) == 4:
                    loss = self.loss(inferred_y, answer_y).numpy()
                else:
                    raise ValueError(
                        f"Unknown shape of answer_y: {answer_y.shape}")
        else:
            # Answer data does not exist
            loss = None

        return inversed_dict_x, inversed_dict_y, loss

    def _infer_single_directory(
            self, postprocessor, directory, x, dict_dir_y, *, save=True,
            overwrite=False,
            output_directory=None, write_simulation=False, write_npy=True,
            write_yaml=True, convert_to_order1=False,
            write_simulation_base=None, write_simulation_stem=None,
            write_simulation_type='fistr', read_simulation_type='fistr',
            data_addition_function=None, accomodate_length=False,
            load_function=None, required_file_names=[]):

        if isinstance(x, list):
            x, supports = x
        else:
            supports = None

        if directory in dict_dir_y:
            # Answer data exists
            answer_y = dict_dir_y[directory]
        else:
            answer_y = None

        if save:
            if output_directory is None:
                output_directory = prepost.determine_output_directory(
                    directory, self.setting.data.inferred,
                    self.setting.data.preprocessed.stem) \
                    / f"{self.setting.trainer.name}_{util.date_string()}"
            output_directory.mkdir(parents=True, exist_ok=overwrite)
        else:
            output_directory = None

        inversed_dict_x, inversed_dict_y, loss = self._infer_single_data(
            postprocessor, x, answer_y=answer_y, overwrite=overwrite,
            output_directory=output_directory, supports=supports,
            write_simulation=write_simulation, write_npy=write_npy,
            write_simulation_base=write_simulation_base,
            write_simulation_stem=write_simulation_stem,
            write_simulation_type=write_simulation_type,
            read_simulation_type=read_simulation_type,
            data_addition_function=data_addition_function,
            accomodate_length=accomodate_length, load_function=load_function,
            required_file_names=required_file_names,
            convert_to_order1=convert_to_order1)

        if loss is not None:
            print(f"data: {directory}")
            print(f"loss: {loss}")

        if save:
            if write_yaml:
                setting.write_yaml(
                    self.setting, output_directory / 'settings.yml',
                    overwrite=overwrite)
            with open(output_directory / 'loss.dat', 'w') as f:
                f.write(f"loss: {loss}")
            print(f"Inferred data saved in: {output_directory}")

        return {
            'dict_x': inversed_dict_x, 'dict_y': inversed_dict_y, 'loss': loss,
            'output_directory': output_directory, 'data_directory': directory}

    def _load_data(
            self, variable_names, directories, *,
            return_dict=False, supports=None):

        data_directories = []
        for directory in directories:
            data_directories += util.collect_data_directories(
                directory, required_file_names=[f"{variable_names[0]}.npy"])
        data_directories = np.unique(data_directories)

        if len(data_directories) == 0:
            raise ValueError(f"No data found in {directories}")

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
                        data_directory: d for data_directory, d
                        in zip(data_directories, data)}
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
                        for data_directory, d
                        in zip(data_directories, data)}
        else:
            return data, support_data