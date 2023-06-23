import pathlib
import pytest

from siml.services.training import LogRecordItems, SimlTrainingFileLogger


@pytest.mark.parametrize(
    "epoch, train_loss, validation_loss, train_other_losses, validation_other_losses, elapsed_time",  # NOQA
    [
        (12, 0.3455, 0.1223, {"sample": 2.1234}, {"sample": 3.456}, 120)
    ]
)
def test__can_create_log_record_items(
    epoch,
    train_loss,
    validation_loss,
    train_other_losses,
    validation_other_losses,
    elapsed_time
):
    _ = LogRecordItems(
        epoch=epoch,
        train_loss=train_loss,
        validation_loss=validation_loss,
        train_other_losses=train_other_losses,
        validation_other_losses=validation_other_losses,
        elapsed_time=elapsed_time
    )


@pytest.mark.parametrize("loss_keys, expected", [
    (
        ["val", "val2"],
        'epoch, train_loss, train/val, train/val2, '
        'validation_loss, validation/val, validation/val2, elapsed_time'
    ),
    (
        [],
        'epoch, train_loss, validation_loss, elapsed_time'
    )
])
def test__file_logger_headers(loss_keys, expected):
    logger = SimlTrainingFileLogger(
        file_path=pathlib.Path("some_pathes"),
        loss_figure_path=pathlib.Path("some_pathes"),
        loss_keys=loss_keys
    )
    actual = logger._header_strings()
    assert actual == expected

