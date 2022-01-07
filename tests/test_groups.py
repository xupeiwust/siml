from pathlib import Path
import shutil
import unittest

import numpy as np

import siml.inferer as inferer
import siml.setting as setting
import siml.trainer as trainer


class TestGroups(unittest.TestCase):

    def test_group_simple(self):
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/rotation_thermal_stress/group_simple.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss = tr.train()
        np.testing.assert_array_less(loss, 1.)

    def test_group_repeat(self):
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/rotation_thermal_stress/group_repeat.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss = tr.train()
        np.testing.assert_array_less(loss, 1.)

    def test_heat_group_repeat(self):
        # NL solver repeat
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_time_series/heat_group_nl_repeat.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss_implicit = tr.train()
        np.testing.assert_array_less(loss_implicit, 1.)

        ir = inferer.Inferer(
            main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        ir.infer(
            model=main_setting.trainer.output_directory,
            output_directory_base=tr.setting.trainer.output_directory,
            data_directories=main_setting.data.preprocessed_root)

        # Simple repeat
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_time_series/heat_group_repeat.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss_repeat = tr.train()
        np.testing.assert_array_less(loss_repeat, 1.)

        ir = inferer.Inferer(
            main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        ir.infer(
            model=main_setting.trainer.output_directory,
            output_directory_base=tr.setting.trainer.output_directory,
            data_directories=main_setting.data.preprocessed_root)

        # No repeat
        main_setting_wo_repeat = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_time_series/heat.yml'))
        tr_wo_repeat = trainer.Trainer(main_setting_wo_repeat)
        if tr_wo_repeat.setting.trainer.output_directory.exists():
            shutil.rmtree(tr_wo_repeat.setting.trainer.output_directory)
        loss_wo_repeat = tr_wo_repeat.train()
        np.testing.assert_array_less(loss_wo_repeat, .1)

        ir_wo_repeat = inferer.Inferer(
            main_setting_wo_repeat,
            converter_parameters_pkl=main_setting_wo_repeat
            .data.preprocessed_root
            / 'preprocessors.pkl')
        ir_wo_repeat.infer(
            model=main_setting_wo_repeat.trainer.output_directory,
            output_directory_base=tr_wo_repeat.setting
            .trainer.output_directory,
            data_directories=main_setting_wo_repeat.data.preprocessed_root)

        self.assertLess(loss_implicit, loss_repeat)
        self.assertLess(loss_repeat, loss_wo_repeat)

    def test_heat_boundary_repeat(self):
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_boundary/repeat.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss_repeat = tr.train()
        np.testing.assert_array_less(loss_repeat, 5.e-2)

        ir = inferer.Inferer(
            main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        ir.infer(
            model=main_setting.trainer.output_directory,
            output_directory_base=tr.setting.trainer.output_directory,
            data_directories=main_setting.data.preprocessed_root)

    def test_heat_boundary_implicit(self):
        main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_boundary/boundary_isogcn.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss_implicit = tr.train()
        np.testing.assert_array_less(loss_implicit, 5.e-2)

        ir = inferer.Inferer(
            main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        ir.infer(
            model=main_setting.trainer.output_directory,
            output_directory_base=tr.setting.trainer.output_directory,
            data_directories=main_setting.data.preprocessed_root)

    def test_heat_timeseries_better(self):
        main_setting = setting.MainSetting.read_settings_yaml(Path(
            'tests/data/heat_time_series/heat_group_time_series.yml'))
        tr = trainer.Trainer(main_setting)
        if tr.setting.trainer.output_directory.exists():
            shutil.rmtree(tr.setting.trainer.output_directory)
        loss = tr.train()
        np.testing.assert_array_less(loss, 5.e-2)

        ir = inferer.Inferer(
            main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        results = ir.infer(
            model=main_setting.trainer.output_directory,
            output_directory_base=tr.setting.trainer.output_directory,
            data_directories=Path(
                'tests/data/heat_time_series/preprocessed/2'))
        mse = np.mean((
            results[0]['dict_y']['t_10'] - results[0]['dict_x']['t_10'])**2)

        ref_main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_time_series/heat_group_nl_repeat4.yml'))
        ref_tr = trainer.Trainer(ref_main_setting)
        if ref_tr.setting.trainer.output_directory.exists():
            shutil.rmtree(ref_tr.setting.trainer.output_directory)
        ref_tr.train()

        ref_ir = inferer.Inferer(
            ref_main_setting,
            converter_parameters_pkl=main_setting.data.preprocessed_root
            / 'preprocessors.pkl')
        ref_results = ref_ir.infer(
            model=ref_main_setting.trainer.output_directory,
            output_directory_base=ref_tr.setting.trainer.output_directory,
            data_directories=Path(
                'tests/data/heat_time_series/preprocessed/2'))
        ref_mse = np.mean((
            ref_results[0]['dict_y']['t_10']
            - ref_results[0]['dict_x']['t_10'])**2)

        self.assertLess(mse, ref_mse)

    def test_heat_timeseries_1step(self):
        main_setting_1step = setting.MainSetting.read_settings_yaml(Path(
            'tests/data/heat_time_series/heat_group_time_series_1step.yml'))
        tr_1step = trainer.Trainer(main_setting_1step)
        if tr_1step.setting.trainer.output_directory.exists():
            shutil.rmtree(tr_1step.setting.trainer.output_directory)
        loss_1step = tr_1step.train()
        np.testing.assert_array_less(loss_1step, 5.e-2)

        ref_main_setting = setting.MainSetting.read_settings_yaml(
            Path('tests/data/heat_time_series/heat_group_nl_repeat.yml'))
        ref_tr = trainer.Trainer(ref_main_setting)
        if ref_tr.setting.trainer.output_directory.exists():
            shutil.rmtree(ref_tr.setting.trainer.output_directory)
        ref_loss = ref_tr.train()

        np.testing.assert_almost_equal(loss_1step, ref_loss)
