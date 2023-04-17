import scipy.sparse as sp
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin


class IsoAMScaler(TransformerMixin, BaseEstimator):
    """Class to perform scaling for IsoAM based on
    https://arxiv.org/abs/2005.06316.
    """

    def __init__(self, other_components=[]):
        self.var_ = 0.
        self.std_ = 0.
        self.mean_square_ = 0.
        self.n_ = 0
        self.component_dim = len(other_components) + 1
        if self.component_dim == 1:
            raise ValueError(
                'To use IsoAMScaler, feed other_components: '
                f"{other_components}")
        return

    def partial_fit(self, data):
        self._update(data)
        return self

    def _update(self, diagonal_data):
        if len(diagonal_data.shape) != 1:
            raise ValueError(f"Input data should be 1D: {diagonal_data}")
        m = len(diagonal_data)
        mean_square = (
            self.mean_square_ * self.n_ + np.sum(diagonal_data**2)) / (
                self.n_ + m)

        self.mean_square_ = mean_square
        self.n_ += m

        # To use mean_i [x_i^2 + y_i^2 + z_i^2], multiply by the dim
        self.var_ = self.mean_square_ * self.component_dim
        self.std_ = np.sqrt(self.var_)
        return

    def _raise_if_not_sparse(self, data):
        if not sp.issparse(data):
            raise ValueError('Data is not sparse')
        return

    def transform(self, data):
        if self.std_ == 0.:
            scale = 0.
        else:
            scale = (1 / self.std_)
        return data * scale

    def inverse_transform(self, data):
        inverse_scale = self.std_
        return data * inverse_scale
